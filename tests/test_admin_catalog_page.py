"""Tests of the Catalog page ``/admin/catalog`` (CORE-120, CORE-133).

Implements:
- CORE-120 (Core spec 0.44, §10.6): the page lists the catalog, searches it, shows a module's
  details and offers Install, Update and Remove through the installer, with the equivalent CLI
  command as an alternative.
- CORE-133 (Core spec 0.44, §10.7): before the confirmation the dialog shows the module's badge,
  check report, requested capabilities and grant scopes.
- CORE-132 (Core spec 0.44, §10.7): every change asks for the installer passphrase and offers a
  field for the TOTP code.
- CORE-356 (Core spec 0.44, §10.7): the page reads the catalog routes and the ``installer`` topic.
"""

# ruff: noqa: E501 - the Node script below is data, kept in long lines like the checks it runs
from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from installer_fakes import index_bytes
from mm3.core.catalog import CatalogMeta
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
from mm3.core.web.apps import ADMIN_STATIC_DIR, admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.installer import InstallerLink
from mm3.core.web.pages import admin_catalog_page, admin_texts
from mm3.core.web.security import CSP
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.view import cached_view
from mm3.core.web.ws import ClientHub
from mm3.installer.client import Reply
from mm3.testing import VirtualClock
from test_admin_catalog import (
    PASSPHRASE,
    TOTP,
    ScriptedInstaller,
    default_modules,
    operation,
)
from test_admin_debug_page import Panel, chromium, client, session_cookie

LANGS = ("en", "it", "fr", "es", "de")


