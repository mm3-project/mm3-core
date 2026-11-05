"""Milestone 6, part D2: the draft layout object and Show on mirror (server side)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm3.core.config import load_config
from mm3.core.config.editor import ConfigEditor
from mm3.core.config.history import ConfigHistory
from mm3.core.config.layout import layout_object
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.mirrorpreview import MirrorPreview, NoKioskError
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"


class Panel(MirrorModule):
    name = "panel"
    element = "mm3-panel"


MAIN = """engine:
  locale: en
pages:
  list:
    - id: home
    - id: energy
modules:
  - module: panel
    id: a
    position: top_left
  - module: panel
    id: b
    position: top_left
  - module: panel
    id: c
    pages: [energy]
    position: { col: 1, row: 9, width: 6, height: 3 }
"""


def registry() -> Registry:
    reg = Registry()
    reg.add(Panel)
    return reg


@dataclass
class FakeShells:
    """The hub as Show on mirror sees it."""

    kiosk: bool = True
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def kiosk_connected(self) -> bool:
        return self.kiosk

    def control_kiosk(self, action: str, data: Mapping[str, Any]) -> int:
        self.sent.append((action, dict(data)))
        return 1 if self.kiosk else 0


RUNNING = {"viewport": [1920, 1080], "cell": [1.0, 1.0], "grid": {}, "pages": {"main": []}}
DRAFT = {
    "viewport": [1920, 1080],
    "cell": [130.0, 70.0],
    "grid": {"columns": 12, "rows": 12, "gap": 16.0, "padding": 32.0},
    "pages": {
        "main": [
            {
                "instance": "a",
                "col": 1,
                "row": 1,
                "width": 4,
                "height": 3,
                "z": 0,
                "preset": None,
                "stack_index": 0,
                "stack_size": 1,
                "area": [550.0, 240.0],
            }
        ]
    },
}


@dataclass
class World:
    root: Path
    history: ConfigHistory
    editor: ConfigEditor
    client: httpx.AsyncClient
    shells: FakeShells
    mirror: MirrorPreview
    sessions: set[str]
    applied: list[tuple[int, int] | None]

    async def etag(self) -> str:
        return str((await self.client.get("/api/v1/config")).json()["etag"])


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    configure_logging("info", stream=io.StringIO(), json=True)
    root = tmp_path / "etc"
    root.mkdir()
    (root / "config.yaml").write_text(MAIN)
    env = tmp_path / "mm3.env"
    env.write_text("")
    env.chmod(0o600)
    reg = registry()
    result = load_config(root / "config.yaml", registry=reg, env_file=env, environ={})
    assert result.ok, result.records
    history = ConfigHistory(
        root / "config.yaml", env_file=env, registry=reg, state_dir=tmp_path / "state", environ={}
    )
    with history.store.lock():
        history.record_disk()
    applied: list[tuple[int, int] | None] = [None]
    editor = ConfigEditor(history, applied=lambda: applied[0])
    engine = Engine(
        EngineSettings(
            timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
        ),
        registry=reg,
        clock=VirtualClock(),
        state_dir=tmp_path / "state",
    )
    shells = FakeShells()
    sessions: set[str] = set()
    mirror = MirrorPreview(shells, lambda: RUNNING, lambda sid: sid in sessions, ttl=0.2)
    services = ApiServices(engine, None, None, None, None, "1.0+t", editor=editor, mirror=mirror)
    auth = WebAuth.open(tmp_path / "web")
    token = auth.tokens.new("cfg")
    app = protect(admin_app(auth, api=services), lambda: [], "admin")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    await engine.start(result.instance_specs())
    async with httpx.AsyncClient(
        transport=transport, base_url=ADMIN, headers={"authorization": f"Bearer {token}"}
    ) as client:
        try:
            yield World(root, history, editor, client, shells, mirror, sessions, applied)
        finally:
            mirror.clear()
            await engine.stop()


def placements(layout: Mapping[str, Any], page: str) -> dict[str, dict[str, Any]]:
    return {p["instance"]: p for p in layout["pages"][page]}


# --- CORE-301, CORE-302: the draft layout object -----------------------------------------


async def test_core_301_patch_returns_the_draft_layout(world: World) -> None:
    r = await world.client.post(
        "/api/v1/config/patch", json={"base": await world.etag(), "files": [], "ops": []}
    )
    assert r.status_code == 200, r.text
    layout = r.json()["layout"]
    assert layout["viewport"] == [1920, 1080]
    assert layout["grid"] == {"columns": 12, "rows": 12, "gap": 16.0, "padding": 32.0}
    assert set(layout["pages"]) == {"home", "energy"}
    home = placements(layout, "home")
    assert set(home) == {"a", "b"}  # c is only on energy
    assert (home["a"]["preset"], home["a"]["stack_index"], home["a"]["stack_size"]) == (
        "top_left",
        0,
        2,
    )
    assert home["b"]["stack_index"] == 1
    energy = placements(layout, "energy")
    assert (energy["c"]["col"], energy["c"]["row"], energy["c"]["width"]) == (1, 9, 6)


async def test_core_301_draft_layout_with_errors_and_the_applied_mode(world: World) -> None:
    # An overlap is an error, but the layout is still computed; an instance
    # outside the grid is left out, and the kiosk's applied mode sets the viewport.
    world.applied[0] = (1280, 720)
    ops = [
        {
            "file": "config.yaml",
            "patch": [
                {"op": "replace", "path": "/modules/2/pages", "value": "all"},
                {
                    "op": "replace",
                    "path": "/modules/2/position",
                    "value": {"col": 1, "row": 2, "width": 4, "height": 2},
                },
                {
                    "op": "add",
                    "path": "/modules/-",
                    "value": {
                        "module": "panel",
                        "id": "d",
                        "position": {"col": 11, "row": 1, "width": 4, "height": 1},
                    },
                },
            ],
        }
    ]
    r = await world.client.post(
        "/api/v1/config/patch", json={"base": await world.etag(), "files": [], "ops": ops}
    )
    body = r.json()
    codes = {rec["code"] for rec in body["records"]}
    assert {"CORE_CFG_OVERLAP", "CORE_CFG_OUT_OF_GRID"} <= codes
    layout = body["layout"]
    assert layout["viewport"] == [1280, 720]
    home = placements(layout, "home")
    assert "c" in home
    assert "d" not in home


async def test_core_301_draft_layout_is_null_when_the_top_level_does_not_validate(
    world: World,
) -> None:
    files = [{"path": "config.yaml", "content": MAIN.replace("rows: 12", "") + "layout: 3\n"}]
    r = await world.client.post(
        "/api/v1/config/patch", json={"base": await world.etag(), "files": files, "ops": []}
    )
    assert r.status_code == 200, r.text
    assert r.json()["layout"] is None


async def test_core_302_validate_returns_the_draft_layout(world: World) -> None:
    files = [{"path": "config.yaml", "content": MAIN.replace("width: 6", "width: 5")}]
    r = await world.client.post(
        "/api/v1/config/validate", json={"base": await world.etag(), "files": files}
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["layout"]["pages"]) == {"home", "energy"}


def test_core_274_layout_object_uses_its_own_grid(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(MAIN)
    env = tmp_path / "mm3.env"
    env.write_text("")
    env.chmod(0o600)
    result = load_config(tmp_path / "config.yaml", registry=registry(), env_file=env, environ={})
    assert result.layout is not None
    assert result.geometry is result.layout
    assert layout_object(result.layout)["grid"]["columns"] == 12
    assert layout_object(result.layout, {"columns": 3})["grid"] == {"columns": 3}


# --- CORE-307: Show on mirror ------------------------------------------------------------


async def test_core_307_push_goes_to_the_kiosk_only_and_answers_204(world: World) -> None:
    r = await world.client.post("/api/v1/preview/mirror", json=DRAFT)
    assert r.status_code == 204, r.text
    assert r.content == b""
    assert world.shells.sent == [("layout", {"layout": DRAFT})]
    assert world.mirror.active
    r = await world.client.delete("/api/v1/preview/mirror")
    assert r.status_code == 204
    assert world.shells.sent[-1] == ("layout", {"layout": RUNNING})
    assert not world.mirror.active


async def test_core_307_without_a_kiosk_is_409(world: World) -> None:
    world.shells.kiosk = False
    r = await world.client.post("/api/v1/preview/mirror", json=DRAFT)
    assert r.status_code == 409
    assert r.json()["code"] == "CORE_API_NO_KIOSK"
    assert world.shells.sent == []


async def test_core_307_body_is_a_layout_object(world: World) -> None:
    bad = {**DRAFT, "grid": {"columns": 0, "rows": 12, "gap": 0, "padding": 0}}
    r = await world.client.post("/api/v1/preview/mirror", json=bad)
    assert r.status_code == 422
    assert r.json()["code"] == "CORE_API_INVALID_REQUEST"


async def test_core_307_reverts_60_s_after_the_last_push(world: World) -> None:
    world.mirror.push(DRAFT, None)
    await asyncio.sleep(0.12)
    world.mirror.push(DRAFT, None)  # a new push restarts the timer
    await asyncio.sleep(0.12)
    assert world.mirror.active
    await asyncio.sleep(0.15)
    assert not world.mirror.active
    assert world.shells.sent[-1] == ("layout", {"layout": RUNNING})


async def test_core_307_reverts_when_the_session_closes_its_last_channel(world: World) -> None:
    world.sessions.add("s1")
    world.mirror.push(DRAFT, "s1")
    world.mirror.session_closed("other")
    assert world.mirror.active
    world.mirror.session_closed("s1")
    assert not world.mirror.active
    # A push from another admin replaces the owner.
    world.sessions.add("s2")
    world.mirror.push(DRAFT, "s1")
    world.mirror.push(DRAFT, "s2")
    world.mirror.session_closed("s1")
    assert world.mirror.active
    # A session without an open channel owns nothing: only DELETE, reload or 60 s end it.
    world.mirror.push(DRAFT, "s9")
    world.mirror.session_closed("s9")
    assert world.mirror.active


async def test_core_307_a_save_ends_the_draft(world: World) -> None:
    world.mirror.push(DRAFT, None)
    await world.mirror.reloaded(object(), object())
    assert not world.mirror.active
    assert world.shells.sent[-1] == ("layout", {"layout": RUNNING})
    assert not world.mirror.clear()  # nothing left to end


def test_core_307_no_kiosk_error_from_the_service() -> None:
    shells = FakeShells(kiosk=False)
    mirror = MirrorPreview(shells, lambda: RUNNING)
    with pytest.raises(NoKioskError):
        mirror.push(DRAFT, None)


# --- the Layout tab in Chromium ------------------------------------------------------------

BOX_PACKAGE = """
from mm3.module import MirrorModule


