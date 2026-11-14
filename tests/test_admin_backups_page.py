"""Tests of the Backups page ``/admin/backups`` (CORE-361).

Implements:
- CORE-361 (Core spec 0.45, §10.8): the page lists the archives, makes a backup, downloads,
  verifies and restores an archive, uploads one, makes an age key and manages the remotes through
  the backup routes, and follows the operations on the ``installer`` topic.
- CORE-162 (Core spec 0.45, §10.8): the restore dialog shows what a restore would change.
- CORE-161, CORE-364 (Core spec 0.45, §10.8): secrets only in an encrypted archive; the age
  identity is shown once.
- CORE-365 (Core spec 0.45, §10.8): the remotes, their test, and the masked secrets.
- CORE-132, CORE-356 (Core spec 0.44, §10.7): a restore and a change of a remote ask for the
  passphrase and the TOTP code, sent once and never kept.
"""

# ruff: noqa: E501 - the Node script below is data, kept in long lines like the checks it runs
# ruff: noqa: F811 - the shared fixtures are imported, then named by the tests
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mm3.core.engine import Engine
from mm3.core.web.apps import ADMIN_STATIC_DIR, admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.pages import TEXT, admin_backups_page, admin_texts
from mm3.core.web.security import CSP
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.installer.client import InstallerUnavailable, Reply
from test_admin_backups import FakeStream
from test_admin_catalog import OP_A, PASSPHRASE, TOTP, ScriptedInstaller, operation
from test_admin_catalog_page import (
    attribute_is,
    count_is,
    mirror_admin,  # noqa: F401 - the shared running-gateway fixture
    text_has,
    until,
)
from test_admin_debug_page import chromium, client, session_cookie

LANGS = ("en", "it", "fr", "es", "de")
HOST = "mirror"
OLD = "mm3-20261101-040000-scheduled.mm3backup"
NEW = "mm3-20261113-101500-manual.mm3backup"
UPLOAD = "0123456789abcdef0123456789abcdef"


def entry(name: str, kind: str, created: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "created": created,
        "kind": kind,
        "hostname": HOST,
        "engine_version": "1.0",
        "api_version": "1.0",
        "parts": ["config", "data", "modules"],
        "size": 2_621_440,
        "encrypted": False,
        "sha256": "0" * 64,
        **extra,
    }


def summary(**extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "encrypted": False,
        "hostname": HOST,
        "created": "2026-11-13T10:15:00+00:00",
        "kind": "manual",
        "engine_version": "1.0",
        "api_version": "1.0",
        "parts": ["config", "data", "modules"],
        "omitted": ["secrets"],
        "files": 42,
        "warnings": ["Taken on another host"],
        **extra,
    }