async def until(check: Callable[[], Awaitable[bool]], what: str, limit: float = 6.0) -> None:
    """Poll `check` until it holds. The admin's CSP forbids the eval behind Playwright's own
    ``wait_for_function`` polling, so the page is read with locators instead."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def text_has(locator: Any, text: str) -> bool:
    """Whether the first element of `locator` exists and its text contains `text`."""
    return await locator.count() > 0 and text in await locator.first.inner_text()


async def attribute_is(locator: Any, name: str, value: str) -> bool:
    return await locator.count() > 0 and await locator.first.get_attribute(name) == value


async def count_is(locator: Any, n: int) -> bool:
    return await locator.count() == n


# --- the page and its files -----------------------------------------------------------------


async def test_core_120_the_page_needs_a_session_and_has_no_inline_code(tmp_path: Path) -> None:
    auth = WebAuth.open(tmp_path / "web")
    app = protect(admin_app(auth), lambda: [], "admin")
    async with client(app) as c:
        r = await c.get("/admin/catalog")
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login?next=/admin/catalog"
        c.cookies.update(session_cookie(auth))
        page = await c.get("/admin/catalog")
        script = await c.get("/admin/static/catalog.js")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == CSP
    text = page.text
    assert '<script type="module" src="/admin/static/catalog.js"></script>' in text
    assert "<mm3-admin-catalog></mm3-admin-catalog>" in text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)  # CORE-63
    assert "<style" not in text
    assert " style=" not in text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_core_120_every_admin_page_links_to_the_catalog() -> None:
    html = admin_catalog_page()
    assert '<a href="/admin/catalog" aria-current="page">Catalog</a>' in html
    assert '<a href="/admin/config">' in html
    assert '<a href="/admin/debug">' in html


def test_core_120_the_script_keeps_to_the_rules_of_the_admin() -> None:
    source = (ADMIN_STATIC_DIR / "catalog.js").read_text()
    assert " style=" not in source  # CORE-63
    for banned in ("window.confirm", "window.alert", "window.prompt", "confirm(", "alert("):
        assert banned not in source, banned  # CORE-384
    assert 'type="password"' in source  # CORE-132
    assert 'autocomplete="off"' in source
    assert "localStorage" not in source  # never kept
    assert "sessionStorage" not in source
    assert "fetch(" not in source  # only the REST API through admin.js (CORE-298)
    # External links open in a new tab without the opener; the page never fetches a report.
    assert 'rel="noopener noreferrer"' in source
    # The shared patterns of CORE-384..387 come from ui.js.
    for pattern in ("actionRow(", "banner(", "closeOnBackdrop("):
        assert pattern in source, pattern


def test_core_120_texts_are_in_five_languages_with_the_same_placeholders() -> None:
    source = (ADMIN_STATIC_DIR / "catalog.js").read_text()
    used = set(re.findall(r"CORE_ADMIN_CATALOG_[A-Z_]+", source))
    # Keys built from a stem: the badges, the states, the actions and the dialogs.
    for badge in ("OFFICIAL", "VERIFIED", "COMMUNITY"):
        used.add(f"CORE_ADMIN_CATALOG_BADGE_{badge}")
    for state in (
        "QUEUED",
        "VERIFYING",
        "BUILDING",
        "SWITCHING",
        "RESTARTING",
        "CHECKING",
        "DONE",
        "ROLLED_BACK",
        "FAILED",
    ):
        used.add(f"CORE_ADMIN_CATALOG_STATE_{state}")
    for action in ("INSTALL", "UPDATE", "REMOVE", "RESTORE_PREVIOUS"):
        used.add(f"CORE_ADMIN_CATALOG_ACTION_{action}")
        used.add(f"CORE_ADMIN_CATALOG_DLG_{action}_TITLE")
        used.add(f"CORE_ADMIN_CATALOG_DLG_{action}_TEXT")
    for view in ("ALL", "INSTALLED", "UPDATES"):
        used.add(f"CORE_ADMIN_CATALOG_VIEW_{view}")
    used = {k for k in used if not k.endswith("_")}
    used.add("CORE_API_REAUTH_FAILED")
    english = admin_texts("en")
    assert len(used) > 60
    for lang in LANGS:
        table = admin_texts(lang)
        for key in sorted(used):
            assert table.get(key), (lang, key)
            assert set(re.findall(r"\{(\w+)\}", table[key])) == set(
                re.findall(r"\{(\w+)\}", english[key])
            ), (lang, key)
    from mm3.core.web.pages import TEXT

    assert TEXT["CORE_WEB_NAV_CATALOG"] == "Catalog"
    for lang in LANGS[1:]:
        assert (
            admin_texts(lang)["CORE_ADMIN_CATALOG_ACTION_INSTALL"]
            != english["CORE_ADMIN_CATALOG_ACTION_INSTALL"]
        )


# --- the logic of the page (CORE-120, CORE-133) ---------------------------------------------
#
# catalog.js runs in the browser; its decisions do not touch the DOM, so Node runs them here with
# stubs for the modules it imports.

_STUBS = {
    "lit.mjs": "export const html=(s,...v)=>({s,v});export const nothing=Symbol('nothing');"
    "export const render=()=>{};",
    "admin.mjs": (
        "export class ApiError extends Error{constructor(p){super(p.code?'text:'+p.code:p.detail);"
        "this.problem=p;}}"
        "export const t=(k,p={})=>k+(Object.keys(p).length?JSON.stringify(p):'');"
        "export const api=()=>{};export const fmtTime=(v)=>'@'+v;export const loadTexts=()=>{};"
        "export class AdminChannel{}"
    ),
    "ui.mjs": (
        "export class Messages{};export const actionRow=()=>null;export const banner=()=>null;"
        "export const closeOnBackdrop=()=>{};"
    ),
}

_NODE_TEST = r"""
import assert from 'node:assert/strict';
const page = await import(process.argv[2]);
const { ApiError } = await import(process.argv[3]);
const { cliCommand, reasonFor, operationBanner, problemText, pickRelease, normalize, isFinal } = page;
const free = {available: true, busy: false, locked_until: null};
const NOW = Date.parse('2026-11-14T12:00:00+00:00');
const item = (extra = {}) => ({name: 'mm3-alpha', installed: null, compatible: true, latest: '1.1.0',
  update_available: false, unlisted: false, ...extra});
