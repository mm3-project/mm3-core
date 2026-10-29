"""Milestone 3, part C: pages and visibility (core spec 6.9, CORE-233..241, CORE-260)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mm3.core.config import load_config
from mm3.core.config.validate import ConfigResult
from mm3.core.engine import Engine
from mm3.core.pages import PageService
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.core.supervisor import State
from mm3.messages import Notification
from mm3.module import MirrorModule
from mm3.testing import VirtualClock


class Panel(MirrorModule):
    name = "panel"
    element = "mm3-panel"


class Boss(MirrorModule):
    name = "boss"
    capabilities = frozenset({"layout.control"})


CONFIG = """
pages:
  default: a
  rotate: 30
  pause_on_interaction: 60
  list:
    - id: a
      title: {en: Alpha, it: Alfa}
    - id: b
    - id: c
      rotate: 5
modules:
  - module: panel
    id: one
    position: top_left
    pages: [a]
  - module: panel
    id: two
    position: top_right
    pages: [b, c]
  - module: panel
    id: always
    position: bottom_bar
  - module: boss
    id: boss
"""


class Shells:
    def __init__(self) -> None:
        self.pages: list[tuple[str, int, int, str]] = []
        self.visibility: list[tuple[str, bool, int]] = []

    def page_changed(self, page: str, index: int, count: int, transition: str) -> None:
        self.pages.append((page, index, count, transition))

    def visibility_changed(self, instance_id: str, hidden: bool, fade: int) -> None:
        self.visibility.append((instance_id, hidden, fade))


class Recorder:
    def __init__(self) -> None:
        self.seen: list[Notification] = []

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        self.seen.append(n)

    def pages(self) -> list[dict[str, Any]]:
        return [dict(n.payload) for n in self.seen if n.name == "CORE_PAGE_CHANGED"]


class World:
    def __init__(
        self,
        engine: Engine,
        pages: PageService,
        clock: VirtualClock,
        shells: Shells,
        rec: Recorder,
        result: ConfigResult,
    ) -> None:
        self.engine = engine
        self.pages = pages
        self.clock = clock
        self.shells = shells
        self.rec = rec
        self.result = result

    def state(self, iid: str) -> State:
        return self.engine.supervisor.get(iid).state

    def ctx(self, iid: str) -> Any:
        ctx = self.engine.supervisor.get(iid).context
        assert ctx is not None
        return ctx

    async def settle(self) -> None:
        for _ in range(5):
            await self.pages.idle()
            await asyncio.sleep(0)


def load(tmp_path: Path, text: str, registry: Registry) -> ConfigResult:
    (tmp_path / "config.yaml").write_text(text)
    return load_config(tmp_path / "config.yaml", registry=registry, env_file=tmp_path / "x.env")


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    registry = Registry()
    registry.add(Panel)
    registry.add(Boss)
    result = load(tmp_path, CONFIG, registry)
    assert result.ok, result.records
    clock = VirtualClock()
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    )
    engine = Engine(settings, registry=registry, clock=clock, state_dir=tmp_path / "state")
    pages = PageService(result, engine.supervisor, clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    shells = Shells()
    pages.shells = shells
    rec = Recorder()
    engine.bus.add_endpoint(rec)
    await engine.start(result.instance_specs())
    await pages.start()
    w = World(engine, pages, clock, shells, rec, result)
    await w.settle()
    try:
        yield w
    finally:
        await pages.close()
        await engine.stop()


# --- CORE-233: the current page --------------------------------------------------------


async def test_core_233_start_page_and_visibility(world: World) -> None:
    assert world.pages.page == "a"
    assert world.pages.visible_ids() == ["one", "always", "boss"]
    assert world.state("two") is State.SUSPENDED
    assert world.state("one") is State.RUNNING
    assert world.shells.pages[-1] == ("a", 0, 3, "fade")
    first = world.rec.pages()[-1]
    assert (first["page"], first["reason"], first["count"]) == ("a", "start", 3)
    assert first["pages"][0] == {"id": "a", "title": "Alpha"}


async def test_core_233_page_commands_on_the_bus(world: World) -> None:
    sup = world.engine.supervisor
    sup.emit("PAGE_NEXT", {})
    await world.settle()
    assert world.pages.page == "b"
    assert world.state("two") is State.RUNNING
    assert world.state("one") is State.SUSPENDED
    sup.emit("PAGE_PREV", {})
    sup.emit("PAGE_PREV", {})
    await world.settle()
    assert world.pages.page == "c"
    sup.emit("PAGE_SET", {"page": "a"})
    sup.emit("PAGE_SET", {"page": "nope"})
    await world.settle()
    assert world.pages.page == "a"
    assert world.rec.pages()[-1]["reason"] == "notification"


async def test_core_233_reload_keeps_or_resets_the_page(world: World, tmp_path: Path) -> None:
    world.pages.set_page("c", "ha")
    await world.settle()
    registry = world.engine.registry
    same = load(tmp_path, CONFIG, registry)
    world.pages.reload(same)
    await world.settle()
    assert world.pages.page == "c"
    text = CONFIG.replace("    - id: c\n      rotate: 5\n", "").replace("[b, c]", "[b]")
    fewer = load(tmp_path, text, registry)
    assert fewer.ok, fewer.records
    world.pages.reload(fewer)
    await world.settle()
    assert world.pages.page == "a"
    assert world.rec.pages()[-1]["reason"] == "reload"


# --- CORE-234: rotation -------------------------------------------------------------------


async def test_core_234_rotation_and_page_override(world: World) -> None:
    await world.clock.advance(31)
    await world.settle()
    assert world.pages.page == "b"
    await world.clock.advance(31)
    await world.settle()
    assert world.pages.page == "c"
    await world.clock.advance(6)  # page c rotates after 5 s
    await world.settle()
    assert world.pages.page == "a"
    assert world.rec.pages()[-1]["reason"] == "rotate"


async def test_core_234_interaction_pauses_rotation(world: World) -> None:
    world.pages.touch("tap")
    await world.settle()
    await world.clock.advance(31)
    await world.settle()
    assert world.pages.page == "a"
    await world.clock.advance(31)
    await world.settle()
    assert world.pages.page == "b"


# --- CORE-235, CORE-260: touch --------------------------------------------------------------


async def test_core_260_swipes_change_page(world: World) -> None:
    world.pages.touch("swipe_left")
    await world.settle()
    assert world.pages.page == "b"
    world.pages.touch("swipe_right")
    world.pages.touch("swipe_right")
    await world.settle()
    assert world.pages.page == "c"
    assert world.rec.pages()[-1]["reason"] == "touch"


# --- CORE-238, CORE-239: hide reasons ---------------------------------------------------------


async def test_core_238_reasons_combine(world: World) -> None:
    world.pages.set_self("always", True, 0)
    world.pages.set_manual("always", True)
    await world.settle()
    assert world.pages.reasons("always") == {"self", "manual"}
    assert world.state("always") is State.SUSPENDED
    assert world.shells.visibility == [("always", True, 0)]
    world.pages.set_self("always", False)
    await world.settle()
    assert not world.pages.visible("always")
    world.pages.set_manual("always", False)
    await world.settle()
    assert world.pages.visible("always")
    assert world.state("always") is State.RUNNING
    assert world.shells.visibility[-1] == ("always", False, 300)


async def test_core_238_unknown_instance_is_ignored(world: World) -> None:
    world.pages.set_manual("ghost", True)
    await world.settle()
    assert world.shells.visibility == []


# --- CORE-241: ctx.layout -------------------------------------------------------------------


async def test_core_241_module_reason_cleared_when_module_stops(world: World) -> None:
    ctx = world.ctx("boss")
    ctx.layout.hide("always")
    await world.settle()
    assert world.pages.reasons("always") == {"module:boss"}
    assert ctx.page == "a"
    assert [p.id for p in ctx.pages] == ["a", "b", "c"]
    await world.engine.supervisor.get("boss").stop()
    await world.settle()
    assert world.pages.visible("always")


async def test_core_241_layout_needs_the_capability(world: World) -> None:
    ctx = world.ctx("always")
    with pytest.raises(PermissionError):
        _ = ctx.layout


async def test_core_233_resume_resets_to_default(world: World) -> None:
    world.pages.set_page("b", "ha")
    await world.settle()
    world.engine.supervisor.emit("CORE_RESUME", {})
    await world.settle()
    assert world.pages.page == "a"
    assert world.rec.pages()[-1]["reason"] == "resume"
