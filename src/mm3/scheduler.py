"""Scheduler interface seen by modules as ``ctx.scheduler``.

Jobs are in memory, owned by the instance that created them and cancelled
when it stops. Semantics are in core spec section 3.3.

Implements:
    CORE-180, CORE-187: Core spec 0.4, §3.3 Scheduler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

__all__ = ["Job", "JobFn", "Scheduler"]

JobFn = Callable[[], Awaitable[None]]
"""An async function without arguments, run once per job run."""


class Job(Protocol):
    """A scheduled job.

    Attributes:
        name: Job name, used in logs.
        next_run: Next planned run (aware, UTC); ``None`` when finished or cancelled.
    """

    @property
    def name(self) -> str:
        """Job name."""
        ...

    @property
    def next_run(self) -> datetime | None:
        """Next planned run, aware UTC; ``None`` when finished or cancelled."""
        ...

    def cancel(self) -> None:
        """Cancel future runs; a run in progress finishes."""
        ...


class Scheduler(Protocol):
    """Interval, cron and one-shot jobs for one module instance.

    Implements:
        CORE-180: Core spec 0.4, §3.3 Scheduler.
    """

    def every(
        self,
        interval: timedelta | float,
        fn: JobFn,
        *,
        start_now: bool = False,
        jitter: float = 0.0,
        pause_on_suspend: bool = False,
        name: str | None = None,
    ) -> Job:
        """Run ``fn`` every ``interval`` (seconds or timedelta)."""
        ...

    def cron(
        self,
        expr: str,
        fn: JobFn,
        *,
        timezone: str | None = None,
        pause_on_suspend: bool = False,
        name: str | None = None,
    ) -> Job:
        """Run ``fn`` at the wall-clock times of a cron expression.

        Raises:
            ValueError: The expression is invalid or can never match.
        """
        ...

    def at(
        self,
        when: datetime,
        fn: JobFn,
        *,
        timezone: str | None = None,
        name: str | None = None,
    ) -> Job:
        """Run ``fn`` once at ``when``; a naive time is read in ``timezone``."""
        ...
