"""Milestone 6, part D3: admin extension pages (CORE-329..331, MOD-38, MOD-39)."""

from __future__ import annotations

import html
import importlib
import io
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm3.core.config import load_config
from mm3.core.config.editor import ConfigEditor
from mm3.core.config.history import ConfigHistory
from mm3.core.config.manager import ConfigManager
from mm3.core.engine import Engine
from mm3.core.i18n import compile_locales
from mm3.core.logging import configure_logging, recent_records
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.security import CSP
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.shell import ShellSources
from mm3.core.web.view import cached_view
from mm3.testing import VirtualClock
from test_i18n import write_catalog

ADMIN = "http://127.0.0.1:8081"

PACKAGE = """
from typing import Any
from mm3.module import MirrorModule


class ExtMod(MirrorModule):
    name = "extmod"
    element = "mm3-extmod"
    scripts = ["element.js"]
    admin_element = "mm3-extmod-admin"
    admin_scripts = ["admin.js"]
    capabilities = frozenset({"admin.extension", "api.routes"})

    async def start(self) -> None:
        iid = self.ctx.instance_id

        @self.ctx.router.get("/ping")
        async def ping() -> dict[str, Any]:
            return {"pong": iid}


class Undeclared(MirrorModule):
    name = "undeclared"
    admin_element = "mm3-undeclared-admin"
    admin_scripts = ["admin.js"]


class Unused(MirrorModule):
    name = "unused"
    admin_element = "mm3-unused-admin"
    admin_scripts = ["admin.js"]
    capabilities = frozenset({"admin.extension"})
"""

ELEMENT = """
import { MirrorElement, html } from "/core/mm3.js";
class ExtMod extends MirrorElement {
  render() { return html`<div>mirror</div>`; }
}
customElements.define("mm3-extmod", ExtMod);
"""

ADMIN_JS = """
import { html, render } from "/core/mm3.js";
import { helper } from "./helper.js";

class ExtModAdmin extends HTMLElement {
  async connectedCallback() {
    const [first] = this.instances;
    const pong = await this.api.module(first.id).get("ping");
    const status = await this.api.get("status");
    let code = "";
    try {
      await this.api.get("nothing/here");
    } catch (err) {
      code = `${err.status} ${err.code}`;
    }
    render(
      html`<h2 class="title">${this.t("HEADER")}</h2>
        <p class="hello">${this.t("ADMIN_HELLO", { count: this.instances.length })}</p>
        <p class="pong">${pong.pong}</p>
        <p class="status">${typeof status.uptime_s}</p>
        <p class="error">${code}</p>
        <p class="meta">${this.module} ${this.locale} ${first.enabled} ${helper()}</p>`,
      this,
    );
  }
}
customElements.define("mm3-extmod-admin", ExtModAdmin);
"""

HELPER = "export function helper() { return 'helped'; }\n"

CATALOGS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "en": {"HEADER": "Ext tools", "ADMIN_HELLO": ("{count} instance", "{count} instances")},
    "it": {"HEADER": "Strumenti", "ADMIN_HELLO": ("{count} istanza", "{count} istanze")},
    "fr": {"HEADER": "Outils", "ADMIN_HELLO": ("{count} instance", "{count} instances")},
    "es": {"HEADER": "Herramientas", "ADMIN_HELLO": ("{count} instancia", "{count} instancias")},
    "de": {"HEADER": "Werkzeuge", "ADMIN_HELLO": ("{count} Instanz", "{count} Instanzen")},
}

CONFIG = """
engine:
  locale: en
modules:
  - module: extmod
    id: x
    position: top_left
  - module: undeclared
    id: u
"""


@pytest.fixture(scope="module")
def ext_module(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("extpkg")
    pkg = root / "mm3_extmod"
    (pkg / "static").mkdir(parents=True)
    (pkg / "__init__.py").write_text(PACKAGE)
    (pkg / "static" / "element.js").write_text(ELEMENT)
    (pkg / "static" / "admin.js").write_text(ADMIN_JS)
    (pkg / "static" / "helper.js").write_text(HELPER)
    for lang, entries in CATALOGS.items():
        write_catalog(pkg / "locales", lang, entries)
    compile_locales(pkg / "locales")
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("mm3_extmod")
    finally:
        sys.path.remove(str(root))


@dataclass
class World:
    engine: Engine
    auth: WebAuth
    token: str
    services: ApiServices
    sources: ShellSources

    def admin(self) -> httpx.AsyncClient:
        app = protect(admin_app(self.auth, api=self.services), lambda: [], "admin")
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)),
            base_url=ADMIN,
            headers={"authorization": f"Bearer {self.token}"},
        )


