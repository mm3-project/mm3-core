"""Pages and visibility (core spec 6.9, CORE-233..241, CORE-260, CORE-333..337)."""

from __future__ import annotations

import asyncio
import contextlib
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


@contextlib.asynccontextmanager
async def started(tmp_path: Path, text: str) -> AsyncIterator[World]:
    registry = Registry()
    registry.add(Panel)
    registry.add(Boss)
    result = load(tmp_path, text, registry)
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


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    async with started(tmp_path, CONFIG) as w:
        yield w


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


async def test_core_343_page_and_visibility_changes_reach_the_listeners(world: World) -> None:
    calls: list[str] = []
    world.pages.listeners.append(lambda: calls.append(world.pages.page))
    world.engine.supervisor.emit("PAGE_NEXT", {})
    await world.settle()
    assert calls[-1] == "b"
    before = len(calls)
    world.pages.set_manual("one", True)
    await world.settle()
    assert len(calls) > before


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


# --- CORE-333..337: cycle, timed jump, idle return, pause holds ----------------------------


def cycle_config(*, rotate: int = 0, return_after: int = 0) -> str:
    return f"""
pages:
  default: home
  rotate: {rotate}
  return_after: {return_after}
  pause_on_interaction: 60
  list:
    - id: home
    - id: energy
    - id: guest
      cycle: false
      after: {{time: 5m}}
    - id: lounge
      cycle: false
    - id: tail
modules:
  - module: panel
    id: one
    position: top_left
    pages: [home]
  - module: boss
    id: boss
"""


async def go(w: World, seconds: float) -> None:
    await w.clock.advance(seconds)
    await w.settle()


async def goto(w: World, page: str) -> None:
    w.pages.set_page(page, "rest")
    await w.settle()


async def test_core_333_next_and_prev_skip_pages_out_of_the_cycle(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config()) as w:
        assert w.pages.cycle_ids == ["home", "energy", "tail"]
        seen = []
        for _ in range(4):
            w.pages.next("rest")
            await w.settle()
            seen.append(w.pages.page)
        assert seen == ["energy", "tail", "home", "energy"]
        seen = []
        for _ in range(3):
            w.pages.prev("rest")
            await w.settle()
            seen.append(w.pages.page)
        assert seen == ["home", "tail", "energy"]
        w.pages.touch("swipe_left")
        await w.settle()
        assert w.pages.page == "tail"


async def test_core_333_steps_from_a_page_out_of_the_cycle(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config()) as w:
        await goto(w, "lounge")
        w.pages.next("notification")
        await w.settle()
        assert w.pages.page == "tail"  # the cycle page after its place in the list
        await goto(w, "lounge")
        w.pages.prev("notification")
        await w.settle()
        assert w.pages.page == "energy"  # the cycle page before it
        await goto(w, "lounge")
        w.pages.touch("swipe_right")
        await w.settle()
        assert w.pages.page == "energy"
        # the last list entry wraps to the first
        await goto(w, "guest")
        w.engine.supervisor.emit("PAGE_NEXT", {})
        await w.settle()
        assert w.pages.page == "tail"


async def test_core_333_nothing_changes_without_pages_in_the_cycle(tmp_path: Path) -> None:
    text = "pages:\n  list:\n    - id: a\n      cycle: false\n    - id: b\n      cycle: false\n"
    async with started(tmp_path, text) as w:
        before = len(w.rec.pages())
        w.pages.next("rest")
        w.pages.prev("rest")
        w.pages.touch("swipe_left")
        await w.settle()
        assert w.pages.page == "a"
        assert len(w.rec.pages()) == before
        w.pages.set_page("b", "rest")  # PAGE_SET still works
        await w.settle()
        assert w.pages.page == "b"


async def test_core_333_rotation_skips_and_does_not_run_on_pages_out_of_the_cycle(
    tmp_path: Path,
) -> None:
    async with started(tmp_path, cycle_config(rotate=30)) as w:
        seen = []
        for _ in range(3):
            await go(w, 31)
            seen.append(w.pages.page)
        assert seen == ["energy", "tail", "home"]
        assert w.rec.pages()[-1]["reason"] == "rotate"
        await goto(w, "lounge")
        await go(w, 3600)
        assert w.pages.page == "lounge"


