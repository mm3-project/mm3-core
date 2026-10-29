"""Milestone 3, part C: the shell, its files and control frames (core spec 5.6, 6.8-6.10)."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from mm3.core.config import load_config
from mm3.core.config.diff import ReloadPlan
from mm3.core.config.validate import ConfigResult
from mm3.core.engine import Engine
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
"""

ELEMENT = """
import { MirrorElement, html } from "/core/mm3.js";

class ShellTest extends MirrorElement {
  render(state) {
    return html`<div class="temp">${state.temp ?? "-"}</div>`;
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
  - module: shelltest
    id: both
    position: bottom_bar
"""

CUSTOM_CSS = "body { --mm3-color-fg: #bbb; }\n"


@pytest.fixture(scope="module")
def shell_module(tmp_path_factory: pytest.TempPathFactory) -> type[Any]:
    root = tmp_path_factory.mktemp("pkg")
    pkg = root / "mm3_shelltest"
    (pkg / "static").mkdir(parents=True)
    (pkg / "__init__.py").write_text(PACKAGE)
    (pkg / "static" / "element.js").write_text(ELEMENT)
    (pkg / "static" / "element.css").write_text(":host { color: var(--mm3-color-bright); }\n")
    sys.path.insert(0, str(root))
    try:
        cls: type[Any] = importlib.import_module("mm3_shelltest").ShellTest
    finally:
        sys.path.remove(str(root))
    return cls


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
async def mirror(tmp_path: Path, shell_module: type[Any]) -> AsyncIterator[Mirror]:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.yaml").write_text(CONFIG)
    (etc / "custom.css").write_text(CUSTOM_CSS)
    registry = Registry()
    registry.add(shell_module)
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
    view = cached_view(lambda: holder["result"], registry)
    hub = ClientHub(view, WebAuth.open(tmp_path / "state"), "1.0+test", hello_timeout=1)
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    sources = ShellSources(view, lambda: holder["result"])
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
        assert (welcome["timezone"], welcome["touch"]) == (config.engine.timezone, False)
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
            "visible": ["home", "both"],
        }


async def test_core_275_page_hide_status_and_reload(mirror: Mirror) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws") as ws:
        await ws.send(hello())
        await frames_until(ws, control("page"))
        mirror.pages.next("ha")
        page = await frames_until(ws, control("page"))
        assert (page["data"]["page"], page["data"]["visible"]) == ("b", ["other", "both"])
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


@pytest.mark.browser
async def test_core_255_shell_renders_pages_and_state(mirror: Mirror, browser: Any) -> None:
    # A paired display is interactive (CORE-248, CORE-254).
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
        """() => {
            const el = document.querySelector('[data-instance="home"] mm3-shelltest');
            return el && el.shadowRoot.querySelector('.temp')?.textContent === '21';
        }"""
    )
    # CORE-256: header outside the shadow root; CORE-255: styles adopted.
    header = await page.text_content('[data-instance="home"] .mm3-header')
    assert header == "Hello"
    sheets = await page.evaluate(
        "() => document.querySelector('[data-instance=\"home\"] mm3-shelltest')"
        ".shadowRoot.adoptedStyleSheets.length"
    )
    assert sheets == 3  # variables, module styles, appearance.css
    color = await page.evaluate(
        "() => getComputedStyle(document.querySelector('[data-instance=\"home\"] mm3-shelltest')"
        ".shadowRoot.querySelector('.temp')).color"
    )
    assert color == "rgb(255, 0, 0)"
    size = await page.evaluate(
        "() => document.querySelector('[data-instance=\"home\"] mm3-shelltest').size"
    )
    assert size is not None
    assert size[0] > 0
    assert await gone(page, "other") == "true"
    # CORE-262: custom.css loaded with its version.
    href = await page.get_attribute('link[href^="/custom.css"]', "href")
    assert href == f"/custom.css?v={content_version(CUSTOM_CSS)}"
    # Element -> backend -> state (MOD send, CORE-249).
    await page.evaluate(
        "() => document.querySelector('[data-instance=\"home\"] mm3-shelltest')"
        ".send('bump', {n: 5})"
    )
    await page.wait_for_function(
        "() => document.querySelector('[data-instance=\"home\"] mm3-shelltest')"
        ".shadowRoot.querySelector('.temp').textContent === '5'"
    )
    # CORE-237: page change hides home and shows other.
    mirror.pages.next("ha")
    await page.wait_for_function(
        "() => document.querySelector('[data-instance=\"home\"]').dataset.gone === 'true'"
        " && document.querySelector('[data-instance=\"other\"]').dataset.hidden === 'false'"
    )
    assert await text_of(page, "other") == "21"
    # CORE-259: a hidden element keeps its state.
    assert await text_of(page, "home") == "5"
    # CORE-117: failure placeholder.
    await mirror.engine.supervisor.get("both")._disable("test")
    await page.wait_for_function(
        "() => document.querySelector('[data-instance=\"both\"]').dataset.status === 'unavailable'"
    )
    # CORE-275: resync rebuilds from a new welcome and keeps unchanged elements.
    await page.evaluate(
        "() => { window.__home = "
        "document.querySelector('[data-instance=\"home\"] mm3-shelltest'); }"
    )
    mirror.hub.resync()
    await asyncio.sleep(0.5)
    await page.wait_for_function(
        "() => document.querySelector('[data-instance=\"home\"] mm3-shelltest') === window.__home"
    )
    assert problems == []
