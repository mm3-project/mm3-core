"""The player in the shell: ``mm3-player``, ``mm3-youtube``, sound, CSP and ``welcome``.

The browser tests serve their media as static files of a test module, so they
need no media service: the player takes any same-origin URL. Chromium is
started with the kiosk's ``--autoplay-policy=no-user-gesture-required``
(CORE-268), so sound is decided only by the rule of CORE-409.

Implements:
    CORE-409, CORE-413, CORE-417: Core spec 0.53, §9.7 Media service.
    MOD-103: Module Contract 1.0 rev 16, §4.5 Media.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import struct
import sys
import uuid
import wave
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from pydantic import ValidationError

from mm3.core.config import load_config
from mm3.core.config.diff import ReloadPlan
from mm3.core.config.model import Grants
from mm3.core.config.validate import ConfigResult
from mm3.core.engine import Engine
from mm3.core.i18n import compile_locales
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.security import YOUTUBE_ORIGIN, shell_csp
from mm3.core.web.sessions import ADMIN_COOKIE, DISPLAY_COOKIE
from mm3.core.web.shell import ShellSources
from mm3.core.web.view import cached_view
from mm3.core.web.ws import ClientHub
from mm3.testing import VirtualClock
from test_i18n import write_catalog

PACKAGE = """
from mm3.config import ModuleConfig
from mm3.module import MirrorModule


class MediaTestConfig(ModuleConfig):
    src: str = ""
    video: str = ""
    muted: bool = False


class MediaTest(MirrorModule):
    name = "mediatest"
    config_model = MediaTestConfig
    element = "mm3-mediatest"
    scripts = ["media.js"]
    capabilities = {"media.play", "media.youtube"}


class PlainTest(MirrorModule):
    name = "plaintest"
    config_model = MediaTestConfig
    element = "mm3-plaintest"
    scripts = ["plain.js"]
"""

ELEMENT = """
import { MirrorElement, html } from "/core/mm3.js";

class MediaTest extends MirrorElement {
  constructor() {
    super();
    this.events = [];
    for (const type of ["mm3-media-error", "mm3-media-playing"]) {
      this.shadow.addEventListener(type, (ev) => this.events.push({ type, ...ev.detail }));
    }
  }
  render() {
    const c = this.config;
    if (c.video) return html`<mm3-youtube video-id=${c.video} ?muted=${c.muted}></mm3-youtube>`;
    return html`<mm3-player src=${c.src} ?muted=${c.muted}></mm3-player>`;
  }
}
customElements.define("mm3-mediatest", MediaTest);
"""

PLAIN = ELEMENT.replace("MediaTest", "PlainTest").replace("mm3-mediatest", "mm3-plaintest")

BASE = "/modules/mediatest/0"

CONFIG = f"""
engine:
  locale: en
modules:
  - module: mediatest
    id: tone
    position: top_left
    config: {{src: {BASE}/tone.wav}}
  - module: mediatest
    id: quiet
    position: top_right
    config: {{src: {BASE}/tone.wav, muted: true}}
  - module: mediatest
    id: picture
    position: middle_center
    config: {{src: {BASE}/ok.png}}
  - module: mediatest
    id: broken
    position: bottom_left
    config: {{src: {BASE}/bad.png}}
  - module: mediatest
    id: missing
    position: bottom_right
    config: {{src: {BASE}/missing.png}}
"""

YOUTUBE = """
engine:
  locale: en
modules:
  - module: mediatest
    id: granted
    position: top_left
    grants: {youtube: true}
    config: {video: dQw4w9WgXcQ}
  - module: mediatest
    id: nogrant
    position: top_right
    config: {video: dQw4w9WgXcQ}
  - module: plaintest
    id: nocap
    position: bottom_left
    grants: {youtube: true}
    config: {video: dQw4w9WgXcQ}