def registry(mod: Any) -> Registry:
    reg = Registry()
    for cls in (mod.ExtMod, mod.Undeclared, mod.Unused):
        reg.add(cls)
    return reg


@pytest.fixture
async def world(tmp_path: Path, ext_module: Any) -> AsyncIterator[World]:
    configure_logging("info", stream=io.StringIO(), json=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.yaml").write_text(CONFIG)
    env = tmp_path / "mm3.env"
    env.write_text("")
    env.chmod(0o600)
    reg = registry(ext_module)
    result = load_config(etc / "config.yaml", registry=reg, env_file=env, environ={})
    assert result.ok, result.records
    history = ConfigHistory(
        etc / "config.yaml", env_file=env, registry=reg, state_dir=tmp_path / "state", environ={}
    )
    with history.store.lock():
        history.record_disk()
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    )
    engine = Engine(settings, registry=reg, clock=VirtualClock(), state_dir=tmp_path / "state")
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    services = ApiServices(engine, manager, editor=ConfigEditor(history))
    view = cached_view(lambda: manager.current, reg, i18n=engine.i18n)
    sources = ShellSources(view, lambda: manager.current, engine.i18n)
    auth = WebAuth.open(tmp_path / "web")
    await engine.start(result.instance_specs())
    try:
        yield World(engine, auth, auth.tokens.new("t"), services, sources)
    finally:
        await engine.stop()


def page_data(text: str) -> dict[str, Any]:
    m = re.search(r'<mm3-admin-ext data-ext="([^"]*)">', text)
    assert m is not None, text
    data: dict[str, Any] = json.loads(html.unescape(m.group(1)))
    return data


# --- CORE-329: pages and files -----------------------------------------------------------


async def test_core_329_page_for_a_module_with_capability_and_instance(world: World) -> None:
    async with world.admin() as c:
        r = await c.get("/admin/ext/extmod")
        assert r.status_code == 200
        assert r.headers["content-security-policy"] == CSP
        assert "<script>" not in r.text
        assert 'src="/admin/static/ext.js"' in r.text
        data = page_data(r.text)
        assert data["module"] == "extmod"
        assert data["element"] == "mm3-extmod-admin"
        assert data["scripts"] == ["admin.js"]
        assert data["base"].startswith("/admin/ext/extmod/")
        assert data["instances"] == [{"id": "x", "enabled": True, "state": "RUNNING"}]
        # Without the capability, or without an instance: no page.
        assert (await c.get("/admin/ext/undeclared")).status_code == 404
        assert (await c.get("/admin/ext/unused")).status_code == 404
        assert (await c.get("/admin/ext/nobody")).status_code == 404
    events = [r for r in recent_records() if r.get("event") == "admin_extension_undeclared"]
    assert events
    assert events[0]["module"] == "undeclared"


async def test_core_329_header_links_with_the_header_text(world: World) -> None:
    async with world.admin() as c:
        for path in ("/admin/config", "/admin/debug", "/admin/ext/extmod"):
            text = (await c.get(path)).text
            assert '<a href="/admin/ext/extmod"' in text, path
            assert ">Ext tools</a>" in text
            assert "/admin/ext/undeclared" not in text


