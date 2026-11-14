"""Tests of the engine's restart and reboot routes and ``GET /api/v1/installer``.

Implements:
- CORE-382 (Core spec 0.42, §9.1): the three routes forward to the installer, answer 202, map
  its refusals to 409 and 503, and are not on the admin socket.
- CORE-356 (Core spec 0.33, §10.7): ``GET /api/v1/installer`` and the installer problem codes.
"""

# ruff: noqa: F811 - the shared `api` fixture is imported, then named by the tests
from __future__ import annotations

import io
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm3.core.config import load_config
from mm3.core.config.manager import ConfigManager
from mm3.core.diag.inspector import BusInspector
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging
from mm3.core.pages import PageService
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.adminws import AdminChannel
from mm3.core.web.api import ApiServices, status_object
from mm3.core.web.apps import ADMIN_STATIC_DIR, socket_app
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.installer import SYSTEM_ACTIONS, InstallerLink
from mm3.core.web.pages import admin_texts
from mm3.core.web.problems import CODES
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.view import cached_view
from mm3.core.web.ws import ClientHub
from mm3.installer.client import InstallerUnavailable, Reply
from mm3.testing import VirtualClock
from test_admin_api import (
    Api,
    api,  # noqa: F401 - the shared admin API fixture
    is_problem,
)
from test_admin_debug_page import Panel, chromium, session_cookie

LOOPBACK = ("127.0.0.1", 50000)
LANGS = ("en", "it", "fr", "es", "de")