async def test_core_334_timed_jump_to_the_default_page(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30)) as w:
        await goto(w, "guest")
        await go(w, 299)
        assert w.pages.page == "guest"  # no rotation on a page with after
        await go(w, 2)
        assert w.pages.page == "home"
        last = w.rec.pages()[-1]
        assert (last["reason"], last["previous"]) == ("after", "guest")


async def test_core_334_jump_target_and_touch_restart(tmp_path: Path) -> None:
    text = cycle_config().replace("after: {time: 5m}", "after: {time: 5m, page: tail}")
    async with started(tmp_path, text) as w:
        await goto(w, "guest")
        await go(w, 240)
        w.pages.touch("tap")  # a touch restarts the count
        await w.settle()
        await go(w, 240)
        assert w.pages.page == "guest"
        await go(w, 61)
        assert w.pages.page == "tail"
        assert w.rec.pages()[-1]["reason"] == "after"
        # a tap on the kiosk does not pause anything it did not before
        await go(w, 3600)
        assert w.pages.page == "tail"


async def test_core_334_the_count_stops_while_suspended_and_starts_from_zero(
    tmp_path: Path,
) -> None:
    text = cycle_config().replace("rotate: 0", "rotate: 0\n  reset_on_resume: false")
    async with started(tmp_path, text) as w:
        sup = w.engine.supervisor
        await goto(w, "guest")
        await go(w, 200)
        await sup.set_suspended(True)
        await w.settle()
        await go(w, 3600)
        assert w.pages.page == "guest"
        await sup.set_suspended(False)
        await w.settle()
        await go(w, 299)  # the 200 s before the suspend are gone
        assert w.pages.page == "guest"
        await go(w, 2)
        assert (w.pages.page, w.rec.pages()[-1]["reason"]) == ("home", "after")


async def test_core_335_idle_return_to_default(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(return_after=600)) as w:
        await goto(w, "energy")
        await go(w, 599)
        assert w.pages.page == "energy"
        await go(w, 2)
        assert w.pages.page == "home"
        last = w.rec.pages()[-1]
        assert (last["reason"], last["previous"]) == ("idle", "energy")
        before = len(w.rec.pages())
        await go(w, 7200)  # nothing to return from on the default page
        assert len(w.rec.pages()) == before


async def test_core_335_interactions_restart_the_count(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(return_after=600)) as w:
        await goto(w, "energy")
        await go(w, 400)
        w.pages.touch("tap")
        await w.settle()
        await go(w, 400)
        assert w.pages.page == "energy"
        w.engine.supervisor.emit("PAGE_SET", {"page": "tail"})  # not an automatic change
        await w.settle()
        await go(w, 400)
        assert w.pages.page == "tail"
        await go(w, 201)
        assert w.pages.page == "home"


async def test_core_335_not_where_rotation_or_a_timed_jump_runs(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30, return_after=60)) as w:
        await go(w, 31)
        assert (w.pages.page, w.rec.pages()[-1]["reason"]) == ("energy", "rotate")
        await goto(w, "guest")  # the timed jump (5 min) rules, not the idle return (1 min)
        await go(w, 120)
        assert w.pages.page == "guest"
        await go(w, 181)
        assert w.pages.page == "home"
        assert w.rec.pages()[-1]["reason"] == "after"
        await goto(w, "lounge")  # no rotation here, so the idle return applies
        await go(w, 61)
        assert (w.pages.page, w.rec.pages()[-1]["reason"]) == ("home", "idle")


async def test_core_335_automatic_changes_are_not_interactions(tmp_path: Path) -> None:
    text = cycle_config(rotate=30, return_after=100).replace(
        "    - id: tail", "    - id: tail\n      rotate: 0"
    )
    async with started(tmp_path, text) as w:
        await goto(w, "energy")  # the interaction; rotation stays paused for 60 s
        await go(w, 61)
        assert (w.pages.page, w.rec.pages()[-1]["reason"]) == ("tail", "rotate")
        await go(w, 38)  # the rotation did not restart the count (100 s since the interaction)
        assert w.pages.page == "tail"
        await go(w, 2)
        assert (w.pages.page, w.rec.pages()[-1]["reason"]) == ("home", "idle")


