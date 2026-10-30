"""Lifecycle supervisor: one state machine per module instance.

Each instance incarnation runs in its own ``asyncio.TaskGroup`` holding its
inbox consumer, scheduler driver, job runs and context tasks; a fault
cancels only that group. Failures restart the instance with backoff; the
fifth failure in a sliding 10-minute window disables it.

Implements:
    CORE-04: Core spec 0.7, §3 Engine components.
    -: Core spec 0.7, §3.2 Concurrency rules.
    CORE-28, CORE-29: Core spec 0.7, §5.3 Routing rules.
    CORE-221, CORE-222: Core spec 0.7, §5.4 Request and reply.
    CORE-225: Core spec 0.7, §5.5 Shared notification registry.
    CORE-190..196: Core spec 0.7, §7 Lifecycle and error handling.
    -: Core spec 0.7, §7.2 Error reporting.
    CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
    MOD-18, MOD-19: Module Contract 1.0 rev 6, §3.3 Rules.
    CORE-202: Core spec 0.8, §6.2 Rules.
    CORE-211: Core spec 0.9, §6.3 Hot reload.
    CORE-239: Core spec 0.14, §6.9 Pages and visibility.
    CORE-278: Core spec 0.17, §3.3 Scheduler.
    CORE-100: Core spec 0.18, §11.1 Engine additions.
    MOD-85: Module Contract 1.0 rev 9, §9 Logging, health and errors.
"""

from __future__ import annotations

import asyncio
import functools
import traceback
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Executor
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from mm3.config import ModuleConfig
from mm3.core.bus import Bus, ClientChannel, Inbox, MqttDelivery
from mm3.core.clock import Clock
from mm3.core.context import (
    REQUEST_FAILED,
    EngineContext,
    InstanceCounters,
    PageControl,
    error_reply_payload,
)
from mm3.core.i18n.catalogs import I18n
from mm3.core.logging import get_logger
from mm3.core.registry import Registry
from mm3.core.services.grants import GrantSet
from mm3.core.services.http import HttpTransport, UnavailableTransport
from mm3.core.services.mqtt import MqttBroker, NoBroker
from mm3.core.settings import EngineSettings
from mm3.core.trace import current_trace, trace_scope
from mm3.errors import ConfigurationError, TransientError
from mm3.messages import ClientMessage, Notification, check_payload
from mm3.module import MirrorModule
from mm3.notifications import validate_payload

__all__ = [
    "BACKOFF_S",
    "FAILURE_WINDOW_S",
    "HandlerTimeoutsExceeded",
    "InstanceSpec",
    "State",
    "Supervisor",
]

BACKOFF_S = (1.0, 2.0, 4.0, 8.0)
"""Restart delay after the 1st..4th failure in the window (CORE-194)."""
FAILURE_WINDOW_S = 600.0
"""Sliding failure window (CORE-194)."""
MAX_FAILURES = 5
"""The fifth failure in the window disables the instance (CORE-194)."""
HANDLER_TIMEOUT_LIMIT = 3
"""Handler timeouts in a row that fail the instance (CORE-193)."""

_log = get_logger(subsystem="supervisor")


class State(StrEnum):
    """States of a module instance (CORE-190)."""

    LOADED = "LOADED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class HandlerTimeoutsExceeded(Exception):
    """Three handler timeouts in a row (CORE-193)."""


@dataclass(frozen=True)
class InstanceSpec:
    """What to run: an instance of a module with its raw configuration.

    Attributes:
        instance_id: Instance id.
        module: Module name.
        config: Raw ``config`` block, validated by the module's ``config_model``.
        locale: Instance ``locale`` overriding ``engine.locale`` (CORE-202).
        grants: Scopes granted to the instance (Contract section 8).
    """

    instance_id: str
    module: str
    config: Mapping[str, Any] = field(default_factory=dict)
    locale: str | None = None
    grants: GrantSet = field(default_factory=GrantSet)


@dataclass
class _Incarnation:
    ctx: EngineContext
    obj: MirrorModule
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    group: asyncio.TaskGroup | None = None
    host: asyncio.Task[None] | None = None
    consumer: asyncio.Task[None] | None = None
    ending: bool = False


