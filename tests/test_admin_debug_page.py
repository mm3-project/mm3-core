"""Milestone 6, part B2: the admin debug page /admin/debug (core spec 10.5)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm3.core.config import load_config
from mm3.core.config.manager import ConfigManager
from mm3.core.diag.inspector import BusInspector
from mm3.core.engine import Engine
from mm3.core.i18n.catalogs import CORE_CATALOG, Table
from mm3.core.logging import configure_logging, get_logger
from mm3.core.pages import PageService
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.adminws import AdminChannel
from mm3.core.web.api import ApiServices, status_object
from mm3.core.web.apps import ADMIN_STATIC_DIR, admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.pages import admin_texts
from mm3.core.web.security import CSP
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.view import cached_view
from mm3.core.web.ws import BROWSER_LOGS_PER_MINUTE, ClientHub, _Client
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"
LOOPBACK = ("127.0.0.1", 50000)
LANGS = ("en", "it", "fr", "es", "de")


class Panel(MirrorModule):
    name = "panel"
    element = "mm3-panel"


@pytest.fixture
def auth(tmp_path: Path) -> WebAuth:
    return WebAuth.open(tmp_path / "web")


def client(app: Any, base: str = ADMIN) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=LOOPBACK), base_url=base)


def session_cookie(auth: WebAuth) -> dict[str, str]:
    token = auth.tokens.new("page")
    rec = auth.tokens.verify(token, "admin")
    assert rec is not None
    return {ADMIN_COOKIE: auth.sessions.create(rec)}


# --- the page and its files ------------------------------------------------------------


async def test_debug_page_needs_a_session_and_has_no_inline_code(auth: WebAuth) -> None:
    app = protect(admin_app(auth), lambda: [], "admin")
    async with client(app) as c:
        r = await c.get("/admin/debug")
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login?next=/admin/debug"
        c.cookies.update(session_cookie(auth))
        page = await c.get("/admin/debug")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == CSP
    text = page.text
    assert '<script type="module" src="/admin/static/debug.js"></script>' in text
    assert '<link rel="stylesheet" href="/admin/static/admin.css">' in text
    assert "<mm3-admin-debug>" in text
    # CORE-63: no inline script or style anywhere.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert "<style" not in text
    assert " style=" not in text


async def test_admin_static_files_only_on_the_admin_listener(auth: WebAuth) -> None:
    admin = protect(admin_app(auth), lambda: [], "admin")
    async with client(admin) as c:
        for name, kind in (
            ("debug.js", "javascript"),
            ("home.js", "javascript"),
            ("status.js", "javascript"),
            ("admin.js", "javascript"),
            ("admin.css", "css"),
            ("lit-html.js", "javascript"),
        ):
            r = await c.get(f"/admin/static/{name}")
            assert r.status_code == 200, name
            assert kind in r.headers["content-type"]
        assert (await c.get("/admin/static/../apps.py")).status_code == 404
        assert (await c.get("/admin/static/nope.js")).status_code == 404
    mirror = protect(mirror_app(auth), lambda: [], "mirror")
    async with client(mirror, "http://127.0.0.1:8080") as c:
        assert (await c.get("/admin/static/debug.js")).status_code == 404
        assert (await c.get("/admin/debug")).status_code == 404


def test_admin_scripts_use_no_inline_styles_or_eval() -> None:
    for path in ADMIN_STATIC_DIR.glob("*.js"):
        source = path.read_text()
        assert "eval(" not in source
        assert "new Function" not in source
        assert "style=" not in source, path.name  # CORE-63: classes only
        assert "innerHTML" not in source


async def test_admin_texts_follow_the_engine_locale(auth: WebAuth) -> None:
    app = protect(admin_app(auth, locale=lambda: "it"), lambda: [], "admin")
    async with client(app) as c:
        assert (await c.get("/admin/i18n.json")).status_code == 401
        c.cookies.update(session_cookie(auth))
        data = (await c.get("/admin/i18n.json")).json()
    assert data["locale"] == "it"
    assert data["messages"]["CORE_ADMIN_TAB_OVERVIEW"] == "Panoramica"
    assert data["messages"]["CORE_API_NOT_FOUND"] == "Non trovato."  # CORE-298 code texts
    # CORE-206: record codes are catalog keys, so the admin shows them localized (ISS-010).
    assert data["messages"]["CORE_CFG_REQUIRED"] == "Questo valore è obbligatorio"
    assert admin_texts("pt-BR")["CORE_ADMIN_TAB_OVERVIEW"] == "Overview"  # English fallback


def test_every_admin_key_is_in_five_languages() -> None:
    used: set[str] = set()
    for path in ADMIN_STATIC_DIR.glob("*.js"):
        used.update(
            k for k in re.findall(r"CORE_ADMIN_[A-Z_]+", path.read_text()) if not k.endswith("_")
        )
    assert len(used) > 40
    table = Table(CORE_CATALOG)
    for lang in LANGS:
        missing = used - set(table.get(lang))
        assert not missing, (lang, sorted(missing))


# --- CORE-114: browser errors ----------------------------------------------------------


def test_core_114_browser_logs_rate_limited_per_instance(tmp_path: Path, auth: WebAuth) -> None:
    configure_logging("info", stream=(out := io.StringIO()), json=True)
    reg = Registry()
    reg.add(Panel)
    (tmp_path / "config.yaml").write_text("modules:\n  - module: panel\n    position: top_left\n")
    result = load_config(tmp_path / "config.yaml", registry=reg, env_file=tmp_path / "x.env")
    hub = ClientHub(cached_view(lambda: result, reg), auth, "1.0+t")
    shell = _Client(ws=None, client_id="c1", role="kiosk", interactive=True, instances=set())  # type: ignore[arg-type]
    for i in range(BROWSER_LOGS_PER_MINUTE + 5):
        frame = {"level": "error", "message": f"boom {i}", "stack": "at x", "user_agent": "UA"}
        hub._log_frame(shell, "panel", frame)
    hub._log_frame(shell, "other", {"level": "error", "message": "elsewhere"})
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    logged = [r for r in records if r["event"] == "browser_log"]
    limited = [r for r in records if r["event"] == "browser_log_limited"]
    assert len([r for r in logged if r["instance"] == "panel"]) == BROWSER_LOGS_PER_MINUTE
    assert len(limited) == 1
    assert any(r["instance"] == "other" for r in logged)
    first = logged[0]
    assert first["source"] == "browser"
    assert first["stack"] == "at x"
    assert first["user_agent"] == "UA"


def test_core_114_elements_send_stack_and_user_agent() -> None:
    mm3 = (ADMIN_STATIC_DIR.parent / "static" / "mm3.js").read_text()
    fail = mm3[mm3.index("_fail(where, err)") :]
    assert "stack:" in fail[:600]
    assert "user_agent: navigator.userAgent" in fail[:600]


# --- the page in Chromium --------------------------------------------------------------


def chromium() -> str | None:
    try:
        from module_projects import chromium_available
    except ImportError:  # pragma: no cover
        return None
    return os.environ.get("MM3_CHROMIUM", "") if chromium_available() else None


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[tuple[WebGateway, WebAuth, Engine]]:
    configure_logging("info", stream=io.StringIO(), json=True)
    reg = Registry()
    reg.add(Panel)
    (tmp_path / "config.yaml").write_text(
        "modules:\n  - module: panel\n    id: one\n    position: top_left\n"
    )
    result = load_config(tmp_path / "config.yaml", registry=reg, env_file=tmp_path / "x.env")
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    ).model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    engine = Engine(settings, registry=reg, clock=VirtualClock(), state_dir=tmp_path / "state")
    auth = WebAuth.open(tmp_path / "web")
    hub = ClientHub(cached_view(lambda: result, reg), auth, "1.0+t")
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, engine.clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    manager = ConfigManager(engine, result, registry=reg, env_file=tmp_path / "x.env")
    services = ApiServices(engine, manager, pages, hub, None, "1.0+t")
    inspector = BusInspector()
    engine.bus.add_endpoint(inspector)
    channel = AdminChannel(auth, lambda: status_object(services), inspector, interval=0.1)
    gateway = WebGateway(settings, tmp_path, hub=hub, api=services, channel=channel)
    await engine.start(result.instance_specs())
    await pages.start()
    await gateway.start()
    try:
        yield gateway, auth, engine
    finally:
        await gateway.stop()
        await pages.close()
        await engine.stop()


async def until_shown(
    page: Any, selector: str, emit: Callable[[], Awaitable[None] | None], limit: float = 10.0
) -> None:
    """Emit an event every 250 ms until ``selector`` shows, for at most ``limit`` s."""
    deadline = asyncio.get_running_loop().time() + limit
    while True:
        result = emit()
        if result is not None:
            await result
        try:
            await page.wait_for_selector(selector, timeout=250)
            return
        except Exception:
            if asyncio.get_running_loop().time() > deadline:
                raise


async def test_debug_page_renders_in_chromium(
    running: tuple[WebGateway, WebAuth, Engine],
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, _engine = running
    base = f"http://127.0.0.1:{gateway.admin.port}"
    cookie = session_cookie(auth)[ADMIN_COOKIE]
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=exe or None)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [{"name": ADMIN_COOKIE, "value": cookie, "url": base, "httpOnly": True}]
            )
            page = await context.new_page()
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.on(
                "response",
                lambda r: problems.append(f"{r.status} {r.url}") if r.status >= 400 else None,
            )
            await page.goto(f"{base}/admin/debug")
            await page.wait_for_selector("text=Overview")
            await page.wait_for_selector("td >> text=one")
            await page.wait_for_selector(".live.on")
            await page.click("td >> text=one")
            await page.wait_for_selector("text=Health record")
            await page.click("text=Live logs")
            # A subscribe on /admin/ws has no acknowledgement, so the test
            # repeats the event until the open tab shows it.
            await until_shown(
                page,
                "text=seen_in_browser",
                lambda: get_logger("panel", "one").warning("seen_in_browser"),
            )
            await page.click("text=Bus inspector")
            async with httpx.AsyncClient(base_url=base, cookies={ADMIN_COOKIE: cookie}) as c:

                async def notify() -> None:
                    r = await c.post(
                        "/api/v1/notify",
                        json={"name": "PAGE_TEST", "payload": {}},
                        headers={"origin": base},
                    )
                    assert r.status_code == 200

                await until_shown(page, "text=PAGE_TEST", notify)
        finally:
            await browser.close()
    assert not problems, problems


# --- CORE-381: the connection indicator and the admin home -----------------------------


async def test_core_381_admin_home_is_the_status_view_without_inline_code(auth: WebAuth) -> None:
    app = protect(admin_app(auth), lambda: [], "admin")
    async with client(app) as c:
        c.cookies.update(session_cookie(auth))
        page = await c.get("/admin")
    assert page.status_code == 200
    text = page.text
    assert '<script type="module" src="/admin/static/home.js"></script>' in text
    assert "<mm3-admin-home>" in text
    assert '<header class="bar">' in text  # the indicator is added to this header by admin.js
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert "<style" not in text
    assert " style=" not in text


def test_core_381_indicator_texts_are_in_five_languages() -> None:
    keys = [
        "CORE_ADMIN_MQTT",
        "CORE_ADMIN_MQTT_OFF",
        "CORE_ADMIN_MQTT_CONNECTED",
        "CORE_ADMIN_MQTT_CONNECTING",
        "CORE_ADMIN_MQTT_ERROR",
        "CORE_ADMIN_MQTT_UNAVAILABLE",
        "CORE_ADMIN_MQTT_SINCE",
        "CORE_ADMIN_STATUS",
    ]
    labels = {k: set() for k in keys[1:6]}
    for lang in LANGS:
        table = admin_texts(lang)
        for key in keys:
            assert table[key], (lang, key)
        assert "{time}" in table["CORE_ADMIN_MQTT_SINCE"], lang
        for key in labels:
            labels[key].add(table[key])
    # Each state has its own text in every language: colour is never the only signal.
    assert len({table for values in labels.values() for table in values}) >= 5 * 4


async def test_core_381_indicator_and_home_follow_the_mqtt_status_in_chromium(
    running: tuple[WebGateway, WebAuth, Engine],
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, engine = running
    base = f"http://127.0.0.1:{gateway.admin.port}"
    cookie = session_cookie(auth)[ADMIN_COOKIE]
    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=exe or None)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [{"name": ADMIN_COOKIE, "value": cookie, "url": base, "httpOnly": True}]
            )
            page = await context.new_page()
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.on(
                "response",
                lambda r: problems.append(f"{r.status} {r.url}") if r.status >= 400 else None,
            )
            await page.goto(f"{base}/admin")
            conn = page.locator("header.bar .conn")
            # No bridge: grey, with its own text.
            await page.wait_for_selector('header.bar .conn[data-state="off"]')
            assert await conn.inner_text() == "MQTT off"
            # The status view of the home (CORE-305): engine, clients, kiosk and MQTT cards.
            await page.wait_for_selector("mm3-admin-home .card >> text=Engine")
            await page.wait_for_selector("mm3-admin-home .card.mqtt[data-state='off']")

            def set_mqtt(value: dict[str, Any] | None) -> None:
                engine.mqtt.status = lambda: value  # type: ignore[method-assign]

            # Configured, not connected, no error: amber.
            set_mqtt({"connected": False, "since": None, "error": None})
            await page.wait_for_selector('header.bar .conn[data-state="connecting"]')
            assert await conn.inner_text() == "MQTT connecting"
            # An error: red, the reason in the tooltip and highlighted on the home.
            set_mqtt({"connected": False, "since": None, "error": "refused"})
            await page.wait_for_selector('header.bar .conn[data-state="error"]')
            assert await conn.inner_text() == "MQTT error"
            assert "refused" in (await conn.get_attribute("title") or "")
            await page.wait_for_selector("mm3-admin-home .alert >> text=MQTT error: refused")
            # Connected: green, with `since` in the tooltip.
            set_mqtt({"connected": True, "since": "2026-11-14T10:00:00+00:00", "error": None})
            await page.wait_for_selector('header.bar .conn[data-state="connected"]')
            assert await conn.inner_text() == "MQTT connected"
            assert (await conn.get_attribute("title") or "").startswith("Since ")
            await page.wait_for_selector("mm3-admin-home .alert", state="detached")
            # The same indicator is in the header of the other admin pages.
            await page.goto(f"{base}/admin/debug")
            await page.wait_for_selector('header.bar .conn[data-state="connected"]')
            await page.wait_for_selector("text=MQTT connected")  # the MQTT card of the overview
            await page.goto(f"{base}/admin/config")
            await page.wait_for_selector('header.bar .conn[data-state="connected"]')
            # The admin channel closes (the engine restarts): grey, "status unavailable".
            await gateway.stop()
            await page.wait_for_selector('header.bar .conn[data-state="unavailable"]')
            assert await conn.inner_text() == "MQTT status unavailable"
        finally:
            await browser.close()
    # The editor page asks for /api/v1/config, which this fixture does not serve.
    ignored = ("WebSocket", "ERR_", "Failed to load resource", "/api/v1/config")
    assert not [p for p in problems if not any(i in p for i in ignored)], problems