const reason = (a, i, inst = free, mine = false) => {
  const r = reasonFor(a, i, inst, mine, NOW); return r && r.key.replace('CORE_ADMIN_CATALOG_REASON_', '');
};

// The CLI command is the alternative of each action.
assert.equal(cliCommand('install', 'mm3-alpha', '1.1.0'), 'mm3 modules install mm3-alpha==1.1.0');
assert.equal(cliCommand('install', 'mm3-alpha'), 'mm3 modules install mm3-alpha');
assert.equal(cliCommand('update', 'mm3-alpha'), 'mm3 modules update mm3-alpha');
assert.equal(cliCommand('remove', 'mm3-alpha'), 'mm3 modules remove mm3-alpha');
assert.equal(cliCommand('restore_previous'), 'mm3 modules restore-previous');

// The installer comes first: unknown, away, locked, busy; then what is true of the module.
assert.equal(reason('install', item(), null), 'CHECKING');
assert.equal(reason('install', item(), {...free, available: false}), 'AWAY');
assert.equal(reason('install', item(), {...free, locked_until: '2026-11-14T12:30:00+00:00'}), 'LOCKED');
assert.equal(reason('install', item(), {...free, locked_until: '2026-11-14T11:30:00+00:00'}), null);  // over
assert.equal(reason('install', item(), {...free, busy: true}), 'BUSY');
assert.equal(reason('install', item(), free, true), 'BUSY');  // one started here and not over
assert.equal(reason('restore_previous', null, {...free, busy: true}), 'BUSY');
assert.equal(reason('restore_previous', null, free), null);
// Install: free module; installed; incompatible; unlisted.
assert.equal(reason('install', item()), null);
assert.equal(reason('install', item({installed: '1.0.0'})), 'INSTALLED');
assert.equal(reason('install', item({compatible: false, latest: null})), 'INCOMPATIBLE');
assert.equal(reason('install', item({unlisted: true, compatible: null, installed: '0.1'})), 'INSTALLED');
// Update needs an installed module and a newer release; remove only an installed module.
assert.equal(reason('update', item()), 'NOT_INSTALLED');
assert.equal(reason('update', item({installed: '1.1.0'})), 'UP_TO_DATE');
assert.equal(reason('update', item({installed: '1.0.0', update_available: true})), null);
assert.equal(reason('remove', item()), 'NOT_INSTALLED');
assert.equal(reason('remove', item({installed: '1.0.0'})), null);
assert.equal(reason('remove', item({installed: '0.1', unlisted: true, compatible: null})), null);

// The banner of an operation: running, done, rolled back, failed.
const op = (state, extra = {}) => ({action: 'update', name: 'mm3-alpha', state, error: null, ...extra});
const text = (o) => operationBanner(o);
assert.equal(text(op('building')).kind, 'info');
assert.match(text(op('building')).text, /CORE_ADMIN_CATALOG_OP_RUNNING/);
assert.match(text(op('building')).text, /CORE_ADMIN_CATALOG_STATE_BUILDING/);
assert.equal(text(op('done')).kind, 'ok');
assert.equal(text(op('rolled_back')).kind, 'warn');
assert.equal(text(op('failed', {error: 'no wheel'})).kind, 'error');
assert.match(text(op('failed', {error: 'no wheel'})).text, /"error":"no wheel"/);
assert.deepEqual(['queued', 'building', 'done', 'rolled_back', 'failed'].map((s) => isFinal(op(s))),
  [false, false, true, true, true]);
assert.equal(isFinal(null), false);

// A failed request shows the catalog text of its code; a lock adds when it ends.
assert.equal(problemText(new ApiError({code: 'CORE_API_REAUTH_FAILED'})), 'text:CORE_API_REAUTH_FAILED');
assert.match(problemText(new ApiError({code: 'CORE_API_INSTALLER_LOCKED', locked_until: 'T'})),
  /^text:CORE_API_INSTALLER_LOCKED CORE_ADMIN_CATALOG_LOCKED_UNTIL\{"time":"@T"\}$/);