_BACKEND_HOOKS = (
    "start",
    "stop",
    "suspend",
    "resume",
    "reconfigure",
    "on_notification",
    "on_client_message",
    "on_request",
    "snapshot",
)


def has_backend(cls: type[MirrorModule]) -> bool:
    """Tell whether a module has a backend: it overrides at least one hook.

    Front-end-only modules ship the class with no hooks (Contract section 1).

    Args:
        cls: The module class.

    Returns:
        True when any hook of ``MirrorModule`` is overridden.

    Implements:
        CORE-211: Core spec 0.9, §6.3 Hot reload.
    """
    return any(getattr(cls, h) is not getattr(MirrorModule, h) for h in _BACKEND_HOOKS)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


class ManagedInstance:
    """One instance and its state machine.

    Args:
        supervisor: Owner.
        spec: What to run.

    Implements:
        CORE-190..196: Core spec 0.7, §7 Lifecycle and error handling.
    """

    def __init__(self, supervisor: Supervisor, spec: InstanceSpec) -> None:
        self._sup = supervisor
        self.spec = spec
        self.state = State.LOADED
        self.counters = InstanceCounters()
        self.inbox = Inbox(spec.instance_id, supervisor.settings.bus.queue_size, self._monotonic)
        self.failures: deque[float] = deque()
        self.restarts = 0
        self.started_at: datetime | None = None
        self.last_error: dict[str, Any] | None = None
        self.reason: str | None = None
        self._inc: _Incarnation | None = None
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task[None] | None = None
        self._consecutive_timeouts = 0
        self.last_in: datetime | None = None
        self.hidden = False

    @property
    def instance_id(self) -> str:
        """Instance id."""
        return self.spec.instance_id

    @property
    def obj(self) -> MirrorModule | None:
        """The current module object, if an incarnation exists."""
        return self._inc.obj if self._inc else None

    def _monotonic(self) -> float:
        return self._sup.clock.boottime()

    def _set(self, state: State) -> None:
        if state != self.state:
            _log.info("instance_state", instance=self.instance_id, old=self.state, new=state)
            old, self.state = self.state, state
            for listener in list(self._sup.state_listeners):
                listener(self.instance_id, old, state)

    # --- launch -----------------------------------------------------------------

    async def launch(self) -> None:
        """Build a new incarnation and start it."""
        async with self._lock:
            await self._launch()

    async def _launch(self) -> None:
        # Implements CORE-191 (Core spec 0.7, §7 Lifecycle and error handling):
        # a restart builds a new object with a new context.
        sup = self._sup
        entry = sup.registry.get(self.spec.module)
        if entry is None or entry.cls is None:
            await self._disable(entry.error if entry else f"unknown module {self.spec.module!r}")
            return
        cls = entry.cls
        try:
            config = cls.config_model.model_validate(dict(self.spec.config))
        except ValidationError as exc:
            await self._disable("config_invalid", exc)
            return
        self.inbox.clear()
        holder: dict[str, _Incarnation] = {}

        def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
            inc = holder.get("inc")
            if inc is None or inc.group is None or inc.ending:
                coro.close()
                raise RuntimeError("instance is not running")
            return inc.group.create_task(coro, name=f"{self.instance_id}:{name}")

        ctx = sup.make_context(self, spawn)
        try:
            obj = cls(ctx, config)
        except Exception as exc:
            ctx.close()
            await self._disable("init_failed", exc)
            return
        inc = _Incarnation(ctx=ctx, obj=obj)
        holder["inc"] = inc
        self._inc = inc
        inc.host = asyncio.create_task(self._host(inc), name=f"instance:{self.instance_id}")
        inc.host.add_done_callback(functools.partial(self._host_done, inc))
        await inc.ready.wait()

        self._set(State.STARTING)
        self.started_at = sup.clock.wall()
        outcome = await self._call(inc, obj.start(), sup.settings.timeouts.start, "start")
        if outcome is not None:
            await self._handle_outcome(outcome)
            return
        if inc.host.done():
            await self._crashed(inc)
            return
        sup.bus.register(self.inbox, cls.subscribes)
        inc.consumer = inc.group.create_task(  # type: ignore[union-attr]
            self._consume(inc), name=f"{self.instance_id}:inbox"
        )
        self._set(State.RUNNING)
        if sup.suspended or self.hidden:
            await self._transition_suspend(inc, True)

    async def _host(self, inc: _Incarnation) -> None:
        async with asyncio.TaskGroup() as group:
            inc.group = group
            group.create_task(inc.ctx.engine_scheduler.run(), name=f"{self.instance_id}:scheduler")
            inc.ready.set()
            await asyncio.Future()

    def _host_done(self, inc: _Incarnation, _task: asyncio.Task[None]) -> None:
        if inc.ending or self._inc is not inc:
            return
        self._sup.background(self._on_crash(inc), f"crash:{self.instance_id}")

    async def _on_crash(self, inc: _Incarnation) -> None:
        async with self._lock:
            if inc.ending or self._inc is not inc:
                return
            await self._crashed(inc)

    async def _crashed(self, inc: _Incarnation) -> None:
        assert inc.host is not None  # noqa: S101
        exc = inc.host.exception() if not inc.host.cancelled() else None
        leaves = _leaves(exc) if exc else [RuntimeError("instance task group ended")]
        config_errors = [e for e in leaves if isinstance(e, ConfigurationError)]
        if config_errors:
            await self._teardown(call_stop=True)
            await self._disable("configuration_error", config_errors[0])
            return
        await self._fail("task_failed", leaves[0])

    # --- hooks ------------------------------------------------------------------

    async def _call(
        self, inc: _Incarnation, coro: Coroutine[Any, Any, Any], budget: float, hook: str
    ) -> tuple[str, BaseException] | None:
        """Run a lifecycle hook under its budget; return a failure, if any."""
        cm = self._sup.clock.timeout(budget)
        try:
            with trace_scope(uuid4()):
                async with cm:
                    await coro
        except TimeoutError as exc:
            if cm.expired():
                return (f"{hook}_timeout", exc)
            return (f"{hook}_failed", exc)
        except TransientError as exc:
            self.counters.transient_errors += 1
            get_logger(self.spec.module, self.instance_id).warning(
                f"{hook}_transient_error", key=exc.message_key
            )
        except ConfigurationError as exc:
            return ("configuration_error", exc)
        except Exception as exc:
            return (f"{hook}_failed", exc)
        return None

    async def _handle_outcome(self, outcome: tuple[str, BaseException]) -> None:
        reason, exc = outcome
        if isinstance(exc, ConfigurationError):
            await self._teardown(call_stop=True)
            await self._disable(reason, exc)
        else:
            await self._fail(reason, exc)

    async def _consume(self, inc: _Incarnation) -> None:
        # Implements CORE-28 (Core spec 0.7, §5.3 Routing rules): one item at a
        # time, in arrival order; CORE-193 handler budget.
        budget = self._sup.settings.timeouts.handler
        loop = asyncio.get_running_loop()
        log = get_logger(self.spec.module, self.instance_id)
        while True:
            item = await self.inbox.get()
            self.last_in = self._sup.clock.wall()
            started = loop.time()
            cm = self._sup.clock.timeout(budget)
            with trace_scope(item.trace):
                try:
                    async with cm:
                        if isinstance(item, ClientMessage):
                            await inc.obj.on_client_message(item)
                        elif isinstance(item, MqttDelivery):
                            # CORE-100: MQTT messages are handled like notifications.
                            if item.active():
                                await item.handler(item.message)
                        elif item.is_request:
                            await self._answer(inc, item)
                        else:
                            await inc.obj.on_notification(item)
                    self._consecutive_timeouts = 0
                except TimeoutError:
                    if not cm.expired():
                        raise
                    self.counters.handler_timeouts += 1
                    self._consecutive_timeouts += 1
                    log.warning("handler_timeout", budget_s=budget, row=self._consecutive_timeouts)
                    if self._consecutive_timeouts >= HANDLER_TIMEOUT_LIMIT:
                        raise HandlerTimeoutsExceeded(
                            f"{HANDLER_TIMEOUT_LIMIT} handler timeouts in a row"
                        ) from None
                except TransientError as exc:
                    self.counters.transient_errors += 1
                    self._consecutive_timeouts = 0
                    log.warning("handler_transient_error", key=exc.message_key)
            self.counters.handler_calls += 1
            self.counters.durations.append(loop.time() - started)
            del self.counters.durations[:-200]

    async def _answer(self, inc: _Incarnation, req: Notification) -> None:
        # Implements CORE-221 (Core spec 0.7, §5.4 Request and reply): a dict is
        # the reply, None sends nothing; a raising on_request sends an error
        # reply and still counts as a handler failure. A cancelled (timed out)
        # on_request sends no reply.
        try:
            result = await inc.obj.on_request(req)
            if result is not None:
                if not isinstance(result, dict):
                    raise TypeError(f"on_request returned {type(result).__name__}, not dict")
                check_payload(result)
        except TransientError as exc:
            inc.ctx.send_reply(req, error_reply_payload("transient", exc.message_key))
            raise
        except ConfigurationError as exc:
            inc.ctx.send_reply(req, error_reply_payload("configuration", exc.message_key))
            raise
        except Exception:
            inc.ctx.send_reply(req, error_reply_payload("internal", REQUEST_FAILED))
            raise
        if result is not None:
            inc.ctx.send_reply(req, result)

    # --- failure handling -------------------------------------------------------

    def _record_error(self, reason: str, exc: BaseException | None) -> None:
        self.reason = reason
        if exc is None:
            return
        trace = current_trace()
        self.last_error = {
            "reason": reason,
            "message": f"{type(exc).__name__}: {exc}",
            "traceback": "".join(traceback.format_exception(exc)),
            "time": self._sup.clock.wall().isoformat(),
            "trace_id": str(trace) if trace else None,
        }

    async def _fail(self, reason: str, exc: BaseException | None) -> None:
        # Implements CORE-194 (Core spec 0.7, §7 Lifecycle and error handling).
        self._record_error(reason, exc)
        get_logger(self.spec.module, self.instance_id).error(
            "instance_failed", reason=reason, error=self.last_error and self.last_error["message"]
        )
        await self._teardown(call_stop=True)
        now = self._sup.clock.boottime()
        while self.failures and now - self.failures[0] > FAILURE_WINDOW_S:
            self.failures.popleft()
        self.failures.append(now)
        count = len(self.failures)
        if count >= MAX_FAILURES:
            await self._disable("too_many_failures", None)
            return
        self._set(State.FAILED)
        self._sup.emit_failure(self, State.FAILED, reason)
        delay = BACKOFF_S[count - 1]
        self._timer = asyncio.create_task(
            self._restart_later(delay), name=f"restart:{self.instance_id}"
        )

    async def _restart_later(self, delay: float) -> None:
        await self._sup.clock.sleep(delay)
        async with self._lock:
            if self.state is State.FAILED:
                self._timer = None
                self.restarts += 1
                await self._launch()

    async def _disable(self, reason: str | None, exc: BaseException | None = None) -> None:
        self._record_error(reason or "disabled", exc)
        get_logger(self.spec.module, self.instance_id).error(
            "instance_disabled", reason=self.reason
        )
        self._set(State.DISABLED)
        self._sup.emit_failure(self, State.DISABLED, self.reason or "disabled")

    async def _teardown(self, *, call_stop: bool) -> None:
        inc = self._inc
        if inc is None:
            return
        inc.ending = True
        self._sup.bus.unregister(self.instance_id)
        if inc.consumer is not None and not inc.consumer.done():
            inc.consumer.cancel()
            await asyncio.wait([inc.consumer])
        if call_stop:
            self._set(State.STOPPING)
            outcome = await self._call(
                inc, inc.obj.stop(), self._sup.settings.timeouts.stop, "stop"
            )
            if outcome is not None:
                _log.warning("stop_failed", instance=self.instance_id, reason=outcome[0])
        inc.ctx.close()
        if inc.host is not None:
            inc.host.cancel()
            await asyncio.wait([inc.host])
        self._inc = None
        self._consecutive_timeouts = 0

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    # --- external transitions ---------------------------------------------------

    async def stop(self) -> None:
        """Stop the instance (removal or shutdown)."""
        async with self._lock:
            self._cancel_timer()
            if self._inc is not None:
                await self._teardown(call_stop=True)
            if self.state is not State.DISABLED:
                self._set(State.STOPPED)

    async def restart(self) -> None:
        """Manual restart from any state; clears the failure window (CORE-195)."""
        async with self._lock:
            self._cancel_timer()
            self.failures.clear()
            await self._teardown(call_stop=True)
            self.restarts += 1
            await self._launch()

    async def reconfigure(self, config: Mapping[str, Any]) -> None:
        """Apply a changed configuration.

        Args:
            config: The new raw configuration block.
        """
        async with self._lock:
            self.spec = replace(self.spec, config=dict(config))
            inc = self._inc
            if self.state not in (State.RUNNING, State.SUSPENDED) or inc is None:
                if self.state in (State.DISABLED, State.FAILED):
                    self._cancel_timer()
                    self.failures.clear()
                    await self._launch()
                return
            try:
                new: ModuleConfig = type(inc.obj).config_model.model_validate(dict(config))
            except ValidationError as exc:
                await self._teardown(call_stop=True)
                await self._disable("config_invalid", exc)
                return
            applied: list[bool] = []

            async def call() -> None:
                applied.append(await inc.obj.reconfigure(new))

            outcome = await self._call(
                inc, call(), self._sup.settings.timeouts.reconfigure, "reconfigure"
            )
            if outcome is not None:
                await self._handle_outcome(outcome)
            elif applied and applied[0]:
                inc.obj.config = new
            else:
                await self._teardown(call_stop=True)
                await self._launch()

    def set_locale(self, locale: str | None) -> None:
        """Change the instance ``locale`` override; the running context follows it.

        Args:
            locale: New override, ``None`` to follow ``engine.locale``.

        Implements:
            CORE-211: Core spec 0.9, §6.3 Hot reload.
        """
        self.spec = replace(self.spec, locale=locale)
        if self._inc is not None:
            self._inc.ctx.update(locale=locale or self._sup.settings.locale)

    @property
    def context(self) -> EngineContext | None:
        """The context of the current incarnation, if any."""
        return self._inc.ctx if self._inc is not None else None

    async def set_hidden(self, hidden: bool) -> None:
        """Mark the instance hidden or visible and suspend or resume it (CORE-239).

        A hidden instance stays suspended while the mirror is resumed; a
        visible one follows the mirror.

        Args:
            hidden: Whether the instance is hidden.

        Implements:
            CORE-239: Core spec 0.14, §6.9 Pages and visibility.
        """
        if hidden == self.hidden:
            return
        self.hidden = hidden
        await self.set_suspended(hidden or self._sup.suspended)

    async def set_suspended(self, suspended: bool) -> None:
        """Call ``suspend()`` or ``resume()`` on a running instance.

        Args:
            suspended: Target state.
        """
        async with self._lock:
            if self._inc is not None and self.state in (State.RUNNING, State.SUSPENDED):
                await self._transition_suspend(self._inc, suspended)

    async def _transition_suspend(self, inc: _Incarnation, suspended: bool) -> None:
        if (self.state is State.SUSPENDED) == suspended:
            return
        hook = inc.obj.suspend() if suspended else inc.obj.resume()
        name = "suspend" if suspended else "resume"
        outcome = await self._call(inc, hook, self._sup.settings.timeouts.suspend, name)
        if outcome is not None:
            await self._handle_outcome(outcome)
            return
        inc.ctx.engine_scheduler.set_suspended(suspended)
        self._set(State.SUSPENDED if suspended else State.RUNNING)

    def health(self) -> dict[str, Any]:
        """Return the instance's health record.

        Served over REST by the admin API (core spec 9.1, milestone 6).

        Returns:
            State, start time, restarts, last error, handler calls, p95
            duration and timeouts, queue depth, and time of the last message
            in and out.

        Implements:
            CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
        """
        durations = sorted(self.counters.durations)
        p95 = durations[int(0.95 * (len(durations) - 1))] if durations else None
        return {
            "instance": self.instance_id,
            "module": self.spec.module,
            "state": str(self.state),
            "reason": self.reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "restarts": self.restarts,
            "failures_in_window": len(self.failures),
            "last_error": self.last_error,
            "handler_calls": self.counters.handler_calls,
            "handler_p95_s": p95,
            "handler_timeouts": self.counters.handler_timeouts,
            "job_timeouts": self.counters.job_timeouts,
            "transient_errors": self.counters.transient_errors,
            "queue_depth": len(self.inbox),
            "dropped": self.inbox.dropped,
            "last_in": self.last_in.isoformat() if self.last_in else None,
            "last_out": out.isoformat() if (out := self.counters.last_out) else None,
            "fields": dict(self.counters.health_fields),
        }


