"""Milestone 6, part C: config set, patch, validate, save, history API and the History tab."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import Field, SecretStr

from mm3.config import ModuleConfig
from mm3.core.config import load_config
from mm3.core.config.editor import ConfigEditor, etag_of, file_role
from mm3.core.config.history import ConfigHistory, ConfigSet
from mm3.core.config.loader import PatchError, json_to_node, node_to_json, rt_load, rt_patch
from mm3.core.config.manager import ConfigManager
from mm3.core.config.schema import config_schema
from mm3.core.diag.secrets import Masker, config_secrets
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"
SECRET = "sk-config-secret-1"  # noqa: S105 - test data


class KeyConfig(ModuleConfig):
    api_key: SecretStr = Field(json_schema_extra={"secret": True})
    city: str = Field("Rome", json_schema_extra={"ui": {"order": 1, "group": "place"}})
    lat: float = Field(41.9, ge=-90, le=90)


class Keyed(MirrorModule):
    name = "keyed"
    config_model = KeyConfig


class Panel(MirrorModule):
    name = "panel"
    element = "mm3-panel"


MAIN = f"""# The mirror
engine:
  locale: en  # language
layout: !include layout.yaml
modules:
  - module: keyed
    id: k
    config:
      api_key: {SECRET}
      city: !env [CITY, Rome]
  - module: panel
    id: p
    position: top_left