"""


def png() -> bytes:
    """A valid 2x2 grey PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def wav(path: Path) -> None:
    """Two seconds of a quiet square wave, 8 kHz mono."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(
            b"".join(struct.pack("<h", 800 if (i // 20) % 2 else -800) for i in range(16000))
        )


@pytest.fixture(scope="module")
def media_module(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("pkg")
    pkg = root / "mm3_mediatest"
    static = pkg / "static"
    static.mkdir(parents=True)
    (pkg / "__init__.py").write_text(PACKAGE)
    (static / "media.js").write_text(ELEMENT)
    (static / "plain.js").write_text(PLAIN)
    (static / "ok.png").write_bytes(png())
    (static / "bad.png").write_bytes(b"not a picture")
    wav(static / "tone.wav")
    for lang in ("en", "it", "fr", "es", "de"):
        write_catalog(pkg / "locales", lang, {"HEADER": "Media"})
    compile_locales(pkg / "locales")
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("mm3_mediatest")
    finally:
        sys.path.remove(str(root))


class Mirror:
    def __init__(
        self, hub: ClientHub, gateway: WebGateway, holder: dict[str, ConfigResult], etc: Path
    ) -> None:
        self.hub = hub
        self.gateway = gateway
        self.holder = holder
        self.etc = etc
        self.registry: Registry | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.gateway.mirror.port}"

    @property
    def admin(self) -> str:
        return f"http://127.0.0.1:{self.gateway.admin.port}"

    def reload(self, text: str) -> ConfigResult:
        assert self.registry is not None
        (self.etc / "config.yaml").write_text(text)
        result = load_config(
            self.etc / "config.yaml", registry=self.registry, env_file=self.etc / "none.env"
        )
        assert result.ok, result.records
        self.holder["result"] = result
        return result

    def admin_cookie(self) -> str:
        auth = self.hub.auth
        rec = auth.tokens.verify(auth.tokens.new("me"), "admin")
        assert rec is not None
        return auth.sessions.create(rec)


async def start_mirror(tmp_path: Path, module: Any, text: str) -> AsyncIterator[Mirror]:
    etc = tmp_path / "etc"
    etc.mkdir(parents=True)
    (etc / "config.yaml").write_text(text)
    registry = Registry()
    registry.add(module.MediaTest)
    registry.add(module.PlainTest)
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
    engine = Engine(settings, registry=registry, clock=VirtualClock(), state_dir=tmp_path / "s")
    view = cached_view(lambda: holder["result"], registry, i18n=engine.i18n)
    hub = ClientHub(view, WebAuth.open(tmp_path / "s"), "1.0+test", hello_timeout=1)
    engine.attach_clients(hub)
    sources = ShellSources(view, lambda: holder["result"], engine.i18n)
    gateway = WebGateway(settings, tmp_path / "s", hub=hub, sources=sources)
    await engine.start(result.instance_specs())
    await gateway.start()
    mirror = Mirror(hub, gateway, holder, etc)
    mirror.registry = registry
    try:
        yield mirror
    finally:
        await gateway.stop()
        await engine.stop()


@pytest.fixture
async def mirror(tmp_path: Path, media_module: Any) -> AsyncIterator[Mirror]:
    async for m in start_mirror(tmp_path / "mirror", media_module, CONFIG):
        yield m


@pytest.fixture
async def tube(tmp_path: Path, media_module: Any) -> AsyncIterator[Mirror]:
    async for m in start_mirror(tmp_path / "tube", media_module, YOUTUBE):
        yield m


def hello(role: str = "browser", key: str | None = None) -> str:
    data: dict[str, Any] = {
        "protocol": 1,
        "client_id": str(uuid.uuid4()),
        "role": role,
        "viewport": [1280, 720],
        "dpr": 1,
    }
    if key is not None:
        data["key"] = key
    return json.dumps({"v": 1, "kind": "hello", "data": data})


async def welcome_of(mirror: Mirror, role: str = "browser") -> dict[str, Any]:
    key = mirror.hub.kiosk_keys.issue() if role == "kiosk" else None
    async with websockets.connect(f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws") as ws:
        await ws.send(hello(role, key))
        frame: dict[str, Any] = json.loads(await ws.recv())
    assert frame["kind"] == "welcome", frame
    return frame["data"]  # type: ignore[no-any-return]


# --- CORE-417: grants and welcome ---------------------------------------------------------


def test_core_417_grants_take_media_and_youtube() -> None:
    grants = Grants.model_validate({"media": ["door", "*"], "youtube": True})
    assert (grants.media, grants.youtube) == (["door", "*"], True)
    assert (Grants().media, Grants().youtube) == ([], False)
    with pytest.raises(ValidationError):
        Grants.model_validate({"media": ["Door"]})
    with pytest.raises(ValidationError):
        Grants.model_validate({"youtube": "yes please"})


async def test_core_417_welcome_audio_only_on_the_kiosk(mirror: Mirror) -> None:
    assert (await welcome_of(mirror, "browser"))["audio"] is False
    assert (await welcome_of(mirror, "kiosk"))["audio"] is True


async def test_core_417_browser_audio_turns_sound_on_for_browsers(mirror: Mirror) -> None:
    mirror.reload(CONFIG + "media:\n  browser_audio: true\n")
    assert (await welcome_of(mirror, "browser"))["audio"] is True
    assert (await welcome_of(mirror, "kiosk"))["audio"] is True


async def test_core_417_youtube_needs_capability_and_grant(tube: Mirror) -> None:
    entries = {i["id"]: i["youtube"] for i in (await welcome_of(tube))["instances"]}
    assert entries == {"granted": True, "nogrant": False, "nocap": False}


async def frames_until_control(ws: Any, wait: float = 0.5) -> dict[str, Any] | None:
    try:
        async with asyncio.timeout(wait):
            while True:
                frame: dict[str, Any] = json.loads(await ws.recv())
                if frame["kind"] == "control":
                    return frame
    except TimeoutError:
        return None


async def test_core_417_browser_audio_change_resyncs_the_shells(mirror: Mirror) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{mirror.gateway.mirror.port}/ws") as ws:
        await ws.send(hello())
        page = await frames_until_control(ws, wait=2)
        assert page is not None
        assert page["data"]["action"] == "page"
        # a media change that leaves welcome alone: no resync
        same = mirror.reload(CONFIG + "media:\n  limits: {processes: 2}\n")
        await mirror.hub.reloaded(same, ReloadPlan(media=True, sections=["media"]))
        assert await frames_until_control(ws) is None
        louder = mirror.reload(CONFIG + "media:\n  browser_audio: true\n")
        await mirror.hub.reloaded(louder, ReloadPlan(media=True, sections=["media"]))
        frame = await frames_until_control(ws, wait=2)
        assert frame is not None
        assert frame["data"] == {"action": "reload", "scope": "resync"}


async def test_core_417_youtube_grant_change_resyncs_the_shells(tube: Mirror) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{tube.gateway.mirror.port}/ws") as ws:
        await ws.send(hello())
        assert await frames_until_control(ws, wait=2) is not None
        changed = tube.reload(YOUTUBE.replace("    grants: {youtube: true}\n", "", 1))
        await tube.hub.reloaded(changed, ReloadPlan(restart=["granted"]))
        frame = await frames_until_control(ws, wait=2)
        assert frame is not None
        assert frame["data"] == {"action": "reload", "scope": "resync"}


# --- CORE-413: the CSP exception ------------------------------------------------------------


def test_core_413_shell_csp_adds_only_frame_src() -> None:
    plain = shell_csp(preview=False, youtube=False)
    framed = shell_csp(preview=False, youtube=True)
    assert "frame-src" not in plain
    assert framed == plain + f"; frame-src {YOUTUBE_ORIGIN}"
    assert "frame-ancestors 'self'" in shell_csp(preview=True, youtube=True)
    assert "media-src 'self';" in framed  # CORE-409: never blob:


async def test_core_413_frame_src_only_while_an_instance_may_embed(
    mirror: Mirror, tube: Mirror
) -> None:
    async with httpx.AsyncClient(base_url=mirror.base) as c:
        assert "frame-src" not in (await c.get("/")).headers["content-security-policy"]
    async with httpx.AsyncClient(base_url=tube.base) as c:
        csp = (await c.get("/")).headers["content-security-policy"]
        assert f"frame-src {YOUTUBE_ORIGIN}" in csp
        assert csp.count("frame-src") == 1
        # only the shell page: scripts and other files keep the CSP of CORE-63
        assert "frame-src" not in (await c.get("/core/mm3.js")).headers["content-security-policy"]
    async with httpx.AsyncClient(base_url=tube.admin) as c:
        c.cookies.set(ADMIN_COOKIE, tube.admin_cookie())
        preview = await c.get("/admin/preview")
        assert preview.status_code == 200
        csp = preview.headers["content-security-policy"]
        assert f"frame-src {YOUTUBE_ORIGIN}" in csp
        assert "frame-ancestors 'self'" in csp


# --- CORE-409, CORE-413: the elements in a browser -------------------------------------------


@pytest.fixture
async def browser() -> AsyncIterator[Any]:
    async_api = pytest.importorskip("playwright.async_api")
    async with async_api.async_playwright() as p:
        try:
            b = await p.chromium.launch(
                executable_path=os.environ.get("MM3_CHROMIUM") or None,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
        except Exception as exc:  # no browser on this machine
            pytest.skip(f"Chromium not available: {exc}")
        try:
            yield b
        finally:
            await b.close()


def el(instance: str) -> str:
    return f"document.querySelector('[data-instance=\"{instance}\"] .mm3-fit > *')"


def inner(instance: str, tag: str) -> str:
    """The media element inside the engine element inside the module element."""
    return f"{el(instance)}?.shadowRoot.querySelector('{tag}')?.shadowRoot"


async def events(page: Any, instance: str) -> list[dict[str, Any]]:
    return await page.evaluate(f"() => {el(instance)}?.events ?? []")  # type: ignore[no-any-return]


async def open_shell(mirror: Mirror, browser: Any, *, kiosk: bool = False) -> Any:
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    page = await context.new_page()
    if kiosk:
        await page.goto(f"{mirror.base}/#kiosk={mirror.hub.kiosk_keys.issue()}")
    else:
        auth = mirror.hub.auth
        rec = auth.tokens.verify(auth.tokens.new("hall", "display"), "display")
        assert rec is not None
        await context.add_cookies(
            [{"name": DISPLAY_COOKIE, "value": auth.sessions.create(rec), "url": mirror.base}]
        )
        await page.goto(mirror.base + "/")
    return page


@pytest.mark.browser
async def test_core_409_player_plays_and_reports_errors(mirror: Mirror, browser: Any) -> None:
    page = await open_shell(mirror, browser)
    playing = "(es) => es.some((e) => e.type === 'mm3-media-playing')"
    await page.wait_for_function(f"() => ({playing})({el('picture')}?.events ?? [])")
    assert await page.evaluate(f"() => !!{inner('picture', 'mm3-player')}.querySelector('img')")
    await page.wait_for_function(f"() => ({el('broken')}?.events ?? []).length > 0")
    assert await events(page, "broken") == [
        {"type": "mm3-media-error", "reason": "decode", "status": 200}
    ]
    await page.wait_for_function(f"() => ({el('missing')}?.events ?? []).length > 0")
    assert await events(page, "missing") == [
        {"type": "mm3-media-error", "reason": "unavailable", "status": 404}
    ]
    # the event bubbles up to the document
    bubbled = await page.evaluate(
        """() => new Promise((resolve) => {
            document.addEventListener('mm3-media-error', () => resolve(true), { once: true });
            const p = document.createElement('mm3-player');
            p.setAttribute('src', '/nope/x.png');
            document.body.append(p);
        })"""
    )
    assert bubbled is True
    # composed: an event from inside a module's shadow root reaches the document too
    composed = await page.evaluate(
        f"""() => new Promise((resolve) => {{
            const done = (e) => resolve(e.composed);
            document.addEventListener('mm3-media-error', done, {{ once: true }});
            {el("missing")}.shadowRoot.querySelector('mm3-player').start();
        }})"""
    )
    assert composed is True
    await page.context.close()


@pytest.mark.browser
async def test_core_409_sound_only_on_the_kiosk(mirror: Mirror, browser: Any) -> None:
    audio = f"() => {inner('tone', 'mm3-player')}?.querySelector('audio')?.muted"
    quiet = f"() => {inner('quiet', 'mm3-player')}?.querySelector('audio')?.muted"
    page = await open_shell(mirror, browser)
    await page.wait_for_function(f"() => ({audio})() !== undefined")
    assert await page.evaluate(audio) is True  # a LAN browser is muted
    await page.context.close()
    kiosk = await open_shell(mirror, browser, kiosk=True)
    await kiosk.wait_for_function(f"() => ({audio})() !== undefined")
    assert await kiosk.evaluate(audio) is False
    await kiosk.wait_for_function(f"() => ({quiet})() !== undefined")
    assert await kiosk.evaluate(quiet) is True  # the muted attribute always wins
    await kiosk.context.close()


@pytest.mark.browser
async def test_core_409_stop_closes_and_start_plays_again(mirror: Mirror, browser: Any) -> None:
    page = await open_shell(mirror, browser)
    player = f"{el('picture')}.shadowRoot.querySelector('mm3-player')"
    await page.wait_for_function(f"() => !!{inner('picture', 'mm3-player')}?.querySelector('img')")
    stopped = f"() => {{ {player}.stop(); return {player}.shadowRoot.childElementCount; }}"
    assert await page.evaluate(stopped) == 0
    await page.evaluate(f"() => {player}.start()")
    await page.wait_for_function(f"() => !!{player}.shadowRoot.querySelector('img')")
    await page.evaluate(f"() => {player}.removeAttribute('src')")
    assert await page.evaluate(f"() => {player}.shadowRoot.childElementCount") == 0
    await page.context.close()


@pytest.mark.browser
async def test_core_409_retries_every_30_s(mirror: Mirror, browser: Any) -> None:
    context = await browser.new_context()
    page = await context.new_page()
    await page.clock.install()
    hits: list[str] = []
    state = {"ok": False}

    async def later(route: Any) -> None:
        hits.append(route.request.url)
        if state["ok"]:
            await route.fulfill(status=200, content_type="image/png", body=png())
        else:
            await route.fulfill(status=503, body="busy")

    await page.route("**/later.png", later)
    await page.goto(mirror.base + "/core/theme.css")
    await page.evaluate(
        """() => import('/core/mm3.js').then(() => {
            window.seen = [];
            const p = document.createElement('mm3-player');
            for (const t of ['mm3-media-error', 'mm3-media-playing'])
              p.addEventListener(t, (e) => window.seen.push({ type: t, ...e.detail }));
            p.setAttribute('src', '/later.png');
            document.body.append(p);
        })"""
    )
    await page.wait_for_function("() => window.seen.length === 1")
    assert await page.evaluate("() => window.seen") == [
        {"type": "mm3-media-error", "reason": "unavailable", "status": 503}
    ]
    state["ok"] = True
    await page.clock.run_for(29_000)
    assert await page.evaluate("() => window.seen.length") == 1
    await page.clock.run_for(1_500)
    await page.wait_for_function("() => window.seen.length === 2")
    assert (await page.evaluate("() => window.seen"))[1] == {"type": "mm3-media-playing"}
    await context.close()


@pytest.mark.browser
async def test_core_409_hls_without_native_support(mirror: Mirror, browser: Any) -> None:
    page = await open_shell(mirror, browser)
    await page.wait_for_function(f"() => !!{el('picture')}")
    native = await page.evaluate(
        "() => document.createElement('video').canPlayType('application/vnd.apple.mpegurl')"
    )
    if native:
        pytest.skip("this Chromium plays HLS natively")
    got = await page.evaluate(
        """() => new Promise((resolve) => {
            const p = document.createElement('mm3-player');
            p.addEventListener('mm3-media-error', (e) => resolve(e.detail));
            p.setAttribute('src', '/media/door/hls/index.m3u8');
            document.body.append(p);
        })"""
    )
    assert got == {"reason": "unsupported"}
    await page.context.close()


@pytest.mark.browser
async def test_core_409_preview_plays_media_from_the_admin_listener(
    mirror: Mirror, browser: Any
) -> None:
    context = await browser.new_context()
    await context.add_cookies(
        [{"name": ADMIN_COOKIE, "value": mirror.admin_cookie(), "url": mirror.admin}]
    )
    page = await context.new_page()
    await page.goto(mirror.admin + "/admin/preview")
    await page.wait_for_function(f"() => !!{el('picture')}")
    src = await page.evaluate(
        """() => {
            const p = document.createElement('mm3-player');
            p.setAttribute('src', '/media/files/files/a.png');
            document.body.append(p);
            return p.shadowRoot.querySelector('img').getAttribute('src');
        }"""
    )
    assert src == "/admin/preview/media/files/files/a.png"
    await context.close()


@pytest.mark.browser
async def test_core_413_youtube_iframe_only_with_capability_and_grant(
    tube: Mirror, browser: Any
) -> None:
    context = await browser.new_context()
    await context.route(f"{YOUTUBE_ORIGIN}/**", lambda r: r.fulfill(body="<p>video</p>"))
    page = await context.new_page()
    await page.goto(tube.base + "/")
    frame = f"{inner('granted', 'mm3-youtube')}?.querySelector('iframe')"
    await page.wait_for_function(f"() => !!{frame}")
    attrs = await page.evaluate(
        f"""() => {{
            const f = {frame};
            return [f.getAttribute('src'), f.referrerPolicy, f.allow];
        }}"""
    )
    assert attrs[0].startswith(f"{YOUTUBE_ORIGIN}/embed/dQw4w9WgXcQ?")
    assert "mute=1" in attrs[0]  # a LAN browser: no sound
    assert attrs[1:] == ["strict-origin-when-cross-origin", "autoplay; encrypted-media"]
    for instance in ("nogrant", "nocap"):
        root = inner(instance, "mm3-youtube")
        await page.wait_for_function(f"() => !!{root}?.querySelector('.placeholder')")
        assert await page.evaluate(f"() => !{root}.querySelector('iframe')")
        assert await events(page, instance) == [{"type": "mm3-media-error", "reason": "forbidden"}]
    await context.close()