class ScriptedBackups(ScriptedInstaller):
    """The installer with the backup routes scripted."""

    def __init__(self) -> None:
        super().__init__()
        self.listing: dict[str, Any] = {
            "backups": [
                entry(NEW, "manual", "2026-11-13T10:15:00+00:00"),
                entry(OLD, "scheduled", "2026-11-01T04:00:00+00:00", encrypted=True),
            ],
            "status": {
                "scheduled": {
                    "at": "2026-11-01T04:00:00+00:00",
                    "ok": True,
                    "name": OLD,
                    "error": None,
                    "omitted": [],
                },
                "remote": {
                    "at": "2026-11-01T04:01:00+00:00",
                    "ok": False,
                    "name": OLD,
                    "error": "no route to host",
                    "omitted": [],
                },
            },
        }
        self.remotes: list[dict[str, Any]] = [
            {
                "name": "nas",
                "type": "sftp",
                "params": {"host": "nas.local", "pass": {"!masked": None}},
            }
        ]
        self.remote_listing: list[dict[str, Any]] = [
            entry(OLD, "scheduled", "2026-11-01T04:00:00+00:00")
        ]
        self.uploaded: list[tuple[str, int, bytes]] = []
        self.inspected: list[tuple[str, dict[str, Any]]] = []

    def backups(self, method: str, target: str, body: dict[str, Any] | None) -> Reply | None:
        route = (method, target)
        if route == ("GET", "/v1/backups"):
            return Reply(200, self.listing)
        if route == ("GET", "/v1/backups/remote"):
            return Reply(200, {"backups": self.remote_listing})
        if route == ("GET", "/v1/backups/remotes"):
            return Reply(200, {"remotes": self.remotes})
        if route == ("POST", "/v1/backups"):
            return Reply(202, {"id": OP_A})
        if route in (("POST", "/v1/backups/verify"), ("POST", "/v1/backups/preview")):
            self.inspected.append((target, body or {}))
            if (body or {}).get("name") == OLD and not (body or {}).get("key"):
                return Reply(
                    400, {"error": "the archive is encrypted: a key or passphrase is needed"}
                )
            extra = {
                "preview": ["config"],
                "lines": ["config.yaml replaced", "2 module instances restarted"],
            }
            return Reply(200, {**summary(), **(extra if target.endswith("preview") else {})})
        if route == ("POST", "/v1/backups/restore"):
            if (body or {}).get("passphrase") != PASSPHRASE:
                return Reply(403, {"error": "refused"})
            return Reply(202, {"id": OP_A})
        if route == ("POST", "/v1/backups/keygen"):
            return Reply(
                200,
                {
                    "public": "age1publickey",
                    "identity": "AGE-SECRET-KEY-1SECRET",
                    "kept": bool((body or {}).get("keep")),
                },
            )
        if method == "PUT" and target.startswith("/v1/backups/remotes/"):
            return Reply(204, {})
        if method == "POST" and target.endswith("/test"):
            return Reply(204, {})
        return None

    def request(
        self,
        method: str,
        target: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Reply:
        if target.startswith("/v1/backups"):
            self.calls.append((method, target, body, timeout))
            if self.down:
                raise InstallerUnavailable("installer socket /run/mm3-install/installer.sock: gone")
            if (method, target) in self.replies:
                return self.replies[(method, target)]
            reply = self.backups(method, target, body)
            if reply is None:
                raise AssertionError(f"unscripted request {method} {target}")
            return reply
        return super().request(method, target, body, timeout=timeout)

    def upload(self, target: str, chunks: Any, length: int, *, timeout: float = 60.0) -> Reply:
        data = b"".join(chunks)
        self.calls.append(("POST", target, {"length": length}, timeout))
        self.uploaded.append((target, length, data))
        return Reply(201, {"upload": UPLOAD, "size": len(data)})

    def stream(self, method: str, target: str, *, timeout: float = 60.0) -> Any:
        self.calls.append((method, target, None, timeout))
        return FakeStream(200, b"archive-bytes")


@pytest.fixture
def scripted() -> ScriptedBackups:
    return ScriptedBackups()


# --- the page and its files -----------------------------------------------------------------


async def test_core_361_the_page_needs_a_session_and_has_no_inline_code(tmp_path: Path) -> None:
    auth = WebAuth.open(tmp_path / "web")
    app = protect(admin_app(auth), lambda: [], "admin")
    async with client(app) as c:
        r = await c.get("/admin/backups")
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login?next=/admin/backups"
        c.cookies.update(session_cookie(auth))
        page = await c.get("/admin/backups")
        script = await c.get("/admin/static/backups.js")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == CSP
    text = page.text
    assert '<script type="module" src="/admin/static/backups.js"></script>' in text
    assert "<mm3-admin-backups></mm3-admin-backups>" in text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)  # CORE-63
    assert "<style" not in text
    assert " style=" not in text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_core_361_every_admin_page_links_to_the_backups() -> None:
    html = admin_backups_page()
    assert '<a href="/admin/backups" aria-current="page">Backups</a>' in html
    assert '<a href="/admin/catalog">' in html
    assert '<a href="/admin/config">' in html
    assert TEXT["CORE_WEB_NAV_BACKUPS"] == "Backups"


def test_core_361_the_script_keeps_to_the_rules_of_the_admin() -> None:
    source = (ADMIN_STATIC_DIR / "backups.js").read_text()
    assert " style=" not in source  # CORE-63
    for banned in ("window.confirm", "window.alert", "window.prompt", "confirm(", "alert("):
        assert banned not in source, banned  # CORE-384
    assert 'type="password"' in source  # CORE-132
    assert 'autocomplete="off"' in source
    for storage in ("localStorage", "sessionStorage", "document.cookie"):
        assert storage not in source, storage  # secrets and keys are never kept
    assert "fetch(" not in source  # only the REST API through admin.js (CORE-298)
    assert "XMLHttpRequest" not in source  # the upload goes through admin.js
    assert "innerHTML" not in source  # texts are escaped by the templates
    for pattern in ("actionRow(", "banner(", "closeOnBackdrop(", "upload("):
        assert pattern in source, pattern
    admin = (ADMIN_STATIC_DIR / "admin.js").read_text()
    assert "export function upload(" in admin