async def test_core_336_pause_holds_stop_automatic_changes(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30, return_after=100)) as w:
        await w.ctx("boss").send_notification("PAGE_PAUSE", {})
        await w.settle()
        assert w.pages.paused == ["boss"]
        await go(w, 3600)
        assert w.pages.page == "home"  # rotation made no change
        w.pages.next("rest")  # every other change still works
        await w.settle()
        assert w.pages.page == "energy"
        await w.ctx("boss").send_notification("PAGE_RESUME", {})
        await w.settle()
        assert w.pages.paused == []
        # rotation counts again from zero; the interaction pause of CORE-234 from the
        # PAGE_NEXT above (60 s) is separate and still runs
        await go(w, 59)
        assert w.pages.page == "energy"
        await go(w, 2)
        assert w.pages.page == "tail"


async def test_core_336_timed_jump_and_idle_return_are_held_too(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config()) as w:
        await goto(w, "guest")
        await go(w, 100)
        w.pages.pause_hold("rest")
        await go(w, 3600)
        assert w.pages.page == "guest"
        w.pages.resume("rest")
        await w.settle()
        await go(w, 299)
        assert w.pages.page == "guest"
        await go(w, 2)
        assert w.pages.page == "home"
    async with started(tmp_path, cycle_config(return_after=60)) as w:
        await goto(w, "lounge")
        w.pages.pause_hold("rest", 300)
        await go(w, 299)
        assert w.pages.page == "lounge"
        await go(w, 2)  # the hold ended; the idle count starts from zero
        assert (w.pages.page, w.pages.paused) == ("lounge", [])
        await go(w, 58)
        assert w.pages.page == "lounge"
        await go(w, 3)
        assert w.pages.page == "home"


async def test_core_336_holders_are_separate_and_replaced(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30)) as w:
        w.pages.pause_hold("boss", 10)
        w.pages.pause_hold("rest")
        assert w.pages.paused == ["boss", "rest"]
        w.pages.pause_hold("boss", 100)  # a new pause replaces the hold of that holder
        await go(w, 50)
        assert w.pages.paused == ["boss", "rest"]
        w.pages.resume("nobody")  # without a hold nothing changes
        w.pages.resume("rest")
        assert w.pages.paused == ["boss"]
        await go(w, 60)  # boss's replaced hold ends after 100 s
        assert w.pages.paused == []
        await go(w, 31)
        assert w.pages.page == "energy"


async def test_core_336_for_ends_the_hold_and_notifies(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30)) as w:
        calls: list[list[str]] = []
        w.pages.listeners.append(lambda: calls.append(w.pages.paused))
        w.pages.pause_hold("rest", 60)
        await go(w, 59)
        assert w.pages.page == "home"
        await go(w, 2)
        assert calls[0] == ["rest"]
        assert calls[-1] == []
        assert w.pages.paused == []
        await go(w, 31)
        assert w.pages.page == "energy"


async def test_core_336_hold_ends_when_the_holding_instance_stops(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30)) as w:
        await w.ctx("boss").send_notification("PAGE_PAUSE", {"for": 3600})
        w.pages.pause_hold("rest")
        await w.settle()
        assert w.pages.paused == ["boss", "rest"]
        await w.engine.supervisor.get("boss").stop()
        await w.settle()
        assert w.pages.paused == ["rest"]


async def test_core_336_pause_payload_is_checked(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config()) as w:
        ctx = w.ctx("boss")
        for bad in ({"for": 0}, {"for": 3601}, {"for": "x"}):
            with pytest.raises(ValueError, match=r"PAGE_PAUSE|payload|for"):
                await ctx.send_notification("PAGE_PAUSE", bad)
        assert w.pages.paused == []


async def test_core_337_previous_and_reasons(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config()) as w:
        first = w.rec.pages()[0]
        assert (first["reason"], first["previous"], first["page"]) == ("start", None, "home")
        await goto(w, "energy")
        last = w.rec.pages()[-1]
        assert (last["previous"], last["page"], last["reason"]) == ("home", "energy", "rest")
        w.engine.supervisor.emit("CORE_RESUME", {})
        await w.settle()
        last = w.rec.pages()[-1]
        assert (last["previous"], last["page"], last["reason"]) == ("energy", "home", "resume")


async def test_core_337_after_and_idle_do_not_pause_rotation(tmp_path: Path) -> None:
    async with started(tmp_path, cycle_config(rotate=30, return_after=60)) as w:
        await goto(w, "guest")
        await go(w, 301)
        assert (w.pages.page, w.rec.pages()[-1]["reason"]) == ("home", "after")
        await go(w, 31)  # rotation was not paused by the jump
        assert w.pages.page == "energy"