assert.equal(problemText(new Error('boom')), 'boom');

// The version the dialog shows: the named one, else the newest compatible one.
const detail = {releases: [
  {version: '2.0.0', compatible: false}, {version: '1.1.0', compatible: true},
  {version: '1.0.0', compatible: true}]};
assert.equal(pickRelease(detail).version, '1.1.0');
assert.equal(pickRelease(detail, '1.0.0').version, '1.0.0');
assert.equal(pickRelease(detail, '9.9.9'), null);
assert.equal(pickRelease({releases: []}), null);
assert.equal(pickRelease(null), null);
assert.equal(normalize('MM3_Alpha.x'), 'mm3-alpha-x');
console.log('ok');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_120_the_decisions_of_the_page(tmp_path: Path) -> None:
    source = (ADMIN_STATIC_DIR / "catalog.js").read_text()
    source = source.replace('"/admin/static/lit-html.js"', '"./lit.mjs"')
    source = source.replace('"/admin/static/admin.js"', '"./admin.mjs"')
    source = source.replace('"/admin/static/ui.js"', '"./ui.mjs"')
    assert "/admin/static/" not in source
    (tmp_path / "catalog.mjs").write_text(source)
    for name, text in _STUBS.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "check.mjs").write_text(_NODE_TEST)
    out = subprocess.run(  # noqa: S603
        [
            shutil.which("node") or "node",
            str(tmp_path / "check.mjs"),
            (tmp_path / "catalog.mjs").as_uri(),
            (tmp_path / "admin.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr + out.stdout
    assert out.stdout.strip() == "ok"


# --- the page in Chromium -------------------------------------------------------------------


@pytest.fixture
def scripted() -> ScriptedInstaller:
    fake = ScriptedInstaller()
    fake.status["installed"] = [{"name": "mm3-alpha", "version": "1.0.0", "origin": "catalog"}]
    return fake


@pytest.fixture
async def mirror_admin(
    tmp_path: Path, scripted: ScriptedInstaller
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
    link = InstallerLink(scripted)  # type: ignore[arg-type]
    services = ApiServices(engine, manager, pages, hub, None, "1.0+t", installer=link)
    inspector = BusInspector()
    engine.bus.add_endpoint(inspector)
    channel = AdminChannel(
        auth,
        lambda: status_object(services),
        inspector,
        interval=0.1,
        installer=link,
        installer_interval=0.1,
        installer_away_interval=0.2,
    )
    gateway = WebGateway(settings, tmp_path, hub=hub, api=services, channel=channel)
    await engine.start(result.instance_specs())
    await pages.start()
    await gateway.start()
    # The engine's copy of the index, as the installer would have written it.
    now = engine.clock.wall()
    generated = now - timedelta(hours=1)
    root = tmp_path / "state" / "catalog"
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_bytes(
        index_bytes(default_modules(), serial=12, generated=generated, lifetime=timedelta(days=7))
    )
    meta = CatalogMeta(12, now - timedelta(minutes=30), generated + timedelta(days=7))
    (root / "meta.json").write_text(json.dumps(meta.to_json()), encoding="utf-8")
    try:
        yield gateway, auth, engine
    finally:
        await gateway.stop()
        await pages.close()
        await engine.stop()


async def test_core_120_catalog_page_in_chromium(
    mirror_admin: tuple[WebGateway, WebAuth, Engine], scripted: ScriptedInstaller
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, _engine = mirror_admin
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
            await page.goto(f"{base}/admin/catalog")
            root = page.locator("mm3-admin-catalog")

            # The list: sorted by name, badge, state of each module (CORE-120, CORE-356).
            rows = root.locator("table.catalog-list tbody tr")
            await rows.first.wait_for()
            assert await rows.locator("td:first-child").all_inner_texts() == [
                "mm3-alpha",
                "mm3-beta",
                "mm3-gamma",
            ]
            assert await rows.locator("td:nth-child(5)").all_inner_texts() == [
                "Update to 1.1.0",
                "No release fits this engine",
                "Available, 0.9.0",
            ]
            assert (
                await root.locator("tr[data-name=mm3-gamma] .badge.official").inner_text()
                == "official"
            )
            assert "Updates available: 1" in await root.locator(".update-count").inner_text()
            assert (
                await page.locator("header.bar nav a[aria-current=page]").inner_text() == "Catalog"
            )

            # Search and filters go to the server and the list.
            await root.locator("input[name=q]").fill("gamma")
            await until(lambda: count_is(rows, 1), "1 rows")
            assert await rows.first.get_attribute("data-name") == "mm3-gamma"
            await root.locator("input[name=q]").fill("")
            await until(lambda: count_is(rows, 3), "3 rows")
            await root.locator("select[name=view]").select_option("updates")
            await until(lambda: count_is(rows, 1), "1 rows")
            assert await rows.first.get_attribute("data-name") == "mm3-alpha"
            await root.locator("select[name=view]").select_option("installed")
            assert await rows.count() == 1
            await root.locator("select[name=view]").select_option("all")
            await root.locator("select[name=category]").select_option("media")
            await until(lambda: count_is(rows, 1), "1 rows")
            await root.locator("select[name=category]").select_option("")
            await until(lambda: count_is(rows, 3), "3 rows")

            # The details: releases with the check report, capabilities and grants (CORE-133).
            await rows.first.click()
            card = root.locator("section.detail-card")
            await card.locator("table.releases").wait_for()
            assert await card.locator("h2").inner_text() == "mm3-alpha"
            releases = card.locator("table.releases tbody tr")
            assert await releases.locator("td:first-child").all_inner_texts() == ["1.1.0", "1.0.0"]
            link = releases.first.locator("a")
            assert await link.inner_text() == "Open report"
            assert await link.get_attribute("href") == "https://example.com/report"
            assert await link.get_attribute("target") == "_blank"
            assert "noopener" in (await link.get_attribute("rel") or "")
            assert "http.outbound" in await releases.first.inner_text()
            assert "https://api.example.com/" in await releases.first.inner_text()
            # The actions: install is disabled with its reason; update and remove work.
            install = card.locator('button[data-action="install"]')
            assert await install.get_attribute("aria-disabled") == "true"
            assert await install.get_attribute("title") == "Already installed."
            assert (
                await card.locator('button[data-action="update"]').get_attribute("aria-disabled")
                == "false"
            )
            assert "mm3 modules update mm3-alpha" in await card.inner_text()

            # Update: the dialog shows what the user confirms, with the focus on the passphrase.
            await card.locator('button[data-action="update"]').click()
            dialog = root.locator("dialog.catalog-dialog")
            await dialog.locator("input[name=passphrase]").wait_for()
            assert await dialog.locator("h2").inner_text() == "Update mm3-alpha to 1.1.0?"
            await dialog.locator("dd.mono").wait_for()
            body = await dialog.inner_text()
            assert "verified" in body  # the badge
            assert "Passed" in body  # the check report
            assert "Open report" in body
            assert "http.outbound" in body  # the requested capabilities
            assert "https://api.example.com/" in body  # the grant scopes
            assert "Granting scopes to instances stays a separate step" in body
            assert "mm3 modules update mm3-alpha" in body  # the CLI alternative
            assert await page.evaluate("document.activeElement.name") == "passphrase"
            assert (
                await dialog.locator("input[name=passphrase]").get_attribute("type") == "password"
            )
            assert await dialog.locator("input[name=totp]").count() == 1
            # Cancel sends nothing.
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.catalog-dialog", state="hidden")
            assert not [c for c in scripted.calls if c[0] == "POST"]

            # A wrong passphrase: the dialog stays, with the text of the code, and is emptied.
            scripted.replies[("POST", "/v1/operations")] = Reply(403, {"error": "refused"})
            await card.locator('button[data-action="update"]').click()
            await dialog.locator("input[name=passphrase]").fill("wrong")
            await dialog.locator("button.confirm").click()
            error = dialog.locator("div.alert[role=alert]")
            await error.wait_for()
            assert await error.inner_text() == admin_texts("en")["CORE_API_REAUTH_FAILED"]
            assert await dialog.locator("input[name=passphrase]").input_value() == ""
            assert await page.locator("dialog.catalog-dialog[open]").count() == 1
            await dialog.locator("button.cancel").click()
            del scripted.replies[("POST", "/v1/operations")]

            # Confirm: the request carries the passphrase and the code, once, and no version.
            await card.locator('button[data-action="update"]').click()
            await dialog.locator("input[name=passphrase]").fill(PASSPHRASE)
            await dialog.locator("input[name=totp]").fill(TOTP)
            scripted.operations = [operation(state="queued", action="update")]
            await dialog.locator("button.confirm").click()
            await page.wait_for_selector("dialog.catalog-dialog", state="hidden")
            posts = [c for c in scripted.calls if (c[0], c[1]) == ("POST", "/v1/operations")]
            body_sent = posts[-1][2]
            assert body_sent is not None
            assert body_sent["action"] == "update"
            assert body_sent["name"] == "mm3-alpha"
            assert body_sent["passphrase"] == PASSPHRASE
            assert body_sent["totp"] == TOTP
            assert "version" not in body_sent
            assert PASSPHRASE not in await page.content()

            # The operation is followed live; the others are disabled meanwhile.
            scripted.operations = [operation(state="building", action="update", steps=2)]
            scripted.status["busy"] = True
            info = root.locator("div.alert.info")
            await info.wait_for()
            await until(lambda: text_has(info, "building the environment"), "the building step")
            assert (
                await card.locator('button[data-action="remove"]').get_attribute("title")
                == "The installer is busy with another operation."
            )
            assert await root.locator("section.operation ol.steps li").count() == 2
            # It ends: the banner says so and the list is read again with the new version.
            scripted.status["busy"] = False
            scripted.status["installed"] = [
                {"name": "mm3-alpha", "version": "1.1.0", "origin": "catalog"}
            ]
            scripted.operations = [
                operation(
                    state="done", action="update", steps=3, finished_at="2026-11-14T10:05:00+00:00"
                )
            ]
            done = root.locator("div.alert.ok")
            await done.wait_for()
            assert "Update mm3-alpha: done." in await done.inner_text()
            await until(
                lambda: text_has(
                    root.locator("tr[data-name=mm3-alpha] td:nth-child(5)"), "Installed 1.1.0"
                ),
                "the new version in the list",
            )
            assert await root.locator(".update-count").count() == 0

            # Remove: the instances of the module are named, and the dialog sends the removal.
            await rows.first.click()
            await card.locator('button[data-action="remove"]').click()
            await dialog.locator("input[name=passphrase]").wait_for()
            assert await dialog.locator("h2").inner_text() == "Remove mm3-alpha?"
            await until(lambda: text_has(dialog, "no configured instance"), "the instances line")
            await dialog.locator("input[name=passphrase]").fill(PASSPHRASE)
            scripted.operations = []
            await dialog.locator("button.confirm").click()
            await page.wait_for_selector("dialog.catalog-dialog", state="hidden")
            removal = [c for c in scripted.calls if (c[0], c[1]) == ("POST", "/v1/operations")][-1][
                2
            ]
            assert removal is not None
            assert removal["action"] == "remove"
            assert removal["name"] == "mm3-alpha"
            assert "version" not in removal
            assert "totp" not in removal  # no code was typed

            # A failed operation shows the installer's reason.
            scripted.operations = [
                operation(
                    "0000000000000009",
                    state="failed",
                    action="install",
                    error="verification failed",
                )
            ]
            await page.reload()
            await root.locator("table.catalog-list").wait_for()
            # An operation that ended before the page opened is not shown as news.
            assert await root.locator("div.alert.error").count() == 0
        finally:
            await browser.close()
    assert not [p for p in problems if "WebSocket" not in p and "Failed to load" not in p], problems


async def test_core_133_dialog_shows_badge_report_and_scopes_before_confirmation(
    mirror_admin: tuple[WebGateway, WebAuth, Engine], scripted: ScriptedInstaller
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, _engine = mirror_admin
    base = f"http://127.0.0.1:{gateway.admin.port}"
    cookie = session_cookie(auth)[ADMIN_COOKIE]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=exe or None)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [{"name": ADMIN_COOKIE, "value": cookie, "url": base, "httpOnly": True}]
            )
            page = await context.new_page()
            await page.goto(f"{base}/admin/catalog")
            root = page.locator("mm3-admin-catalog")
            await root.locator("tr[data-name=mm3-alpha]").click()
            card = root.locator("section.detail-card")
            await card.locator('button[data-action="update"]').click()
            dialog = root.locator("dialog.catalog-dialog")
            await dialog.locator("dd.mono").wait_for()
            body = await dialog.inner_text()
            assert "verified" in body  # the badge
            assert "Passed" in body  # the check report
            assert "Open report" in body
            assert "http.outbound" in body  # the requested capabilities
            assert "https://api.example.com/" in body  # the grant scopes
            assert "Granting scopes to instances stays a separate step" in body
            # Nothing was sent: the user has not confirmed.
            assert not [c for c in scripted.calls if c[0] == "POST"]
        finally:
            await browser.close()


async def test_core_120_catalog_page_installer_away_and_restore_in_chromium(
    mirror_admin: tuple[WebGateway, WebAuth, Engine], scripted: ScriptedInstaller
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, _engine = mirror_admin
    base = f"http://127.0.0.1:{gateway.admin.port}"
    cookie = session_cookie(auth)[ADMIN_COOKIE]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=exe or None)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [{"name": ADMIN_COOKIE, "value": cookie, "url": base, "httpOnly": True}]
            )
            page = await context.new_page()
            await page.goto(f"{base}/admin/catalog")
            root = page.locator("mm3-admin-catalog")
            await root.locator("table.catalog-list").wait_for()
            restore = root.locator('button[data-action="restore_previous"]')
            await until(
                lambda: attribute_is(restore, "aria-disabled", "false"),
                "the restore button to be enabled",
            )
            # Restore previous version: a dialog with the passphrase, and the CLI alternative.
            await restore.click()
            dialog = root.locator("dialog.catalog-dialog")
            await dialog.locator("input[name=passphrase]").wait_for()
            assert await dialog.locator("h2").inner_text() == "Restore the previous version?"
            assert "mm3 modules restore-previous" in await dialog.inner_text()
            await dialog.locator("input[name=passphrase]").fill(PASSPHRASE)
            await dialog.locator("button.confirm").click()
            await page.wait_for_selector("dialog.catalog-dialog", state="hidden")
            posts = [c for c in scripted.calls if (c[0], c[1]) == ("POST", "/v1/operations")]
            assert posts[-1][2] is not None
            assert posts[-1][2]["action"] == "restore_previous"
            assert "name" not in posts[-1][2]
            scripted.operations = [
                operation(state="rolled_back", action="restore_previous", name=None)
            ]
            warn = root.locator("div.alert.warn")
            await warn.wait_for()
            assert "rolled back" in await warn.inner_text()
            # The installer goes away: a warning, and every action says why it is disabled.
            scripted.down = True
            away = root.locator("div.alert.warn", has_text="cannot be reached")
            await until(lambda: count_is(away, 1), "the installer warning")
            await until(
                lambda: attribute_is(
                    restore,
                    "title",
                    "The installer cannot be reached, so modules cannot be changed from here.",
                ),
                "the reason on the restore button",
            )
            await restore.click(force=True)  # a disabled button opens nothing
            assert await page.locator("dialog.catalog-dialog[open]").count() == 0
            # The list still works from the engine's copy of the index.
            assert await root.locator("table.catalog-list tbody tr").count() == 3
        finally:
            await browser.close()
