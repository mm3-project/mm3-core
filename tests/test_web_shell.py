"""Milestone 3, part C: the shell, its files and control frames (core spec 5.6, 6.8-6.10)."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from mm3.core.config import load_config
from mm3.core.config.diff import ReloadPlan, plan_reload
from mm3.core.config.validate import ConfigResult
from mm3.core.engine import Engine
from mm3.core.i18n import compile_locales
from mm3.core.pages import PageService
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.sessions import ADMIN_COOKIE, DISPLAY_COOKIE
from mm3.core.web.shell import IMMUTABLE, ShellSources, shell_page
from mm3.core.web.view import cached_view, content_version
from mm3.core.web.ws import ClientHub
from mm3.testing import VirtualClock
from test_i18n import write_catalog

PACKAGE = """
from typing import Any
from mm3.messages import ClientMessage
from mm3.module import MirrorModule


class ShellTest(MirrorModule):
    name = "shelltest"
    element = "mm3-shelltest"
    scripts = ["element.js"]
    styles = ["element.css"]

    async def start(self) -> None:
        self.ctx.set_state({"temp": 21})

    async def on_client_message(self, msg: ClientMessage) -> None:
        if msg.type == "bump":
            self.ctx.set_state({"temp": msg.payload.get("n")})


class TouchTest(MirrorModule):
    name = "touchtest"
    element = "mm3-touchtest"
    scripts = ["touch.js"]
    capabilities = {"input.touch"}
"""

TOUCH_ELEMENT = """
import { MirrorElement, html } from "/core/mm3.js";

class TouchTest extends MirrorElement {
  render() {
    return html`<div class="pad">pad</div>`;
  }
}
customElements.define("mm3-touchtest", TouchTest);
"""

CATALOGS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "en": {"HEADER": "Weather", "FILES": ("{count} file", "{count} files")},
    "it": {"HEADER": "Meteo", "FILES": ("{count} file", "{count} file")},
    "fr": {"HEADER": "Météo", "FILES": ("{count} fichier", "{count} fichiers")},
    "es": {"HEADER": "Tiempo", "FILES": ("{count} archivo", "{count} archivos")},
    "de": {"HEADER": "Wetter", "FILES": ("{count} Datei", "{count} Dateien")},
}

ELEMENT = """
import { MirrorElement, html } from "/core/mm3.js";

class ShellTest extends MirrorElement {
  render(state) {
    const label = this.t("FILES", { count: 2 });
    return html`<div class="temp">${state.temp ?? "-"}</div><div class="label">${label}</div>`;
  }
  onResize(w, h) {
    this.size = [w, h];
  }
}
customElements.define("mm3-shelltest", ShellTest);
"""

CONFIG = """
engine:
  locale: en
  input:
    touch: true
pages:
  default: a
  list:
    - id: a
    - id: b
modules:
  - module: shelltest
    id: home
    position: top_left
    pages: [a]
    appearance:
      header: Hello
      css: ".temp { color: red; }"
  - module: shelltest
    id: other
    position: top_right
    pages: [b]
    locale: it
    appearance:
      header: true
  - module: shelltest
    id: both
    position: bottom_bar
    appearance:
      overflow: scroll
  - module: shelltest
    id: also
    position: bottom_bar
    locale: it
    appearance:
      overflow: fit
  - module: touchtest
    id: pad
    position: middle_center
    pages: [a]
    appearance:
      header: true
