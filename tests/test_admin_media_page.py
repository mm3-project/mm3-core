"""Tests of the Media page ``/admin/media`` (CORE-414).

Implements:
- CORE-414 (Core spec 0.58, §9.7): the page has the parts Sources, Files and Shares, reads the media
  routes when it opens and after its own actions, previews a source on request in an
  ``mm3-player``, shows a source full screen and stops it, browses, uploads to, makes folders in
  and deletes from ``files/`` (a folder that is not empty asks twice), keeps ``shares/`` read-only
  and syncs a share through the ``installer`` topic.
- CORE-392 (Core spec 0.47, §6.11): the preview of a camera whose device has ``images: false``
  is disabled with the reason, and a ``forbidden`` answer of the player says the same.
- CORE-411 (Core spec 0.54, §9.7): Show full screen sends ``POST /api/v1/media/takeover`` with the
  file of a folder source, Stop full screen sends ``DELETE``.
- CORE-384, CORE-385, CORE-386, CORE-387 (Core spec 0.44, §9.1): the dialogs, action rows and
  banners of the shared patterns.
"""

# ruff: noqa: E501 - the Node script below is data, kept in long lines like the checks it runs
# ruff: noqa: F811 - the shared fixtures are imported, then named by the tests
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from mm3.core.engine import Engine
from mm3.core.web.apps import ADMIN_STATIC_DIR, admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.pages import TEXT, admin_media_page, admin_texts
from mm3.core.web.security import CSP
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.shell import STATIC_DIR, VENDOR_DIR
from test_admin_catalog import OP_A, ScriptedInstaller, operation
from test_admin_catalog_page import (
    attribute_is,
    count_is,
    mirror_admin,  # noqa: F401 - the shared running-gateway fixture
    text_has,
    until,
)
from test_admin_debug_page import chromium, client, session_cookie

LANGS = ("en", "it", "fr", "es", "de")


@pytest.fixture
def scripted() -> ScriptedInstaller:
    return ScriptedInstaller()


# --- the page and its files -----------------------------------------------------------------


async def test_core_414_the_page_needs_a_session_and_has_no_inline_code(tmp_path: Path) -> None:
    auth = WebAuth.open(tmp_path / "web")
    app = protect(admin_app(auth), lambda: [], "admin")
    async with client(app) as c:
        r = await c.get("/admin/media")
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login?next=/admin/media"
        c.cookies.update(session_cookie(auth))
        page = await c.get("/admin/media")
        script = await c.get("/admin/static/mediapage.js")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == CSP
    text = page.text
    assert '<script type="module" src="/admin/static/mediapage.js"></script>' in text
    assert "<mm3-admin-media></mm3-admin-media>" in text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)  # CORE-63
    assert "<style" not in text
    assert " style=" not in text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_core_414_every_admin_page_links_to_the_media_page() -> None:
    html = admin_media_page()
    assert '<a href="/admin/media" aria-current="page">Media</a>' in html
    assert '<a href="/admin/backups">' in html
    assert '<a href="/admin/debug">' in html
    assert TEXT["CORE_WEB_NAV_MEDIA"] == "Media"


def test_core_414_the_script_keeps_to_the_rules_of_the_admin() -> None:
    source = (ADMIN_STATIC_DIR / "mediapage.js").read_text()
    assert " style=" not in source  # CORE-63
    for banned in ("window.confirm", "window.alert", "window.prompt", "confirm(", "alert("):
        assert banned not in source.replace("confirmDialog(", ""), banned  # CORE-384
    for storage in ("localStorage", "sessionStorage", "document.cookie"):
        assert storage not in source, storage
    assert "fetch(" not in source  # only the REST API through admin.js (CORE-298)
    assert "XMLHttpRequest" not in source  # the upload goes through admin.js
    assert "innerHTML" not in source  # texts are escaped by the templates
    assert "setInterval" not in source  # the page never polls (CORE-414)
    for pattern in (
        "actionRow(",
        "banner(",
        "closeOnBackdrop(",
        "confirmDialog(",
        "upload(",
        "onStatus(",
    ):
        assert pattern in source, pattern
    assert "<mm3-player" in source  # the preview is the engine's player (CORE-409)
    assert "/admin/preview/media" in source  # never the mirror's /media/ (CORE-400)
    assert 'subscribe("installer")' in source  # a sync is followed on its topic (CORE-356)