def test_core_361_texts_are_in_five_languages_with_the_same_placeholders() -> None:
    source = (ADMIN_STATIC_DIR / "backups.js").read_text()
    used = set(re.findall(r"CORE_ADMIN_BACKUPS(?:_[A-Z_]*[A-Z])?", source))
    # Keys built from a stem: actions, parts, kinds, encryption modes and operation states.
    stems = {
        "ACTION": ("BACKUP", "RESTORE"),
        "PART": ("CONFIG", "DATA", "MODULES", "SECRETS"),
        "KIND": ("MANUAL", "SCHEDULED", "PRE"),
        "MODE": ("NONE", "CONFIGURED", "KEY", "PASSPHRASE"),
        "STATE": (
            "QUEUED", "VERIFYING", "BACKING_UP", "RESTORING", "BUILDING", "SWITCHING",
            "RESTARTING", "CHECKING", "DONE", "ROLLED_BACK", "FAILED",
        ),
    }  # fmt: skip
    for stem, names in stems.items():
        used.discard(f"CORE_ADMIN_BACKUPS_{stem}")
        used.update(f"CORE_ADMIN_BACKUPS_{stem}_{name}" for name in names)
    used.update({"CORE_WEB_NAV_BACKUPS", "CORE_API_UPLOAD_TOO_LARGE", "CORE_API_REAUTH_FAILED"})
    english = admin_texts("en")
    assert len(used) > 100
    for lang in LANGS:
        table = admin_texts(lang)
        for key in sorted(used):
            assert table.get(key), (lang, key)
            assert set(re.findall(r"\{(\w+)\}", table[key])) == set(
                re.findall(r"\{(\w+)\}", english[key])
            ), (lang, key)
    for lang in LANGS[1:]:
        for key in ("CORE_ADMIN_BACKUPS_CREATE_NOW", "CORE_ADMIN_BACKUPS_DOWNLOAD"):
            assert admin_texts(lang)[key] != english[key], (lang, key)


# --- the logic of the page (CORE-361) -------------------------------------------------------
#
# backups.js runs in the browser; its decisions do not touch the DOM, so Node runs them here with
# stubs for the modules it imports.

