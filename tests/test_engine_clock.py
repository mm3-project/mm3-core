"""One engine clock for ctx.now(), the scheduler and the budgets (CORE-278)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import EngineFactory, advancing
from mm3 import MirrorModule
from mm3.core.clock import ClockTimeout, SystemClock
from mm3.core.supervisor import InstanceSpec, State
from mm3.errors import ContextClosedError
from mm3.testing import VirtualClock


class Probe(MirrorModule):
    name = "probe"


def obj(engine: object, iid: str) -> MirrorModule:
    inst = engine.supervisor.get(iid)  # type: ignore[attr-defined]
    assert inst.obj is not None
    return inst.obj  # type: ignore[no-any-return]


async def test_core_278_now_is_aware_in_engine_timezone(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe, timezone="Europe/Rome")
    await engine.start([InstanceSpec("p", "probe")])
    ctx = obj(engine, "p").ctx
    now = ctx.now()
    assert now.tzinfo == ZoneInfo("Europe/Rome")
    assert now == clock.wall()
    assert now.utcoffset() == timedelta(hours=1)


async def test_core_278_now_follows_the_virtual_clock(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p", "probe")])
    ctx = obj(engine, "p").ctx
    before = ctx.now()
    await clock.advance(90)
    assert ctx.now() - before == timedelta(seconds=90)
    await clock.jump(-30)
    assert ctx.now() - before == timedelta(seconds=60)


async def test_mod_28_scheduler_and_now_share_the_clock(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    seen: list[datetime] = []

    class Ticker(MirrorModule):
        name = "ticker"

        async def start(self) -> None:
            self.ctx.scheduler.every(60, self.tick, name="tick")

        async def tick(self) -> None:
            seen.append(self.ctx.now())

    engine = make_engine(Ticker)
    await engine.start([InstanceSpec("t", "ticker")])
    start = clock.wall()
    await clock.advance(180)
    assert [s.astimezone(UTC) - start for s in seen] == [
        timedelta(seconds=60),
        timedelta(seconds=120),
        timedelta(seconds=180),
    ]


async def test_core_278_budget_runs_on_the_engine_clock(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    class Hang(MirrorModule):
        name = "hang"

        async def start(self) -> None:
            await asyncio.Event().wait()

    engine = make_engine(Hang)  # start budget 0.5 s
    starting = asyncio.ensure_future(engine.start([InstanceSpec("h", "hang")]))
    await clock.advance(0.4)
    assert engine.supervisor.get("h").state is not State.FAILED
    await advancing(clock, starting, 0.1)
    await clock.settle()
    assert engine.supervisor.get("h").reason == "start_timeout"


async def test_core_278_now_closed_after_stop(make_engine: EngineFactory) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p", "probe")])
    ctx = obj(engine, "p").ctx
    await engine.stop()
    with pytest.raises(ContextClosedError):
        ctx.now()


async def test_core_278_clock_timeout_expires_and_cancels(clock: VirtualClock) -> None:
    budget = clock.timeout(2)

    async def body() -> None:
        async with budget:
            await asyncio.Event().wait()

    task = asyncio.ensure_future(body())
    await clock.advance(1.9)
    assert not task.done()
    await clock.advance(0.1)
    with pytest.raises(TimeoutError):
        await task
    assert budget.expired()


async def test_core_278_clock_timeout_not_expired_when_body_finishes(
    clock: VirtualClock,
) -> None:
    budget = ClockTimeout(clock, 5)
    async with budget:
        await clock.settle()
    assert not budget.expired()
    await clock.advance(10)
    assert not budget.expired()


async def test_core_278_system_clock_timeout() -> None:
    budget = SystemClock().timeout(0.01)
    with pytest.raises(TimeoutError):
        async with budget:
            await asyncio.sleep(1)
    assert budget.expired()