class Supervisor:
    """Owns every instance's state machine.

    Args:
        settings: Engine settings.
        registry: Module registry.
        bus: The bus.
        clients: Path to front-end elements.
        clock: Time source.
        executor: Shared ``run_blocking`` pool.
        state_dir: Engine state directory.

    Implements:
        CORE-04: Core spec 0.7, §3 Engine components.
    """

    def __init__(
        self,
        *,
        settings: EngineSettings,
        registry: Registry,
        bus: Bus,
        clients: ClientChannel,
        clock: Clock,
        executor: Executor,
        state_dir: Path,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.bus = bus
        self.clients = clients
        self.clock = clock
        self.executor = executor
        self.state_dir = state_dir
        self.timezone = ZoneInfo(settings.timezone)
        self.suspended = False
        self._instances: dict[str, ManagedInstance] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self.state_listeners: list[Callable[[str, State, State], None]] = []
        self.pages: PageControl | None = None
        self.http_transport: HttpTransport = UnavailableTransport()
        self.mqtt_broker: MqttBroker = NoBroker()
        self.on_denied: Callable[[str, str], None] | None = None
        self.i18n: I18n | None = None

    def make_context(
        self,
        inst: ManagedInstance,
        spawn: Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]],
    ) -> EngineContext:
        """Build a new context for an instance incarnation.

        Args:
            inst: The instance.
            spawn: Starts a coroutine in the incarnation's task group.

        Returns:
            The context.
        """
        entry = self.registry.get(inst.spec.module)
        cls = entry.cls if entry is not None else None
        return EngineContext(
            instance_id=inst.instance_id,
            module_name=inst.spec.module,
            bus=self.bus,
            clients=self.clients,
            clock=self.clock,
            executor=self.executor,
            timeouts=self.settings.timeouts,
            timezone=self.timezone,
            locale=inst.spec.locale or self.settings.locale,
            units=self.settings.units,
            state_dir=self.state_dir,
            counters=inst.counters,
            spawn=spawn,
            pages=self.pages,
            capabilities=frozenset(cls.capabilities) if cls is not None else frozenset(),
            i18n=self.i18n,
            engine_locale=lambda: self.settings.locale,
            grants=inst.spec.grants,
            http_transport=self.http_transport,
            mqtt_broker=self.mqtt_broker,
            enqueue=inst.inbox.put,
            on_denied=(
                functools.partial(self.on_denied, inst.instance_id)
                if self.on_denied is not None
                else None
            ),
        )

    def background(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        """Run supervisor work in a tracked task.

        Args:
            coro: The coroutine.
            name: Task name.
        """
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def emit(self, name: str, payload: dict[str, Any], target: str | None = None) -> None:
        """Publish an engine notification (``CORE_*``).

        Args:
            name: Notification name.
            payload: Payload; checked against the registry (CORE-225).
            target: Target instance, if any.
        """
        validate_payload(name, payload)
        nid = uuid4()
        self.bus.publish(
            Notification(
                id=nid,
                trace=current_trace() or nid,
                ts=self.clock.wall(),
                name=name,
                source="core",
                target=target,
                payload=payload,
            )
        )

    def emit_failure(self, inst: ManagedInstance, state: State, reason: str) -> None:
        """Emit ``CORE_MODULE_FAILED`` for a failed or disabled instance (7.2).

        Args:
            inst: The instance.
            state: FAILED or DISABLED.
            reason: Short reason code.
        """
        self.emit(
            "CORE_MODULE_FAILED",
            {
                "instance": inst.instance_id,
                "module": inst.spec.module,
                "state": str(state),
                "reason": reason,
            },
        )

    # --- instances ----------------------------------------------------------------

    def add(self, spec: InstanceSpec) -> ManagedInstance:
        """Register an instance in state LOADED.

        Args:
            spec: What to run.

        Returns:
            The managed instance.

        Raises:
            ValueError: Duplicate instance id.
        """
        if spec.instance_id in self._instances:
            raise ValueError(f"duplicate instance id {spec.instance_id!r}")
        inst = ManagedInstance(self, spec)
        self._instances[spec.instance_id] = inst
        return inst

    def get(self, instance_id: str) -> ManagedInstance:
        """Return an instance.

        Args:
            instance_id: Instance id.

        Returns:
            The managed instance.

        Raises:
            KeyError: Unknown instance.
        """
        return self._instances[instance_id]

    def instances(self) -> list[ManagedInstance]:
        """Return all instances in insertion order.

        Returns:
            The instances.
        """
        return list(self._instances.values())

    async def remove(self, instance_id: str) -> None:
        """Stop and forget an instance; its stored data is kept (CORE-198).

        Args:
            instance_id: Instance id.
        """
        inst = self._instances.pop(instance_id)
        await inst.stop()

    async def restart(self, instance_id: str) -> None:
        """Manual restart (CORE-195).

        Args:
            instance_id: Instance id.
        """
        await self._instances[instance_id].restart()

    async def start_all(self) -> None:
        """Start every LOADED instance concurrently (core spec 7.1 step 4)."""
        await asyncio.gather(
            *(i.launch() for i in self._instances.values() if i.state is State.LOADED)
        )

    async def stop_all(self) -> None:
        """Stop every instance concurrently."""
        await asyncio.gather(*(i.stop() for i in self._instances.values()))
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.wait(self._background)

    async def set_suspended(self, suspended: bool) -> None:
        """Suspend or resume every instance (core spec 9.2 engine hooks).

        Args:
            suspended: Target state.
        """
        if suspended == self.suspended:
            return
        self.suspended = suspended
        self.emit("CORE_SUSPEND" if suspended else "CORE_RESUME", {})
        await asyncio.gather(
            *(i.set_suspended(suspended or i.hidden) for i in self._instances.values())
        )

    def set_timezone(self, timezone: str) -> None:
        """Change ``engine.timezone`` and re-plan wall-clock jobs (CORE-189).

        Args:
            timezone: IANA zone name.
        """
        self.timezone = ZoneInfo(timezone)
        for inst in self._instances.values():
            if (ctx := inst.context) is not None:
                ctx.update(timezone=self.timezone)

    def apply_settings(self, settings: EngineSettings) -> None:
        """Use reloaded engine settings for everything that changes live.

        Time budgets apply to calls started afterwards, ``bus.queue_size`` to
        inboxes created afterwards; ``engine.locale`` and ``engine.timezone``
        reach every running context (CORE-211). ``engine.units`` is applied
        by restarting instances (see :func:`has_backend`).

        Args:
            settings: The new ``engine`` block.

        Implements:
            CORE-211: Core spec 0.9, §6.3 Hot reload.
        """
        old = self.settings
        self.settings = settings
        if settings.timezone != old.timezone:
            self.set_timezone(settings.timezone)
        for inst in self._instances.values():
            if (ctx := inst.context) is not None:
                ctx.update(
                    locale=inst.spec.locale or settings.locale,
                    units=settings.units,
                    timeouts=settings.timeouts,
                )

    def deliver_client_message(self, instance_id: str, msg: ClientMessage) -> bool:
        """Queue a front-end message for its instance.

        Args:
            instance_id: Receiving instance.
            msg: The message.

        Returns:
            True when queued.
        """
        return self.bus.deliver_client_message(instance_id, msg)