class BoxMod(MirrorModule):
    name = "boxmod"
    element = "mm3-boxmod"
    scripts = ["box.js"]
"""

BOX_ELEMENT = """
import { MirrorElement, html } from "/core/mm3.js";

class BoxMod extends MirrorElement {
  render() {
    return html`<div class="box">${this.preview ? "PREVIEW" : "LIVE"} ${this.instanceId}</div>`;
  }
}
customElements.define("mm3-boxmod", BoxMod);
"""

PAGE_MAIN = """engine:
  locale: en
pages:
  list:
    - id: home
    - id: energy
modules:
  - module: boxmod
    id: a
    position: top_left
  - module: boxmod
    id: b
    pages: [home, energy]
    position: { col: 7, row: 2, width: 3, height: 3 }
  - module: boxmod
    id: c
    pages: [energy]
    position: { col: 1, row: 9, width: 6, height: 3 }
"""


def chromium() -> str | None:
    try:
        from module_projects import chromium_available
    except ImportError:  # pragma: no cover
        return None
    import os

    return os.environ.get("MM3_CHROMIUM", "") if chromium_available() else None


@pytest.fixture(scope="module")
def box_module(tmp_path_factory: pytest.TempPathFactory) -> Any:
    import importlib
    import sys

    root = tmp_path_factory.mktemp("boxpkg")
    pkg = root / "mm3_boxmod"
    (pkg / "static").mkdir(parents=True)
    (pkg / "__init__.py").write_text(BOX_PACKAGE)
    (pkg / "static" / "box.js").write_text(BOX_ELEMENT)
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("mm3_boxmod")
    finally:
        sys.path.remove(str(root))


@dataclass
class Running:
    base: str
    sid: str
    root: Path
    mirror: MirrorPreview


@pytest.fixture
async def running(tmp_path: Path, box_module: Any) -> AsyncIterator[Running]:
    from mm3.core.config.manager import ConfigManager
    from mm3.core.diag.inspector import BusInspector
    from mm3.core.pages import PageService
    from mm3.core.settings import AdminSettings, ServerSettings
    from mm3.core.web.adminws import AdminChannel
    from mm3.core.web.api import status_object
    from mm3.core.web.gateway import WebGateway
    from mm3.core.web.shell import ShellSources
    from mm3.core.web.view import cached_view
    from mm3.core.web.ws import ClientHub

    if chromium() is None:
        pytest.skip("Chromium is not available")
    configure_logging("info", stream=io.StringIO(), json=True)
    root = tmp_path / "etc"
    root.mkdir()
    (root / "config.yaml").write_text(PAGE_MAIN)
    env = tmp_path / "mm3.env"
    env.write_text("")
    env.chmod(0o600)
    reg = Registry()
    reg.add(box_module.BoxMod)
    result = load_config(root / "config.yaml", registry=reg, env_file=env, environ={})
    assert result.ok, result.records
    history = ConfigHistory(
        root / "config.yaml", env_file=env, registry=reg, state_dir=tmp_path / "state", environ={}
    )
    with history.store.lock():
        history.record_disk()
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    ).model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    engine = Engine(settings, registry=reg, clock=VirtualClock(), state_dir=tmp_path / "state")
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    auth = WebAuth.open(tmp_path / "web")
    view = cached_view(lambda: manager.current, reg, i18n=engine.i18n)
    hub = ClientHub(view, auth, "1.0+t")
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, engine.clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    editor = ConfigEditor(history, applied=lambda: manager.applied)
    inspector = BusInspector()
    services = ApiServices(engine, manager, pages, hub, None, "1.0+t", editor=editor)
    channel = AdminChannel(auth, lambda: status_object(services), inspector)
    mirror = MirrorPreview(hub, lambda: hub.view().layout, channel.has_session)
    channel.session_closed.append(mirror.session_closed)
    services.mirror = mirror
    gateway = WebGateway(
        settings,
        tmp_path / "state",
        hub=hub,
        api=services,
        channel=channel,
        sources=ShellSources(view, lambda: manager.current, engine.i18n),
    )
    await engine.start(result.instance_specs())
    await pages.start()
    await gateway.start()
    rec = auth.tokens.verify(auth.tokens.new("ui"), "admin")
    assert rec is not None
    sid = auth.sessions.create(rec)
    try:
        yield Running(f"http://127.0.0.1:{gateway.admin.port}", sid, root, mirror)
    finally:
        mirror.clear()
        await gateway.stop()
        await pages.close()
        await engine.stop()


async def browse(base: str, sid: str) -> Any:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path=chromium() or None)
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    await context.add_cookies([{"name": "mm3_admin", "value": sid, "url": base}])
    page = await context.new_page()
    problems: list[str] = []
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    return pw, browser, page, problems


async def drag(page: Any, selector: str, dx_cells: float, dy_cells: float, scale: float) -> None:
    """Drag from the middle of ``selector`` by whole cells (cell plus gap of the layout)."""
    pitch = await page.evaluate(
        "() => { const l = document.querySelector('mm3-admin-config').draft.layout;"
        " return [l.cell[0] + l.grid.gap, l.cell[1] + l.grid.gap]; }"
    )
    box = await page.locator(selector).bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    steps = 6
    for i in range(1, steps + 1):
        await page.mouse.move(
            x + dx_cells * pitch[0] * scale * i / steps, y + dy_cells * pitch[1] * scale * i / steps
        )
    await page.mouse.up()


async def draft(page: Any) -> Any:
    return await page.evaluate(
        "() => { const d = document.querySelector('mm3-admin-config').draft;"
        " return {docs: d.documents, steps: d.undoStack.map((s) => s.label)}; }"
    )


async def wait_steps(page: Any, n: int) -> None:
    await page.wait_for_function(
        f"() => document.querySelector('mm3-admin-config').draft.undoStack.length === {n}"
        " && !document.querySelector('mm3-admin-config').busy",
        timeout=10000,
    )


@contextlib.asynccontextmanager
async def layout_tab(running: Running) -> AsyncIterator[tuple[Any, Any, float]]:
    """Open /admin/config on the Layout tab; yield the page, the iframe and the scale."""
    pw, browser, page, problems = await browse(running.base, running.sid)
    try:
        await page.goto(f"{running.base}/admin/config")
        await page.click("nav.tabs >> text=Layout")
        await page.wait_for_selector('.overlay .box[data-instance="a"]')
        frame = page.frame_locator("iframe")
        await frame.locator("mm3-boxmod").first.wait_for()
        scale = await page.evaluate("() => document.querySelector('mm3-layout-view').scale")
        yield page, frame, scale
    finally:
        await browser.close()
        await pw.stop()
    assert not problems, problems


async def iframe_area(page: Any, instance: str) -> str:
    return str(
        await page.evaluate(
            "(id) => document.querySelector('iframe').contentDocument"
            ".querySelector(`[data-instance=\"${id}\"]`)?.style.gridArea || ''",
            instance,
        )
    )


async def test_core_97_preview_shell_scaled_under_the_overlay(running: Running) -> None:
    async with layout_tab(running) as (page, frame, scale):
        assert 0 < scale <= 1
        size = await page.evaluate(
            "() => { const f = document.querySelector('iframe');"
            " return [f.style.width, f.style.height, f.style.transform]; }"
        )
        assert size[:2] == ["1920px", "1080px"]
        assert abs(float(size[2][len("scale(") : -1]) - scale) < 1e-4
        assert await frame.locator("mm3-boxmod").count() == 3
        boxes = page.locator(".overlay .box")
        assert sorted(await boxes.evaluate_all("(l) => l.map((b) => b.dataset.instance)")) == [
            "a",
            "b",
        ]


async def test_core_98_elements_know_they_are_in_the_preview(running: Running) -> None:
    async with layout_tab(running) as (_page, frame, _scale):
        await frame.locator("text=PREVIEW a").wait_for()


async def test_core_242_page_selector_starts_on_the_current_page(running: Running) -> None:
    async with layout_tab(running) as (page, _frame, _scale):
        assert await page.locator("select.page").input_value() == "home"
        options = await page.locator("select.page option").evaluate_all(
            "(l) => l.map((o) => o.value)"
        )
        assert options == ["home", "energy"]


async def test_core_326_invalid_drop_is_refused_and_a_move_is_one_step(running: Running) -> None:
    async with layout_tab(running) as (page, _frame, scale):
        # Onto b (same z): red while dragging, nothing written on drop.
        await page.click('.overlay .box[data-instance="a"]')
        box = await page.locator('.overlay .box[data-instance="a"]').bounding_box()
        assert box is not None
        x, y = box["x"] + 20, box["y"] + 20
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 4 * 156 * scale, y, steps=5)  # 140 px cells + 16 px gap
        assert await page.locator(".overlay .box.invalid").count() == 1
        await page.mouse.up()
        assert (await draft(page))["steps"] == []
        assert await page.locator(".overlay .box.invalid").count() == 0
        # A valid move: the preset instance gets a rectangle, as one labelled step.
        await drag(page, '.overlay .box[data-instance="a"]', 0, 6, scale)
        await wait_steps(page, 1)
        d = await draft(page)
        assert d["steps"] == ["a: moved"]
        assert d["docs"]["config.yaml"]["modules"][0]["position"] == {
            "col": 1,
            "row": 8,
            "width": 4,
            "height": 3,
        }
        # Corner resize, and undo through the shared history (CORE-151).
        await page.click('.overlay .box[data-instance="a"]')
        await drag(page, '.overlay .box[data-instance="a"] .handle.se', 1, 0, scale)
        await wait_steps(page, 2)
        assert (await draft(page))["steps"][-1] == "a: resized"
        await page.keyboard.press("Control+z")
        await wait_steps(page, 1)


async def test_core_325_preview_follows_the_draft_and_the_selected_page(
    running: Running,
) -> None:
    async with layout_tab(running) as (page, _frame, scale):
        assert (await iframe_area(page, "a")).startswith("2 / 1")
        await drag(page, '.overlay .box[data-instance="a"]', 0, 6, scale)
        await wait_steps(page, 1)
        await page.wait_for_function(
            "() => document.querySelector('iframe').contentDocument"
            ".querySelector('[data-instance=\"a\"]').style.gridArea.startsWith('8 / 1')",
            timeout=5000,
        )
        # The selected page: c (only on energy) shows in the preview.
        await page.select_option("select.page", "energy")
        await page.wait_for_selector('.overlay .box[data-instance="c"]')
        await page.wait_for_function(
            "() => { const c = document.querySelector('iframe').contentDocument"
            ".querySelector('[data-instance=\"c\"]'); return c && c.dataset.gone !== 'true'; }",
            timeout=5000,
        )
        # A new instance of the draft without an element is an empty cell.
        await page.evaluate(
            "() => { const p = document.querySelector('mm3-admin-config');"
            " return p.run(() => p.draft.apply('config.yaml', [{op: 'add', path: '/modules/-',"
            " value: {module: 'boxmod', id: 'd', position: {col: 9, row: 9, width: 2,"
            " height: 2}}}], 'add d')); }"
        )
        await page.wait_for_function(
            "() => document.querySelector('iframe').contentDocument"
            ".querySelector('.mm3-placeholder[data-instance=\"d\"]')",
            timeout=5000,
        )


async def test_core_327_edits_write_the_page_override_or_position(running: Running) -> None:
    async with layout_tab(running) as (page, _frame, scale):
        # b is on two pages: an edit on home is an override for home.
        await page.click('.overlay .box[data-instance="b"]')
        await drag(page, '.overlay .box[data-instance="b"] .handle.se', 1, 0, scale)
        await wait_steps(page, 1)
        b = (await draft(page))["docs"]["config.yaml"]["modules"][1]
        assert b["position"] == {"col": 7, "row": 2, "width": 3, "height": 3}
        assert b["positions"] == {"home": {"col": 7, "row": 2, "width": 4, "height": 3}}
        # On energy: a second override; moved back onto position, it goes away.
        await page.select_option("select.page", "energy")
        await page.wait_for_selector('.overlay .box[data-instance="c"]')
        await page.click('.overlay .box[data-instance="b"]')
        await drag(page, '.overlay .box[data-instance="b"]', 0, 3, scale)
        await wait_steps(page, 2)
        b = (await draft(page))["docs"]["config.yaml"]["modules"][1]
        assert b["positions"]["energy"] == {"col": 7, "row": 5, "width": 3, "height": 3}
        await drag(page, '.overlay .box[data-instance="b"]', 0, -3, scale)
        await wait_steps(page, 3)
        b = (await draft(page))["docs"]["config.yaml"]["modules"][1]
        assert set(b["positions"]) == {"home"}
        # c has a single page and a (pages: all) has none: both write position.
        await page.click('.overlay .box[data-instance="c"]')
        await drag(page, '.overlay .box[data-instance="c"]', 1, 0, scale)
        await wait_steps(page, 4)
        mods = (await draft(page))["docs"]["config.yaml"]["modules"]
        assert mods[2]["position"]["col"] == 2
        assert "positions" not in mods[2]
        await page.click('.overlay .box[data-instance="a"]')
        await drag(page, '.overlay .box[data-instance="a"]', 0, 4, scale)
        await wait_steps(page, 5)
        mods = (await draft(page))["docs"]["config.yaml"]["modules"]
        assert mods[0]["position"]["row"] == 6
        assert "positions" not in mods[0]


async def test_core_328_toolbar_sets_z_preset_appearance_and_enabled(running: Running) -> None:
    async with layout_tab(running) as (page, _frame, _scale):
        await page.click('.overlay .box[data-instance="b"]')
        await page.wait_for_selector("text=3 \u00d7 3 cells")
        assert "px" in await page.locator(".inst-toolbar .size").inner_text()  # CORE-142
        # A preset instance has a fixed z.
        await page.click('.overlay .box[data-instance="a"]')
        assert await page.locator('.inst-toolbar input[name="z"]').is_disabled()
        await page.click('.overlay .box[data-instance="b"]')
        await page.fill('.inst-toolbar input[name="z"]', "2")
        await page.press('.inst-toolbar input[name="z"]', "Tab")
        await wait_steps(page, 1)
        await page.select_option('.inst-toolbar select[name="overflow"]', "fit")
        await wait_steps(page, 2)
        await page.fill('.inst-toolbar input[name="opacity"]', "0.5")
        await page.press('.inst-toolbar input[name="opacity"]', "Tab")
        await wait_steps(page, 3)
        b = (await draft(page))["docs"]["config.yaml"]["modules"][1]
        assert b["positions"]["home"]["z"] == 2
        assert b["appearance"] == {"overflow": "fit", "opacity": 0.5}
        await page.select_option('.inst-toolbar select[name="overflow"]', "clip")
        await wait_steps(page, 4)
        assert (await draft(page))["docs"]["config.yaml"]["modules"][1]["appearance"] == {
            "opacity": 0.5
        }
        await page.select_option('.inst-toolbar select[name="preset"]', "bottom_right")
        await wait_steps(page, 5)
        b = (await draft(page))["docs"]["config.yaml"]["modules"][1]
        assert b["positions"]["home"] == "bottom_right"
        # Disable and enable again (the "Save as disabled" of CORE-240 is the normal save).
        await page.click('.inst-toolbar button[name="disable"]')
        await wait_steps(page, 6)
        assert (await draft(page))["docs"]["config.yaml"]["modules"][1]["disabled"] is True
        await page.wait_for_selector(".disabled-list >> text=b")
        assert await page.locator('.overlay .box[data-instance="b"]').count() == 0
        await page.click(".disabled-list button")
        await wait_steps(page, 7)
        assert "disabled" not in (await draft(page))["docs"]["config.yaml"]["modules"][1]
        # Edit in Form opens the instance's section.
        await page.click('.overlay .box[data-instance="b"]')
        await page.click("text=Edit in Form")
        await page.wait_for_selector("mm3-schema-form")
        assert "modules/b" in (await page.locator("section.card.form h2").inner_text()).lower()


async def test_core_34_layout_edits_show_in_the_yaml_view(running: Running) -> None:
    async with layout_tab(running) as (page, _frame, scale):
        await drag(page, '.overlay .box[data-instance="a"]', 0, 6, scale)
        await wait_steps(page, 1)
        await page.click("nav.tabs >> text=YAML")
        await page.wait_for_selector(".cm-content")
        text = await page.locator(".cm-content").inner_text()
        assert "row: 8" in text
        await page.click("nav.tabs >> text=Layout")
        await page.wait_for_selector('.overlay .box[data-instance="a"]')


async def test_core_307_show_on_mirror_without_a_kiosk(running: Running) -> None:
    pw, browser, page, problems = await browse(running.base, running.sid)
    try:
        await page.goto(f"{running.base}/admin/config")
        await page.click("nav.tabs >> text=Layout")
        await page.wait_for_selector('.overlay .box[data-instance="a"]')
        await page.click("input.mirror")
        await page.wait_for_selector("text=No kiosk")
        assert not await page.is_checked("input.mirror")
        assert not running.mirror.active
    finally:
        await browser.close()
        await pw.stop()
    # The 409 answer shows in the console as a failed request; nothing else.
    assert all("409" in p for p in problems), problems


async def test_core_94_rescale_proportionally(running: Running) -> None:
    pw, browser, page, problems = await browse(running.base, running.sid)
    try:
        await page.goto(f"{running.base}/admin/config")
        await page.click("nav.tabs >> text=Layout")
        await page.wait_for_selector('.overlay .box[data-instance="a"]')
        # The grid shrinks to 6 x 6 in the draft: b and c fall outside.
        await page.evaluate(
            "() => { const p = document.querySelector('mm3-admin-config');"
            " return p.run(() => p.draft.apply('config.yaml', [{op: 'add', path: '/layout',"
            " value: {grid: {columns: 6, rows: 6}}}], 'grid')); }"
        )
        await page.wait_for_selector("button.rescale")
        await page.click("button.rescale")
        await wait_steps(page, 2)
        d = await draft(page)
        assert d["steps"][-1] == "Rescaled to 6 \u00d7 6"
        mods = d["docs"]["config.yaml"]["modules"]
        assert mods[0]["position"] == "top_left"  # presets stay
        # Boundaries b -> round(b x 6 / 12), halves up: cols 6..9 -> 3..5, rows 1..4 -> 1..2.
        assert mods[1]["position"] == {"col": 4, "row": 2, "width": 2, "height": 1}
        assert mods[2]["position"] == {"col": 1, "row": 5, "width": 3, "height": 2}
        assert await page.locator("button.rescale").count() == 0
    finally:
        await browser.close()
        await pw.stop()
    assert not problems, problems


async def test_core_152_section_undo_after_layout_edits(running: Running) -> None:
    async with layout_tab(running) as (page, _frame, scale):
        await drag(page, '.overlay .box[data-instance="a"]', 0, 6, scale)
        await wait_steps(page, 1)
        await page.click('.overlay .box[data-instance="b"]')
        await drag(page, '.overlay .box[data-instance="b"] .handle.se', 1, 0, scale)
        await wait_steps(page, 2)
        # "Undo last change here" on a reverts only a's step; b keeps its edit.
        await page.click("nav.tabs >> text=Form")
        await page.locator("nav.sections button", has_text=re.compile(r"^\W*a$")).click()
        await page.wait_for_selector("section.card.form h2 >> text=modules/a")
        await page.click("text=Undo last change here")
        await wait_steps(page, 3)
        mods = (await draft(page))["docs"]["config.yaml"]["modules"]
        assert mods[0]["position"] == "top_left"
        assert mods[1]["positions"]["home"]["width"] == 4