_STUBS = {
    "lit.mjs": "export const html=(s,...v)=>({s,v});export const nothing=Symbol('nothing');"
    "export const render=()=>{};",
    "admin.mjs": (
        "export class ApiError extends Error{constructor(p){super(p.code?'text:'+p.code:p.detail);"
        "this.problem=p;}}"
        "export const t=(k,p={})=>k+(Object.keys(p).length?JSON.stringify(p):'');"
        "export const api=()=>{};export const fmtTime=(v)=>'@'+v;export const loadTexts=()=>{};"
        "export const upload=()=>{};export class AdminChannel{}"
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
const { cliCommand, reasonFor, operationBanner, problemText, checkFile, createBody, sourceOf,
  restoreBody, formatParams, parseParams, sizeText, isFinal, isBackupOp, MAX_UPLOAD } = page;
const free = {available: true, busy: false, locked_until: null};
const NOW = Date.parse('2026-11-14T12:00:00+00:00');

// Sizes.
assert.equal(sizeText(0), '0 B');
assert.equal(sizeText(1023), '1023 B');
assert.equal(sizeText(1024), '1.0 KiB');
assert.equal(sizeText(2621440), '2.5 MiB');
assert.equal(sizeText(2 * 1024 ** 3), '2.0 GiB');
assert.equal(sizeText(150 * 1024 ** 2), '150 MiB');
assert.equal(sizeText(-1), '');
assert.equal(sizeText('x'), '');
assert.equal(MAX_UPLOAD, 2 * 1024 ** 3);

// The CLI alternatives.
assert.equal(cliCommand('backup'), 'mm3 backup create');
assert.equal(cliCommand('restore', 'a.mm3backup'), 'mm3 backup restore a.mm3backup');
assert.equal(cliCommand('restore'), 'mm3 backup restore <file>');
assert.equal(cliCommand('verify', 'a.mm3backup'), 'mm3 backup verify a.mm3backup');
assert.equal(cliCommand('keygen'), 'mm3 backup keygen');

// The installer comes first: unknown, away, locked, busy.
const reason = (inst, mine = false) => { const r = reasonFor(inst, mine, NOW); return r && r.key.replace('CORE_ADMIN_BACKUPS_REASON_', ''); };
assert.equal(reason(null), 'CHECKING');
assert.equal(reason({...free, available: false}), 'AWAY');
assert.equal(reason({...free, locked_until: '2026-11-14T12:30:00+00:00'}), 'LOCKED');
assert.equal(reason({...free, locked_until: '2026-11-14T11:30:00+00:00'}), null);
assert.equal(reason({...free, busy: true}), 'BUSY');
assert.equal(reason(free, true), 'BUSY');
assert.equal(reason(free), null);

// The banner of an operation.
const op = (state, extra = {}) => ({action: 'backup', name: null, to: 'a.mm3backup', state, error: null, ...extra});
assert.equal(operationBanner(op('backing_up')).kind, 'info');
assert.match(operationBanner(op('backing_up')).text, /CORE_ADMIN_BACKUPS_OP_RUNNING/);
assert.match(operationBanner(op('restoring', {action: 'restore'})).text, /CORE_ADMIN_BACKUPS_STATE_RESTORING/);
assert.equal(operationBanner(op('done')).kind, 'ok');
assert.match(operationBanner(op('done')).text, /"name":"a.mm3backup"/);
assert.equal(operationBanner(op('rolled_back')).kind, 'warn');
assert.equal(operationBanner(op('failed', {error: 'disk full'})).kind, 'error');
assert.match(operationBanner(op('failed', {error: 'disk full'})).text, /"error":"disk full"/);
assert.deepEqual(['queued', 'restoring', 'done', 'rolled_back', 'failed'].map((s) => isFinal(op(s))), [false, false, true, true, true]);
assert.equal(isFinal(null), false);
assert.equal(isBackupOp({action: 'backup'}), true);
assert.equal(isBackupOp({action: 'restore'}), true);
assert.equal(isBackupOp({action: 'install'}), false);
assert.equal(isBackupOp(null), false);

// A failed request: the catalog text of the code; a lock adds when it ends; a refusal adds the detail.
assert.equal(problemText(new ApiError({code: 'CORE_API_REAUTH_FAILED'})), 'text:CORE_API_REAUTH_FAILED');
assert.match(problemText(new ApiError({code: 'CORE_API_INSTALLER_LOCKED', locked_until: 'T'})), /^text:CORE_API_INSTALLER_LOCKED CORE_ADMIN_BACKUPS_LOCKED_UNTIL\{"time":"@T"\}$/);
assert.equal(problemText(new ApiError({code: 'CORE_API_INVALID_REQUEST', detail: 'a key is needed'})), 'text:CORE_API_INVALID_REQUEST: a key is needed');
assert.equal(problemText(new Error('boom')), 'boom');

// Which files can be uploaded.
const file = (name, size) => ({name, size});
assert.equal(checkFile(null).key, 'CORE_ADMIN_BACKUPS_UPLOAD_NONE');
assert.equal(checkFile(file('a.tar', 10)).key, 'CORE_ADMIN_BACKUPS_UPLOAD_NAME');
assert.equal(checkFile(file('a.mm3backup', 0)).key, 'CORE_ADMIN_BACKUPS_UPLOAD_EMPTY');
assert.equal(checkFile(file('a.mm3backup', MAX_UPLOAD + 1)).key, 'CORE_ADMIN_BACKUPS_UPLOAD_LARGE');
assert.equal(checkFile(file('a.mm3backup', MAX_UPLOAD)), null);
assert.equal(checkFile(file('a.mm3backup', 1)), null);

// The body of a backup: secrets only when encrypted; one way of encrypting.
assert.deepEqual(createBody({parts: ['config', 'data', 'modules'], mode: 'none'}), {parts: ['config', 'data', 'modules']});
assert.deepEqual(createBody({parts: ['config', 'secrets'], mode: 'none'}), {parts: ['config']});
assert.deepEqual(createBody({parts: ['secrets', 'config'], mode: 'configured'}), {parts: ['config', 'secrets'], use_configured_key: true});
assert.deepEqual(createBody({parts: ['data'], mode: 'key', recipient: ' age1abc '}), {parts: ['data'], recipient: 'age1abc'});
assert.deepEqual(createBody({parts: ['data'], mode: 'passphrase', passphrase: 'pw'}), {parts: ['data'], archive_passphrase: 'pw'});
assert.deepEqual(createBody({parts: [], mode: 'none'}), {});

// The source of a verify or a restore; no path.
assert.deepEqual(sourceOf({name: 'a.mm3backup'}), {name: 'a.mm3backup'});
assert.deepEqual(sourceOf({name: 'a.mm3backup', remote: true}), {name: 'a.mm3backup', remote: true});
assert.deepEqual(sourceOf({upload: 'f'.repeat(32)}), {upload: 'f'.repeat(32)});
assert.deepEqual(restoreBody({source: {name: 'a'}, parts: ['config'], instance: ' clock ', key: ' k '}), {source: {name: 'a'}, parts: ['config'], instance: 'clock', key: 'k'});
assert.deepEqual(restoreBody({source: {name: 'a'}, parts: [], instance: '', key: ''}), {source: {name: 'a'}});

// The parameters of a remote: key=value lines, a masked secret goes back as the mask.
assert.equal(formatParams({host: 'nas', pass: {'!masked': null}}), 'host=nas\npass=!masked');
assert.equal(formatParams(null), '');
assert.deepEqual(parseParams('host=nas\n# note\n\npass=!masked\nkey_pem = a=b'), {params: {host: 'nas', pass: {'!masked': null}, key_pem: 'a=b'}});
assert.deepEqual(parseParams('host=nas\nbad line'), {error: 2});
assert.deepEqual(parseParams('=x'), {error: 1});
assert.deepEqual(parseParams('host=a\nhost=b'), {error: 2});
assert.deepEqual(parseParams('1x=a'), {error: 1});
assert.deepEqual(parseParams(''), {params: {}});
console.log('ok');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_361_the_decisions_of_the_page(tmp_path: Path) -> None:
    source = (ADMIN_STATIC_DIR / "backups.js").read_text()
    source = source.replace('"/admin/static/lit-html.js"', '"./lit.mjs"')
    source = source.replace('"/admin/static/admin.js"', '"./admin.mjs"')
    source = source.replace('"/admin/static/ui.js"', '"./ui.mjs"')
    assert "/admin/static/" not in source
    (tmp_path / "backups.mjs").write_text(source)
    for name, text in _STUBS.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "check.mjs").write_text(_NODE_TEST)
    out = subprocess.run(  # noqa: S603
        [
            shutil.which("node") or "node",
            str(tmp_path / "check.mjs"),
            (tmp_path / "backups.mjs").as_uri(),
            (tmp_path / "admin.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr + out.stdout
    assert out.stdout.strip() == "ok"


# --- the page in Chromium -------------------------------------------------------------------


async def test_core_361_backups_page_in_chromium(
    mirror_admin: tuple[WebGateway, WebAuth, Engine], scripted: ScriptedBackups
) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    gateway, auth, _engine = mirror_admin
    base = f"http://127.0.0.1:{gateway.admin.port}"
    cookie = session_cookie(auth)[ADMIN_COOKIE]
    problems: list[str] = []
    english = admin_texts("en")

    def posts(target: str) -> list[dict[str, Any]]:
        return [c[2] or {} for c in scripted.calls if (c[0], c[1]) == ("POST", target)]

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
            await page.goto(f"{base}/admin/backups")
            root = page.locator("mm3-admin-backups")
            dialog = root.locator("dialog.backups-dialog")

            # The list: newest first, kind, parts, size, encryption; the last results of the schedule.
            rows = root.locator("table.backup-list tbody tr")
            await rows.first.wait_for()
            assert await rows.evaluate_all("(r) => r.map((x) => x.dataset.name)") == [NEW, OLD]
            first = await rows.first.inner_text()
            assert "manual" in first
            assert "2.5 MiB" in first
            assert "configuration" in first
            assert await rows.nth(1).locator("td:nth-child(6)").inner_text() == "Yes"
            assert (
                await page.locator("header.bar nav a[aria-current=page]").inner_text() == "Backups"
            )
            assert await attribute_is(
                rows.first.locator('a[data-action="download"]'), "href", f"/api/v1/backups/{NEW}"
            )
            assert await attribute_is(
                rows.first.locator('a[data-action="download"]'), "download", NEW
            )
            status = await root.locator("section[aria-labelledby=status-title]").inner_text()
            assert OLD in status
            assert "no route to host" in status

            # A backup: secrets need encryption; the request carries the parts and no passphrase.
            create = root.locator("section[aria-labelledby=create-title]")
            secrets = create.locator("input[name=part][value=secrets]")
            assert await secrets.is_disabled()
            await create.locator("input[name=mode][value=passphrase]").check()
            assert not await secrets.is_disabled()
            await create.locator("button[data-action=backup]").click()
            assert "Enter a passphrase" in await root.locator("div.alert").inner_text()
            assert posts("/v1/backups") == []
            await create.locator("input[name=mode][value=none]").check()
            assert await secrets.is_disabled()
            await create.locator("button[data-action=backup]").click()
            await until(lambda: count_is(root.locator("section.operation"), 1), "operation card")
            body = posts("/v1/backups")[-1]
            assert body["parts"] == ["config", "data", "modules"]
            assert "archive_passphrase" not in body
            assert "mm3 backup create" in await create.inner_text()
            scripted.operations = [operation(OP_A, "done", action="backup", name=None, to=NEW)]
            await until(
                lambda: text_has(root.locator("div.alert").first, "done"), "the done banner"
            )

            # Verify: the summary and the warnings of the archive.
            await rows.first.locator("button[data-action=verify]").click()
            await dialog.locator("dl.facts").wait_for()
            assert await dialog.locator("h2").inner_text() == f"Verify {NEW}"
            text = await dialog.inner_text()
            assert english["CORE_ADMIN_BACKUPS_VERIFY_OK"] in text
            assert HOST in text
            assert "Taken on another host" in text
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")

            # An encrypted archive asks for its key: a refusal shows the installer's words.
            await rows.nth(1).locator("button[data-action=verify]").click()
            await dialog.locator("div.alert[role=alert]").wait_for()
            assert "a key or passphrase is needed" in await dialog.locator("div.alert").inner_text()
            await dialog.locator("input[name=key]").fill("AGE-SECRET-KEY-1X")
            await dialog.locator("button.confirm").click()
            await dialog.locator("dl.facts").wait_for()
            assert scripted.inspected[-1][1]["key"] == "AGE-SECRET-KEY-1X"
            assert "AGE-SECRET-KEY-1X" not in await page.content()
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")

            # Restore: the preview first, then the passphrase; a wrong one shows the code's text.
            await rows.first.locator("button[data-action=restore]").click()
            await dialog.locator("ul.preview").wait_for()
            assert await dialog.locator("h2").inner_text() == f"Restore {NEW}?"
            text = await dialog.inner_text()
            assert "config.yaml replaced" in text
            assert "2 module instances restarted" in text
            assert f"mm3 backup restore {NEW}" in text
            assert scripted.inspected[-1][0] == "/v1/backups/preview"
            assert (
                await dialog.locator("input[name=passphrase]").get_attribute("type") == "password"
            )
            await dialog.locator("input[name=passphrase]").fill("wrong")
            await dialog.locator("button.confirm").click()
            error = dialog.locator("div.alert[role=alert]")
            await error.wait_for()
            assert await error.inner_text() == english["CORE_API_REAUTH_FAILED"]
            assert await dialog.locator("input[name=passphrase]").input_value() == ""
            await dialog.locator("input[name=passphrase]").fill(PASSPHRASE)
            await dialog.locator("input[name=totp]").fill(TOTP)
            await dialog.locator("button.confirm").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")
            sent = posts("/v1/backups/restore")[-1]
            assert sent["name"] == NEW
            assert sent["passphrase"] == PASSPHRASE
            assert sent["totp"] == TOTP
            assert "(session)" in sent["client"]  # the engine names the caller
            assert "path" not in sent
            assert PASSPHRASE not in await page.content()

            # An upload: the file goes to the installer and waits for a verify or a restore.
            upload = root.locator("section[aria-labelledby=upload-title]")
            await upload.locator("input[name=archive]").set_input_files(
                {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"abc"}
            )
            assert "ends in .mm3backup" in await root.locator("div.alert").inner_text()
            assert scripted.uploaded == []
            await upload.locator("input[name=archive]").set_input_files(
                {
                    "name": "mirror.mm3backup",
                    "mimeType": "application/octet-stream",
                    "buffer": b"abcdef",
                }
            )
            await upload.locator("div.staged").wait_for()
            assert scripted.uploaded == [("/v1/backups/upload", 6, b"abcdef")]
            assert "mirror.mm3backup" in await upload.locator("div.staged").inner_text()
            await upload.locator("button[data-action=restore-upload]").click()
            await dialog.locator("ul.preview").wait_for()
            assert await dialog.locator("h2").inner_text() == "Restore the uploaded file?"
            assert scripted.inspected[-1][1]["upload"] == UPLOAD
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")

            # A key: the identity is shown once, with its warning, and is gone once closed.
            await root.locator("button[data-action=keygen]").click()
            await dialog.locator("input[name=keep]").check()
            await dialog.locator("button.confirm").click()
            await dialog.locator("textarea[name=identity]").wait_for()
            assert (
                await dialog.locator("textarea[name=identity]").input_value()
                == "AGE-SECRET-KEY-1SECRET"
            )
            assert await dialog.locator("textarea[name=public]").input_value() == "age1publickey"
            assert posts("/v1/backups/keygen")[-1]["keep"] is True
            assert "not shown again" in await dialog.inner_text()
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")
            assert "AGE-SECRET-KEY-1SECRET" not in await page.content()
            await root.locator("button[data-action=keygen]").click()
            assert await dialog.locator("textarea[name=identity]").count() == 0
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")

            # Remotes: the secret is masked, saved unchanged with the passphrase, and tested.
            remotes = root.locator("table.remote-list tbody tr")
            await remotes.first.wait_for()
            assert await remotes.first.get_attribute("data-remote") == "nas"
            await remotes.first.locator("button[data-action=test-remote]").click()
            await until(lambda: text_has(root.locator("div.alert").first, "nas"), "the test banner")
            await remotes.first.locator("button[data-action=edit-remote]").click()
            params = dialog.locator("textarea[name=params]")
            assert await params.input_value() == "host=nas.local\npass=!masked"
            assert await dialog.locator("input[name=name]").get_attribute("readonly") is not None
            await dialog.locator("input[name=passphrase]").fill(PASSPHRASE)
            await dialog.locator("button.confirm").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")
            put = next(c for c in scripted.calls if c[0] == "PUT")
            assert put[1] == "/v1/backups/remotes/nas"
            assert put[2] == {
                "type": "sftp",
                "params": {"host": "nas.local", "pass": {"!masked": None}},
                "passphrase": PASSPHRASE,
            }
            # A badly written line is refused before anything is sent.
            await root.locator("button[data-action=add-remote]").click()
            await dialog.locator("input[name=name]").fill("cloud")
            await dialog.locator("textarea[name=params]").fill("host nas")
            await dialog.locator("input[name=passphrase]").fill(PASSPHRASE)
            await dialog.locator("button.confirm").click()
            await dialog.locator("div.alert[role=alert]").wait_for()
            assert "Line 1" in await dialog.locator("div.alert").inner_text()
            assert len([c for c in scripted.calls if c[0] == "PUT"]) == 1
            await dialog.locator("button.cancel").click()
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")

            # The archives on the remote: restore from there names the remote.
            await root.locator("button[data-action=show-remote]").click()
            remote_rows = root.locator("section[aria-labelledby=remote-title] tbody tr")
            await remote_rows.first.wait_for()
            assert await remote_rows.first.locator("a[data-action=download]").count() == 0
            await remote_rows.first.locator("button[data-action=restore]").click()
            await dialog.locator("h2").wait_for()
            assert scripted.inspected[-1][1] == {"name": OLD, "remote": True}
            await page.keyboard.press("Escape")
            await page.wait_for_selector("dialog.backups-dialog", state="hidden")

            # The installer away or busy: backup and restore are disabled with the reason.
            scripted.down = True
            await until(
                lambda: attribute_is(
                    create.locator("button[data-action=backup]"), "aria-disabled", "true"
                ),
                "the installer away",
            )
            assert (
                await create.locator("button[data-action=backup]").get_attribute("title")
                == (english["CORE_ADMIN_BACKUPS_REASON_AWAY"])
            )
            assert await attribute_is(
                rows.first.locator("button[data-action=restore]"), "aria-disabled", "true"
            )
            scripted.down = False
        finally:
            await browser.close()
    # The refusals the test provokes are logged by the browser; a script error is not.
    assert not [p for p in problems if "WebSocket" not in p and "Failed to load" not in p], problems
