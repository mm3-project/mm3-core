"""Shared fixtures: an engine on a virtual clock and recording endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest

from mm3.core.engine import Engine
from mm3.core.logging import configure_logging
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.messages import Notification
from mm3.testing import VirtualClock


class Recorder:
    """Bus endpoint and client channel that records what it receives."""

    def __init__(self) -> None:
        self.notifications: list[tuple[Notification, str | None]] = []
        self.client: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.state: list[tuple[str, dict[str, Any], bool]] = []

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        self.notifications.append((n, exclude_instance))

    def send_client(
        self, instance_id: str, type: str, payload: dict[str, Any], client: str | None
    ) -> None:
        self.client.append((instance_id, type, payload, client))

    def send_state(self, instance_id: str, state: dict[str, Any], delta: bool) -> None:
        self.state.append((instance_id, state, delta))

    def names(self) -> list[str]:
        return [n.name for n, _ in self.notifications]


@pytest.fixture(autouse=True)
def _logging(tmp_path: Path) -> Iterator[None]:
    with (tmp_path / "log.jsonl").open("w") as stream:
        configure_logging("debug", stream=stream, json=True)
        yield


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(datetime(2026, 10, 27, 12, 0, tzinfo=UTC))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


EngineFactory = Callable[..., Engine]


@pytest.fixture
async def make_engine(
    clock: VirtualClock, recorder: Recorder, tmp_path: Path
) -> AsyncIterator[EngineFactory]:
    engines: list[Engine] = []

    def factory(*classes: type, **settings: Any) -> Engine:
        registry = Registry()
        for cls in classes:
            registry.add(cls)
        timeouts = settings.pop(
            "timeouts",
            Timeouts(start=0.5, stop=0.5, suspend=0.5, reconfigure=0.5, handler=0.2, job=0.3),
        )
        engine = Engine(
            EngineSettings(timeouts=timeouts, **settings),
            registry=registry,
            clock=clock,
            clients=recorder,
            state_dir=tmp_path / "state",
        )
        engine.bus.add_endpoint(recorder)
        engines.append(engine)
        return engine

    yield factory
    for engine in engines:
        await engine.stop()


async def settle(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


T = TypeVar("T")


async def advancing(clock: VirtualClock, aw: Awaitable[T], seconds: float) -> T:
    """Await ``aw`` while the virtual clock moves ``seconds``.

    Budgets and request timeouts run on the engine clock (CORE-278), so a
    call that waits for one needs the clock to move while it is pending.
    """
    task = asyncio.ensure_future(aw)
    await clock.advance(seconds)
    return await task