"""
LAYOUT = "grid:\n  columns: 12\n  rows: 12\n"


def registry() -> Registry:
    reg = Registry()
    reg.add(Keyed)
    reg.add(Panel)
    return reg


@dataclass
class World:
    root: Path
    history: ConfigHistory
    editor: ConfigEditor
    client: httpx.AsyncClient
    engine: Engine

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(path, **kw)

    async def etag(self) -> str:
        return str((await self.get("/api/v1/config")).json()["etag"])

    def main(self) -> str:
        return (self.root / "config.yaml").read_text()

    def files(self, r: httpx.Response) -> dict[str, dict[str, Any]]:
        return {f["path"]: f for f in r.json()["files"]}


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    configure_logging("info", stream=io.StringIO(), json=True)
    root = tmp_path / "etc"
    root.mkdir()
    (root / "config.yaml").write_text(MAIN)
    (root / "layout.yaml").write_text(LAYOUT)
    (root / "custom.css").write_text("body { margin: 0 }\n")
    (root / "widget-templates").mkdir()
    (root / "widget-templates" / "card.yaml").write_text("kind: card\n")
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
    engine = Engine(
        EngineSettings(
            timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
        ),
        registry=reg,
        clock=VirtualClock(),
        state_dir=tmp_path / "state",
    )
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    services = ApiServices(engine, manager, None, None, None, "1.0+t", editor=editor)
    auth = WebAuth.open(tmp_path / "web")
    token = auth.tokens.new("cfg")
    app = protect(admin_app(auth, api=services), lambda: [], "admin")
    await engine.start(result.instance_specs())
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(
        transport=transport, base_url=ADMIN, headers={"authorization": f"Bearer {token}"}
    ) as client:
        try:
            yield World(root, history, editor, client, engine)
        finally:
            await engine.stop()


def problem(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = r.json()
    assert body["code"] == code
    return body


def masked_main(world: World) -> str:
    return world.editor.files_view(world.history.disk()[0], ["config.yaml"])[0]["content"]


# --- CORE-320: JSON Patch on round-trip documents ----------------------------------------


def test_core_320_tagged_nodes_round_trip() -> None:
    for value in (
        {"!env": "NAME"},
        {"!env": ["NAME", "x"]},
        {"!include": "f.yaml"},
        {"!masked": None},
        {"plain": {"nested": [1, "two", {"!env": "N"}]}},
    ):
        assert node_to_json(json_to_node(value)) == value
    for bad in ({"!evil": 1}, {"!env": 3}, {"!env": ["a"]}, {"!masked": "x"}):
        with pytest.raises(PatchError, match="tagged node"):
            json_to_node(bad)


def test_core_320_patch_keeps_comments_and_order() -> None:
    text = "# head\na: 1  # one\nb:\n  c: [1, 2]\nenv: !env X\n"
    out = rt_patch(
        text,
        [
            {"op": "replace", "path": "/a", "value": 2},
            {"op": "add", "path": "/b/c/-", "value": 3},
            {"op": "add", "path": "/b/key", "value": {"!env": ["KEY", "dflt"]}},
            {"op": "copy", "from": "/a", "path": "/z"},
            {"op": "move", "from": "/z", "path": "/b/y"},
            {"op": "test", "path": "/env", "value": {"!env": "X"}},
            {"op": "remove", "path": "/b/c/0"},
        ],
    )
    assert out.startswith("# head\na: 2  # one\n")
    doc = node_to_json(rt_load(out))
    assert doc == {
        "a": 2,
        "b": {"c": [2, 3], "key": {"!env": ["KEY", "dflt"]}, "y": 2},
        "env": {"!env": "X"},
    }
    for ops, where in (
        ([{"op": "remove", "path": "/nope"}], 0),
        ([{"op": "add", "path": "/a", "value": 1}, {"op": "add", "path": "/env/x", "value": 1}], 1),
        ([{"op": "test", "path": "/a", "value": 9}], 0),
        ([{"op": "jump", "path": "/a"}], 0),
        ([{"op": "move", "from": "/b", "path": "/b/inner"}], 0),
        ([{"op": "replace", "path": "", "value": 1}], 0),
    ):
        with pytest.raises(PatchError) as info:
            rt_patch(text, ops)
        assert info.value.index == where


# --- CORE-301, CORE-303, CORE-35: the config set ------------------------------------------


async def test_core_301_get_config_roles_etag_and_masking(world: World) -> None:
    r = await world.get("/api/v1/config")
    assert r.status_code == 200
    body = r.json()
    assert r.headers["etag"] == body["etag"] == etag_of(world.history.disk()[0])
    assert body["version"] == 1
    files = world.files(r)
    assert {p: f["role"] for p, f in files.items()} == {
        "config.yaml": "main",
        "layout.yaml": "include",
        "custom.css": "custom_css",
        "widget-templates/card.yaml": "widget_template",
    }
    main = files["config.yaml"]["content"]
    assert SECRET not in r.text  # CORE-303
    assert "api_key: !masked ''" in main
    assert "city: !env [CITY, Rome]" in main  # !env shown as written
    assert "# language" in main  # the true file content


def test_core_301_file_roles() -> None:
    s = ConfigSet("config.yaml", {})
    assert file_role(s, "config.yaml") == "main"
    assert file_role(s, "custom.css") == "custom_css"
    assert file_role(s, os.path.join("widget-templates", "a.yaml")) == "widget_template"
    assert file_role(s, "inc/modules.yaml") == "include"


async def test_core_301_patch_writes_nothing(world: World) -> None:
    before = world.main()
    etag = await world.etag()
    body = {
        "base": etag,
        "ops": [
            {
                "file": "config.yaml",
                "patch": [
                    {"op": "replace", "path": "/engine/locale", "value": "it"},
                    {"op": "add", "path": "/modules/0/config/lat", "value": 45.4},
                ],
            }
        ],
    }
    r = await world.client.post("/api/v1/config/patch", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    (changed,) = out["files"]
    assert changed["path"] == "config.yaml"
    assert "locale: it  # language" in changed["content"]
    assert "lat: 45.4" in changed["content"]
    assert SECRET not in changed["content"]
    assert out["records"] == []
    assert world.main() == before  # nothing written
    bad = await world.client.post(
        "/api/v1/config/patch",
        json={
            "base": etag,
            "ops": [{"file": "config.yaml", "patch": [{"op": "remove", "path": "/x"}]}],
        },
    )
    body_bad = problem(bad, 422, "CORE_API_PATCH_FAILED")
    assert body_bad["index"] == 0
    tag = await world.client.post(
        "/api/v1/config/patch",
        json={
            "base": etag,
            "ops": [
                {
                    "file": "config.yaml",
                    "patch": [{"op": "add", "path": "/x", "value": {"!evil": 1}}],
                }
            ],
        },
    )
    problem(tag, 422, "CORE_API_PATCH_FAILED")


async def test_core_35_patch_returns_full_records(world: World) -> None:
    etag = await world.etag()
    r = await world.client.post(
        "/api/v1/config/patch",
        json={
            "base": etag,
            "ops": [
                {
                    "file": "config.yaml",
                    "patch": [{"op": "add", "path": "/modules/0/config/lat", "value": 99}],
                }
            ],
        },
    )
    records = r.json()["records"]
    assert records
    rec = records[0]
    assert {"line", "col", "severity", "path", "message"} <= set(rec)
    assert rec["severity"] == "error"
    assert rec["path"] == "/modules/0/config/lat"


# --- CORE-302, CORE-36: validate and save -------------------------------------------------


async def test_core_302_validate_diff_preview(world: World) -> None:
    etag = await world.etag()
    content = masked_main(world).replace("locale: en", "locale: de")
    r = await world.client.post(
        "/api/v1/config/validate",
        json={"base": etag, "files": [{"path": "config.yaml", "content": content}]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["records"] == []
    assert [d["section"] for d in out["diff"]] == ["engine"]
    assert "en" in out["diff"][0]["before"]
    assert "de" in out["diff"][0]["after"]


async def test_core_36_same_validator_as_check_config(world: World) -> None:
    etag = await world.etag()
    content = masked_main(world).replace("module: panel", "module: missing")
    r = await world.client.post(
        "/api/v1/config/validate",
        json={"base": etag, "files": [{"path": "config.yaml", "content": content}]},
    )
    records = r.json()["records"]
    scratch = world.root.parent / "check"
    scratch.mkdir()
    (scratch / "config.yaml").write_text(content.replace("!masked ''", "x"))
    (scratch / "layout.yaml").write_text(LAYOUT)
    direct = load_config(
        scratch / "config.yaml", registry=registry(), env_file=scratch / "e", environ={}
    )
    assert {r["code"] for r in records if r["severity"] == "error"} == {
        r.code for r in direct.errors
    }


async def test_core_302_save_creates_a_version(world: World) -> None:
    etag = await world.etag()
    content = masked_main(world).replace("locale: en", "locale: fr")
    draft = {"base": etag, "files": [{"path": "config.yaml", "content": content}]}
    r = await world.client.put("/api/v1/config", json=draft, headers={"If-Match": etag})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["version"] == 2
    assert out["etag"] != etag
    written = world.main()
    assert "locale: fr  # language" in written
    assert f"api_key: {SECRET}" in written  # CORE-303: the masked secret kept its value
    meta = world.history.store.meta(2)
    assert meta.origin == "admin"
    assert meta.author == "127.0.0.1"
    assert meta.sections == ["engine"]
    # Same content again: 200 with the current version, no new one.
    same = {"base": out["etag"], "files": [{"path": "config.yaml", "content": content}]}
    again = await world.client.put("/api/v1/config", json=same, headers={"If-Match": out["etag"]})
    assert again.json() == {"version": 2, "etag": out["etag"]}
    assert world.history.store.versions() == [1, 2]


async def test_core_302_invalid_save_writes_nothing(world: World) -> None:
    etag = await world.etag()
    before = world.main()
    content = (
        masked_main(world).replace("lat:", "unknown_key:")
        if "lat:" in masked_main(world)
        else (masked_main(world).replace("city:", "nonsense: 1\n      city:"))
    )
    r = await world.client.put(
        "/api/v1/config",
        json={"base": etag, "files": [{"path": "config.yaml", "content": content}]},
        headers={"If-Match": etag},
    )
    body = problem(r, 422, "CORE_API_CONFIG_INVALID")
    assert body["errors"]
    assert world.main() == before
    assert world.history.store.versions() == [1]


async def test_core_303_masked_without_secret(world: World) -> None:
    etag = await world.etag()
    content = masked_main(world).replace("id: p", "id: p\n    locale: !masked ''")
    r = await world.client.post(
        "/api/v1/config/validate",
        json={"base": etag, "files": [{"path": "config.yaml", "content": content}]},
    )
    problem(r, 422, "CORE_API_MASKED_UNKNOWN")


# --- CORE-300: ETag and conflicts ---------------------------------------------------------


async def test_core_300_preconditions_and_conflicts(world: World) -> None:
    etag = await world.etag()
    draft = {"base": etag, "files": []}
    problem(
        await world.client.put("/api/v1/config", json=draft), 428, "CORE_API_PRECONDITION_REQUIRED"
    )
    for path in ("/api/v1/config/undo", "/api/v1/config/redo"):
        problem(await world.client.post(path), 428, "CORE_API_PRECONDITION_REQUIRED")
    (world.root / "config.yaml").write_text(MAIN.replace("locale: en", "locale: es"))
    r = await world.client.put("/api/v1/config", json=draft, headers={"If-Match": etag})
    body = problem(r, 409, "CORE_API_CONFLICT")
    assert body["etag"] != etag
    assert "locale: es" in {f["path"]: f for f in body["files"]}["config.yaml"]["content"]
    assert SECRET not in r.text
    stale = await world.client.post("/api/v1/config/patch", json={"base": etag, "ops": []})
    problem(stale, 409, "CORE_API_CONFLICT")
    (world.root / "config.yaml").write_text("modules: [\n")
    broken = await world.client.put("/api/v1/config", json=draft, headers={"If-Match": etag})
    body = problem(broken, 409, "CORE_API_DISK_INVALID")
    assert body["errors"]


# --- CORE-321: history --------------------------------------------------------------------


async def save(world: World, old: str, new: str) -> dict[str, Any]:
    etag = await world.etag()
    content = masked_main(world).replace(old, new)
    r = await world.client.put(
        "/api/v1/config",
        json={"base": etag, "files": [{"path": "config.yaml", "content": content}]},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def test_core_321_history_list_and_diff(world: World) -> None:
    await save(world, "locale: en", "locale: it")
    await save(world, "id: p\n    position: top_left", "id: p\n    position: top_right")
    r = await world.get("/api/v1/config/history", params={"limit": 2})
    page = r.json()
    assert [i["version"] for i in page["items"]] == [3, 2]
    assert page["items"][0]["current"] is True
    assert page["items"][1]["current"] is False
    assert set(page["items"][0]) == {
        "version",
        "time",
        "origin",
        "author",
        "parent",
        "sections",
        "sha256",
        "current",
    }
    assert page["items"][0]["sections"] == ["modules/p"]
    nxt = await world.get("/api/v1/config/history", params={"limit": 2, "before": page["next"]})
    assert [i["version"] for i in nxt.json()["items"]] == [1]
    assert nxt.json()["next"] is None
    diff = (await world.get("/api/v1/config/history/1/diff/3")).json()
    assert {d["section"] for d in diff} == {"engine", "modules/p"}
    assert all(SECRET not in d["before"] + d["after"] for d in diff)
    one = (await world.get("/api/v1/config/history/1/diff/3", params={"section": "engine"})).json()
    assert [d["section"] for d in one] == ["engine"]
    problem(await world.get("/api/v1/config/history/1/diff/99"), 404, "CORE_API_NOT_FOUND")
    problem(
        await world.get("/api/v1/config/history/1/diff/3", params={"section": "nope"}),
        404,
        "CORE_API_NOT_FOUND",
    )
    problem(
        await world.get("/api/v1/config/history", params={"limit": 0}),
        422,
        "CORE_API_INVALID_REQUEST",
    )


async def test_core_321_undo_redo_and_nothing_to_do(world: World) -> None:
    etag = await world.etag()
    problem(
        await world.client.post("/api/v1/config/undo", headers={"If-Match": etag}),
        409,
        "CORE_API_NOTHING_TO_DO",
    )
    problem(
        await world.client.post("/api/v1/config/redo", headers={"If-Match": etag}),
        409,
        "CORE_API_NOTHING_TO_DO",
    )
    saved = await save(world, "locale: en", "locale: it")
    undo = await world.client.post("/api/v1/config/undo", headers={"If-Match": saved["etag"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["version"] == 3
    assert "locale: en" in world.main()
    redo = await world.client.post("/api/v1/config/redo", headers={"If-Match": undo.json()["etag"]})
    assert redo.json()["version"] == 4
    assert "locale: it" in world.main()
    assert world.history.store.meta(4).origin == "redo"
    stale = await world.client.post("/api/v1/config/undo", headers={"If-Match": etag})
    problem(stale, 409, "CORE_API_CONFLICT")


async def test_core_321_restore_whole_and_section(world: World) -> None:
    await save(world, "locale: en", "locale: it")
    s3 = await save(world, "id: p\n    position: top_left", "id: p\n    position: top_right")
    r = await world.client.post(
        "/api/v1/config/restore",
        json={"version": 1, "section": "engine"},
        headers={"If-Match": s3["etag"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 4
    assert "locale: en" in world.main()
    assert "position: top_right" in world.main()  # the rest stays
    whole = await world.client.post(
        "/api/v1/config/restore", json={"version": 1}, headers={"If-Match": r.json()["etag"]}
    )
    assert whole.json()["version"] == 5
    assert "position: top_left" in world.main()
    etag = whole.json()["etag"]
    problem(
        await world.client.post(
            "/api/v1/config/restore", json={"version": 99}, headers={"If-Match": etag}
        ),
        404,
        "CORE_API_NOT_FOUND",
    )
    problem(
        await world.client.post(
            "/api/v1/config/restore",
            json={"version": 1, "section": "nope"},
            headers={"If-Match": etag},
        ),
        404,
        "CORE_API_NOT_FOUND",
    )


async def test_core_321_restore_that_fails_validation(world: World) -> None:
    current, _ = world.history.disk()
    broken = current.with_file("config.yaml", b"modules:\n  - module: gone\n")
    world.history.store.add(broken, origin="cli", author="test", parent=1, sections=[])
    etag = await world.etag()
    r = await world.client.post(
        "/api/v1/config/restore", json={"version": 2}, headers={"If-Match": etag}
    )
    body = problem(r, 422, "CORE_API_CONFIG_INVALID")
    assert body["errors"]
    assert "module: gone" not in world.main()


# --- CORE-37 and 6.4: the schema -----------------------------------------------------------


def test_core_37_schema_with_modules_tags_and_hints() -> None:
    schema = config_schema(registry())
    assert schema["$schema"].endswith("2020-12/schema")
    assert set(schema["x-mm3-modules"]) == {"keyed", "panel"}
    variants = schema["properties"]["modules"]["anyOf"][0]["items"]["oneOf"]
    consts = {v["properties"]["module"]["anyOf"][0]["const"] for v in variants}
    assert consts == {"keyed", "panel"}
    key_ref = schema["x-mm3-modules"]["keyed"].split("/")[-1]
    city = schema["$defs"][key_ref]["properties"]["city"]["anyOf"][0]
    assert city["ui"] == {"order": 1, "group": "place"}  # CORE-37 hints pass through
    assert schema["$defs"]["mm3_env"]["required"] == ["!env"]
    assert "mm3_include" in schema["$defs"]
    position = schema["$defs"]["InstanceConfig"]["properties"]["position"]
    assert "top_left" in str(position)  # region presets as an enum


async def test_schema_route(world: World) -> None:
    r = await world.get("/api/v1/config/schema")
    assert r.status_code == 200
    assert "keyed" in r.json()["x-mm3-modules"]


# --- CORE-322 and CORE-34: the page ---------------------------------------------------------


async def test_core_322_config_page_only_on_the_admin_listener(tmp_path: Path) -> None:
    auth = WebAuth.open(tmp_path)
    token = auth.tokens.new("p")
    rec = auth.tokens.verify(token, "admin")
    assert rec is not None
    sid = auth.sessions.create(rec)
    admin = protect(admin_app(auth), lambda: [], "admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin, client=("127.0.0.1", 5)), base_url=ADMIN
    ) as c:
        assert (await c.get("/admin/config")).status_code == 303
        c.cookies.set(ADMIN_COOKIE, sid)
        page = await c.get("/admin/config")
        assert page.status_code == 200
        assert '<script type="module" src="/admin/static/config.js"></script>' in page.text
        assert "<mm3-admin-config>" in page.text
        assert 'href="/admin/config" aria-current="page"' in page.text
        assert (await c.get("/admin/static/config.js")).status_code == 200
    mirror = protect(mirror_app(auth), lambda: [], "mirror")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mirror, client=("127.0.0.1", 5)), base_url="http://m"
    ) as c:
        assert (await c.get("/admin/config")).status_code == 404  # CORE-34


def chromium() -> str | None:
    try:
        from module_projects import chromium_available
    except ImportError:  # pragma: no cover
        return None
    return os.environ.get("MM3_CHROMIUM", "") if chromium_available() else None


async def test_core_322_history_tab_in_chromium(tmp_path: Path) -> None:
    exe = chromium()
    if exe is None:
        pytest.skip("Chromium is not available")
    from playwright.async_api import async_playwright

    configure_logging("info", stream=io.StringIO(), json=True)
    root = tmp_path / "etc"
    root.mkdir()
    (root / "config.yaml").write_text(MAIN)
    (root / "layout.yaml").write_text(LAYOUT)
    env = tmp_path / "e.env"
    reg = registry()
    history = ConfigHistory(
        root / "config.yaml", env_file=env, registry=reg, state_dir=tmp_path / "s", environ={}
    )
    with history.store.lock():
        history.record_disk()
    (root / "config.yaml").write_text(MAIN.replace("locale: en", "locale: it"))
    with history.store.lock():
        history.record_disk()
    result = load_config(root / "config.yaml", registry=reg, env_file=env, environ={})
    settings = EngineSettings().model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    engine = Engine(settings, registry=reg, clock=VirtualClock(), state_dir=tmp_path / "s")
    editor = ConfigEditor(history, lambda: Masker(config_secrets(result, env, environ={})))
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    services = ApiServices(engine, manager, editor=editor)
    gateway = WebGateway(settings, tmp_path, api=services)
    await engine.start(result.instance_specs())
    await gateway.start()
    base = f"http://127.0.0.1:{gateway.admin.port}"
    rec = gateway.auth.tokens.verify(gateway.auth.tokens.new("ui"), "admin")
    assert rec is not None
    sid = gateway.auth.sessions.create(rec)
    problems: list[str] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=exe or None)
            try:
                context = await browser.new_context()
                await context.add_cookies([{"name": ADMIN_COOKIE, "value": sid, "url": base}])
                page = await context.new_page()
                page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: problems.append(str(e)))
                await page.goto(f"{base}/admin/config")
                await page.wait_for_selector("text=History")
                await page.wait_for_selector("td >> text=engine")
                await page.locator("tbody tr").nth(1).locator("input").first.check()
                await page.click("text=Compare")
                await page.wait_for_selector("text=Changes from version 1 to version 2")
                await page.click("text=Restore all")
                await page.wait_for_selector("dialog >> text=Restore version 1?")
                await page.click("dialog >> text=Confirm")
                await page.wait_for_selector("text=Version 3 created.")
            finally:
                await browser.close()
    finally:
        await gateway.stop()
        await engine.stop()
    assert "locale: en" in (root / "config.yaml").read_text()
    assert not problems, problems
    await asyncio.sleep(0)
