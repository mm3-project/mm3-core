"""Virtual time on the running event loop, for the module harness.

The harness runs the module on the test's own event loop. :class:`LoopClock`
makes that loop's clock virtual while a harness is open: ``loop.time()``
returns the virtual monotonic time, so ``asyncio.sleep``, ``asyncio.timeout``,
the engine scheduler, the hook and handler budgets and ``ctx.now()`` all move
together, and only when the test advances them (core CORE-278, Module
Contract MOD-28). Work handed to the thread pool runs in real time; the
clock waits for it before moving on, so ``ctx.run_blocking`` and storage
behave as on a mirror.

Implements:
    CORE-278: Core spec 0.17, §3.3 Scheduler.
    -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from mm3.core.clock import Budget

__all__ = ["CountingExecutor", "DriveError", "LoopClock"]

T = TypeVar("T")

_SELECT_CAP = 0.05
"""Longest real wait of the patched loop; virtual timers never make it block long."""


class DriveError(RuntimeError):
    """An awaited operation can never finish: nothing is scheduled and nothing runs."""


class CountingExecutor(concurrent.futures.ThreadPoolExecutor):
    """A thread pool that knows which of its jobs are still running."""

    def __init__(self, max_workers: int | None = None) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix="mm3-kit-blocking")
        self._pending: set[concurrent.futures.Future[Any]] = set()

    def submit(
        self, fn: Callable[..., T], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[T]:
        """Run ``fn`` in a thread and track it until it finishes."""
        future = super().submit(fn, *args, **kwargs)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)
        return future

    def pending(self) -> list[concurrent.futures.Future[Any]]:
        """Jobs not finished yet."""
        return [f for f in list(self._pending) if not f.done()]


class LoopClock:
    """The engine clock of a harness: virtual time on the running loop.

    One clock is shared by all harnesses open on the same loop, so
    ``check_contract()`` inside an open harness runs on the same time line.

    Args:
        start: Initial wall-clock time, aware.
    """

    _by_loop: dict[asyncio.AbstractEventLoop, LoopClock] = {}  # noqa: RUF012

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._wall = start.astimezone(UTC)
        self._now = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._users = 0
        self._select: Any = None
        self.executors: list[CountingExecutor] = []

    # --- attaching ------------------------------------------------------------

    @classmethod
    def acquire(cls, start: datetime) -> LoopClock:
        """Return the clock of the running loop, attaching a new one if needed.

        Args:
            start: Wall-clock time for a new clock; ignored when one is attached.

        Returns:
            The attached clock; call :meth:`release` when done.
        """
        loop = asyncio.get_running_loop()
        clock = cls._by_loop.get(loop)
        if clock is None:
            clock = cls(start)
            clock._attach(loop)
            cls._by_loop[loop] = clock
        clock._users += 1
        return clock

    def release(self) -> None:
        """Give the clock back; the last user restores the loop's real clock."""
        self._users -= 1
        if self._users <= 0 and self._loop is not None:
            self._by_loop.pop(self._loop, None)
            self._detach()

    def _attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._now = loop.time()
        loop.time = self.boottime  # type: ignore[method-assign]
        selector = getattr(loop, "_selector", None)
        if selector is not None:
            original = selector.select

            def select(timeout: float | None = None) -> Any:
                capped = _SELECT_CAP if timeout is None else min(timeout, _SELECT_CAP)
                return original(capped)

            self._select = (selector, original)
            selector.select = select

    def _detach(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if "time" in vars(loop):
            del loop.time
        if self._select is not None:
            selector, original = self._select
            selector.select = original
            self._select = None
        self._loop = None

    # --- Clock protocol -------------------------------------------------------

    def wall(self) -> datetime:
        """Current virtual wall-clock time, aware UTC."""
        return self._wall

    def boottime(self) -> float:
        """Current virtual monotonic time; also the loop's ``time()``."""
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Sleep on the loop, in virtual time."""
        await asyncio.sleep(max(seconds, 0.0))

    async def wait(self, event: asyncio.Event, timeout: float) -> bool:  # noqa: ASYNC109
        """Wait until ``event`` is set or ``timeout`` virtual seconds pass."""
        try:
            async with asyncio.timeout(max(timeout, 0.0)):
                await event.wait()
        except TimeoutError:
            return False
        return True

    def timeout(self, seconds: float) -> Budget:
        """A budget in virtual seconds (the loop's own timeout)."""
        return asyncio.timeout(max(seconds, 0.0))

    # --- driving --------------------------------------------------------------

    def _ready(self) -> int:
        return len(getattr(self._loop, "_ready", ()))

    def next_timer(self) -> float | None:
        """Virtual time of the next pending timer on the loop, if any."""
        scheduled = getattr(self._loop, "_scheduled", ())
        times = [h.when() for h in scheduled if not h.cancelled()]
        return min(times) if times else None

    async def settle(self, rounds: int = 2000) -> None:
        """Run everything that is ready now, including thread-pool jobs."""
        quiet = 0
        for _ in range(rounds):
            await asyncio.sleep(0)
            pending = [f for ex in self.executors for f in ex.pending()]
            if pending:
                await asyncio.wait([asyncio.wrap_future(f) for f in pending])
                quiet = 0
                continue
            quiet = quiet + 1 if self._ready() == 0 else 0
            if quiet >= 3:
                return

    def _move(self, seconds: float) -> None:
        if seconds > 0:
            self._now += seconds
            self._wall += timedelta(seconds=seconds)

    async def advance(self, seconds: float | timedelta) -> None:
        """Move time forward, running every timer that falls due, in order.

        Args:
            seconds: How far to move.
        """
        step = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
        if step < 0:
            raise ValueError("advance() moves forward; use jump() for a clock step back")
        target = self._now + step
        await self.settle()
        while (due := self.next_timer()) is not None and due <= target:
            self._move(due - self._now)
            await self.settle()
        self._move(target - self._now)
        await self.settle()

    async def jump(self, seconds: float | timedelta) -> None:
        """Move only the wall clock, as an NTP step would.

        Args:
            seconds: Signed jump.
        """
        step = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        self._wall += step
        await self.settle()

    async def drive(self, aw: Awaitable[T], *, limit: float = 3600.0) -> T:
        """Await ``aw``, moving virtual time to each next timer until it is done.

        Harness operations use it so a hook that sleeps or a request that
        waits for its timeout completes without the test advancing by hand.

        Args:
            aw: What to await.
            limit: Most virtual seconds to move.

        Returns:
            Its result.

        Raises:
            DriveError: Nothing is scheduled and the awaitable is not done,
                or the limit was reached.
        """
        task = asyncio.ensure_future(aw)
        end = self._now + limit
        while True:
            await self.settle()
            if task.done():
                return task.result()
            due = self.next_timer()
            if due is None or due > end:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                why = "nothing scheduled" if due is None else f"more than {limit:g} s"
                raise DriveError(f"operation does not finish ({why})")
            self._move(due - self._now)
