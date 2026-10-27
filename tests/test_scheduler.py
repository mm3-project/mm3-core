"""In-house scheduler on a virtual clock (CORE-180..189)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mm3.core.logging import get_logger
from mm3.core.services.scheduler import InstanceScheduler
from mm3.scheduler import JobFn
from mm3.testing import VirtualClock

ROME = ZoneInfo("Europe/Rome")


class Harness:
    def __init__(self, clock: VirtualClock, tz: Any = UTC) -> None:
        self.clock = clock
        self.tasks: list[asyncio.Task[Any]] = []
        self.sched = InstanceScheduler(clock, tz, self._run, self._spawn, get_logger("test", "t1"))

    async def _run(self, fn: JobFn, name: str) -> None:
        await fn()

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task


@pytest.fixture
async def h(clock: VirtualClock) -> AsyncIterator[Harness]:
    harness = Harness(clock)
    driver = asyncio.create_task(harness.sched.run())
    await clock.settle()
    yield harness
    driver.cancel()
    for t in harness.tasks:
        t.cancel()
    await asyncio.gather(driver, *harness.tasks, return_exceptions=True)


def recorder(clock: VirtualClock, out: list[datetime]) -> JobFn:
    async def job() -> None:
        out.append(clock.wall())

    return job


async def test_core_180_interval_runs_every_interval(h: Harness) -> None:
    runs: list[datetime] = []
    h.sched.every(10, recorder(h.clock, runs))
    await h.clock.advance(35)
    assert len(runs) == 3
    assert [(b - a).total_seconds() for a, b in itertools.pairwise(runs)] == [10, 10]


async def test_core_180_start_now_and_timedelta(h: Harness) -> None:
    runs: list[datetime] = []
    h.sched.every(timedelta(minutes=1), recorder(h.clock, runs), start_now=True)
    await h.clock.advance(0.5)
    assert len(runs) == 1
    await h.clock.advance(60)
    assert len(runs) == 2


async def test_core_180_bad_arguments(h: Harness) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        h.sched.every(0, recorder(h.clock, []))
    with pytest.raises(ValueError, match="jitter"):
        h.sched.every(1, recorder(h.clock, []), jitter=-1)
    with pytest.raises(ValueError, match="5 fields"):
        h.sched.cron("bad", recorder(h.clock, []))
    with pytest.raises(ValueError, match="time zone"):
        h.sched.cron("@daily", recorder(h.clock, []), timezone="Mars/Olympus")


async def test_core_184_no_overlap_and_missed_runs_collapse(h: Harness) -> None:
    starts: list[float] = []
    release = asyncio.Event()

    async def slow() -> None:
        starts.append(h.clock.boottime())
        await release.wait()

    h.sched.every(10, slow)
    await h.clock.advance(55)  # due at 10, still running through 20..50
    assert starts == [10.0]
    release.set()
    await h.clock.settle()
    await h.clock.advance(0.5)  # missed runs collapse into one, as soon as possible
    assert len(starts) == 2
    second = starts[1]
    release.clear()
    release.set()
    await h.clock.advance(10)  # next one planned from that point
    assert starts[2] == pytest.approx(second + 10, abs=1.0)


async def test_core_183_interval_overdue_after_host_suspend_runs_once(h: Harness) -> None:
    runs: list[datetime] = []
    h.sched.every(60, recorder(h.clock, runs))
    await h.clock.advance(30)
    await h.clock.suspend_host(3600)  # CLOCK_BOOTTIME kept counting
    await h.clock.advance(0.5)
    assert len(runs) == 1
    await h.clock.advance(60)
    assert len(runs) == 2


async def test_core_186_jitter_delays_every_run_within_bound(h: Harness) -> None:
    runs: list[float] = []

    async def job() -> None:
        runs.append(h.clock.boottime())

    h.sched.every(10, job, jitter=3)
    await h.clock.advance(100)
    assert 7 <= len(runs) <= 10
    assert 10 <= runs[0] <= 13
    for a, b in itertools.pairwise(runs):
        assert 10 <= b - a <= 13 + 1


async def test_core_181_cron_runs_at_wall_clock_times(h: Harness) -> None:
    runs: list[datetime] = []
    job = h.sched.cron("*/30 * * * *", recorder(h.clock, runs), name="half-hourly")
    assert job.name == "half-hourly"
    assert job.next_run == datetime(2026, 10, 27, 12, 30, tzinfo=UTC)
    await h.clock.advance(3600)
    assert runs == [
        datetime(2026, 10, 27, 12, 30, tzinfo=UTC),
        datetime(2026, 10, 27, 13, 0, tzinfo=UTC),
    ]


async def test_core_182_cron_across_spring_forward_in_scheduler() -> None:
    clock = VirtualClock(datetime(2026, 3, 29, 0, 0, tzinfo=UTC))  # 01:00 CET
    harness = Harness(clock, ROME)
    driver = asyncio.create_task(harness.sched.run())
    runs: list[datetime] = []
    harness.sched.cron("30 2 * * *", recorder(clock, runs))
    await clock.advance(2 * 3600)
    driver.cancel()
    await asyncio.gather(driver, *harness.tasks, return_exceptions=True)
    assert [r.astimezone(ROME).strftime("%H:%M %Z") for r in runs] == ["03:00 CEST"]


async def test_core_183_clock_jump_replans_wall_clock_jobs(h: Harness) -> None:
    runs: list[datetime] = []
    h.sched.cron("0 13 * * *", recorder(h.clock, runs))
    # NTP corrects the clock by +50 min: 12:00 -> 12:50.
    await h.clock.jump(timedelta(minutes=50))
    await h.clock.advance(11 * 60)
    assert runs == [datetime(2026, 10, 27, 13, 0, tzinfo=UTC)]


async def test_core_183_backward_jump_does_not_run_early(h: Harness) -> None:
    runs: list[datetime] = []
    h.sched.cron("0 13 * * *", recorder(h.clock, runs))
    await h.clock.jump(timedelta(minutes=-30))  # 11:30
    await h.clock.advance(60 * 60)  # 12:30
    assert runs == []
    await h.clock.advance(31 * 60)
    assert runs == [datetime(2026, 10, 27, 13, 0, tzinfo=UTC)]


async def test_core_183_boot_without_rtc_does_not_fire_everything(clock: VirtualClock) -> None:
    early = VirtualClock(datetime(1970, 1, 1, tzinfo=UTC))
    harness = Harness(early)
    driver = asyncio.create_task(harness.sched.run())
    runs: list[datetime] = []
    harness.sched.cron("0 9 * * *", recorder(early, runs))
    await early.settle()
    await early.jump(datetime(2026, 10, 27, 12, tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC))
    await early.advance(60)
    driver.cancel()
    await asyncio.gather(driver, *harness.tasks, return_exceptions=True)
    assert runs == []


async def test_core_187_at_naive_in_job_timezone_and_aware(h: Harness) -> None:
    runs: list[datetime] = []
    job = h.sched.at(datetime(2026, 10, 27, 14, 0), recorder(h.clock, runs), timezone="Europe/Rome")
    assert job.next_run == datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    h.sched.at(datetime(2026, 10, 27, 12, 30, tzinfo=UTC), recorder(h.clock, runs))
    await h.clock.advance(2 * 3600)
    assert runs == [
        datetime(2026, 10, 27, 12, 30, tzinfo=UTC),
        datetime(2026, 10, 27, 13, 0, tzinfo=UTC),
    ]
    assert job.next_run is None


async def test_core_187_at_in_the_past_runs_once_asap(h: Harness) -> None:
    runs: list[datetime] = []
    h.sched.at(datetime(2020, 1, 1, tzinfo=UTC), recorder(h.clock, runs))
    await h.clock.advance(0.1)
    await h.clock.advance(3600)
    assert len(runs) == 1


async def test_core_189_timezone_change_replans(h: Harness) -> None:
    runs: list[datetime] = []
    job = h.sched.cron("0 14 * * *", recorder(h.clock, runs))
    assert job.next_run == datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
    h.sched.set_timezone(ROME)
    assert job.next_run == datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    await h.clock.advance(3600 + 1)
    assert runs == [datetime(2026, 10, 27, 13, 0, tzinfo=UTC)]


async def test_core_186_pause_on_suspend_runs_once_on_resume(h: Harness) -> None:
    paused: list[datetime] = []
    free: list[datetime] = []
    h.sched.every(10, recorder(h.clock, paused), pause_on_suspend=True)
    h.sched.every(10, recorder(h.clock, free))
    h.sched.set_suspended(True)
    await h.clock.advance(55)
    assert paused == []
    assert len(free) == 5
    h.sched.set_suspended(False)
    await h.clock.advance(0.5)
    assert len(paused) == 1


async def test_core_186_cancel_and_cancel_all(h: Harness) -> None:
    runs: list[datetime] = []
    job = h.sched.every(10, recorder(h.clock, runs))
    job.cancel()
    assert job.next_run is None
    other = h.sched.cron("* * * * *", recorder(h.clock, runs))
    h.sched.cancel_all()
    await h.clock.advance(120)
    assert runs == []
    assert other.next_run is None


async def test_core_180_interval_next_run(h: Harness) -> None:
    job = h.sched.every(30, recorder(h.clock, []))
    assert job.next_run == datetime(2026, 10, 27, 12, 0, 30, tzinfo=UTC)
