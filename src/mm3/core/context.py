"""Engine-side implementation of ``ModuleContext``.

One context per instance incarnation: a restart builds a new one and closes
the old, after which every member raises ``ContextClosedError``.

Implements:
    -: Module Contract 1.0 rev 6, §4 ModuleContext reference.
    -: Module Contract 1.0 rev 6, §4.1 Exceptions raised by the context.
    MOD-21, MOD-22: Module Contract 1.0 rev 6, §4.2 Guarantees.
    MOD-91: Module Contract 1.0 rev 6, §10 API versioning and deprecation.
    CORE-22, CORE-26, CORE-29: Core spec 0.7, §5.3 Routing rules.
    CORE-220, CORE-221, CORE-222: Core spec 0.7, §5.4 Request and reply.
    CORE-225: Core spec 0.7, §5.5 Shared notification registry.
    CORE-111, CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
    CORE-185, CORE-193, CORE-197: Core spec 0.7, §7 Lifecycle and error handling.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeVar
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from mm3.core.bus import Bus, ClientChannel
from mm3.core.clock import Clock
from mm3.core.logging import Logger, get_logger
from mm3.core.services.scheduler import InstanceScheduler
from mm3.core.services.storage import InstanceStorage
from mm3.core.settings import Timeouts
from mm3.core.trace import current_trace, trace_scope
from mm3.errors import (
    ContextClosedError,
    RemoteError,
    RequestTimeoutError,
    ReservedNameError,
    TransientError,
)
from mm3.messages import NAME_PATTERN, Notification, check_payload
from mm3.notifications import validate_payload
from mm3.scheduler import JobFn

__all__ = [
    "REQUEST_FAILED",
    "REQUEST_TIMEOUT_RANGE",
    "EngineContext",
    "InstanceCounters",
    "error_reply_payload",
]

REQUEST_TIMEOUT_RANGE = (0.1, 60.0)
"""Allowed ``timeout`` of ``ctx.request``, in seconds (CORE-220)."""
REQUEST_FAILED = "REQUEST_FAILED"
"""Core catalog key of an ``internal`` error reply (CORE-221)."""


def error_reply_payload(code: str, message_key: str) -> dict[str, Any]:
    """Build the payload of an error reply.

    Args:
        code: ``transient``, ``configuration`` or ``internal``.
        message_key: Catalog key.

    Returns:
        ``{"error": {"code": ..., "message_key": ...}}``.

    Implements:
        CORE-221: Core spec 0.7, §5.4 Request and reply.
    """
    return {"error": {"code": code, "message_key": message_key}}


def _as_error_reply(payload: dict[str, Any]) -> tuple[str, str] | None:
    error = payload.get("error") if len(payload) == 1 else None
    if isinstance(error, dict) and set(error) == {"code", "message_key"}:
        code, key = error["code"], error["message_key"]
        if isinstance(code, str) and isinstance(key, str):
            return code, key
    return None


T = TypeVar("T")


@dataclass
class InstanceCounters:
    """Counters shared by all incarnations of an instance (health record, CORE-113).

    Attributes:
        handler_calls: Handlers run.
        handler_timeouts: Handlers cancelled for exceeding their budget.
        job_timeouts: Job runs cancelled for exceeding their budget.
        transient_errors: ``TransientError`` raised by hooks, tasks or jobs.
        durations: Recent handler durations in seconds (for p95).
        last_out: Time the instance last sent a notification, reply or
            client message.
        deprecated_used: Deprecated members already reported (MOD-91).
    """

    handler_calls: int = 0
    handler_timeouts: int = 0
    job_timeouts: int = 0
    transient_errors: int = 0
    durations: list[float] = field(default_factory=list)
    last_out: datetime | None = None
    deprecated_used: set[str] = field(default_factory=set)


class EngineContext:
    """The context handed to one instance incarnation.

    Args:
        instance_id: Instance id.
        module_name: Module name.
        bus: The bus.
        clients: Path to the instance's front-end elements.
        clock: Time source.
        executor: Shared ``run_blocking`` pool.
        timeouts: Time budgets.
        timezone: Engine time zone.
        locale: Active locale.
        units: Unit system.
        state_dir: Engine state directory.
        counters: Health counters of the instance.
        spawn: Starts a coroutine in the instance's task group.

    Implements:
        -: Module Contract 1.0 rev 6, §4 ModuleContext reference.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        module_name: str,
        bus: Bus,
        clients: ClientChannel,
        clock: Clock,
        executor: Executor,
        timeouts: Timeouts,
        timezone: ZoneInfo,
        locale: str,
        units: Literal["metric", "imperial"],
        state_dir: Path,
        counters: InstanceCounters,
        spawn: Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]],
    ) -> None:
        self._instance_id = instance_id
        self._module_name = module_name
        self._bus = bus
        self._clients = clients
        self._clock = clock
        self._executor = executor
        self._timeouts = timeouts
        self._timezone = timezone
        self._locale = locale
        self._units = units
        self._counters = counters
        self._spawn = spawn
        self._closed = False
        self._state: dict[str, Any] = {}
        self._log = get_logger(module_name, instance_id)
        self._storage = InstanceStorage(state_dir, instance_id, self.run_blocking)
        self._scheduler = InstanceScheduler(
            clock, timezone, self._run_job, self._spawn_job, self._log
        )

    # --- engine side ----------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once the instance incarnation has stopped."""
        return self._closed

    def close(self) -> None:
        """Close the context and cancel its jobs (MOD-22)."""
        self._closed = True
        self._scheduler.cancel_all()

    @property
    def engine_scheduler(self) -> InstanceScheduler:
        """The scheduler with its engine-side controls."""
        return self._scheduler

    def _guard(self) -> None:
        if self._closed:
            raise ContextClosedError(f"context of {self._instance_id} is closed")

    @property
    def _source(self) -> str:
        return f"{self._module_name}/{self._instance_id}"

    def _sent(self) -> None:
        self._counters.last_out = self._clock.wall()

    def deprecated(self, member: str, replacement: str) -> None:
        """Report use of a deprecated member, once per instance.

        Deprecated members call this and keep working for at least one
        further minor release.

        Args:
            member: The deprecated member, e.g. ``"ctx.old()"``.
            replacement: What to use instead.

        Implements:
            MOD-91: Module Contract 1.0 rev 6, §10 API versioning and deprecation.
        """
        if member in self._counters.deprecated_used:
            return
        self._counters.deprecated_used.add(member)
        self._log.warning("deprecated_api", member=member, replacement=replacement)

    def _outgoing(self, name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if name.startswith("CORE_"):
            raise ReservedNameError(f"{name}: CORE_* names are reserved for the engine")
        if not NAME_PATTERN.match(name):
            raise ValueError(f"notification name {name!r} is not UPPER_SNAKE")
        body = {} if payload is None else payload
        check_payload(body)
        validate_payload(name, body)
        return body

    def send_reply(self, request: Notification, payload: dict[str, Any]) -> None:
        """Answer a request on behalf of this instance (engine side).

        The reply has the request's name and trace, ``reply_to`` = request id
        and ``target`` = the requesting instance, if any.

        Args:
            request: The request being answered.
            payload: Reply payload (already checked).

        Implements:
            CORE-221: Core spec 0.7, §5.4 Request and reply.
        """
        requester = request.source.split("/", 1)[1] if "/" in request.source else None
        self._bus.publish(
            Notification(
                id=uuid4(),
                trace=request.trace,
                ts=datetime.now(UTC),
                name=request.name,
                source=self._source,
                target=requester,
                reply_to=request.id,
                payload=payload,
            ),
            sender_instance=self._instance_id,
        )
        self._sent()

    def _spawn_job(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[Any]:
        return self._spawn(coro, name)

    async def _run_job(self, fn: JobFn, name: str) -> None:
        # Implements CORE-185 (Core spec 0.7, §3.3 Scheduler): own trace id and
        # job timeout; TransientError is a warning, other exceptions fail the instance.
        trace = uuid4()
        with trace_scope(trace):
            cm = asyncio.timeout(self._timeouts.job)
            try:
                async with cm:
                    await fn()
            except TimeoutError:
                if not cm.expired():
                    raise
                self._counters.job_timeouts += 1
                self._log.warning("job_timeout", job=name, budget_s=self._timeouts.job)
            except TransientError as exc:
                self._counters.transient_errors += 1
                self._log.warning("job_transient_error", job=name, key=exc.message_key)

    # --- identity -------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        """Instance id."""
        self._guard()
        return self._instance_id

    @property
    def module_name(self) -> str:
        """Module name."""
        self._guard()
        return self._module_name

    @property
    def locale(self) -> str:
        """Active locale."""
        self._guard()
        return self._locale

    @property
    def timezone(self) -> ZoneInfo:
        """Engine time zone."""
        self._guard()
        return self._timezone

    @property
    def units(self) -> Literal["metric", "imperial"]:
        """Unit system."""
        self._guard()
        return self._units

    # --- messaging and state --------------------------------------------------

    async def send_notification(
        self, name: str, payload: dict[str, Any] | None = None, target: str | None = None
    ) -> UUID:
        """Send a notification on the bus.

        Args:
            name: ``UPPER_SNAKE`` name, not starting with ``CORE_``.
            payload: JSON object, at most 256 KiB.
            target: Instance id for a targeted notification; ``None`` broadcasts.

        Returns:
            The notification id.

        Raises:
            ReservedNameError: The name starts with ``CORE_``.
            ValueError: Bad name or payload, or the payload of a registered
                name does not match its schema (CORE-225).
        """
        self._guard()
        body = self._outgoing(name, payload)
        nid = uuid4()
        n = Notification(
            id=nid,
            trace=current_trace() or nid,
            ts=datetime.now(UTC),
            name=name,
            source=self._source,
            target=target,
            payload=body,
        )
        self._bus.publish(n, sender_instance=self._instance_id)
        self._sent()
        return nid

    async def request(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        target: str | None = None,
        timeout: float = 5.0,  # noqa: ASYNC109 - signature fixed by Contract §4
    ) -> dict[str, Any]:
        """Ask another module for data and wait for the first reply.

        With ``target`` only that instance is asked; without, the request is
        delivered like a broadcast and the first reply wins. The reply
        bypasses this instance's inbox, so a handler may await it.

        Args:
            name: ``UPPER_SNAKE`` name, not starting with ``CORE_``.
            payload: JSON object, at most 256 KiB.
            target: Instance to ask; ``None`` asks every subscriber.
            timeout: Seconds to wait, 0.1 to 60.

        Returns:
            The reply payload.

        Raises:
            ReservedNameError: The name starts with ``CORE_``.
            ValueError: Bad name, payload or timeout.
            RequestTimeoutError: No reply within ``timeout``.
            RemoteError: The responder's ``on_request`` raised.

        Implements:
            CORE-220, CORE-221, CORE-222: Core spec 0.7, §5.4 Request and reply.
        """
        self._guard()
        low, high = REQUEST_TIMEOUT_RANGE
        if not low <= timeout <= high:
            raise ValueError(f"timeout must be between {low} and {high} s, got {timeout}")
        body = self._outgoing(name, payload)
        nid = uuid4()
        n = Notification(
            id=nid,
            trace=current_trace() or nid,
            ts=datetime.now(UTC),
            name=name,
            source=self._source,
            target=target,
            expects_reply=True,
            payload=body,
        )
        future = self._bus.open_request(nid)
        try:
            self._bus.publish(n, sender_instance=self._instance_id)
            self._sent()
            try:
                async with asyncio.timeout(timeout):
                    reply = await future
            except TimeoutError:
                raise RequestTimeoutError(name, timeout) from None
        finally:
            self._bus.close_request(nid)
        error = _as_error_reply(reply.payload)
        if error is not None:
            raise RemoteError(error[0], error[1], reply.source)
        return dict(reply.payload)

    async def send_to_client(
        self, type: str, payload: dict[str, Any] | None = None, client: str | None = None
    ) -> None:
        """Send a private message to this instance's front-end elements.

        Args:
            type: Module-private message type.
            payload: JSON object, at most 256 KiB.
            client: One client id, or ``None`` for all.

        Raises:
            ValueError: Bad payload.
        """
        self._guard()
        body = {} if payload is None else payload
        check_payload(body)
        self._clients.send_client(self._instance_id, type, body, client)
        self._sent()

    @property
    def state(self) -> Mapping[str, Any]:
        """Last state set, read-only."""
        self._guard()
        return MappingProxyType(self._state)

    def set_state(self, state: dict[str, Any]) -> None:
        """Replace the snapshot and push it as ``state``.

        Args:
            state: The new state.

        Raises:
            ValueError: Not a JSON object or larger than 256 KiB.
        """
        self._guard()
        check_payload(state)
        self._state = dict(state)
        self._clients.send_state(self._instance_id, dict(self._state), delta=False)

    def patch_state(self, delta: dict[str, Any]) -> None:
        """Shallow-merge into the snapshot and push the delta.

        Args:
            delta: Keys to replace.

        Raises:
            ValueError: The delta or the merged state is invalid.
        """
        self._guard()
        check_payload(delta)
        merged = {**self._state, **delta}
        check_payload(merged)
        self._state = merged
        self._clients.send_state(self._instance_id, dict(delta), delta=True)

    # --- tasks and time -------------------------------------------------------

    def create_task(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Start background work in the instance's task group.

        A ``TransientError`` ends the task with a warning; any other exception
        moves the instance to FAILED.

        Args:
            coro: The coroutine.
            name: Task name.

        Returns:
            The task.
        """
        try:
            self._guard()
        except ContextClosedError:
            coro.close()
            raise

        async def guarded() -> Any:
            try:
                return await coro
            except TransientError as exc:
                self._counters.transient_errors += 1
                self._log.warning("task_transient_error", task=name, key=exc.message_key)
                return None

        return self._spawn(guarded(), name)

    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T:
        """Run blocking code in the shared thread pool.

        Args:
            fn: The function.
            *args: Its positional arguments.

        Returns:
            Its result; discarded if the caller is cancelled.
        """
        self._guard()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    @property
    def scheduler(self) -> InstanceScheduler:
        """This instance's scheduler."""
        self._guard()
        return self._scheduler

    @property
    def storage(self) -> InstanceStorage:
        """This instance's storage."""
        self._guard()
        return self._storage

    @property
    def log(self) -> Logger:
        """This instance's logger."""
        self._guard()
        return self._log