async def test_core_329_files_only_on_the_admin_page(world: World) -> None:
    async with world.admin() as c:
        base = page_data((await c.get("/admin/ext/extmod")).text)["base"]
        r = await c.get(base + "admin.js")
        assert r.status_code == 200
        assert "mm3-extmod-admin" in r.text
        assert "immutable" in r.headers["cache-control"]
        assert (await c.get(base + "helper.js")).status_code == 200
        assert (await c.get("/admin/ext/extmod/0.0.bad/admin.js")).status_code == 404
        # A path that leaves static/ (encoded, so the client does not normalise it).
        assert (await c.get(base + "..%2F__init__.py")).status_code == 404
        assert (await c.get(base + "admin.js", headers={"authorization": ""})).status_code == 401
    # The shell's /modules/ route leaves the admin scripts out; the mirror has no /admin/ext.
    version = base.rstrip("/").rsplit("/", 1)[1]
    mirror = protect(mirror_app(world.auth, sources=world.sources), lambda: [], "mirror")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mirror, client=("127.0.0.1", 5)), base_url="http://m"
    ) as m:
        assert (await m.get(f"/modules/extmod/{version}/element.js")).status_code == 200
        assert (await m.get(f"/modules/extmod/{version}/admin.js")).status_code == 404
        assert (await m.get(f"/admin/ext/extmod/{version}/admin.js")).status_code == 404


async def test_core_330_texts_in_the_admin_language(world: World) -> None:
    async with world.admin() as c:
        r = await c.get("/admin/ext/extmod/i18n.json")
        assert r.status_code == 200
        data = r.json()
        assert data["HEADER"] == "Ext tools"
        assert data["ADMIN_HELLO"]["other"] == "{count} instances"
        assert "CORE_ADMIN_SAVE" in data  # the core catalog behind the module's
        assert (await c.get("/admin/ext/unused/i18n.json")).status_code == 404


async def test_core_329_form_view_lists_the_capabilities(world: World) -> None:
    async with world.admin() as c:
        schema = (await c.get("/api/v1/config/schema")).json()
    assert schema["x-mm3-capabilities"]["extmod"] == ["admin.extension", "api.routes"]
    assert schema["x-mm3-capabilities"]["undeclared"] == []


# --- the page in Chromium ----------------------------------------------------------------


def chromium() -> str | None:
    try:
        from module_projects import chromium_available
    except ImportError:  # pragma: no cover
        return None
    return os.environ.get("MM3_CHROMIUM", "") if chromium_available() else None


async def test_core_331_admin_element_with_api_and_texts(world: World, tmp_path: Path) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    settings = EngineSettings().model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    gateway = WebGateway(
        settings, tmp_path / "gw", auth=world.auth, api=world.services, sources=world.sources
    )
    await gateway.start()
    rec = world.auth.tokens.verify(world.token, "admin")
    assert rec is not None
    sid = world.auth.sessions.create(rec)
    base = f"http://127.0.0.1:{gateway.admin.port}"
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path=exe or None)
    problems: list[str] = []
    try:
        context = await browser.new_context()
        await context.add_cookies([{"name": ADMIN_COOKIE, "value": sid, "url": base}])
        page = await context.new_page()
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        await page.goto(f"{base}/admin/ext/extmod")
        await page.wait_for_selector("mm3-extmod-admin .title")
        assert await page.inner_text("mm3-extmod-admin .title") == "Ext tools"
        assert await page.inner_text("mm3-extmod-admin .hello") == "1 instance"
        assert await page.inner_text("mm3-extmod-admin .pong") == "x"
        assert await page.inner_text("mm3-extmod-admin .status") == "number"
        assert await page.inner_text("mm3-extmod-admin .error") == "404 CORE_API_NOT_FOUND"
        assert await page.inner_text("mm3-extmod-admin .meta") == "extmod en true helped"
        # The Form view shows the capabilities of the instance (MOD-71).
        await page.goto(f"{base}/admin/config")
        await page.locator("nav.sections button", has_text=re.compile(r"^\W*x$")).click()
        await page.wait_for_selector(".capabilities >> text=admin.extension")
    finally:
        await browser.close()
        await pw.stop()
        await gateway.stop()
    # The probe of a missing path shows up as one failed request in the console.
    assert all("404" in p for p in problems), problems


async def test_core_102_declared_element_and_scripts_reach_the_admin_page(
    world: World, ext_module: Any
) -> None:
    cls = ext_module.ExtMod
    async with world.admin() as c:
        data = page_data((await c.get("/admin/ext/extmod")).text)
        assert (data["element"], data["scripts"]) == (cls.admin_element, cls.admin_scripts)
        for script in cls.admin_scripts:
            assert (await c.get(data["base"] + script)).status_code == 200
    # Modules declare nothing by default.
    from mm3.module import MirrorModule

    assert (MirrorModule.admin_element, MirrorModule.admin_scripts) == (None, [])
