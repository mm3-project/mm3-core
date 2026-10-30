"""Clocks used by the scheduler and the supervisor.

Intervals are measured on CLOCK_BOOTTIME, a monotonic clock that keeps
counting during host suspend; wall-clock time is UTC. The scheduler, the hook,
handler, job and request budgets and ``ctx.now()`` all read one engine clock;
tests replace it with ``mm3.testing.VirtualClock``.

Implements:
    CORE-183: Core spec 0.4, §3.3 Scheduler.
    CORE-278: Core spec 0.17, §3.3 Scheduler.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self

__all__ = ["Budget", "Clock", "ClockTimeout", "SystemClock"]


class Budget(Protocol):
    """An async context manager that cancels its body when the time is up.

    Like ``asyncio.timeout``: a body that runs too long raises ``TimeoutError``
    and ``expired()`` then returns True.
    """

    def expired(self) -> bool:
        """True when the budget ran out."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


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

    def timeout(self, seconds: float) -> Budget:
        """A budget of ``seconds`` measured on this clock (CORE-278)."""
        ...


class ClockTimeout:
    """A budget measured with ``clock.sleep``, for clocks other than the loop's.

    Args:
        clock: Clock whose ``sleep`` measures the budget.
        seconds: Length of the budget.

    Implements:
        CORE-278: Core spec 0.17, §3.3 Scheduler.
    """

    def __init__(self, clock: Clock, seconds: float) -> None:
        self._clock = clock
        self._seconds = max(seconds, 0.0)
        self._inner = asyncio.timeout(None)
        self._watcher: asyncio.Task[None] | None = None

    def expired(self) -> bool:
        """True when the budget ran out."""
        return self._inner.expired()

    async def _expire(self) -> None:
        await self._clock.sleep(self._seconds)
        self._inner.reschedule(asyncio.get_running_loop().time())

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self._watcher = asyncio.ensure_future(self._expire())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        if self._watcher is not None:
            self._watcher.cancel()
        await self._inner.__aexit__(exc_type, exc, tb)
        return None


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

    def timeout(self, seconds: float) -> Budget:
        """A budget on the event loop's clock, the same one ``sleep`` uses."""
        return asyncio.timeout(max(seconds, 0.0))
