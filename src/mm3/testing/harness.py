"""``ModuleHarness``: run one module instance in a test, as on a mirror.

The harness builds a real engine around the module (registry, supervisor,
bus, ``ModuleContext``, scheduler, storage in a temporary directory) and
replaces what a test cannot have: the network (``h.http``, ``h.mqtt``), the
browsers (recorded client messages and states) and time (a virtual clock on
the test's event loop, core CORE-278). Grants and capabilities are checked
as on a mirror.

Implements:
    -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
    CORE-278: Core spec 0.17, §3.3 Scheduler.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from mm3.context import PageInfo
from mm3.core.context import REQUEST_FAILED, _as_error_reply, error_reply_payload
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging, is_configured, observe
from mm3.core.registry import Registry
from mm3.core.services.grants import GrantSet
from mm3.core.settings import EngineSettings
from mm3.core.supervisor import InstanceSpec, ManagedInstance, State
from mm3.errors import ConfigurationError, RemoteError, RequestTimeoutError, TransientError
from mm3.messages import ClientMessage, Notification
from mm3.module import MirrorModule
from mm3.notifications import validate_payload
from mm3.testing.fakes import FakeHttp, FakeMqtt
from mm3.testing.loop import CountingExecutor, LoopClock
from mm3.testing.recorders import ClientRecord, LogRecords, NotificationLog

__all__ = ["DEFAULT_NOW", "ModuleHarness", "Responder"]

DEFAULT_NOW = "2026-01-01T12:00:00"
"""Default virtual wall clock, in the harness time zone (Contract 12.2)."""

KIT_SOURCE = "kit/harness"
"""``source`` of notifications and requests the harness sends."""

Responder = Callable[[dict[str, Any]], "dict[str, Any] | Awaitable[dict[str, Any]]"]
"""``h.respond`` handler: request payload in, reply payload out, plain or async."""


def _start_time(now: str | datetime | None, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if now is None:
        return datetime.fromisoformat(DEFAULT_NOW).replace(tzinfo=zone)
    value = datetime.fromisoformat(now) if isinstance(now, str) else now
    return value if value.tzinfo is not None else value.replace(tzinfo=zone)


class _Pages:
    """A fixed page list for ``ctx.page`` / ``ctx.pages`` (core spec 6.9)."""

    def __init__(self, pages: Sequence[str | PageInfo], page: str) -> None:
        self.infos = [p if isinstance(p, PageInfo) else PageInfo(p, p) for p in pages]
        self.page = page
        self.hidden_by: dict[str, set[str]] = {}

    def page_infos(self) -> list[PageInfo]:
        return list(self.infos)

    def set_module_reason(self, by: str, target: str, hidden: bool) -> None:
        reasons = self.hidden_by.setdefault(target, set())
        (reasons.add if hidden else reasons.discard)(f"module:{by}")


class _Recorder:
    """Bus endpoint and client channel of the harness."""

    def __init__(self, harness: ModuleHarness) -> None:
        self._h = harness

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        self._h._seen(n)

    def send_client(
        self, instance_id: str, type: str, payload: dict[str, Any], client: str | None
    ) -> None:
        if instance_id == self._h.instance_id:
            self._h.client_messages.append(ClientRecord(type, dict(payload), client))

    def send_state(self, instance_id: str, state: dict[str, Any], delta: bool) -> None:
        if instance_id == self._h.instance_id:
            current = dict(self._h.state_history[-1]) if delta and self._h.state_history else {}
            current.update(state)
            self._h.state_history.append(current)


class ModuleHarness:
    """Run a module instance under test (Contract 12.2).

    ``async with ModuleHarness(Weather, config=...) as h:`` validates the
    configuration with the module's ``config_model``, creates the instance
    and awaits ``start()``; leaving the block awaits ``stop()``.

    Args:
        module: The module class.
        config: The instance ``config`` block.
        grants: The instance ``grants`` block, e.g.
            ``{"http": [...], "mqtt": {"publish": [...], "subscribe": [...]}}``.
        instance_id: Instance id; default ``<name>_test``.
        locale: Instance locale.
        timezone: IANA time zone of the engine.
        units: ``metric`` or ``imperial``.
        location: ``engine.location`` as ``{"lat": .., "lon": .., "name": ..}``
            (``name`` optional text or per-language mapping); ``None`` when unset.
        now: Virtual wall clock at the start (ISO 8601 or aware datetime);
            default 2026-01-01T12:00 in ``timezone``.
        page: Current page id; default the first of ``pages``.
        pages: Page ids or ``PageInfo``s; default one page ``main``.

    Attributes:
        http: Fake behind ``ctx.http``.
        mqtt: Fake behind ``ctx.mqtt``.
        notifications: Notifications the instance sent.
        client_messages: Messages the backend sent to its front-end.
        state_history: Full state after every ``set_state``/``patch_state``.
        logs: The instance's log records.
        received: Notifications delivered to the instance.
        violations: Payloads that did not match the declared models (MOD-29).
        denied: Capabilities used without being declared (MOD-70).

    Implements:
        -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
    """

    def __init__(
        self,
        module: type[MirrorModule],
        *,
        config: Mapping[str, Any] | None = None,
        grants: Mapping[str, Any] | None = None,
        instance_id: str | None = None,
        locale: str = "en",
        timezone: str = "UTC",
        units: Literal["metric", "imperial"] = "metric",
        location: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
        page: str | None = None,
        pages: Sequence[str | PageInfo] | None = None,
    ) -> None:
        self.module = module
        self.config: dict[str, Any] = dict(config or {})
        self.grants: dict[str, Any] = dict(grants or {})
        self.instance_id = instance_id or f"{module.name}_test"
        self.locale = locale
        self.timezone = timezone
        self.units = units
        self.location = location
        self._start = _start_time(now, timezone)
        page_list = list(pages) if pages else ["main"]
        self._pages = _Pages(page_list, page or self._first(page_list))
        self.http = FakeHttp()
        self.mqtt = FakeMqtt()
        self.notifications = NotificationLog()
        self.received = NotificationLog()
        self.client_messages: list[ClientRecord] = []
        self.state_history: list[dict[str, Any]] = []
        self.logs = LogRecords()
        self.violations: list[str] = []
        self.denied: list[str] = []
        self.requests_sent = NotificationLog()
        self._responders: dict[str, Responder] = {}
        self._fail_requests = False
        self._recorder = _Recorder(self)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._engine: Engine | None = None
        self._clock: LoopClock | None = None
        self._tmp: Path | None = None
        self._stop_observing: Callable[[], None] | None = None

    @staticmethod
    def _first(pages: Sequence[str | PageInfo]) -> str:
        first = pages[0]
        return first.id if isinstance(first, PageInfo) else first

    # --- lifecycle --------------------------------------------------------------

    async def __aenter__(self) -> ModuleHarness:
        self.module.config_model.model_validate(self.config)  # errors reach the test
        self._clock = LoopClock.acquire(self._start)
        self.mqtt.settle = self._clock.settle
        if not is_configured():
            configure_logging("debug", stream=io.StringIO(), json=True)
        self._stop_observing = observe(self._log_record)
        self._tmp = Path(tempfile.mkdtemp(prefix="mm3-kit-"))
        registry = Registry()
        registry.add(self.module)
        entry = registry.get(self.module.name)
        if entry is None or entry.cls is None:
            reason = entry.error if entry is not None else "not registered"
            raise ValueError(f"module {self.module.name!r} cannot run: {reason}")
        settings = EngineSettings.model_validate(
            {
                "locale": self.locale,
                "timezone": self.timezone,
                "units": self.units,
                "location": self.location,
            }
        )
        engine = Engine(
            settings,
            registry=registry,
            clock=self._clock,
            clients=self._recorder,
            state_dir=self._tmp / "state",
        )
        engine.executor.shutdown(wait=False)
        executor = CountingExecutor(settings.threads)
        engine.executor = executor
        engine.supervisor.executor = executor
        self._clock.executors.append(executor)
        sup = engine.supervisor
        sup.http_transport = self.http
        sup.mqtt_broker = self.mqtt
        sup.on_denied = self._on_denied
        sup.pages = self._pages
        engine.bus.add_endpoint(self._recorder)
        self._engine = engine
        spec = InstanceSpec(
            instance_id=self.instance_id,
            module=self.module.name,
            config=self.config,
            grants=GrantSet.from_config(self.grants),
        )
        await self._clock.drive(engine.start([spec]))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Stop the instance and release the harness (done on leaving the block)."""
        if self._engine is not None and self._clock is not None:
            engine, self._engine = self._engine, None
            try:
                await self._clock.drive(engine.stop())
            finally:
                for task in list(self._tasks):
                    task.cancel()
                engine.bus.remove_endpoint(self._recorder)
                for ex in list(self._clock.executors):
                    if ex is engine.executor:
                        self._clock.executors.remove(ex)
        if self._stop_observing is not None:
            self._stop_observing()
            self._stop_observing = None
        if self._clock is not None:
            self._clock.release()
            self._clock = None
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    # --- state ------------------------------------------------------------------

    @property
    def engine(self) -> Engine:
        """The engine running the instance."""
        if self._engine is None:
            raise RuntimeError("the harness is not open")
        return self._engine

    @property
    def clock(self) -> LoopClock:
        """The virtual clock."""
        if self._clock is None:
            raise RuntimeError("the harness is not open")
        return self._clock

    @property
    def managed(self) -> ManagedInstance:
        """The supervisor's record of the instance."""
        return self.engine.supervisor.get(self.instance_id)

    @property
    def instance(self) -> MirrorModule | None:
        """The current module object, ``None`` when not running."""
        return self.managed.obj

    @property
    def instance_state(self) -> State:
        """Lifecycle state (``RUNNING``, ``FAILED``, …)."""
        return self.managed.state

    @property
    def state(self) -> Mapping[str, Any]:
        """The instance's current state (``ctx.state``)."""
        ctx = self.managed.context
        if ctx is not None and not ctx.closed:
            return ctx.state
        return self.state_history[-1] if self.state_history else {}

    @property
    def now(self) -> datetime:
        """Current virtual wall-clock time in the harness time zone."""
        return self.clock.wall().astimezone(ZoneInfo(self.timezone))

    def health(self) -> dict[str, Any]:
        """The instance's health record (CORE-113)."""
        return self.managed.health()

    @property
    def storage_dir(self) -> Path:
        """Root of the temporary state directory."""
        if self._tmp is None:
            raise RuntimeError("the harness is not open")
        return self._tmp / "state"

    # --- recording --------------------------------------------------------------

    def _source(self) -> str:
        return f"{self.module.name}/{self.instance_id}"

    def _seen(self, n: Notification) -> None:
        if n.source == self._source():
            self.notifications.append(n)
            if n.is_request:
                self.requests_sent.append(n)
                self._answer_kit(n)
            elif n.is_reply:
                self._check_reply(n)
            else:
                self._check_model("emits", n.name, n.payload)
        elif n.target == self.instance_id or n.target is None:
            if not n.is_reply:
                self.received.append(n)

    def _log_record(self, record: dict[str, Any]) -> None:
        if record.get("instance") == self.instance_id:
            self.logs.append(record)

    def _on_denied(self, instance_id: str, capability: str) -> None:
        if instance_id == self.instance_id:
            self.denied.append(capability)

    def _check_model(self, attr: str, name: str, payload: Mapping[str, Any]) -> None:
        table: Mapping[str, Any] = getattr(self.module, attr, {})
        if attr == "emits" and name not in table:
            self.violations.append(f"{name} is sent but not declared in emits (MOD-29)")
            return
        model = table.get(name)
        if model is None:
            return
        if isinstance(model, tuple):
            model = model[0]
        problem = _validate(model, payload)
        if problem:
            self.violations.append(f"{name} payload does not match {attr} model: {problem}")

    def _check_reply(self, n: Notification) -> None:
        pair = dict(self.module.answers).get(n.name)
        if _as_error_reply(n.payload) is not None:
            return
        if pair is None:
            self.violations.append(f"{n.name} is answered but not declared in answers (MOD-29)")
            return
        problem = _validate(pair[1], n.payload)
        if problem:
            self.violations.append(f"{n.name} reply does not match its answers model: {problem}")

    # --- inputs -----------------------------------------------------------------

    async def settle(self) -> None:
        """Let everything that is ready run, without moving time."""
        await self.clock.settle()

    async def advance(
        self,
        seconds: float = 0.0,
        *,
        minutes: float = 0.0,
        hours: float = 0.0,
        to: str | datetime | None = None,
    ) -> None:
        """Move virtual time, running every job, sleep and budget that falls due.

        Args:
            seconds: Seconds to move.
            minutes: Minutes to move.
            hours: Hours to move.
            to: Wall-clock target instead (ISO 8601 or datetime, harness zone).
        """
        if to is not None:
            target = _start_time(to, self.timezone)
            step = (target - self.clock.wall()).total_seconds()
        else:
            step = timedelta(seconds=seconds, minutes=minutes, hours=hours).total_seconds()
        await self.clock.advance(max(step, 0.0))

    async def notify(
        self, name: str, payload: Mapping[str, Any] | None = None, sender: str | None = None
    ) -> None:
        """Deliver a notification to the instance as if another module sent it.

        Args:
            name: Notification name.
            payload: Payload; checked against the ``consumes`` model when declared
                and against the registry for registered names.
            sender: Instance id of the sending module (``source`` becomes
                ``kit/<sender>``).

        Raises:
            ValueError: The payload does not match the declared or registered model.
        """
        body = dict(payload or {})
        model = dict(self.module.consumes).get(name)
        if model is not None:
            problem = _validate(model, body)
            if problem:
                raise ValueError(f"{name} payload does not match its consumes model: {problem}")
        validate_payload(name, body)
        nid = uuid4()
        self.engine.bus.publish(
            Notification(
                id=nid,
                trace=nid,
                ts=self.clock.wall(),
                name=name,
                source=f"kit/{sender or 'harness'}",
                payload=body,
            )
        )
        await self.settle()

    async def client_message(self, type: str, payload: Mapping[str, Any] | None = None) -> None:
        """Deliver a message from the module's own front-end.

        Args:
            type: Module-private message type.
            payload: Payload.

        Raises:
            RuntimeError: The instance is not running.
        """
        msg = ClientMessage(type=type, payload=dict(payload or {}), client="kit", trace=uuid4())
        if not self.engine.supervisor.deliver_client_message(self.instance_id, msg):
            raise RuntimeError(f"{self.instance_id} is not running")
        await self.settle()

    async def request(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - mirrors ctx.request
    ) -> dict[str, Any]:
        """Send a request to the instance and return its reply (core spec 5.4).

        Virtual time moves on by itself while the instance answers.

        Raises:
            RequestTimeoutError: No reply within ``timeout`` virtual seconds.
            RemoteError: The instance's ``on_request`` raised.
        """
        body = dict(payload or {})
        pair = dict(self.module.answers).get(name)
        if pair is not None:
            problem = _validate(pair[0], body)
            if problem:
                raise ValueError(f"{name} request does not match its answers model: {problem}")
        bus = self.engine.bus
        nid = uuid4()
        future = bus.open_request(nid)
        try:
            bus.publish(
                Notification(
                    id=nid,
                    trace=nid,
                    ts=self.clock.wall(),
                    name=name,
                    source=KIT_SOURCE,
                    target=self.instance_id,
                    expects_reply=True,
                    payload=body,
                )
            )

            async def wait() -> Notification:
                async with asyncio.timeout(timeout):
                    return await future

            try:
                reply = await self.clock.drive(wait(), limit=timeout + 1)
            except TimeoutError:
                raise RequestTimeoutError(name, timeout) from None
        finally:
            bus.close_request(nid)
        error = _as_error_reply(reply.payload)
        if error is not None:
            raise RemoteError(error[0], error[1], reply.source)
        return dict(reply.payload)

    def respond(self, name: str, handler: Responder) -> None:
        """Answer the instance's own requests named ``name``.

        Args:
            name: Request name.
            handler: Takes the request payload and returns the reply, plain or
                async; an exception it raises becomes the error reply of
                CORE-221, so the module sees ``RemoteError``.
        """
        self._responders[name] = handler

    def _answer_kit(self, req: Notification) -> None:
        handler = self._responders.get(req.name)
        if handler is None and not self._fail_requests:
            return
        task = asyncio.ensure_future(self._reply(req, handler))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reply(self, req: Notification, handler: Responder | None) -> None:
        try:
            if handler is None:
                raise RuntimeError("kit: failing responder")
            result = handler(dict(req.payload))
            if inspect.isawaitable(result):
                result = await result
            payload = dict(result)
        except TransientError as exc:
            payload = error_reply_payload("transient", exc.message_key)
        except ConfigurationError as exc:
            payload = error_reply_payload("configuration", exc.message_key)
        except Exception:
            payload = error_reply_payload("internal", REQUEST_FAILED)
        if self._engine is None:
            return
        self._engine.bus.publish(
            Notification(
                id=uuid4(),
                trace=req.trace,
                ts=self.clock.wall(),
                name=req.name,
                source="kit/responder",
                target=self.instance_id,
                reply_to=req.id,
                payload=payload,
            )
        )

    async def set_locale(self, locale: str) -> None:
        """Change the instance locale and send it ``CORE_LOCALE_CHANGED`` (CORE-245)."""
        self.locale = locale
        self.managed.set_locale(locale)
        self.engine.supervisor.emit(
            "CORE_LOCALE_CHANGED", {"locale": locale}, target=self.instance_id
        )
        await self.settle()

    async def set_grants(self, grants: Mapping[str, Any] | None) -> None:
        """Change the grants; like a hot reload, this restarts the instance."""
        self.grants = dict(grants or {})
        inst = self.managed
        inst.spec = replace(inst.spec, grants=GrantSet.from_config(self.grants))
        await self.clock.drive(inst.restart())

    async def set_page(self, page: str) -> None:
        """Make ``page`` current and emit ``CORE_PAGE_CHANGED`` (core spec 6.9)."""
        ids = [p.id for p in self._pages.infos]
        if page not in ids:
            raise ValueError(f"unknown page {page!r}; pages are {ids}")
        self._pages.page = page
        self.engine.supervisor.emit(
            "CORE_PAGE_CHANGED",
            {
                "page": page,
                "index": ids.index(page),
                "count": len(ids),
                "pages": [{"id": p.id, "title": p.title} for p in self._pages.infos],
                "reason": "set",
            },
        )
        await self.settle()

    async def suspend(self) -> None:
        """Hide the instance: ``suspend()`` is awaited (core spec 6.9)."""
        await self.clock.drive(self.managed.set_suspended(True))

    async def resume(self) -> None:
        """Show the instance again: ``resume()`` is awaited."""
        await self.clock.drive(self.managed.set_suspended(False))

    async def reconfigure(self, config: Mapping[str, Any]) -> None:
        """Apply a changed configuration as a hot reload would (MOD-50)."""
        self.module.config_model.model_validate(dict(config))
        self.config = dict(config)
        await self.clock.drive(self.managed.reconfigure(self.config))

    async def restart(self) -> None:
        """Restart the instance: a new object and context (MOD-19)."""
        await self.clock.drive(self.engine.supervisor.restart(self.instance_id))

    async def check_contract(self) -> None:
        """Run every test-kit row of Contract section 11 on fresh instances.

        Raises:
            ContractError: Listing the failed MOD ids and why.
        """
        from mm3.testing.contract import ContractError, run_kit_checks

        results = await run_kit_checks(self)
        failed = [r for r in results if r.status == "fail"]
        if failed:
            raise ContractError(failed)


def _validate(model: type[BaseModel], payload: Mapping[str, Any]) -> str | None:
    try:
        model.model_validate(dict(payload))
    except ValidationError as exc:
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<payload>'}: {e['msg']}" for e in exc.errors()
        )
    return None