class FakeInstaller:
    """A stand-in for the installer socket client with scripted replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.replies: dict[tuple[str, str], Reply] = {}
        self.down = False

    def request(self, method: str, target: str, body: dict[str, Any] | None = None) -> Reply:
        self.calls.append((method, target, body))
        if self.down:
            raise InstallerUnavailable("installer socket /run/mm3-install/installer.sock: gone")
        if (method, target) in self.replies:
            return self.replies[(method, target)]
        if method == "GET":
            return Reply(200, {"busy": False})
        action = target.rsplit("/", 1)[1]
        return Reply(202, {"action": action, "requested_at": "2026-11-14T10:00:00+00:00"})


@pytest.fixture
def installer(api: Api) -> FakeInstaller:
    fake = FakeInstaller()
    api.services.installer = InstallerLink(fake)  # type: ignore[arg-type]
    return fake


def test_core_382_the_new_codes_are_in_the_list() -> None:
    assert CODES["CORE_API_INSTALLER_UNAVAILABLE"][0] == 503
    assert CODES["CORE_API_INSTALLER_BUSY"][0] == 409


@pytest.mark.parametrize("action", SYSTEM_ACTIONS)
async def test_core_382_each_route_forwards_and_answers_202(
    api: Api,
    installer: FakeInstaller,
    action: str,
) -> None:
    r = await api.post(f"/api/v1/system/{action}")
    assert r.status_code == 202, r.text
    assert r.json() == {"action": action, "requested_at": "2026-11-14T10:00:00+00:00"}
    method, target, body = installer.calls[-1]
    assert (method, target) == ("POST", f"/v1/system/{action}")
    assert body is not None
    assert body["client"] == "test (bearer) 127.0.0.1"  # the token's name and the address


async def test_core_382_a_browser_session_is_named_with_its_token_and_origin(
    api: Api,
    installer: FakeInstaller,
) -> None:
    hub = api.services.hub
    assert hub is not None
    rec = hub.auth.tokens.verify(api.token, "admin")
    assert rec is not None
    sid = hub.auth.sessions.create(rec)
    api.client.cookies.set(ADMIN_COOKIE, sid)
    # A session needs a matching Origin for an unsafe method (CORE-253).
    refused = await api.client.post("/api/v1/system/reboot")
    is_problem(refused, 403, "CORE_API_FORBIDDEN")
    r = await api.client.post("/api/v1/system/reboot", headers={"origin": "http://127.0.0.1:8081"})
    assert r.status_code == 202, r.text
    assert installer.calls[-1][2] == {"client": "test (session) 127.0.0.1"}


async def test_core_382_the_routes_need_authentication(
    api: Api,
    installer: FakeInstaller,
) -> None:
    for action in SYSTEM_ACTIONS:
        r = await api.client.post(f"/api/v1/system/{action}")
        is_problem(r, 401, "CORE_API_UNAUTHORIZED")
    assert installer.calls == []


async def test_core_382_a_busy_installer_is_409_installer_busy(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.replies[("POST", "/v1/system/reboot")] = Reply(
        409, {"error": "operation 1234 is running", "code": "busy"}
    )
    body = is_problem(await api.post("/api/v1/system/reboot"), 409, "CORE_API_INSTALLER_BUSY")
    assert "operation 1234" in body["detail"]


async def test_core_382_no_kiosk_unit_is_409_no_kiosk(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.replies[("POST", "/v1/system/restart-kiosk")] = Reply(
        409, {"error": "mm3-kiosk.service is not installed", "code": "no_kiosk"}
    )
    r = await api.post("/api/v1/system/restart-kiosk")
    is_problem(r, 409, "CORE_API_NO_KIOSK")
    # the other actions do not need a kiosk
    assert (await api.post("/api/v1/system/restart-engine")).status_code == 202


async def test_core_382_without_an_installer_the_routes_are_503(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.down = True
    for action in SYSTEM_ACTIONS:
        r = await api.post(f"/api/v1/system/{action}")
        is_problem(r, 503, "CORE_API_INSTALLER_UNAVAILABLE")
    api.services.installer = None  # a system without the installer
    is_problem(await api.post("/api/v1/system/reboot"), 503, "CORE_API_INSTALLER_UNAVAILABLE")


async def test_core_382_an_unexpected_answer_is_503(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.replies[("POST", "/v1/system/reboot")] = Reply(404, {"error": "no route"})
    body = is_problem(
        await api.post("/api/v1/system/reboot"), 503, "CORE_API_INSTALLER_UNAVAILABLE"
    )
    assert "404" in body["detail"]


async def test_core_382_the_routes_take_no_body_and_no_passphrase(
    api: Api,
    installer: FakeInstaller,
) -> None:
    r = await api.client.post("/api/v1/system/restart-engine", headers=api.bearer)
    assert r.status_code == 202
    assert installer.calls[-1][2] == {"client": "test (bearer) 127.0.0.1"}


async def test_core_356_installer_status_reports_available_and_busy(
    api: Api,
    installer: FakeInstaller,
) -> None:
    assert (await api.get("/api/v1/installer")).json() == {"available": True, "busy": False}
    installer.replies[("GET", "/v1/status")] = Reply(200, {"busy": True})
    assert (await api.get("/api/v1/installer")).json() == {"available": True, "busy": True}
    installer.replies[("GET", "/v1/status")] = Reply(500, {"error": "boom"})
    assert (await api.get("/api/v1/installer")).json() == {"available": False, "busy": False}
    installer.down = True
    assert (await api.get("/api/v1/installer")).json() == {"available": False, "busy": False}
    api.services.installer = None
    assert (await api.get("/api/v1/installer")).json() == {"available": False, "busy": False}
    is_problem(await api.client.get("/api/v1/installer"), 401, "CORE_API_UNAUTHORIZED")


async def test_core_382_not_on_the_admin_socket(
    api: Api,
    installer: FakeInstaller,
) -> None:
    app = socket_app(api.services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://socket") as client:
        for action in SYSTEM_ACTIONS:
            r = await client.post(f"/api/v1/system/{action}")
            is_problem(r, 404, "CORE_API_NOT_FOUND")
        assert (await client.get("/api/v1/installer")).status_code == 200
        paths = (await client.get("/api/openapi.json")).json()["paths"]
    assert not any("/system/" in p for p in paths)
    assert installer.calls == [("GET", "/v1/status", None)]
    schema = (await api.get("/api/openapi.json")).json()["paths"]
    assert all(f"/api/v1/system/{a}" in schema for a in SYSTEM_ACTIONS)


# --- the controls of the System card (CORE-383) ---------------------------------------------
#
# system.js runs in the browser; its logic does not touch the DOM, so Node runs it here with
# stubs for the two modules it imports (lit-html and admin.js) and a manual clock.

_STUBS = {
    "lit.mjs": "export const html=(s,...v)=>({s,v});export const nothing=Symbol('nothing');",
    "admin.mjs": (
        "export class ApiError extends Error{constructor(p){super(p.code?'text:'+p.code:p.detail);"
        "this.problem=p;}}"
        "export const t=(k,p={})=>k+(Object.keys(p).length?JSON.stringify(p):'');"
        "export const api=(m,p,b)=>globalThis.__api(m,p,b);"
    ),
}

_NODE_TEST = r"""
import assert from 'node:assert/strict';
const timers = []; let now = 0;
globalThis.setTimeout = (fn, ms) => { const h = {at: now + ms, fn}; timers.push(h); return h; };
globalThis.clearTimeout = (h) => { const i = timers.indexOf(h); if (i >= 0) timers.splice(i, 1); };
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
const advance = (ms) => { now += ms; for (const h of [...timers].filter((x) => x.at <= now)) {
  timers.splice(timers.indexOf(h), 1); h.fn(); } };
