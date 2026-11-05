"""Milestone 6, part D1: YAML and Form views, Open in editor, editor undo, form texts, language."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import Field, SecretStr

from mm3.config import LocalizedText, ModuleConfig
from mm3.core.config import load_config
from mm3.core.config.editor import ConfigEditor
from mm3.core.config.history import ConfigHistory
from mm3.core.config.manager import ConfigManager
from mm3.core.diag.secrets import Masker, config_secrets
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import ADMIN_STATIC_DIR, admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.pages import form_texts, language_names
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"
SECRET = "sk-editor-secret-9"  # noqa: S105 - test data


class WeatherLikeConfig(ModuleConfig):
    api_key: SecretStr = Field(json_schema_extra={"secret": True})
    city: str = "Rome"
    lat: float = Field(41.9, ge=-90, le=90)
    title: LocalizedText | None = None
    show_wind: bool = True


class WeatherLike(MirrorModule):
    name = "wlike"
    config_model = WeatherLikeConfig
    element = "mm3-wlike"


MAIN = f"""# The mirror
engine:
  locale: en  # language
modules:
  - module: wlike
    id: w
    position: top_left
    config:
      api_key: {SECRET}
      city: !env [CITY, Rome]
"""


def registry() -> Registry:
    reg = Registry()
    reg.add(WeatherLike)
    return reg


@dataclass
class World:
    root: Path
    history: ConfigHistory
    services: ApiServices
    auth: WebAuth
    token: str
    engine: Engine
    tmp: Path


def make(tmp_path: Path) -> tuple[Path, ConfigHistory, ConfigEditor, Any, Path]:
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
    secrets = config_secrets(result, env, environ={})
    editor = ConfigEditor(history, lambda: Masker(secrets))
    return root, history, editor, result, env


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    root, history, editor, result, env = make(tmp_path)
    reg = registry()
    engine = Engine(
        EngineSettings(
            timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
        ),
        registry=reg,
        clock=VirtualClock(),
        state_dir=tmp_path / "state",
    )
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    services = ApiServices(engine, manager, editor=editor)
    auth = WebAuth.open(tmp_path / "web")
    token = auth.tokens.new("ed")
    await engine.start(result.instance_specs())
    try:
        yield World(root, history, services, auth, token, engine, tmp_path)
    finally:
        await engine.stop()


def client(world: World, *, bearer: bool = True) -> httpx.AsyncClient:
    app = protect(admin_app(world.auth, api=world.services), lambda: [], "admin")
    headers = {"authorization": f"Bearer {world.token}"} if bearer else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)),
        base_url=ADMIN,
        headers=headers,
    )


def session(world: World) -> str:
    rec = world.auth.tokens.verify(world.token, "admin")
    assert rec is not None
    return world.auth.sessions.create(rec)


# --- CORE-301 (0.27): documents ------------------------------------------------------------


async def test_core_301_patch_returns_documents(world: World) -> None:
    async with client(world) as c:
        etag = (await c.get("/api/v1/config")).json()["etag"]
        r = await c.post("/api/v1/config/patch", json={"base": etag})
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    main = docs["config.yaml"]
    assert main["engine"] == {"locale": "en"}
    cfg = main["modules"][0]["config"]
    assert cfg["api_key"] == {"!masked": None}  # CORE-303
    assert cfg["city"] == {"!env": ["CITY", "Rome"]}  # CORE-320
    assert SECRET not in r.text
    (world.root / "config.yaml").write_text(MAIN)  # unchanged


# --- CORE-324: version files --------------------------------------------------------------


async def test_core_324_version_files(world: World) -> None:
    async with client(world) as c:
        r = await c.get("/api/v1/config/history/1")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1
        (main,) = [f for f in body["files"] if f["role"] == "main"]
        assert "locale: en" in main["content"]
        assert SECRET not in r.text
        missing = await c.get("/api/v1/config/history/42")
        assert missing.status_code == 404
        assert missing.json()["code"] == "CORE_API_NOT_FOUND"


# --- CORE-309: admin language ---------------------------------------------------------------


async def test_core_309_session_locale(world: World) -> None:
    sid = session(world)
    async with client(world, bearer=False) as c:
        c.cookies.set(ADMIN_COOKIE, sid)
        origin = {"origin": ADMIN}
        r = await c.put("/api/v1/session/locale", json={"locale": "it"}, headers=origin)
        assert r.status_code == 200, r.text
        assert r.json() == {"locale": "it", "effective": "it"}
        texts = (await c.get("/admin/i18n.json")).json()
        assert texts["locale"] == "it"
        assert texts["picked"] == "it"
        assert texts["messages"]["CORE_ADMIN_TAB_FORM"] == "Modulo"
        # Accept-Language is ignored.
        de = await c.get("/admin/i18n.json", headers={"accept-language": "de"})
        assert de.json()["locale"] == "it"
        back = await c.put("/api/v1/session/locale", json={"locale": None}, headers=origin)
        assert back.json() == {"locale": None, "effective": "en"}
        assert (await c.get("/admin/i18n.json")).json()["locale"] == "en"
        bad = await c.put("/api/v1/session/locale", json={"locale": "pt"}, headers=origin)
        assert bad.status_code == 422
    async with client(world) as c:
        r = await c.put("/api/v1/session/locale", json={"locale": "it"})
        assert r.status_code == 422  # bearer token: no session to keep it with
        assert r.json()["code"] == "CORE_API_INVALID_REQUEST"


def test_core_309_language_names() -> None:
    names = {x["code"]: x["name"] for x in language_names()}
    assert names == {
        "en": "English",
        "it": "Italiano",
        "fr": "Français",
        "es": "Español",
        "de": "Deutsch",
    }


# --- CORE-310: form texts -------------------------------------------------------------------


def test_core_310_form_texts(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    for lang, label in (("en", "City"), ("it", "Città")):
        (root / lang).mkdir(parents=True)
        (root / lang / "messages.json").write_text(
            json.dumps({"CONFIG_CITY_LABEL": label, "CONFIG_CITY_HELP": "help", "OTHER": "x"})
        )
    texts = form_texts("it", {"wlike": root, "none": None})
    assert texts["wlike"] == {"CONFIG_CITY_LABEL": "Città", "CONFIG_CITY_HELP": "help"}
    assert "none" not in texts
    assert form_texts("fr", {"wlike": root})["wlike"]["CONFIG_CITY_LABEL"] == "City"


# --- CORE-323: the CodeMirror bundle ---------------------------------------------------------


def test_core_323_vendored_bundle_matches_its_manifest() -> None:
    for manifest in (
        ADMIN_STATIC_DIR / "vendor" / "vendor.json",
        ADMIN_STATIC_DIR.parent / "vendor" / "vendor.json",
    ):
        data = json.loads(manifest.read_text())
        for entry in data["vendored"]:
            assert entry["license"]
            for rel, digest in entry["files"].items():
                path = manifest.parent / rel
                assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, rel
    bundle = (ADMIN_STATIC_DIR / "vendor" / "codemirror" / "codemirror.js").read_text()
    for name in ("EditorView", "yaml", "linter", "autocompletion", "syntaxTree"):
        assert name in bundle
    assert "eval(" not in bundle


async def test_core_323_bundle_only_on_the_admin_listener(world: World) -> None:
    async with client(world) as c:
        r = await c.get("/admin/static/codemirror.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert (await c.get("/admin/static/vendor/vendor.json")).status_code == 404
    mirror = protect(mirror_app(world.auth), lambda: [], "mirror")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mirror, client=("127.0.0.1", 5)), base_url="http://m"
    ) as c:
        assert (await c.get("/admin/static/codemirror.js")).status_code == 404


# --- the page in Chromium ---------------------------------------------------------------------


def chromium() -> str | None:
    try:
        from module_projects import chromium_available
    except ImportError:  # pragma: no cover
        return None
    return os.environ.get("MM3_CHROMIUM", "") if chromium_available() else None


@pytest.fixture
async def page_world(tmp_path: Path) -> AsyncIterator[tuple[str, str, Path, ConfigHistory]]:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    root, history, editor, result, env = make(tmp_path)
    reg = registry()
    settings = EngineSettings().model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    engine = Engine(settings, registry=reg, clock=VirtualClock(), state_dir=tmp_path / "state")
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    services = ApiServices(engine, manager, editor=editor)
    gateway = WebGateway(settings, tmp_path, api=services)
    await engine.start(result.instance_specs())
    await gateway.start()
    rec = gateway.auth.tokens.verify(gateway.auth.tokens.new("ui"), "admin")
    assert rec is not None
    sid = gateway.auth.sessions.create(rec)
    try:
        yield f"http://127.0.0.1:{gateway.admin.port}", sid, root, history
    finally:
        await gateway.stop()
        await engine.stop()


async def browse(base: str, sid: str) -> Any:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path=chromium() or None)
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    await context.add_cookies([{"name": ADMIN_COOKIE, "value": sid, "url": base}])
    page = await context.new_page()
    problems: list[str] = []
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on(
        "response", lambda r: problems.append(f"{r.status} {r.url}") if r.status >= 500 else None
    )
    return pw, browser, page, problems


async def test_core_151_form_edit_undo_redo_and_save(
    page_world: tuple[str, str, Path, ConfigHistory],
) -> None:
    base, sid, root, history = page_world
    pw, browser, page, problems = await browse(base, sid)
    try:
        await page.goto(f"{base}/admin/config")
        await page.wait_for_selector("mm3-schema-form .field")
        locale = page.locator('mm3-schema-form [data-path="/locale"] input')
        await locale.fill("it")
        await locale.press("Tab")
        await page.wait_for_selector("text=Unsaved changes")
        await page.wait_for_selector("button:has-text('Undo') >> text=engine.locale en → it")
        # CORE-151: undo and redo through the toolbar and the keyboard.
        await page.keyboard.press("Control+z")
        await page.wait_for_function("() => !document.querySelector('.savebar')")
        await page.keyboard.press("Control+Shift+z")
        await page.wait_for_selector("text=Unsaved changes")
        # A module section: the form shows the secret as kept and !env as written.
        await page.click("nav.sections >> text=w")
        await page.wait_for_selector("text=kept")
        await page.wait_for_selector(".tag >> text=!env CITY (Rome)")
        lat = page.locator('mm3-schema-form [data-path="/config/lat"] input')
        await lat.fill("45.5")
        await lat.press("Tab")
        await page.wait_for_selector("button:has-text('Undo') >> text=modules/w.config.lat")
        # CORE-152: "Undo last change here" on engine reverts only the engine step.
        await page.click("nav.sections >> text=engine")
        await page.click("text=Undo last change here")
        await page.wait_for_function(
            "() => document.querySelector("
            "'mm3-schema-form [data-path=\"/locale\"] input').value === 'en'"
        )
        # Save: the dialog shows the diff, then version 2 is written.
        await page.click(".savebar >> button.primary")
        await page.wait_for_selector("dialog.save >> text=modules/w")
        await page.click("dialog.save >> button.primary")
        await page.wait_for_selector("text=Saved as version 2.")
    finally:
        await browser.close()
        await pw.stop()
    text = (root / "config.yaml").read_text()
    assert "lat: 45.5" in text
    assert "locale: en  # language" in text  # comments survive
    assert f"api_key: {SECRET}" in text  # the masked secret kept its value
    assert history.store.meta(2).origin == "admin"
    assert not problems, problems


async def test_core_323_yaml_view_lints_and_completes(
    page_world: tuple[str, str, Path, ConfigHistory],
) -> None:
    base, sid, _, _ = page_world
    pw, browser, page, problems = await browse(base, sid)
    try:
        await page.goto(f"{base}/admin/config")
        await page.click("nav.tabs >> text=YAML")
        await page.wait_for_selector(".cm-editor .cm-content")
        content = page.locator(".cm-content")
        assert "locale: en" in await content.inner_text()
        # A schema error: the server's records show in the gutter after 300 ms.
        await page.click(".cm-line >> text=locale: en")
        await page.keyboard.press("End")
        await page.keyboard.press("Enter")
        await page.keyboard.type("bogus_key: 1")
        await page.wait_for_selector(".cm-lint-marker-error", timeout=5000)
        await page.wait_for_selector("td >> text=bogus_key")
        # Undo the typing (grouped as one step) with the shared history.
        await page.wait_for_selector(
            "button:has-text('Undo') >> text=config.yaml: YAML", timeout=5000
        )
        await page.keyboard.press("Control+z")
        for _ in range(50):
            if "bogus_key" not in await content.inner_text():
                break
            await asyncio.sleep(0.1)
        assert "bogus_key" not in await content.inner_text()
        # In-house completion of keys from the schema.
        await page.click(".cm-line >> text=locale: en")
        await page.keyboard.press("End")
        await page.keyboard.press("Enter")
        await page.keyboard.press("Home")
        await page.keyboard.type("  time")
        await page.wait_for_selector(".cm-tooltip-autocomplete >> text=timezone", timeout=5000)
    finally:
        await browser.close()
        await pw.stop()
    assert not problems, problems


async def yaml_line(page: Any, text: str) -> None:
    """Type ``text`` as a new line after ``locale: en`` in the YAML view."""
    await page.click(".cm-line >> text=locale: en")
    await page.keyboard.press("End")
    await page.keyboard.press("Enter")
    await page.keyboard.type(text)


async def test_core_151_ctrl_z_in_the_yaml_view_undoes_one_step(
    page_world: tuple[str, str, Path, ConfigHistory],
) -> None:
    # The page's own shortcut handler must leave keys in the editor (inside a
    # shadow root) to the editor's keymap, or one Ctrl+Z undoes two steps.
    base, sid, _, _ = page_world
    pw, browser, page, problems = await browse(base, sid)
    try:
        await page.goto(f"{base}/admin/config")
        await page.click("nav.tabs >> text=YAML")
        await page.wait_for_selector(".cm-editor .cm-content")
        content = page.locator(".cm-content")
        undo = "button:has-text('Undo') >> text=config.yaml: YAML"
        await yaml_line(page, "theme: one")
        await page.wait_for_selector(undo, timeout=5000)
        await yaml_line(page, "timezone: UTC")
        await page.wait_for_function(
            "() => document.querySelector('mm3-admin-config').draft.undoStack.length === 2",
            timeout=5000,
        )
        await page.keyboard.press("Control+z")
        for _ in range(50):
            if "timezone: UTC" not in await content.inner_text():
                break
            await asyncio.sleep(0.1)
        await page.wait_for_function(
            "() => !document.querySelector('mm3-admin-config').busy", timeout=5000
        )
        text = await content.inner_text()
        assert "timezone: UTC" not in text
        assert "theme: one" in text
        steps = await page.evaluate(
            "() => document.querySelector('mm3-admin-config').draft.undoStack.length"
        )
        assert steps == 1
    finally:
        await browser.close()
        await pw.stop()
    assert not problems, problems


async def test_core_151_typing_during_a_refresh_is_kept(
    page_world: tuple[str, str, Path, ConfigHistory],
) -> None:
    # The answer of /config/patch must not replace text typed while it was out.
    base, sid, _, _ = page_world
    pw, browser, page, problems = await browse(base, sid)
    try:
        await page.goto(f"{base}/admin/config")
        await page.click("nav.tabs >> text=YAML")
        await page.wait_for_selector(".cm-editor .cm-content")
        content = page.locator(".cm-content")
        sent: list[str] = []

        async def slow(route: Any) -> None:
            sent.append(route.request.post_data or "")
            await asyncio.sleep(1.5)
            await route.continue_()

        await page.route("**/api/v1/config/patch", slow)
        await yaml_line(page, "theme: one")
        for _ in range(50):  # the typing step is flushed 1 s after the last key
            if sent:
                break
            await asyncio.sleep(0.1)
        assert sent
        assert "theme: one" in sent[0]
        await page.keyboard.type("  # more")
        await page.unroute("**/api/v1/config/patch")
        await page.wait_for_selector(
            "button:has-text('Undo') >> text=config.yaml: YAML", timeout=10000
        )
        await asyncio.sleep(0.5)
        assert "theme: one  # more" in await content.inner_text()
        await page.wait_for_function(
            "() => [...document.querySelector('mm3-admin-config').draft.files.values()]"
            ".some((f) => f.text.includes('theme: one  # more'))",
            timeout=5000,
        )
    finally:
        await browser.close()
        await pw.stop()
    assert not problems, problems


async def test_core_324_open_in_editor_and_language(
    page_world: tuple[str, str, Path, ConfigHistory],
) -> None:
    base, sid, root, history = page_world
    (root / "config.yaml").write_text(MAIN.replace("locale: en", "locale: de"))
    with history.store.lock():
        history.record_disk()
    pw, browser, page, problems = await browse(base, sid)
    try:
        await page.goto(f"{base}/admin/config")
        await page.click("nav.tabs >> text=History")
        await page.click("tr:has(td:text-is('1')) >> text=Open in editor")
        await page.wait_for_selector("text=Unsaved changes")
        value = await page.locator('mm3-schema-form [data-path="/locale"] input').input_value()
        assert value == "en"
        await page.wait_for_selector("button:has-text('Undo') >> text=Open version 1")
        # CORE-309: the language picker keeps Italian for this session.
        await page.select_option("header select.lang", "it")
        await page.wait_for_selector("nav.tabs >> text=Modulo")
        await page.wait_for_selector("text=Modifiche non salvate")  # the draft survived the reload
    finally:
        await browser.close()
        await pw.stop()
    assert not problems, problems
