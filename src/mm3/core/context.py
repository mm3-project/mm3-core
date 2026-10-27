"""Engine-side implementation of ``ModuleContext``.

One context per instance incarnation: a restart builds a new one and closes
the old, after which every member raises ``ContextClosedError``.

Implements:
    -: Module Contract 1.0 rev 4, §4 ModuleContext reference.
    MOD-21, MOD-22: Module Contract 1.0 rev 4, §4.2 Guarantees.
    CORE-22, CORE-26, CORE-29: Core spec 0.4, §5.3 Routing rules.
    CORE-111: Core spec 0.4, §10.5 Diagnostics and debugging.
    CORE-185, CORE-193, CORE-197: Core spec 0.4, §7 Lifecycle and error handling.
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
from mm3.errors import ContextClosedError, ReservedNameError, TransientError
from mm3.messages import NAME_PATTERN, Notification, check_payload
from mm3.scheduler import JobFn

__all__ = ["EngineContext", "InstanceCounters"]

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
    """

    handler_calls: int = 0
    handler_timeouts: int = 0
    job_timeouts: int = 0
    transient_errors: int = 0
    durations: list[float] = field(default_factory=list)


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
        -: Module Contract 1.0 rev 4, §4 ModuleContext reference.
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

    def _spawn_job(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[Any]:
        return self._spawn(coro, name)

    async def _run_job(self, fn: JobFn, name: str) -> None:
        # Implements CORE-185 (Core spec 0.4, §3.3 Scheduler): own trace id and
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
            ValueError: Bad name or payload.
        """
        self._guard()
        if name.startswith("CORE_"):
            raise ReservedNameError(f"{name}: CORE_* names are reserved for the engine")
        if not NAME_PATTERN.match(name):
            raise ValueError(f"notification name {name!r} is not UPPER_SNAKE")
        body = {} if payload is None else payload
        check_payload(body)
        nid = uuid4()
        n = Notification(
            id=nid,
            trace=current_trace() or nid,
            ts=datetime.now(UTC),
            name=name,
            source=f"{self._module_name}/{self._instance_id}",
            target=target,
            payload=body,
        )
        self._bus.publish(n, sender_instance=self._instance_id)
        return nid

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