const calls = []; let installer = {available: true, busy: false}; let failure = null;
globalThis.__api = async (method, path, body) => {
  calls.push([method, path]);
  if (method === 'GET') return installer;
  if (failure) throw failure;
  return {action: path.split('/').pop()};
};
const { SystemControls, EFFECT_WAIT_MS } = await import(process.argv[2]);
const { ApiError } = await import(process.argv[3]);
const flush = () => new Promise((r) => setImmediate(r));
let changes = 0;
const make = () => { const sc = new SystemControls(() => { changes += 1; }); return sc; };
const status = (startedAt, connected = true) => ({started_at: startedAt, kiosk: {connected}});
const T0 = '2026-11-14T10:00:00+00:00', T1 = '2026-11-14T10:05:00+00:00';

// The buttons' reason: unknown, unreachable, busy, free.
let sc = make();
assert.equal(sc.reason, 'CORE_ADMIN_SYSTEM_CHECKING');
installer = {available: false, busy: false}; await sc.refresh();
assert.equal(sc.reason, 'CORE_ADMIN_SYSTEM_INSTALLER_UNAVAILABLE');
sc.ask('reboot'); assert.equal(sc.confirming, null);
installer = {available: true, busy: true}; await sc.refresh();
assert.equal(sc.reason, 'CORE_ADMIN_SYSTEM_INSTALLER_BUSY');
installer = {available: true, busy: false}; await sc.refresh();
assert.equal(sc.reason, null);

// Engine: Cancel sends nothing; Confirm sends and shows the banner until a later started_at.
sc.observe(true, status(T0));
sc.ask('restart-engine'); assert.equal(sc.confirming, 'restart-engine');
sc.cancel(); assert.equal(sc.confirming, null);
assert.deepEqual(calls.filter((c) => c[0] === 'POST'), []);
sc.ask('restart-engine'); await sc.confirm();
assert.deepEqual(calls.filter((c) => c[0] === 'POST'), [['POST', '/system/restart-engine']]);
assert.equal(sc.pending.action, 'restart-engine');
assert.equal(sc.reason, 'CORE_ADMIN_SYSTEM_INSTALLER_BUSY');  // one action at a time
sc.observe(true, status(T0)); assert.equal(sc.pending.outcome, null);  // not yet
sc.observe(false, null); sc.observe(true, status(T0)); assert.equal(sc.pending.outcome, null);
sc.observe(true, status(T1)); assert.equal(sc.pending.outcome, 'seen');
await flush(); advance(9000); assert.equal(sc.pending, null);  // the banner ends

// Without the effect the banner ends after 120 s with its message, and stays until dismissed.
sc = make(); await sc.refresh(); sc.observe(true, status(T0));
sc.ask('reboot'); await sc.confirm();
advance(EFFECT_WAIT_MS - 1); assert.equal(sc.pending.outcome, null);
advance(2); assert.equal(sc.pending.outcome, 'unseen');
sc.observe(true, status(T1)); assert.equal(sc.pending.outcome, 'unseen');  // too late
sc.dismiss(); assert.equal(sc.pending, null);

// Reboot with no status before the request: the effect needs the channel to have closed.
sc = make(); await sc.refresh();
sc.ask('reboot'); await sc.confirm();
sc.observe(true, status(T0)); assert.equal(sc.pending.outcome, null);
sc.observe(false, null); sc.observe(true, status(T1)); assert.equal(sc.pending.outcome, 'seen');

// Kiosk: connected true after it was false.
sc = make(); await sc.refresh(); sc.observe(true, status(T0, true));
sc.ask('restart-kiosk'); await sc.confirm();
sc.observe(true, status(T0, true)); assert.equal(sc.pending.outcome, null);
sc.observe(true, status(T0, false)); assert.equal(sc.pending.outcome, null);
sc.observe(true, status(T0, true)); assert.equal(sc.pending.outcome, 'seen');
// A kiosk that was already down when asked counts as down.
sc = make(); await sc.refresh(); sc.observe(true, status(T0, false));
sc.ask('restart-kiosk'); await sc.confirm();
sc.observe(true, status(T0, true)); assert.equal(sc.pending.outcome, 'seen');
// The kiosk restart does not close the admin channel: a closed channel is no effect.
sc = make(); await sc.refresh(); sc.observe(true, status(T0, true));
sc.ask('restart-kiosk'); await sc.confirm();
sc.observe(false, null); sc.observe(true, status(T1, true)); assert.equal(sc.pending.outcome, null);