def test_core_414_texts_are_in_five_languages_with_the_same_placeholders() -> None:
    source = (ADMIN_STATIC_DIR / "mediapage.js").read_text()
    used = set(re.findall(r"CORE_ADMIN_MEDIA_[A-Z_]*[A-Z]", source))
    # Keys built from a stem: kinds of source, of entry, methods and states of shares.
    stems = {
        "KIND": ("RTSP", "STREAM", "CAMERA", "FILES"),
        "FILE": ("FOLDER", "VIDEO", "AUDIO", "IMAGE"),
        "METHOD": ("SYNC", "MOUNT"),
        "SHARE_STATE": ("IDLE", "SYNCING", "OK", "ERROR"),
        "SHARE_ERROR": ("REMOTE", "TOO_LARGE", "FAILED", "MOUNT", "PATH"),
        "PLAYER": ("UNAVAILABLE", "UNSUPPORTED", "DECODE", "NETWORK"),
    }
    for stem, names in stems.items():
        used.discard(f"CORE_ADMIN_MEDIA_{stem}")
        used.update(f"CORE_ADMIN_MEDIA_{stem}_{name}" for name in names)
    used.add("CORE_ADMIN_MEDIA_KIND")  # the column heading shares its stem
    used.add("CORE_WEB_NAV_MEDIA")
    english = admin_texts("en")
    assert len(used) > 80
    for lang in LANGS:
        table = admin_texts(lang)
        for key in sorted(used):
            assert table.get(key), (lang, key)
            assert set(re.findall(r"\{(\w+)\}", table[key])) == set(
                re.findall(r"\{(\w+)\}", english[key])
            ), (lang, key)
    for lang in LANGS[1:]:
        for key in ("CORE_ADMIN_MEDIA_PREVIEW_NAME", "CORE_ADMIN_MEDIA_SHOW_NAME"):
            assert admin_texts(lang)[key] != english[key], (lang, key)


# --- the logic of the page (CORE-414) -------------------------------------------------------
#
# mediapage.js runs in the browser; its decisions do not touch the DOM, so Node runs them here with
# stubs for the modules it imports.

_STUBS = {
    "lit.mjs": "export const html=(s,...v)=>({s,v});export const nothing=Symbol('nothing');"
    "export const render=()=>{};",
    "admin.mjs": (
        "export class ApiError extends Error{constructor(p){super(p.code?'text:'+p.code:p.detail);"
        "this.problem=p;}}"
        "export const t=(k,p={})=>k+(Object.keys(p).length?JSON.stringify(p):'');"
        "export const api=()=>{};export const fmtTime=(v)=>'@'+v;export const loadTexts=()=>{};"
        "export const upload=()=>{};export const onStatus=()=>()=>{};export class AdminChannel{}"
    ),
    "ui.mjs": (
        "export class Messages{};export const actionRow=()=>null;export const banner=()=>null;"
        "export const closeOnBackdrop=()=>{};export const confirmDialog=()=>true;"
    ),
    "media.mjs": "export const stateText=(s)=>s;export const reasonText=(s)=>s;",
}