"""

CUSTOM_CSS = "body { --mm3-color-fg: #bbb; }\n"


@pytest.fixture(scope="module")
def shell_module(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("pkg")
    pkg = root / "mm3_shelltest"
    (pkg / "static").mkdir(parents=True)
    (pkg / "__init__.py").write_text(PACKAGE)
    (pkg / "static" / "element.js").write_text(ELEMENT)
    (pkg / "static" / "element.css").write_text(":host { color: var(--mm3-color-bright); }\n")
    (pkg / "static" / "touch.js").write_text(TOUCH_ELEMENT)
    for lang, entries in CATALOGS.items():
        write_catalog(pkg / "locales", lang, entries)
    compile_locales(pkg / "locales")
    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module("mm3_shelltest")
    finally:
        sys.path.remove(str(root))
    return module


class Mirror:
    def __init__(
        self,
        engine: Engine,
        hub: ClientHub,
        gateway: WebGateway,
        pages: PageService,
        holder: dict[str, ConfigResult],
        etc: Path,
        registry: Registry,
    ) -> None:
        self.engine = engine
        self.hub = hub
        self.gateway = gateway
        self.pages = pages
        self.holder = holder
        self.etc = etc
        self.registry = registry

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.gateway.mirror.port}"

    @property
    def admin(self) -> str:
        return f"http://127.0.0.1:{self.gateway.admin.port}"

    def reload(self, text: str, css: str | None = CUSTOM_CSS) -> ConfigResult:
        (self.etc / "config.yaml").write_text(text)
        if css is None:
            (self.etc / "custom.css").unlink(missing_ok=True)
        else:
            (self.etc / "custom.css").write_text(css)
        result = load_config(
            self.etc / "config.yaml", registry=self.registry, env_file=self.etc / "none.env"
        )
        assert result.ok, result.records
        self.holder["result"] = result
        return result


@pytest.fixture
async def mirror(tmp_path: Path, shell_module: Any) -> AsyncIterator[Mirror]:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.yaml").write_text(CONFIG)
    (etc / "custom.css").write_text(CUSTOM_CSS)
    registry = Registry()
    registry.add(shell_module.ShellTest)
    registry.add(shell_module.TouchTest)
    result = load_config(etc / "config.yaml", registry=registry, env_file=etc / "none.env")
    assert result.ok, result.records
    holder = {"result": result}
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    ).model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    clock = VirtualClock()
    engine = Engine(settings, registry=registry, clock=clock, state_dir=tmp_path / "state")
    view = cached_view(lambda: holder["result"], registry, i18n=engine.i18n)
    hub = ClientHub(view, WebAuth.open(tmp_path / "state"), "1.0+test", hello_timeout=1)
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    sources = ShellSources(view, lambda: holder["result"], engine.i18n)
    gateway = WebGateway(settings, tmp_path / "state", hub=hub, sources=sources)
    await engine.start(result.instance_specs())
    await pages.start()
    await gateway.start()
    try:
        yield Mirror(engine, hub, gateway, pages, holder, etc, registry)
    finally:
        await gateway.stop()
        await pages.close()
        await engine.stop()


# --- CORE-252: URLs ---------------------------------------------------------------------


def test_core_63_shell_page_has_no_inline_code() -> None:
    page = shell_page("mirror")
    assert "<script>" not in page
    assert "<style" not in page
    assert " style=" not in page
    assert 'src="/core/shell.js"' in page
    assert 'data-mode="mirror"' in page


async def test_core_252_shell_and_engine_files(mirror: Mirror) -> None:
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        root = await c.get("/")
        assert root.status_code == 200
        assert "script-src 'self'" in root.headers["content-security-policy"]
        assert 'data-mode="mirror"' in root.text
        for path in ("/core/shell.js", "/core/mm3.js", "/core/theme.css", "/core/pair.js"):
            assert (await c.get(path)).status_code == 200, path
        lit = await c.get("/core/vendor/lit-html/lit-html.js")
        assert lit.status_code == 200
        font = await c.get("/core/vendor/roboto-condensed/roboto-condensed-latin-400-normal.woff2")
        assert font.status_code == 200
        for path in (
            "/core/vendor/vendor.json",
            "/core/vendor/lit-html/LICENSE",
            "/core/nope.js",
            "/core/..%2Fshell.py",
            "/core/vendor/..%2F..%2Fshell.py",
            "/elsewhere",
        ):
            assert (await c.get(path)).status_code == 404, path


async def test_core_12_elements_cannot_reach_other_origins(mirror: Mirror) -> None:
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        csp = (await c.get("/")).headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "default-src 'none'" in csp


async def test_core_252_module_files_are_versioned_and_immutable(mirror: Mirror) -> None:
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        ok = await c.get("/modules/shelltest/0/element.js")
        assert ok.status_code == 200
        assert ok.headers["cache-control"] == IMMUTABLE
        assert "mm3-shelltest" in ok.text
        for path in (
            "/modules/shelltest/9/element.js",
            "/modules/ghost/0/element.js",
            "/modules/shelltest/0/missing.js",
            "/modules/shelltest/0/..%2F__init__.py",
        ):
            assert (await c.get(path)).status_code == 404, path


async def test_core_252_appearance_css(mirror: Mirror) -> None:
    v = content_version(".temp { color: red; }")
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        versioned = await c.get(f"/appearance/home.css?v={v}")
        assert versioned.status_code == 200
        assert versioned.text == ".temp { color: red; }"
        assert versioned.headers["cache-control"] == IMMUTABLE
        stale = await c.get("/appearance/home.css?v=000")
        assert stale.status_code == 200
        assert "immutable" not in stale.headers.get("cache-control", "")
        assert (await c.get("/appearance/other.css")).status_code == 404
        assert (await c.get("/appearance/home.js")).status_code == 404


async def test_core_262_custom_css(mirror: Mirror) -> None:
    v = content_version(CUSTOM_CSS)
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        r = await c.get(f"/custom.css?v={v}")
        assert r.status_code == 200
        assert r.text == CUSTOM_CSS
        assert r.headers["content-type"].startswith("text/css")
        assert r.headers["cache-control"] == IMMUTABLE
        mirror.reload(CONFIG, css=None)
        assert (await c.get("/custom.css")).status_code == 404


async def test_core_252_preview_needs_an_admin_session(mirror: Mirror) -> None:
    auth = mirror.hub.auth
    rec = auth.tokens.verify(auth.tokens.new("me"), "admin")
    assert rec is not None
    cookie = auth.sessions.create(rec)
    async with httpx.AsyncClient(base_url=mirror.admin) as c:
        anon = await c.get("/admin/preview")
        assert anon.status_code == 303
        assert anon.headers["location"] == "/admin/login"
        c.cookies.set(ADMIN_COOKIE, cookie)
        ok = await c.get("/admin/preview")
        assert ok.status_code == 200
        assert 'data-mode="preview"' in ok.text
        assert (await c.get("/core/shell.js")).status_code == 200
        assert (await c.get("/modules/shelltest/0/element.js")).status_code == 200


# --- CORE-246, CORE-275: welcome and control frames -----------------------------------------


def hello() -> str:
    data = {
        "protocol": 1,
        "client_id": str(uuid.uuid4()),
        "role": "browser",
        "viewport": [1920, 1080],
        "dpr": 1,
    }
    return json.dumps({"v": 1, "kind": "hello", "data": data})


async def frames_until(ws: Any, pred: Any, wait: float = 3.0) -> dict[str, Any]:
    async with asyncio.timeout(wait):
        while True:
            frame: dict[str, Any] = json.loads(await ws.recv())
            if pred(frame):
                return frame


def control(action: str) -> Any:
    return lambda f: f["kind"] == "control" and f["data"]["action"] == action


async def test_core_246_welcome_entry_fields(mirror: Mirror) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws") as ws:
        await ws.send(hello())
        welcome = json.loads(await ws.recv())["data"]
        assert welcome["page"] == "a"
        assert welcome["custom_css"] == content_version(CUSTOM_CSS)
        config = mirror.holder["result"].config
        assert config is not None
        assert (welcome["timezone"], welcome["touch"]) == (config.engine.timezone, True)
        home = welcome["instances"][0]
        assert home["scripts"] == ["/modules/shelltest/0/element.js"]
        assert home["styles"] == ["/modules/shelltest/0/element.css"]
        assert home["status"] == "ok"
        app = home["appearance"]
        assert app["header"] == "Hello"
        assert app["css"] == f"/appearance/home.css?v={content_version('.temp { color: red; }')}"
        assert set(app) == {
            "scale",
            "align",
            "valign",
            "opacity",
            "overflow",
            "header",
            "classes",
            "css",
        }
        first = await frames_until(ws, control("page"))
        assert first["data"] == {
            "action": "page",
            "page": "a",
            "index": 0,
            "count": 2,
            "transition": "fade",
            "visible": ["home", "both", "also", "pad"],
        }


async def test_core_275_page_hide_status_and_reload(mirror: Mirror) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws") as ws:
        await ws.send(hello())
        await frames_until(ws, control("page"))
        mirror.pages.next("ha")
        page = await frames_until(ws, control("page"))
        assert (page["data"]["page"], page["data"]["visible"]) == ("b", ["other", "both", "also"])
        mirror.pages.set_manual("both", True)
        hide = await frames_until(ws, control("hide"))
        assert (hide["instance"], hide["data"]["fade"]) == ("both", 300)
        await mirror.engine.supervisor.get("other")._disable("test")
        status = await frames_until(ws, control("status"))
        assert (status["instance"], status["data"]["status"]) == ("other", "unavailable")
        result = mirror.reload(CONFIG, css="body { color: #fff; }\n")
        await mirror.hub.reloaded(result, ReloadPlan(custom_css=True))
        styles = await frames_until(ws, control("styles"))
        assert styles["data"]["custom_css"] == content_version("body { color: #fff; }\n")
        await mirror.hub.reloaded(result, ReloadPlan(relayout=["home"]))
        reload = await frames_until(ws, control("reload"))
        assert reload["data"]["scope"] == "resync"


def display_headers(mirror: Mirror) -> dict[str, str]:
    auth = mirror.hub.auth
    rec = auth.tokens.verify(auth.tokens.new("hall", "display"), "display")
    assert rec is not None
    return {"Cookie": f"{DISPLAY_COOKIE}={auth.sessions.create(rec)}"}


def touch(gesture: str) -> str:
    return json.dumps({"v": 1, "kind": "touch", "data": {"gesture": gesture}})


async def test_core_235_touch_frames_change_page(mirror: Mirror) -> None:
    url = f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws"
    async with websockets.connect(url, additional_headers=display_headers(mirror)) as ws:
        await ws.send(hello())
        await frames_until(ws, control("page"))
        await ws.send(touch("swipe_left"))
        page = await frames_until(ws, control("page"))
        assert page["data"]["page"] == "b"
        await ws.send(touch("pinch"))
        error = await frames_until(ws, lambda f: f["kind"] == "error")
        assert error["data"]["code"] == "invalid_frame"


async def test_core_239_preview_never_changes_page(mirror: Mirror) -> None:
    auth = mirror.hub.auth
    rec = auth.tokens.verify(auth.tokens.new("me"), "admin")
    assert rec is not None
    headers = {
        "Cookie": f"{ADMIN_COOKIE}={auth.sessions.create(rec)}",
        "Origin": mirror.admin,
    }
    url = f"ws://127.0.0.1:{mirror.gateway.admin.port}/admin/preview/ws"
    async with websockets.connect(url, additional_headers=headers) as ws:
        data = json.loads(hello())
        data["data"]["role"] = "preview"
        await ws.send(json.dumps(data))
        welcome = json.loads(await ws.recv())
        assert welcome["data"]["role"] == "preview"
        await ws.send(touch("swipe_left"))
        await ws.send(
            json.dumps({"v": 1, "kind": "visibility", "instance": "home", "data": {"hidden": True}})
        )
        await asyncio.sleep(0.2)
        await mirror.pages.idle()
    assert mirror.pages.page == "a"
    assert mirror.pages.visible("home")


# --- CORE-255..259: the shell in a browser ---------------------------------------------------


@pytest.fixture
async def browser() -> AsyncIterator[Any]:
    async_api = pytest.importorskip("playwright.async_api")
    async with async_api.async_playwright() as p:
        try:
            b = await p.chromium.launch(executable_path=os.environ.get("MM3_CHROMIUM") or None)
        except Exception as exc:  # no browser on this machine
            pytest.skip(f"Chromium not available: {exc}")
        try:
            yield b
        finally:
            await b.close()


async def text_of(page: Any, instance: str) -> str | None:
    return await page.evaluate(  # type: ignore[no-any-return]
        """(id) => {
            const el = document.querySelector(`[data-instance="${id}"] mm3-shelltest`);
            const t = el && el.shadowRoot.querySelector(".temp");
            return t ? t.textContent : null;
        }""",
        instance,
    )


async def gone(page: Any, instance: str) -> str:
    return await page.evaluate(  # type: ignore[no-any-return]
        '(id) => document.querySelector(`[data-instance="${id}"]`).dataset.gone', instance
    )


HOME = "document.querySelector('[data-instance=\"home\"] mm3-shelltest')"


def cell(instance: str) -> str:
    return f"document.querySelector('[data-instance=\"{instance}\"]')"


@pytest.fixture
async def shell(mirror: Mirror, browser: Any) -> AsyncIterator[Any]:
    """A paired display (interactive, CORE-248, CORE-254) showing the mirror."""
    auth = mirror.hub.auth
    rec = auth.tokens.verify(auth.tokens.new("hall", "display"), "display")
    assert rec is not None
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    await context.add_cookies(
        [{"name": DISPLAY_COOKIE, "value": auth.sessions.create(rec), "url": mirror.base}]
    )
    page = await context.new_page()
    problems: list[str] = []
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    await page.goto(mirror.base + "/")
    await page.wait_for_function(
        f"() => {HOME}?.shadowRoot.querySelector('.temp')?.textContent === '21'"
    )
    yield page
    assert problems == []
    await context.close()


@pytest.mark.browser
async def test_core_255_element_setup(shell: Any) -> None:
    sheets = await shell.evaluate(f"() => {HOME}.shadowRoot.adoptedStyleSheets.length")
    assert sheets == 3  # appearance variables, module styles, appearance.css
    color = await shell.evaluate(
        f"() => getComputedStyle({HOME}.shadowRoot.querySelector('.temp')).color"
    )
    assert color == "rgb(255, 0, 0)"


@pytest.mark.browser
async def test_core_256_header_outside_the_shadow_root(shell: Any) -> None:
    assert await shell.text_content('[data-instance="home"] .mm3-header') == "Hello"
    assert await shell.evaluate(f"() => {HOME}.shadowRoot.textContent.includes('Hello')") is False
    hidden = await shell.evaluate(
        f"() => getComputedStyle({cell('both')}.querySelector('.mm3-header')).display"
    )
    assert hidden == "none"


@pytest.mark.browser
async def test_core_95_on_resize_reports_whole_pixels(shell: Any) -> None:
    await shell.wait_for_function(f"() => {HOME}.size !== undefined")
    size = await shell.evaluate(f"() => {HOME}.size")
    assert size is not None
    assert all(isinstance(v, int) and v > 0 for v in size)
    await shell.set_viewport_size({"width": 1920, "height": 1080})
    await shell.wait_for_function(f"(w) => {HOME}.size[0] > w", arg=size[0])


@pytest.mark.browser
async def test_core_64_templates_escape_state(mirror: Mirror, shell: Any) -> None:
    await shell.evaluate(f"() => {HOME}.send('bump', {{n: '<b>bold</b>'}})")
    await shell.wait_for_function(
        f"() => {HOME}.shadowRoot.querySelector('.temp').textContent === '<b>bold</b>'"
    )
    assert await shell.evaluate(f"() => {HOME}.shadowRoot.querySelector('b')") is None


@pytest.mark.browser
async def test_core_237_page_change_switches_visible_instances(mirror: Mirror, shell: Any) -> None:
    assert await gone(shell, "other") == "true"
    href = await shell.get_attribute('link[href^="/custom.css"]', "href")
    assert href == f"/custom.css?v={content_version(CUSTOM_CSS)}"
    mirror.pages.next("ha")
    await shell.wait_for_function(
        f"() => {cell('home')}.dataset.gone === 'true'"
        f" && {cell('other')}.dataset.hidden === 'false'"
    )
    assert await text_of(shell, "other") == "21"


@pytest.mark.browser
async def test_core_259_hidden_elements_keep_their_state(mirror: Mirror, shell: Any) -> None:
    await shell.evaluate(f"() => {HOME}.send('bump', {{n: 5}})")
    await shell.wait_for_function(
        f"() => {HOME}.shadowRoot.querySelector('.temp').textContent === '5'"
    )
    await shell.evaluate(f"() => {{ window.__home = {HOME}; }}")
    mirror.pages.next("ha")
    await shell.wait_for_function(f"() => {cell('home')}.dataset.gone === 'true'")
    assert await text_of(shell, "home") == "5"
    assert await shell.evaluate(f"() => {HOME} === window.__home") is True


@pytest.mark.browser
async def test_core_276_element_hides_itself(mirror: Mirror, shell: Any) -> None:
    await shell.evaluate(f"() => {HOME}.hide({{fade: 0}})")
    await shell.wait_for_function(f"() => {cell('home')}.dataset.gone === 'true'")
    assert mirror.pages.reasons("home") == {"self"}
    await shell.evaluate(f"() => {HOME}.show()")
    await shell.wait_for_function(f"() => {cell('home')}.dataset.hidden === 'false'")


@pytest.mark.browser
async def test_core_117_failure_placeholder(mirror: Mirror, shell: Any) -> None:
    await mirror.engine.supervisor.get("both")._disable("test")
    await shell.wait_for_function(f"() => {cell('both')}.dataset.status === 'unavailable'")
    shown = await shell.evaluate(
        f"() => getComputedStyle({cell('both')}.querySelector('.mm3-failure')).display"
    )
    assert shown == "flex"


@pytest.mark.browser
async def test_core_250_resync_keeps_unchanged_elements(mirror: Mirror, shell: Any) -> None:
    await shell.evaluate(f"() => {{ window.__home = {HOME}; }}")
    mirror.hub.resync()
    await asyncio.sleep(0.5)
    await shell.wait_for_function(f"() => {HOME} === window.__home")
    assert len(mirror.hub.clients()) == 1


@pytest.mark.browser
async def test_core_261_theme_variables(shell: Any) -> None:
    style = await shell.evaluate(
        "() => { const s = getComputedStyle(document.documentElement);"
        " return [s.getPropertyValue('--mm3-color-bg').trim(),"
        " s.getPropertyValue('--mm3-font-family').trim()]; }"
    )
    assert style[0] == "#000"
    assert style[1].startswith('"Roboto Condensed"')


@pytest.mark.browser
async def test_core_103_pointer_events_follow_touch(shell: Any) -> None:
    events = await shell.evaluate(
        "() => getComputedStyle(document.getElementById('mm3-grid')).pointerEvents"
    )
    assert events == "auto"


@pytest.mark.browser
async def test_core_260_swipe_changes_page(mirror: Mirror, shell: Any) -> None:
    await shell.mouse.move(1000, 300)
    await shell.mouse.down()
    await shell.mouse.move(600, 320, steps=5)
    await shell.mouse.up()
    await shell.wait_for_function(f"() => {cell('other')}.dataset.hidden === 'false'")
    assert mirror.pages.page == "b"


@pytest.mark.browser
async def test_core_257_stack_splits_the_preset_among_visible_instances(
    mirror: Mirror, shell: Any
) -> None:
    async def heights() -> list[float]:
        return await shell.evaluate(  # type: ignore[no-any-return]
            f"() => [{cell('both')}, {cell('also')}].map((c) => c.getBoundingClientRect().height)"
        )

    async def tops() -> list[float]:
        return await shell.evaluate(  # type: ignore[no-any-return]
            f"() => [{cell('both')}, {cell('also')}].map((c) => c.getBoundingClientRect().top)"
        )

    await shell.wait_for_function(f"() => {cell('also')}.dataset.hidden === 'false'")
    both, also = await heights()
    assert abs(both - also) < 1
    top_both, top_also = await tops()
    assert abs(top_also - (top_both + both)) < 1
    mirror.pages.set_manual("both", True)
    await shell.wait_for_function(f"() => {cell('both')}.dataset.gone === 'true'")
    _, alone = await heights()
    assert abs(alone - 2 * also) < 1


@pytest.mark.browser
async def test_core_258_fit_scales_down_and_scroll_scrolls(mirror: Mirror, shell: Any) -> None:
    also = f"{cell('also')}.querySelector('.mm3-fit')"
    read = f"() => parseFloat({also}.style.transform.slice(6)) || 1"
    before = await shell.evaluate(read)
    assert 0 < before <= 1  # never scaled up
    await shell.evaluate(
        f"() => {cell('also')}.querySelector('mm3-shelltest').send('bump', {{n: 'x'.repeat(3000)}})"
    )
    await shell.wait_for_function(f"(b) => ({read})() < b / 2", arg=before)
    overflow = await shell.evaluate(
        f"() => getComputedStyle({cell('both')}.querySelector('.mm3-area')).overflowY"
    )
    assert overflow == "auto"


# --- part D: catalogs in the shell (core spec 8) ---------------------------------------------


async def welcome_of(mirror: Mirror) -> dict[str, Any]:
    async with websockets.connect(f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws") as ws:
        await ws.send(hello())
        data: dict[str, Any] = json.loads(await ws.recv())["data"]
    return data


async def test_core_264_catalog_url_and_version(mirror: Mirror) -> None:
    welcome = await welcome_of(mirror)
    entries = {i["id"]: i for i in welcome["instances"]}
    also = entries["also"]
    version, raw = mirror.engine.i18n.merged("shelltest", ["it", "en"])
    assert also["catalog"] == version
    assert entries["home"]["catalog"] != version  # en chain
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        r = await c.get(f"/i18n/also.json?v={version}")
        assert r.status_code == 200
        assert r.headers["cache-control"] == IMMUTABLE
        data = r.json()
        assert data["FILES"] == {
            "one": "{count} file",
            "many": "{count} file",
            "other": "{count} file",
        }
        assert data["CORE_SHELL_UNAVAILABLE"] == "Non disponibile"
        assert r.content == raw
        stale = await c.get("/i18n/also.json?v=old")
        assert "immutable" not in stale.headers.get("cache-control", "")
        assert (await c.get("/i18n/ghost.json")).status_code == 404
        assert (await c.get("/i18n/also.txt")).status_code == 404


async def test_mod_67_header_from_the_catalog(mirror: Mirror) -> None:
    welcome = await welcome_of(mirror)
    entries = {i["id"]: i for i in welcome["instances"]}
    assert entries["other"]["appearance"]["header"] == "Meteo"
    assert entries["pad"]["appearance"]["header"] == "Weather"  # same package catalog, en
    assert entries["home"]["appearance"]["header"] == "Hello"


async def test_core_246_touch_flag_per_instance(mirror: Mirror) -> None:
    welcome = await welcome_of(mirror)
    flags = {i["id"]: i["touch"] for i in welcome["instances"]}
    assert flags == {
        "home": False,
        "other": False,
        "both": False,
        "also": False,
        "pad": True,
    }


async def test_core_07_backend_translation_and_formatting(mirror: Mirror) -> None:
    ctx = mirror.engine.supervisor.get("also").context
    assert ctx is not None
    assert ctx.t("FILES", count=3) == "3 file"
    assert ctx.t("HEADER") == "Meteo"
    assert ctx.t("CORE_SHELL_UNAVAILABLE") == "Non disponibile"
    assert ctx.format_number(21.5, unit="celsius") == "21,5 °C"
    home = mirror.engine.supervisor.get("home").context
    assert home is not None
    assert home.t("FILES", count=1) == "1 file"
    assert home.t("FILES", count=2) == "2 files"


@pytest.mark.browser
async def test_core_265_element_text_and_plurals(shell: Any) -> None:
    async def label(instance: str) -> str:
        return await shell.evaluate(  # type: ignore[no-any-return]
            f"() => {cell(instance)}.querySelector('mm3-shelltest')"
            ".shadowRoot.querySelector('.label').textContent"
        )

    assert await label("home") == "2 files"
    assert await label("also") == "2 file"
    missing = await shell.evaluate(f"() => {HOME}.t('NOPE')")
    assert missing == "NOPE"


@pytest.mark.browser
async def test_core_266_fmt_date_uses_the_engine_time_zone(mirror: Mirror, shell: Any) -> None:
    config = mirror.holder["result"].config
    assert config is not None
    when = "new Date(Date.UTC(2026, 9, 29, 23, 30))"
    got = await shell.evaluate(f"() => {HOME}.fmtDate({when}, 'time')")
    ctx = mirror.engine.supervisor.get("home").context
    assert ctx is not None
    expected = ctx.format_date(datetime(2026, 10, 29, 23, 30, tzinfo=UTC), "time")
    assert got.replace("\u202f", " ") == expected.replace("\u202f", " ")
    number = await shell.evaluate(
        f"() => {cell('also')}.querySelector('mm3-shelltest').fmtNumber(21.5, 'celsius')"
    )
    assert number.replace("\u202f", " ").replace("\xa0", " ") == "21,5 °C"


@pytest.mark.browser
async def test_core_267_locale_change_refetches_the_catalog(mirror: Mirror, shell: Any) -> None:
    old = mirror.holder["result"]
    text = CONFIG.replace(
        "    id: both\n    position: bottom_bar\n",
        "    id: both\n    position: bottom_bar\n    locale: de\n",
    )
    new = mirror.reload(text)
    plan = plan_reload(old, new)
    assert plan.locale == {"both": "de"}
    await mirror.hub.reloaded(new, plan)
    await shell.wait_for_function(
        f"() => {cell('both')}.querySelector('mm3-shelltest')"
        ".shadowRoot.querySelector('.label').textContent === '2 Dateien'"
    )
    assert (
        await shell.evaluate(f"() => {cell('both')}.querySelector('mm3-shelltest').locale") == "de"
    )


@pytest.mark.browser
async def test_core_117_placeholder_is_localized(mirror: Mirror, shell: Any) -> None:
    await mirror.engine.supervisor.get("also")._disable("test")
    await shell.wait_for_function(f"() => {cell('also')}.dataset.status === 'unavailable'")
    text = await shell.text_content('[data-instance="also"] .mm3-failure')
    assert text == "Non disponibile"


@pytest.mark.browser
async def test_core_235_swipe_on_a_touch_element_stays_with_it(mirror: Mirror, shell: Any) -> None:
    box = await shell.evaluate(
        f"() => {{ const r = {cell('pad')}.getBoundingClientRect();"
        " return [r.left, r.top, r.width, r.height]; }"
    )
    x, y = box[0] + box[2] - 2, box[1] + box[3] / 2
    await shell.mouse.move(x, y)
    await shell.mouse.down()
    await shell.mouse.move(x - 600, y, steps=5)
    await shell.mouse.up()
    await asyncio.sleep(0.5)
    await mirror.pages.idle()
    assert mirror.pages.page == "a"