// An error answer shows the catalog text of its code and no banner.
sc = make(); await sc.refresh(); sc.observe(true, status(T0));
failure = new ApiError({code: 'CORE_API_INSTALLER_BUSY'});
sc.ask('restart-engine'); await sc.confirm();
assert.equal(sc.pending, null);
assert.equal(sc.error, 'text:CORE_API_INSTALLER_BUSY');
sc.dismiss(); assert.equal(sc.error, null);
failure = null;
console.log('ok');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_383_controls_follow_the_status_and_the_installer(tmp_path: Path) -> None:
    source = (ADMIN_STATIC_DIR / "system.js").read_text()
    source = source.replace('"/admin/static/lit-html.js"', '"./lit.mjs"')
    source = source.replace('"/admin/static/admin.js"', '"./admin.mjs"')
    assert "/admin/static/" not in source
    (tmp_path / "system.mjs").write_text(source)
    for name, text in _STUBS.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "check.mjs").write_text(_NODE_TEST)
    out = subprocess.run(  # noqa: S603
        [
            shutil.which("node") or "node",
            str(tmp_path / "check.mjs"),
            (tmp_path / "system.mjs").as_uri(),
            (tmp_path / "admin.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr + out.stdout
    assert out.stdout.strip() == "ok"


def test_core_383_card_texts_are_in_five_languages() -> None:
    stems = ("RESTART_ENGINE", "RESTART_KIOSK", "REBOOT")
    keys = ["CORE_ADMIN_SYSTEM", "CORE_ADMIN_SYSTEM_CHECKING", "CORE_ADMIN_DISMISS"]
    keys += ["CORE_ADMIN_SYSTEM_INSTALLER_UNAVAILABLE", "CORE_ADMIN_SYSTEM_INSTALLER_BUSY"]
    keys += [
        "CORE_ADMIN_SYSTEM_UNSEEN",
        "CORE_API_INSTALLER_UNAVAILABLE",
        "CORE_API_INSTALLER_BUSY",
    ]
    for stem in stems:
        keys += [f"CORE_ADMIN_SYSTEM_{stem}{s}" for s in ("", "_EFFECT", "_TITLE", "_CONFIRM_TEXT")]
        keys += [f"CORE_ADMIN_SYSTEM_PENDING_{stem}", f"CORE_ADMIN_SYSTEM_SEEN_{stem}"]
    source = (ADMIN_STATIC_DIR / "system.js").read_text()
    used = set(re.findall(r"CORE_ADMIN_[A-Z_]+", source))
    assert {k for k in used if "$" not in k and k in keys} <= set(keys)
    english = admin_texts("en")
    for lang in LANGS:
        table = admin_texts(lang)
        for key in keys:
            assert table.get(key), (lang, key)
        assert "{action}" in table["CORE_ADMIN_SYSTEM_UNSEEN"], lang
    # The confirmation texts name the action: each differs from the others.
    for lang in LANGS:
        table = admin_texts(lang)
        texts = {table[f"CORE_ADMIN_SYSTEM_{s}_CONFIRM_TEXT"] for s in stems}
        assert len(texts) == 3, lang
    assert english["CORE_ADMIN_SYSTEM_REBOOT"] == "Reboot system"
    assert english["CORE_ADMIN_SYSTEM_RESTART_ENGINE"] == "Restart engine"
    assert english["CORE_ADMIN_SYSTEM_RESTART_KIOSK"] == "Restart kiosk"


def test_core_383_the_home_has_the_card_and_no_inline_styles() -> None:
    system = (ADMIN_STATIC_DIR / "system.js").read_text()
    home = (ADMIN_STATIC_DIR / "home.js").read_text()
    assert " style=" not in system  # CORE-63
    assert "systemCard(" in home
    assert "systemBanner(" in home
    assert "systemDialog(" in home
    assert "autofocus" in system  # Cancel has the focus (CORE-383)
    assert 'class="cancel" autofocus' in system


# --- the card in Chromium (CORE-383) ----------------------------------------------------------


@pytest.fixture
async def mirror_admin(
    tmp_path: Path, installer_fake: FakeInstaller
) -> AsyncIterator[tuple[WebGateway, WebAuth, Engine]]:
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
    services = ApiServices(
        engine,
        manager,
        pages,
        hub,
        None,
        "1.0+t",
        installer=InstallerLink(installer_fake),  # type: ignore[arg-type]
    )
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


@pytest.fixture
def installer_fake() -> FakeInstaller:
    return FakeInstaller()


async def test_core_383_system_card_in_chromium(
    mirror_admin: tuple[WebGateway, WebAuth, Engine], installer_fake: FakeInstaller
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, engine = mirror_admin
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
            await page.goto(f"{base}/admin")
            card = page.locator("mm3-admin-home section.system")
            await card.wait_for()
            assert (await card.locator("h2").text_content()) == "System"
            buttons = card.locator("button[data-action]")
            assert await buttons.all_inner_texts() == [
                "Restart engine",
                "Restart kiosk",
                "Reboot system",
            ]
            # The installer answers: the buttons work.
            await page.wait_for_selector('button[data-action][aria-disabled="false"]')
            # Cancel sends nothing and has the focus.
            await card.locator('button[data-action="restart-engine"]').click()
            dialog = page.locator("dialog.system-confirm")
            await dialog.wait_for()
            assert await dialog.locator("h2").inner_text() == "Restart the engine?"
            focused = await page.evaluate("document.activeElement.className")
            assert focused == "cancel"
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.system-confirm", state="hidden")
            assert not [c for c in installer_fake.calls if c[0] == "POST"]
            # The reboot dialog names its effect and has the danger button.
            await card.locator('button[data-action="reboot"]').click()
            assert "whole machine" in await dialog.inner_text()
            assert await dialog.locator("button.danger.confirm").count() == 1
            await dialog.locator("button.cancel").click()
            # Confirm the engine restart: the request goes out and the banner shows.
            await card.locator('button[data-action="restart-engine"]').click()
            await dialog.locator("button.confirm").click()
            banner = page.locator("mm3-admin-home p.alert.info")
            await banner.wait_for()
            assert "Restart engine requested" in await banner.inner_text()
            posts = [c for c in installer_fake.calls if c[0] == "POST"]
            assert [p[1] for p in posts] == ["/v1/system/restart-engine"]
            # Busy while the action is pending: the buttons say why.
            reason = await buttons.first.get_attribute("title")
            assert reason == "The installer is busy with another operation."
            # The status shows a later start: the banner says the engine restarted.
            started = engine.started_at
            assert started is not None
            engine.started_at = started + timedelta(minutes=5)
            await page.wait_for_selector("mm3-admin-home p.alert.ok >> text=The engine restarted.")
            assert await page.locator("mm3-admin-home p.alert.info").count() == 0
        finally:
            await browser.close()
    assert not [p for p in problems if "WebSocket" not in p and "Failed to load" not in p], problems


async def test_core_383_system_card_disabled_and_errors_in_chromium(
    mirror_admin: tuple[WebGateway, WebAuth, Engine], installer_fake: FakeInstaller
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, _engine = mirror_admin
    base = f"http://127.0.0.1:{gateway.admin.port}"
    cookie = session_cookie(auth)[ADMIN_COOKIE]
    installer_fake.down = True  # no installer: disabled, with the reason as the tooltip
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=exe or None)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [{"name": ADMIN_COOKIE, "value": cookie, "url": base, "httpOnly": True}]
            )
            page = await context.new_page()
            await page.goto(f"{base}/admin")
            first = page.locator("mm3-admin-home button[data-action]").first
            await page.wait_for_selector('button[data-action][aria-disabled="true"]')
            await page.wait_for_function(
                "document.querySelector('button[data-action]').title.includes('cannot be reached')"
            )
            assert await first.get_attribute("title") == "The installer cannot be reached."
            await first.click(force=True)  # a disabled button opens nothing
            assert await page.locator("dialog.system-confirm[open]").count() == 0
            # A busy answer from the installer shows its catalog text and no banner.
            installer_fake.down = False
            installer_fake.replies[("POST", "/v1/system/restart-kiosk")] = Reply(
                409, {"error": "operation 1 is running", "code": "busy"}
            )
            await page.reload()
            await page.wait_for_selector('button[data-action][aria-disabled="false"]')
            await page.locator('button[data-action="restart-kiosk"]').click()
            await page.locator("dialog.system-confirm button.confirm").click()
            alert = page.locator("mm3-admin-home p.alert[role=alert]")
            await alert.wait_for()
            assert "The installer is busy with another operation." in await alert.inner_text()
            assert await page.locator("mm3-admin-home p.alert.info").count() == 0
        finally:
            await browser.close()
