"""In-house asyncio scheduler: interval, cron and one-shot jobs.

One scheduler per module instance. Its driver runs inside the instance's
task group, so all jobs end with the instance. Intervals are measured on
CLOCK_BOOTTIME; wall-clock jobs are planned in UTC instants and re-planned
after a clock jump larger than 2 s or a time zone change. The driver wakes
at least once per ``max_tick`` seconds, which bounds how late a jump or a
host resume is noticed.

Implements:
    CORE-180..189: Core spec 0.4, §3.3 Scheduler.
    CORE-196: Core spec 0.4, §7 Lifecycle and error handling.
    MOD-22: Module Contract 1.0 rev 4, §4.2 Guarantees.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mm3.core.clock import Clock
from mm3.core.logging import Logger
from mm3.core.services.cron import CronSpec, next_fire, parse_cron, resolve_local
from mm3.scheduler import JobFn

__all__ = ["CLOCK_JUMP_THRESHOLD", "InstanceScheduler", "ScheduledJob"]

CLOCK_JUMP_THRESHOLD = 2.0
"""Wall-clock jumps larger than this many seconds re-plan wall-clock jobs (CORE-183)."""

RunFn = Callable[[JobFn, str], Coroutine[Any, Any, None]]
SpawnFn = Callable[[Coroutine[Any, Any, None], str], object]


def _zone(name: str | None) -> ZoneInfo | None:
    if name is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc


class ScheduledJob:
    """A job planned by :class:`InstanceScheduler`.

    Implements:
        CORE-184, CORE-186: Core spec 0.4, §3.3 Scheduler.
    """

    def __init__(
        self,
        owner: InstanceScheduler,
        kind: Literal["interval", "cron", "at"],
        fn: JobFn,
        name: str,
        *,
        pause_on_suspend: bool = False,
        interval: float = 0.0,
        jitter: float = 0.0,
        spec: CronSpec | None = None,
        zone: ZoneInfo | None = None,
        when: datetime | None = None,
    ) -> None:
        self._owner = owner
        self.kind = kind
        self.fn = fn
        self._name = name
        self.pause_on_suspend = pause_on_suspend
        self.interval = interval
        self.jitter = jitter
        self.spec = spec
        self.zone = zone
        self.when = when
        self.target: datetime | None = None
        self.due: float | None = None
        self.running = False
        self.done = False
        self.missed = False

    @property
    def name(self) -> str:
        """Job name."""
        return self._name

    @property
    def next_run(self) -> datetime | None:
        """Next planned run, aware UTC; ``None`` when finished, cancelled or paused."""
        if self.done:
            return None
        if self.kind != "interval":
            return self.target
        if self.due is None:
            return None
        clock = self._owner.clock
        return clock.wall() + timedelta(seconds=self.due - clock.boottime())

    def cancel(self) -> None:
        """Cancel future runs; a run in progress finishes."""
        self.done = True
        self.due = None
        self._owner.wake()


class InstanceScheduler:
    """Scheduler of one module instance.

    Args:
        clock: Time source.
        timezone: Engine time zone (``engine.timezone``).
        run: Runs one job run with its timeout and trace (from the context).
        spawn: Starts a coroutine as a task in the instance's task group.
        log: Logger of the instance.
        max_tick: Longest sleep of the driver, in seconds.

    Implements:
        CORE-180..189: Core spec 0.4, §3.3 Scheduler.
    """

    def __init__(
        self,
        clock: Clock,
        timezone: tzinfo,
        run: RunFn,
        spawn: SpawnFn,
        log: Logger,
        *,
        max_tick: float = 1.0,
    ) -> None:
        self.clock = clock
        self._tz = timezone
        self._run_fn = run
        self._spawn = spawn
        self._log = log
        self._max_tick = max_tick
        self._jobs: list[ScheduledJob] = []
        self._wake = asyncio.Event()
        self._suspended = False
        self._random = random.Random()  # noqa: S311 - spreading load, not security
        self._counter = 0

    # --- public API (mm3.scheduler.Scheduler) ---------------------------------

    def every(
        self,
        interval: timedelta | float,
        fn: JobFn,
        *,
        start_now: bool = False,
        jitter: float = 0.0,
        pause_on_suspend: bool = False,
        name: str | None = None,
    ) -> ScheduledJob:
        """Run ``fn`` every ``interval``; see core spec 3.3.

        Args:
            interval: Seconds or timedelta, greater than zero.
            fn: Async job function.
            start_now: First run right away instead of after one interval.
            jitter: Random extra delay up to this many seconds, every run.
            pause_on_suspend: Pause while the instance is suspended.
            name: Job name for logs.

        Returns:
            The job.

        Raises:
            ValueError: Interval not positive or jitter negative.
        """
        seconds = interval.total_seconds() if isinstance(interval, timedelta) else float(interval)
        if seconds <= 0:
            raise ValueError("interval must be greater than zero")
        if jitter < 0:
            raise ValueError("jitter must not be negative")
        job = ScheduledJob(
            self,
            "interval",
            fn,
            self._name(name, fn),
            pause_on_suspend=pause_on_suspend,
            interval=seconds,
            jitter=jitter,
        )
        job.due = self.clock.boottime() + (0.0 if start_now else seconds) + self._jitter(job)
        return self._add(job)

    def cron(
        self,
        expr: str,
        fn: JobFn,
        *,
        timezone: str | None = None,
        pause_on_suspend: bool = False,
        name: str | None = None,
    ) -> ScheduledJob:
        """Run ``fn`` at the wall-clock times of a cron expression.

        Args:
            expr: Cron expression (CORE-181).
            fn: Async job function.
            timezone: IANA zone; default ``engine.timezone``.
            pause_on_suspend: Pause while the instance is suspended.
            name: Job name for logs.

        Returns:
            The job.

        Raises:
            ValueError: Invalid or never-matching expression, unknown zone.
        """
        spec = parse_cron(expr)
        job = ScheduledJob(
            self,
            "cron",
            fn,
            self._name(name, fn),
            pause_on_suspend=pause_on_suspend,
            spec=spec,
            zone=_zone(timezone),
        )
        self._plan_wall(job, self.clock.wall(), self.clock.boottime())
        return self._add(job)

    def at(
        self,
        when: datetime,
        fn: JobFn,
        *,
        timezone: str | None = None,
        name: str | None = None,
    ) -> ScheduledJob:
        """Run ``fn`` once at ``when``; a time already past runs as soon as possible.

        Args:
            when: Aware datetime, or naive local time in ``timezone``.
            fn: Async job function.
            timezone: IANA zone for a naive ``when``; default ``engine.timezone``.
            name: Job name for logs.

        Returns:
            The job.

        Raises:
            ValueError: Unknown zone.
        """
        job = ScheduledJob(self, "at", fn, self._name(name, fn), zone=_zone(timezone), when=when)
        self._plan_wall(job, self.clock.wall(), self.clock.boottime())
        return self._add(job)

    # --- engine side ----------------------------------------------------------

    def wake(self) -> None:
        """Make the driver re-check its jobs now."""
        self._wake.set()

    def set_timezone(self, timezone: tzinfo) -> None:
        """Change the engine time zone and re-plan every wall-clock job.

        Args:
            timezone: The new engine time zone.

        Implements:
            CORE-189: Core spec 0.4, §3.3 Scheduler.
        """
        self._tz = timezone
        self._replan_wall()

    def set_suspended(self, suspended: bool) -> None:
        """Pause or resume jobs created with ``pause_on_suspend``.

        A paused job whose run was due runs once on resume.

        Args:
            suspended: Whether the instance is suspended.

        Implements:
            CORE-186: Core spec 0.4, §3.3 Scheduler.
        """
        self._suspended = suspended
        if not suspended:
            now_b, now_w = self.clock.boottime(), self.clock.wall()
            for job in self._jobs:
                if job.missed and not job.done:
                    job.missed = False
                    job.due = now_b
                    if job.kind != "interval":
                        job.target = now_w
        self.wake()

    def cancel_all(self) -> None:
        """Cancel every job (the instance stops; MOD-22)."""
        for job in self._jobs:
            job.done = True
            job.due = None
        self._jobs.clear()
        self.wake()

    async def run(self) -> None:
        """Drive the jobs until cancelled; runs inside the instance's task group."""
        last_b, last_w = self.clock.boottime(), self.clock.wall()
        while True:
            now_b, now_w = self.clock.boottime(), self.clock.wall()
            drift = (now_w - last_w).total_seconds() - (now_b - last_b)
            if abs(drift) > CLOCK_JUMP_THRESHOLD:
                self._log.info("clock_jump_replanned", drift_s=round(drift, 3))
                self._replan_wall(now_w, now_b)
            last_b, last_w = now_b, now_w
            self._jobs = [j for j in self._jobs if not j.done]
            for job in list(self._jobs):
                if job.running or job.due is None or job.due > now_b:
                    continue
                if job.pause_on_suspend and self._suspended:
                    job.missed, job.due = True, None
                    continue
                self._start(job, now_b, now_w)
            delays = [j.due - now_b for j in self._jobs if not j.running and j.due is not None]
            delay = min([self._max_tick, *delays])
            self._wake.clear()
            await self.clock.wait(self._wake, max(delay, 0.0))

    # --- internals ------------------------------------------------------------

    def _name(self, name: str | None, fn: JobFn) -> str:
        if name:
            return name
        self._counter += 1
        return f"{getattr(fn, '__name__', 'job')}#{self._counter}"

    def _jitter(self, job: ScheduledJob) -> float:
        return self._random.uniform(0.0, job.jitter) if job.jitter else 0.0

    def _add(self, job: ScheduledJob) -> ScheduledJob:
        self._jobs.append(job)
        self.wake()
        return job

    def _zone_of(self, job: ScheduledJob) -> tzinfo:
        return job.zone or self._tz

    def _plan_wall(self, job: ScheduledJob, now_w: datetime, now_b: float) -> None:
        if job.kind == "cron":
            assert job.spec is not None  # noqa: S101
            job.target = next_fire(job.spec, now_w, self._zone_of(job))
        else:
            assert job.when is not None  # noqa: S101
            when = job.when
            job.target = resolve_local(when, self._zone_of(job)) if when.tzinfo is None else when
        job.due = now_b + (job.target - now_w).total_seconds()

    def _replan_wall(self, now_w: datetime | None = None, now_b: float | None = None) -> None:
        now_w = now_w or self.clock.wall()
        now_b = self.clock.boottime() if now_b is None else now_b
        for job in self._jobs:
            if job.kind != "interval" and not job.done and not job.missed:
                self._plan_wall(job, now_w, now_b)
        self.wake()

    def _start(self, job: ScheduledJob, now_b: float, now_w: datetime) -> None:
        # Implements CORE-184 (Core spec 0.4, §3.3 Scheduler): no overlap; missed
        # runs collapse into one and the next is planned from this point.
        job.running = True
        if job.kind == "interval":
            job.due = now_b + job.interval + self._jitter(job)
        elif job.kind == "cron":
            self._plan_wall(job, now_w, now_b)
        else:
            job.done, job.due = True, None
        self._spawn(self._run(job), f"job:{job.name}")

    async def _run(self, job: ScheduledJob) -> None:
        try:
            await self._run_fn(job.fn, job.name)
        finally:
            job.running = False
            self.wake()
