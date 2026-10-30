"""Virtual clock: tests advance time instead of waiting.

``advance()`` moves both clocks and runs everything that becomes due, in
order; ``jump()`` moves only the wall clock, as an NTP correction would;
``suspend_host()`` models a host suspend: both clocks move, and nothing on
the event loop runs until the end.

``timeout()`` gives budgets measured on the same virtual time, so the
scheduler, hook and job budgets, request timeouts and ``ctx.now()`` move
together.

Implements:
    -: Module Contract 1.0 rev 8, §12.2 mm3.testing kit.
    CORE-183: Core spec 0.4, §3.3 Scheduler.
    CORE-278: Core spec 0.17, §3.3 Scheduler.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from mm3.core.clock import Budget, ClockTimeout

__all__ = ["VirtualClock"]


class VirtualClock:
    """A clock driven by the test.

    Args:
        start: Initial wall-clock time (aware); default 2026-01-01 00:00 UTC.
        settle_rounds: Event-loop turns given to woken tasks after each step.
    """

    def __init__(self, start: datetime | None = None, *, settle_rounds: int = 50) -> None:
        self._wall = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._wall.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._boot = 0.0
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self._settle_rounds = settle_rounds

    def wall(self) -> datetime:
        """Current virtual wall-clock time, aware UTC."""
        return self._wall.astimezone(UTC)

    def boottime(self) -> float:
        """Current virtual CLOCK_BOOTTIME seconds."""
        return self._boot

    def _timer(self, seconds: float) -> asyncio.Future[None]:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self._boot + max(seconds, 0.0), next(self._seq), fut))
        return fut

    async def sleep(self, seconds: float) -> None:
        """Wait until the virtual clock has advanced by ``seconds``."""
        await self._timer(seconds)

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:  # noqa: ASYNC109
        """Wait until ``event`` is set or ``timeout`` virtual seconds pass."""
        if event.is_set():
            return True
        timer = self._timer(timeout)
        waiter: asyncio.Future[Any] = asyncio.ensure_future(event.wait())
        pending: list[asyncio.Future[Any]] = [timer, waiter]
        try:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            if not timer.done():
                timer.cancel()
        return event.is_set()

    def timeout(self, seconds: float) -> Budget:
        """A budget of ``seconds`` virtual seconds (CORE-278)."""
        return ClockTimeout(self, seconds)

    async def settle(self) -> None:
        """Give woken tasks a chance to run."""
        for _ in range(self._settle_rounds):
            await asyncio.sleep(0)

    def _fire_due(self) -> None:
        while self._timers and self._timers[0][0] <= self._boot:
            _, _, fut = heapq.heappop(self._timers)
            if not fut.done():
                fut.set_result(None)

    async def advance(self, seconds: float | timedelta) -> None:
        """Move both clocks forward, running everything that falls due.

        Args:
            seconds: How far to move.
        """
        step = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
        target = self._boot + step
        await self.settle()
        while self._timers and self._timers[0][0] <= target:
            deadline = self._timers[0][0]
            if deadline > self._boot:
                self._wall += timedelta(seconds=deadline - self._boot)
                self._boot = deadline
            self._fire_due()
            await self.settle()
        self._wall += timedelta(seconds=target - self._boot)
        self._boot = target
        await self.settle()

    async def jump(self, seconds: float | timedelta) -> None:
        """Move only the wall clock (an NTP step), then wake every waiter.

        Args:
            seconds: Signed jump.
        """
        step = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        self._wall += step
        for _, _, fut in self._timers:
            if not fut.done():
                fut.set_result(None)
        self._timers.clear()
        await self.settle()

    async def suspend_host(self, seconds: float | timedelta) -> None:
        """Model a host suspend: both clocks move at once, then waiters wake.

        Args:
            seconds: Duration of the suspend.
        """
        step = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
        self._boot += step
        self._wall += timedelta(seconds=step)
        self._fire_due()
        await self.settle()