_NODE_TEST = r"""
import assert from 'node:assert/strict';
const page = await import(process.argv[2]);
const { ApiError } = await import(process.argv[3]);
const { sizeText, encodePath, isFolderSource, previewUrl, previewReason, writeReason, syncReason,
  problemText, isFinal } = page;

// Sizes.
assert.equal(sizeText(0), '0 B');
assert.equal(sizeText(1023), '1023 B');
assert.equal(sizeText(1024), '1.0 KiB');
assert.equal(sizeText(2621440), '2.5 MiB');
assert.equal(sizeText(150 * 1024 ** 2), '150 MiB');
assert.equal(sizeText(null), '');
assert.equal(sizeText(undefined), '');
assert.equal(sizeText(-1), '');

// Paths in a URL: each piece escaped, the slashes kept.
assert.equal(encodePath('files/my photos/a#1.jpg'), 'files/my%20photos/a%231.jpg');

// A files source is a folder unless its path is a media file (CORE-411).
const src = (o) => ({name: 's', kind: 'rtsp', delivery: 'mjpeg', device: null, path: null, ...o});
assert.equal(isFolderSource(src({kind: 'files', path: 'files/photos'})), true);
assert.equal(isFolderSource(src({kind: 'files', path: 'shares/nas/2024'})), true);
assert.equal(isFolderSource(src({kind: 'files', path: 'files/intro.MP4'})), false);
assert.equal(isFolderSource(src({kind: 'files', path: 'files/a.png'})), false);
assert.equal(isFolderSource(src({kind: 'camera'})), false);
assert.equal(isFolderSource(null), false);

// The preview URL follows the delivery and stays under /admin/preview/media (CORE-400).
assert.equal(previewUrl(src({name: 'door'})), '/admin/preview/media/door/stream.mjpg');
assert.equal(previewUrl(src({name: 'door', delivery: 'hls'})), '/admin/preview/media/door/hls/index.m3u8');
assert.equal(previewUrl(src({name: 'my cam'})), '/admin/preview/media/my%20cam/stream.mjpg');
assert.equal(previewUrl(src({kind: 'files', delivery: 'file', path: 'files/a b/intro.mp4'})), '/admin/preview/media/files/files/a%20b/intro.mp4');
assert.equal(previewUrl(src({kind: 'files', delivery: 'file', path: 'files/photos'})), null);
assert.equal(previewUrl(src({name: 'old', delivery: undefined})), '/admin/preview/media/old/stream.mjpg');

// Why a preview cannot start: nothing selected, a folder, a camera with images off (CORE-392).
const key = (r) => r && r.key.replace('CORE_ADMIN_MEDIA_REASON_', '');
const devices = [{name: 'usb0', images: false}, {name: 'usb1', images: true}];
assert.equal(key(previewReason(null, devices)), 'NO_SOURCE');
assert.equal(key(previewReason(src({kind: 'files', path: 'files/photos'}), devices)), 'FOLDER');
assert.equal(key(previewReason(src({kind: 'camera', device: 'usb0'}), devices)), 'IMAGES');
assert.deepEqual(previewReason(src({kind: 'camera', device: 'usb0'}), devices).params, {device: 'usb0'});
assert.equal(previewReason(src({kind: 'camera', device: 'usb1'}), devices), null);
assert.equal(previewReason(src({kind: 'camera', device: 'gone'}), devices), null);
assert.equal(previewReason(src({kind: 'rtsp'}), devices), null);
assert.equal(previewReason(src({kind: 'camera', device: 'usb0'}), undefined), null);

// Only files/ can be changed; shares/ and the list of shares are read-only (CORE-405).
assert.equal(writeReason('files'), null);
assert.equal(writeReason('files/a/b'), null);
assert.equal(key(writeReason('shares')), 'SHARES_READONLY');
assert.equal(key(writeReason('shares/nas/2024')), 'SHARES_READONLY');

// Sync now: a mount share, a share that syncs, the installer away or busy (CORE-406).
const free = {available: true, busy: false, locked_until: null};
const share = (o) => ({name: 'nas', method: 'sync', state: 'ok', ...o});
assert.equal(key(syncReason(share({method: 'mount'}), free, null)), 'MOUNT');
assert.equal(key(syncReason(share({state: 'syncing'}), free, null)), 'SYNCING');
assert.equal(key(syncReason(share({}), free, {id: 'x', share: 'nas'})), 'SYNCING');
assert.equal(syncReason(share({}), free, {id: 'x', share: 'other'}), null);
assert.equal(key(syncReason(share({}), {...free, available: false}, null)), 'INSTALLER_AWAY');
assert.equal(key(syncReason(share({}), null, null)), 'INSTALLER_AWAY');
assert.equal(key(syncReason(share({}), {...free, busy: true}, null)), 'INSTALLER_BUSY');
assert.equal(syncReason(share({state: 'error', error: 'failed'}), free, null), null);
assert.equal(syncReason(share({state: 'idle'}), free, null), null);

// Operations: over in done, rolled_back and failed.
assert.deepEqual(['queued', 'running', 'done', 'rolled_back', 'failed'].map((s) => isFinal({state: s})), [false, false, true, true, true]);
assert.equal(isFinal(null), false);

// A failed request: the catalog text of the code; a refusal adds the detail.
assert.equal(problemText(new ApiError({code: 'CORE_API_CONFLICT'})), 'text:CORE_API_CONFLICT');
assert.equal(problemText(new ApiError({code: 'CORE_API_INVALID_REQUEST', detail: 'no such file'})), 'text:CORE_API_INVALID_REQUEST: no such file');
assert.equal(problemText(new Error('boom')), 'boom');
console.log('ok');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_414_the_decisions_of_the_page(tmp_path: Path) -> None:
    source = (ADMIN_STATIC_DIR / "mediapage.js").read_text()
    source = source.replace('"/admin/static/lit-html.js"', '"./lit.mjs"')
    source = source.replace('"/admin/static/admin.js"', '"./admin.mjs"')
    source = source.replace('"/admin/static/ui.js"', '"./ui.mjs"')
    source = source.replace('"/admin/static/media.js"', '"./media.mjs"')
    assert "/admin/static/" not in source
    (tmp_path / "mediapage.mjs").write_text(source)
    for name, text in _STUBS.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "check.mjs").write_text(_NODE_TEST)
    out = subprocess.run(  # noqa: S603
        [
            shutil.which("node") or "node",
            str(tmp_path / "check.mjs"),
            (tmp_path / "mediapage.mjs").as_uri(),
            (tmp_path / "admin.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr + out.stdout
    assert out.stdout.strip() == "ok"


# --- the page in Chromium -------------------------------------------------------------------
#
# The engine under the page is the running gateway of the other page tests, with the installer
# scripted; the media routes are answered by the test, so that each call can be seen and the media
# folder needs no ffmpeg. The admin channel and the installer topic are real.


def source(name: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "delivery": "mjpeg",
        "state": "idle",
        "viewers": 0,
        "reason": None,
        "device": None,
        "path": None,
        **extra,
    }


def listing(*entries: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "kind": kind,
            "size": None if kind == "folder" else 2_621_440,
            "modified": "2026-11-19T10:00:00+00:00",
        }
        for name, kind in entries
    ]


class FakeMedia:
    """The media routes of the engine, as far as the page reads and writes them."""

    def __init__(self) -> None:
        self.takeover: dict[str, Any] | None = None
        self.sources = [
            source("cam", "camera", device="usb0", state="running", viewers=1),
            source("door", "rtsp", delivery="hls", state="error", reason="codec"),
            source("intro", "files", delivery="file", path="files/intro.mp4"),
            source("photos", "files", delivery="file", path="files/photos"),
        ]
        self.devices = [{"name": "usb0", "kind": "camera", "images": False, "owner": "media"}]
        self.shares = [
            {
                "name": "nas",
                "method": "sync",
                "state": "ok",
                "last_sync": "2026-11-20T08:00:00+00:00",
                "size": 5_242_880,
                "error": None,
            },
            {
                "name": "usb",
                "method": "mount",
                "state": "ok",
                "last_sync": None,
                "size": None,
                "error": None,
            },
            {
                "name": "old",
                "method": "sync",
                "state": "error",
                "last_sync": None,
                "size": None,
                "error": "remote",
            },
        ]
        self.tree: dict[str, list[dict[str, Any]]] = {
            "files": listing(("photos", "folder"), ("intro.mp4", "video")),
            "files/photos": listing(("2024", "folder"), ("a.jpg", "image")),
            "files/photos/2024": listing(("b.jpg", "image")),
            "shares": listing(("nas", "folder")),
        }
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.requested: list[str] = []
        self.refuse_delete: set[str] = {"files/photos"}
        self.preview_status = 200

    def calls_to(self, method: str, path: str) -> list[tuple[dict[str, Any], str | None]]:
        return [(q, body) for m, p, q, body in self.calls if (m, p) == (method, path)]

    async def handle(self, route: Any) -> None:
        request = route.request
        url = urlparse(request.url)
        path = url.path.removeprefix("/api/v1")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if path.startswith("/installer"):
            await route.continue_()
            return
        self.calls.append((request.method, path, query, request.post_data))
        route_key = (request.method, path)
        if route_key == ("GET", "/media"):
            body: Any = {"takeover": self.takeover, "sources": self.sources, "children": []}
        elif route_key == ("GET", "/devices"):
            body = self.devices
        elif route_key == ("GET", "/media/shares"):
            body = self.shares
        elif route_key == ("GET", "/media/files"):
            body = self.tree.get(query.get("path", ""))
            if body is None:
                await route.fulfill(
                    status=404, json={"code": "CORE_API_NOT_FOUND", "detail": "no folder"}
                )
                return
        elif route_key == ("POST", "/media/takeover"):
            asked = request.post_data_json
            self.takeover = {
                "id": "abc123abc123",
                "source": asked["source"],
                "file": asked.get("file"),
                "started": "2026-11-20T09:00:00+00:00",
                "until": None,
            }
            await route.fulfill(status=202, json={"id": "abc123abc123"})
            return
        elif route_key == ("DELETE", "/media/takeover"):
            self.takeover = None
            await route.fulfill(status=204)
            return
        elif route_key == ("POST", "/media/files"):
            name = query["name"]
            self.tree[query["path"]] = [*self.tree[query["path"]], *listing((name, "video"))]
            await route.fulfill(status=201, json=listing((name, "video"))[0])
            return
        elif route_key == ("POST", "/media/folders"):
            made = request.post_data_json["path"]
            parent, name = made.rsplit("/", 1)
            self.tree[parent] = [*self.tree[parent], *listing((name, "folder"))]
            self.tree[made] = []
            await route.fulfill(status=201, json=listing((name, "folder"))[0])
            return
        elif route_key == ("DELETE", "/media/files"):
            target = query["path"]
            if target in self.refuse_delete and query.get("recursive") != "true":
                await route.fulfill(
                    status=409, json={"code": "CORE_API_CONFLICT", "detail": "not empty"}
                )
                return
            parent, name = target.rsplit("/", 1)
            self.tree[parent] = [e for e in self.tree[parent] if e["name"] != name]
            await route.fulfill(status=204)
            return
        elif request.method == "POST" and path.endswith("/sync"):
            await route.fulfill(status=202, json={"id": OP_A})
            return
        else:
            raise AssertionError(f"unscripted request {request.method} {path}")
        await route.fulfill(status=200, json=body)

    async def preview(self, route: Any) -> None:
        self.requested.append(urlparse(route.request.url).path)
        await route.fulfill(status=self.preview_status, content_type="video/mp4", body=b"")


async def core_files(route: Any) -> None:
    """Serve /core/ from the engine's files: the gateway of these tests has no shell sources."""
    path = urlparse(route.request.url).path.removeprefix("/core/")
    root = VENDOR_DIR if path.startswith("vendor/") else STATIC_DIR
    target = root / path.removeprefix("vendor/")
    kind = "text/javascript" if target.suffix == ".js" else "text/css"
    await route.fulfill(path=str(target), content_type=kind)


