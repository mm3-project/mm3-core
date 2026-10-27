"""Clocks used by the scheduler and the supervisor.

Intervals are measured on CLOCK_BOOTTIME, a monotonic clock that keeps
counting during host suspend; wall-clock time is UTC. Tests replace the
system clock with ``mm3.testing.VirtualClock``.

Implements:
    CORE-183: Core spec 0.4, §3.3 Scheduler.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

__all__ = ["Clock", "SystemClock"]


class Clock(Protocol):
    """Time source for the engine."""

    def wall(self) -> datetime:
        """Current wall-clock time, aware UTC."""
        ...

    def boottime(self) -> float:
        """Seconds on a monotonic clock that counts during host suspend."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds``."""
        ...

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:  # noqa: ASYNC109
        """Wait until ``event`` is set or ``timeout`` seconds pass.

        Returns:
            True when the event was set.
        """
        ...


class SystemClock:
    """The real clocks of the host.

    Implements:
        CORE-183: Core spec 0.4, §3.3 Scheduler.
    """

    _BOOTTIME = getattr(time, "CLOCK_BOOTTIME", None)

    def wall(self) -> datetime:
        """Current wall-clock time, aware UTC."""
        return datetime.now(UTC)

    def boottime(self) -> float:
        """CLOCK_BOOTTIME seconds (monotonic clock where unavailable)."""
        if self._BOOTTIME is None:  # pragma: no cover - non-Linux hosts
            return time.monotonic()
        return time.clock_gettime(self._BOOTTIME)

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds``."""
        await asyncio.sleep(max(seconds, 0.0))

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:  # noqa: ASYNC109
        """Wait until ``event`` is set or ``timeout`` seconds pass."""
        try:
            async with asyncio.timeout(max(timeout, 0.0)):
                await event.wait()
        except TimeoutError:
            return False
        return True