async def test_core_414_media_page_in_chromium(
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
    english = admin_texts("en")
    media = FakeMedia()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=exe or None)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [{"name": ADMIN_COOKIE, "value": cookie, "url": base, "httpOnly": True}]
            )
            page = await context.new_page()
            page.on(
                "console",
                lambda m: (
                    problems.append(m.text)
                    if m.type == "error" and "Failed to load resource" not in m.text
                    else None
                ),
            )
            page.on("pageerror", lambda e: problems.append(str(e)))
            await page.route("**/core/**", core_files)
            await page.route("**/api/v1/**", media.handle)
            await page.route("**/admin/preview/media/**", media.preview)
            await page.goto(f"{base}/admin/media")
            root = page.locator("mm3-admin-media")
            dialog = root.locator("dialog.media-dialog")

            # Opening the page reads the four routes and starts no preview (CORE-414).
            rows = root.locator("table.sources tbody tr")
            await rows.first.wait_for()
            assert await rows.evaluate_all("(r) => r.map((x) => x.dataset.source)") == [
                "cam",
                "door",
                "intro",
                "photos",
            ]
            assert await page.locator("header.bar nav a[aria-current=page]").inner_text() == "Media"
            assert ("GET", "/media/shares") in [(m, p) for m, p, _q, _b in media.calls]
            assert media.requested == []
            assert await root.locator("mm3-player").count() == 0
            door = await rows.nth(1).inner_text()
            assert "RTSP" in door
            assert english["CORE_ADMIN_MEDIA_STATE_ERROR"] in door
            assert english["CORE_ADMIN_MEDIA_REASON_CODEC"] in door
            assert await root.locator("p.takeover-none").count() == 1

            # The first source is selected; its camera has images off, so the preview is disabled
            # with the reason (CORE-392, CORE-386) and does nothing.
            preview = root.locator("button[data-action=preview]")
            assert await attribute_is(preview, "aria-disabled", "true")
            reason = english["CORE_ADMIN_MEDIA_REASON_IMAGES"].replace("{device}", "usb0")
            assert await attribute_is(preview, "title", reason)
            await preview.click(force=True)  # aria-disabled: the click does nothing
            assert await root.locator("mm3-player").count() == 0
            assert media.requested == []

            # A folder source has no preview either, but Show is open.
            await rows.nth(3).click()
            assert await attribute_is(preview, "aria-disabled", "true")
            assert (
                english["CORE_ADMIN_MEDIA_REASON_FOLDER"]
                in await root.locator(".action-row .reason").first.inner_text()
            )
            assert await attribute_is(
                root.locator("button[data-action=show]"), "aria-disabled", "false"
            )
            stop = root.locator("button[data-action=stop]")
            assert await attribute_is(stop, "aria-disabled", "true")  # nothing runs

            # A file source previews on request, from the admin's preview route, one at a time.
            await rows.nth(2).click()
            assert await attribute_is(preview, "aria-disabled", "false")
            await preview.click()
            player = root.locator("div.media-preview mm3-player")
            await player.wait_for()
            assert await attribute_is(player, "src", "/admin/preview/media/files/files/intro.mp4")
            await until(lambda: _any(media.requested), "the preview request")
            assert media.requested[0] == "/admin/preview/media/files/files/intro.mp4"
            await preview.click()  # now it says Stop preview
            await until(lambda: count_is(root.locator("mm3-player"), 0), "the preview stopped")

            # Show full screen of a file source: POST with the source; the page shows it and Stop opens.
            await root.locator("button[data-action=show]").click()
            await until(
                lambda: text_has(root.locator("p.takeover"), "intro"), "the running takeover"
            )
            assert media.calls_to("POST", "/media/takeover")[-1][1] == '{"source":"intro"}'
            assert await attribute_is(stop, "aria-disabled", "false")
            await stop.click()
            await until(
                lambda: count_is(root.locator("p.takeover-none"), 1), "the takeover stopped"
            )
            assert len(media.calls_to("DELETE", "/media/takeover")) == 1

            # A folder source asks for the file in a form dialog, in the folder and below it.
            await rows.nth(3).click()
            await root.locator("button[data-action=show]").click()
            await dialog.locator("ul.pick").wait_for()
            assert await dialog.locator("h2").inner_text() == "Show a file of photos"
            submit = dialog.locator("button[type=submit]")
            assert await attribute_is(submit, "aria-disabled", "true")
            await submit.click(force=True)
            assert (
                media.calls_to("POST", "/media/takeover")[-1][1] == '{"source":"intro"}'
            )  # not sent again
            await dialog.locator("button[data-open='2024']").click()
            await dialog.locator("input[name=file][value='b.jpg']").wait_for()
            await dialog.locator("input[name=file][value='b.jpg']").check()
            await submit.click()
            await page.wait_for_selector("dialog.media-dialog", state="detached")
            assert (
                media.calls_to("POST", "/media/takeover")[-1][1]
                == '{"source":"photos","file":"2024/b.jpg"}'
            )
            await until(
                lambda: text_has(root.locator("p.takeover"), "photos / 2024/b.jpg"),
                "the takeover with its file",
            )
            await stop.click()
            await until(
                lambda: count_is(root.locator("p.takeover-none"), 1), "the second takeover stopped"
            )

            # A forbidden answer of the player (the device changed) says that images are off.
            media.devices[0]["images"] = True
            media.preview_status = 403
            await root.locator("button[data-action=refresh]").click()
            await rows.nth(0).click()
            await until(
                lambda: attribute_is(preview, "aria-disabled", "false"), "the preview enabled"
            )
            await preview.click()
            await until(
                lambda: text_has(
                    root.locator("div.alert"),
                    english["CORE_ADMIN_MEDIA_PREVIEW_FORBIDDEN"].replace("{name}", "cam"),
                ),
                "the forbidden banner",
            )
            await preview.click()
            media.preview_status = 200

            # Files: the folder with its entries, size and time, and the way into a folder.
            entries = root.locator("table.entries tbody tr")
            assert await entries.evaluate_all("(r) => r.map((x) => x.dataset.entry)") == [
                "photos",
                "intro.mp4",
            ]
            assert "2.5 MiB" in await entries.nth(1).inner_text()
            assert english["CORE_ADMIN_MEDIA_FILE_VIDEO"] in await entries.nth(1).inner_text()
            await root.locator("button[data-open=photos]").click()
            await until(lambda: text_has(root.locator("nav.crumbs"), "photos"), "the folder opens")
            await root.locator("nav.crumbs button[data-path=files]").click()
            await until(
                lambda: count_is(root.locator("table.entries tbody tr"), 2), "back in files"
            )

            # Shares are read-only: Upload, New folder and Delete are disabled with the reason.
            await root.locator("button[data-root=shares]").click()
            await until(
                lambda: count_is(root.locator("table.entries tbody tr[data-entry=nas]"), 1),
                "the shares folder",
            )
            readonly = english["CORE_ADMIN_MEDIA_REASON_SHARES_READONLY"]
            for action in ("upload", "folder"):
                button = root.locator(f".media-files button[data-action={action}]")
                assert await attribute_is(button, "aria-disabled", "true"), action
                assert await attribute_is(button, "title", readonly), action
            assert await attribute_is(
                root.locator("table.entries button[data-action=delete]"), "aria-disabled", "true"
            )
            await root.locator("button[data-root=files]").click()
            await until(
                lambda: count_is(root.locator("table.entries tbody tr[data-entry=intro\\.mp4]"), 1),
                "files again",
            )

            # Upload: needs a file; sends it as the raw body to the open folder.
            upload = root.locator(".media-files button[data-action=upload]")
            assert await attribute_is(upload, "title", english["CORE_ADMIN_MEDIA_REASON_NO_FILE"])
            await root.locator("input[type=file]").set_input_files(
                {"name": "clip.mp4", "mimeType": "video/mp4", "buffer": b"0123456789"}
            )
            await until(
                lambda: attribute_is(upload, "aria-disabled", "false"), "the upload enabled"
            )
            await upload.click()
            await until(
                lambda: text_has(root.locator("div.alert"), "clip.mp4 was uploaded."),
                "the upload done banner",
            )
            query, _body = media.calls_to("POST", "/media/files")[-1]
            assert query == {"path": "files", "name": "clip.mp4"}
            await until(
                lambda: count_is(
                    root.locator("table.entries tbody tr[data-entry='clip\\.mp4']"), 1
                ),
                "the new file is listed",
            )

            # New folder: a form dialog; the path is the open folder and the name.
            await root.locator(".media-files button[data-action=folder]").click()
            await dialog.locator("input[name=name]").fill("trips")
            await dialog.locator("button[type=submit]").click()
            await page.wait_for_selector("dialog.media-dialog", state="detached")
            assert media.calls_to("POST", "/media/folders")[-1][1] == '{"path":"files/trips"}'
            await until(
                lambda: count_is(root.locator("table.entries tbody tr[data-entry=trips]"), 1),
                "the new folder is listed",
            )

            # Delete a file: a confirmation with Cancel focused and a red button (CORE-384).
            clip = root.locator("table.entries tbody tr[data-entry='clip\\.mp4']")
            await clip.locator("button[data-action=delete]").click()
            confirm = page.locator("dialog.confirm")
            await confirm.wait_for()
            assert await confirm.locator("h2").inner_text() == "Delete clip.mp4?"
            assert await confirm.locator("button.cancel").evaluate(
                "(b) => document.activeElement === b"
            )
            assert "danger" in (
                await confirm.locator("button.confirm").get_attribute("class") or ""
            )
            await confirm.locator("button.cancel").click()
            await page.wait_for_selector("dialog.confirm", state="detached")
            assert media.calls_to("DELETE", "/media/files") == []
            await clip.locator("button[data-action=delete]").click()
            await confirm.locator("button.confirm").click()
            await until(
                lambda: count_is(
                    root.locator("table.entries tbody tr[data-entry='clip\\.mp4']"), 0
                ),
                "the file is gone",
            )
            assert media.calls_to("DELETE", "/media/files")[-1][0] == {"path": "files/clip.mp4"}

            # Delete a folder that is not empty: asked again, then sent with recursive=true.
            await root.locator(
                "table.entries tbody tr[data-entry=photos] button[data-action=delete]"
            ).click()
            await confirm.locator("button.confirm").click()
            await confirm.locator("h2", has_text="everything in it").wait_for()
            await confirm.locator("button.confirm").click()
            await until(
                lambda: count_is(root.locator("table.entries tbody tr[data-entry=photos]"), 0),
                "the folder is gone",
            )
            first, second = media.calls_to("DELETE", "/media/files")[-2:]
            assert first[0] == {"path": "files/photos"}
            assert second[0] == {"path": "files/photos", "recursive": "true"}

            # Shares: state, last sync, size and the reason of an error; Sync now where it can run.
            shares = root.locator("table.shares tbody tr")
            assert await shares.evaluate_all("(r) => r.map((x) => x.dataset.share)") == [
                "nas",
                "usb",
                "old",
            ]
            assert "5.0 MiB" in await shares.nth(0).inner_text()
            assert (
                english["CORE_ADMIN_MEDIA_SHARE_ERROR_REMOTE"] in await shares.nth(2).inner_text()
            )
            sync_usb = shares.nth(1).locator("button[data-action=sync]")
            assert await attribute_is(sync_usb, "aria-disabled", "true")
            assert await attribute_is(sync_usb, "title", english["CORE_ADMIN_MEDIA_REASON_MOUNT"])
            before = len(media.calls_to("POST", "/media/shares/nas/sync"))
            await shares.nth(0).locator("button[data-action=sync]").click()
            await until(
                lambda: text_has(root.locator("div.alert"), "Syncing nas"), "the sync started"
            )
            assert len(media.calls_to("POST", "/media/shares/nas/sync")) == before + 1
            assert await attribute_is(
                shares.nth(0).locator("button[data-action=sync]"), "aria-disabled", "true"
            )
            # The end comes from the installer topic, not from polling.
            scripted.operations = [
                operation(OP_A, "done", action="media_sync", name="nas", to=None)
            ]
            await until(
                lambda: text_has(root.locator("div.alert"), "nas was synced."), "the done banner"
            )
            assert await attribute_is(
                shares.nth(0).locator("button[data-action=sync]"), "aria-disabled", "false"
            )
        finally:
            await browser.close()
    assert problems == []


async def _any(items: list[str]) -> bool:
    return bool(items)
