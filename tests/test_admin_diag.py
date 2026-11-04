"""Milestone 6, part B1: admin channel, log paging, bus inspector, bundle, socket, mm3 diag."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import stat
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from pydantic import Field, SecretStr
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from mm3.cli.diag_cmd import (
    Check,
    SocketClient,
    catalog_problems,
    partial_bundle,
    run_checks,
    run_diag,
)
from mm3.config import ModuleConfig
from mm3.core.config import load_config
from mm3.core.config.manager import ConfigManager
from mm3.core.config.validate import ConfigResult
from mm3.core.diag.bundle import Bundle, bundle_name, redact_yaml
from mm3.core.diag.inspector import CAPTURE_SIZE, BusFilter, BusInspector
from mm3.core.diag.logfilter import LogFilter
from mm3.core.diag.secrets import MASK, Masker, config_secrets
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging, get_logger
from mm3.core.pages import PageService
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.adminws import CLOSE_FORBIDDEN, INITIAL_LOGS, QUEUE_FRAMES, AdminChannel
from mm3.core.web.api import ApiServices, status_object
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import SOCKET_MODE, WebGateway
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.view import cached_view
from mm3.core.web.ws import ClientHub
from mm3.messages import Notification
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

SECRET = "sk-very-secret-value"  # noqa: S105 - test data
ENV_SECRET = "env-password-123"  # noqa: S105 - test data


class KeyConfig(ModuleConfig):
    api_key: SecretStr = Field(json_schema_extra={"secret": True})
    label: str = "x"


class Keyed(MirrorModule):
    name = "keyed"
    config_model = KeyConfig


class Panel(MirrorModule):
    name = "panel"
    element = "mm3-panel"


CONFIG = f"""
modules:
  - module: keyed
    id: k
    config:
      api_key: {SECRET}
      label: !env [LABEL, fallback]
  - module: panel
    id: p
    position: top_left
"""


@pytest.fixture(autouse=True)
def logging_on() -> None:
    configure_logging("debug", stream=io.StringIO(), json=True)


def load(tmp_path: Path, registry: Registry) -> ConfigResult:
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "mm3.env").write_text(f"LABEL={ENV_SECRET}\n")
    result = load_config(tmp_path / "config.yaml", registry=registry, env_file=tmp_path / "mm3.env")
    assert result.ok, result.records
    return result


def registry() -> Registry:
    reg = Registry()
    reg.add(Keyed)
    reg.add(Panel)
    return reg


# --- CORE-316: log filters ---------------------------------------------------------------


def test_core_316_filter_semantics() -> None:
    rec = {
        "level": "warning",
        "module": "weather",
        "instance": "w1",
        "trace_id": "t-1",
        "event": "fetch_failed",
        "url": "https://Example.org/x",
    }
    assert LogFilter.parse({"level": "info"}).matches(rec)
    assert LogFilter.parse({"level": "warning"}).matches(rec)
    assert not LogFilter.parse({"level": "error"}).matches(rec)
    assert LogFilter.parse({"module": "weather", "instance": "w1", "trace": "t-1"}).matches(rec)
    assert not LogFilter.parse({"instance": "w2"}).matches(rec)
    assert LogFilter.parse({"text": "FETCH"}).matches(rec)
    assert LogFilter.parse({"text": "example.ORG"}).matches(rec)  # any field, any case
    assert not LogFilter.parse({"text": "nothing"}).matches(rec)
    assert LogFilter.parse({"level": "", "module": None}).matches(rec)  # empty keys ignored
    for bad, match in (
        ({"colour": "red"}, "key"),
        ({"level": "loud"}, "level"),
        ({"text": 3}, "string"),
    ):
        with pytest.raises(ValueError, match=match):
            LogFilter.parse(bad)


# --- CORE-319, CORE-116: secrets and masking ----------------------------------------------


def test_core_319_config_secrets_and_masking(tmp_path: Path) -> None:
    result = load(tmp_path, registry())
    secrets = config_secrets(
        result, tmp_path / "mm3.env", environ={}, extra=["agent-key-xyz", "ab"]
    )
    assert {SECRET, ENV_SECRET, "agent-key-xyz"} <= secrets
    assert "ab" not in secrets  # shorter than 4
    masker = Masker(secrets)
    payload = {"k": SECRET, "nested": [{"v": ENV_SECRET}, "plain"], "n": 3}
    assert masker.mask(payload) == {"k": MASK, "nested": [{"v": MASK}, "plain"], "n": 3}
    assert payload["k"] == SECRET  # not changed in place
    assert masker.mask_text(f"key={SECRET};") == f"key={MASK};"


def test_core_116_redact_yaml_lists_pointers() -> None:
    text = f"# keep me\nmodules:\n  - module: keyed\n    config:\n      api_key: {SECRET}\n"
    out, paths = redact_yaml(text, Masker([SECRET]))
    assert SECRET not in out
    assert "# keep me" in out
    assert "!masked" in out
    assert paths == ["/modules/0/config/api_key"]
    same, none = redact_yaml("a: 1\n", Masker([SECRET]))
    assert (same, none) == ("a: 1\n", [])
    broken, hit = redact_yaml(f"a: [{SECRET}\n", Masker([SECRET]))
    assert SECRET not in broken
    assert hit == [""]


# --- CORE-115, CORE-319: bus inspector ---------------------------------------------------


def note(name: str, payload: dict[str, Any], **kw: Any) -> Notification:
    from uuid import uuid4

    nid = uuid4()
    return Notification(
        id=nid, trace=nid, ts=datetime.now(UTC), name=name, source="remote", payload=payload, **kw
    )


def test_core_115_captures_only_while_subscribed() -> None:
    inspector = BusInspector(lambda: Masker([SECRET]))
    inspector.deliver(note("BEFORE", {}), None)
    assert not inspector.entries
    seen: list[dict[str, Any]] = []
    stop = inspector.subscribe(seen.append)
    assert inspector.capturing
    inspector.deliver(note("SECRET_INSIDE", {"key": SECRET}), None)
    entry = seen[-1]
    assert entry["channel"] == "bus"
    assert entry["name"] == "SECRET_INSIDE"
    assert entry["source"] == "remote"
    assert entry["payload"] == {"key": MASK}  # CORE-319
    assert entry["size"] == len(json.dumps({"key": SECRET}, separators=(",", ":")))
    assert set(entry) == {"ts", "channel", "name", "source", "target", "size", "trace", "payload"}
    for i in range(CAPTURE_SIZE + 5):
        inspector.deliver(note("MANY", {"i": i}), None)
    assert len(inspector.entries) == CAPTURE_SIZE
    stop()
    assert not inspector.capturing
    assert not inspector.entries


def test_core_304_ws_frames_and_bus_filter() -> None:
    inspector = BusInspector()
    seen: list[dict[str, Any]] = []
    inspector.subscribe(seen.append)
    inspector.frame("out", "c1", {"kind": "state", "instance": "p", "data": {"x": 1}})
    inspector.frame("in", "c1", {"kind": "notify", "instance": "p", "data": {"name": "A"}})
    assert [(e["channel"], e["name"], e["source"], e["target"]) for e in seen] == [
        ("ws", "state", "core", "shell/c1/p"),
        ("ws", "notify", "shell/c1", "p"),
    ]
    f = BusFilter.parse({"name": "st*", "source": "core"})
    assert f.matches(seen[0])
    assert not f.matches(seen[1])
    with pytest.raises(ValueError, match="unknown filter key"):
        BusFilter.parse({"kind": "x"})


# --- CORE-318: bundle --------------------------------------------------------------------


def test_core_318_bundle_layout(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(f"secret: {SECRET}\n")
    (tmp_path / "custom.css").write_text("body { color: red }\n")
    bundle = Bundle(masker=Masker([SECRET]), now=datetime(2026, 11, 4, 20, 0, tzinfo=UTC))
    bundle.modules = {"keyed": "1.0"}
    bundle.add_bytes("packages.txt", b"mm3-core==0.0.1\n")
    bundle.add_config([tmp_path / "config.yaml", tmp_path / "custom.css"], tmp_path)
    bundle.add_jsonl("logs.jsonl", [{"event": "x", "v": SECRET}])
    bundle.add_json("health.json", [])
    data = bundle.build()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        logs = zf.read("logs.jsonl").decode()
        config = zf.read("config/config.yaml").decode()
    assert names == {
        "manifest.json",
        "packages.txt",
        "config/config.yaml",
        "config/custom.css",
        "logs.jsonl",
        "health.json",
    }
    assert set(manifest) == {
        "created_at",
        "partial",
        "missing",
        "versions",
        "python",
        "os",
        "redacted",
    }
    assert manifest["partial"] is False
    assert manifest["versions"]["modules"] == {"keyed": "1.0"}
    assert {"file": "config/config.yaml", "path": "/secret"} in manifest["redacted"]
    assert {"file": "logs.jsonl", "path": ""} in manifest["redacted"]
    assert SECRET not in logs + config
    assert bundle_name(datetime(2026, 11, 4, 20, 0, tzinfo=UTC), "mirror.lan") == (
        "mm3-diag-mirror-lan-20261104T200000Z.zip"
    )


def test_core_318_partial_bundle(tmp_path: Path) -> None:
    reg = registry()
    result = load(tmp_path, reg)
    bundle = partial_bundle(result, tmp_path / "mm3.env", reg, journal=lambda: f"x {SECRET} y\n")
    with zipfile.ZipFile(io.BytesIO(bundle.build())) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        journal = zf.read("journal.txt").decode()
        config = zf.read("config/config.yaml").decode()
        names = set(zf.namelist())
    assert manifest["partial"] is True
    assert "logs.jsonl" not in names
    assert {"logs.jsonl", "health.json", "browser-errors.jsonl", "kiosk.json"} <= set(
        manifest["missing"]
    )
    assert SECRET not in journal + config
    no_journal = partial_bundle(None, tmp_path / "mm3.env", reg, journal=lambda: None)
    assert {"config/", "journal.txt"} <= set(no_journal.missing)


# --- the engine rig ------------------------------------------------------------------------


def free_ports() -> EngineSettings:
    return EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    ).model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )


@dataclass
class Rig:
    engine: Engine
    gateway: WebGateway
    auth: WebAuth
    inspector: BusInspector
    channel: AdminChannel
    services: ApiServices
    socket: Path
    token: str

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.gateway.admin.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.gateway.admin.port}/admin/ws"

    def session(self) -> str:
        rec = self.auth.tokens.verify(self.token, "admin")
        assert rec is not None
        return self.auth.sessions.create(rec)

    async def admin(self) -> ClientConnection:
        headers = {"Origin": self.origin, "Cookie": f"{ADMIN_COOKIE}={self.session()}"}
        return await websockets.connect(self.ws_url, additional_headers=headers)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.origin, headers={"authorization": f"Bearer {self.token}"}
        )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    reg = registry()
    result = load(tmp_path, reg)
    engine = Engine(free_ports(), registry=reg, clock=VirtualClock(), state_dir=tmp_path / "state")
    auth = WebAuth.open(tmp_path / "web")
    hub = ClientHub(cached_view(lambda: result, reg), auth, "1.0+t")
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, engine.clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    manager = ConfigManager(engine, result, registry=reg, env_file=tmp_path / "mm3.env")
    secrets = config_secrets(result, tmp_path / "mm3.env", environ={})
    services = ApiServices(engine, manager, pages, hub, None, "1.0+t", lambda: Masker(secrets))
    inspector = BusInspector(services.masker)
    engine.bus.add_endpoint(inspector)
    hub.frame_observer = inspector.frame
    channel = AdminChannel(auth, lambda: status_object(services), inspector, interval=0.05)
    sock = tmp_path / "run" / "admin.sock"
    sock.parent.mkdir()
    gateway = WebGateway(
        free_ports(), tmp_path, hub=hub, api=services, channel=channel, socket_path=sock
    )
    await engine.start(result.instance_specs())
    await pages.start()
    await gateway.start()
    token = auth.tokens.new("rig")
    try:
        yield Rig(engine, gateway, auth, inspector, channel, services, sock, token)
    finally:
        await gateway.stop()
        await pages.close()
        await engine.stop()


async def recv(ws: ClientConnection, wait: float = 3.0) -> dict[str, Any]:
    async with asyncio.timeout(wait):
        frame: dict[str, Any] = json.loads(await ws.recv())
    assert frame["v"] == 1
    return frame


async def recv_kind(ws: ClientConnection, kind: str, wait: float = 3.0) -> dict[str, Any]:
    async with asyncio.timeout(wait):
        while True:
            frame = await recv(ws, wait)
            if frame["kind"] == kind:
                return frame


async def send(ws: ClientConnection, kind: str, data: dict[str, Any]) -> None:
    await ws.send(json.dumps({"v": 1, "kind": kind, "data": data}))


async def drain(ws: ClientConnection) -> None:
    async with asyncio.timeout(3):
        while True:
            await ws.recv()


async def close_code(ws: ClientConnection) -> int | None:
    with pytest.raises(ConnectionClosed):
        await drain(ws)
    return ws.close_code


# --- CORE-304, CORE-317: the admin channel ------------------------------------------------


async def test_core_304_needs_session_and_origin(rig: Rig) -> None:
    sid = rig.session()
    no_origin = await websockets.connect(
        rig.ws_url, additional_headers={"Cookie": f"{ADMIN_COOKIE}={sid}"}
    )
    assert await close_code(no_origin) == CLOSE_FORBIDDEN
    bearer = await websockets.connect(
        rig.ws_url,
        additional_headers={"Origin": rig.origin, "Authorization": f"Bearer {rig.token}"},
    )
    assert await close_code(bearer) == CLOSE_FORBIDDEN
    nothing = await websockets.connect(rig.ws_url, additional_headers={"Origin": rig.origin})
    assert await close_code(nothing) == CLOSE_FORBIDDEN


async def test_core_317_status_at_subscribe_and_on_change(rig: Rig) -> None:
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "status"})
    first = await recv_kind(ws, "status")
    assert first["data"]["status"]["page"]["id"] == "main"
    assert first["data"]["status"]["engine_build"] == "1.0+t"
    # Only uptime changes: no new frame.
    with pytest.raises(TimeoutError):
        await recv(ws, wait=0.3)
    rig.engine.supervisor.suspended = True
    changed = await recv_kind(ws, "status")
    assert changed["data"]["status"]["suspended"] is True
    await send(ws, "unsubscribe", {"topic": "status"})
    rig.engine.supervisor.suspended = False
    with pytest.raises(TimeoutError):
        await recv(ws, wait=0.3)
    await ws.close()


async def test_core_304_logs_topic_backlog_then_live(rig: Rig) -> None:
    log = get_logger("keyed", "k")
    for i in range(INITIAL_LOGS + 20):
        log.info("backlog_line", i=i)
    get_logger("panel", "p").info("other_instance")
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "logs", "filter": {"instance": "k", "text": "BACKLOG"}})
    backlog = [await recv_kind(ws, "log") for _ in range(INITIAL_LOGS)]
    numbers = [f["data"]["record"]["i"] for f in backlog]
    assert numbers == list(range(20, INITIAL_LOGS + 20))  # the newest 200, oldest first
    get_logger("panel", "p").info("backlog_wrong_instance")
    log.info("backlog_live", i=-1)
    live = await recv_kind(ws, "log")
    assert live["data"]["record"]["event"] == "backlog_live"
    await ws.close()


async def test_core_316_invalid_subscribe_is_ignored(rig: Rig) -> None:
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "weather"})
    await send(ws, "subscribe", {"topic": "logs", "filter": {"level": "loud"}})
    await send(ws, "subscribe", {"topic": "logs", "filter": {"colour": "red"}})
    await ws.send("not json")
    await send(ws, "subscribe", {"topic": "status"})
    frame = await recv(ws)
    assert frame["kind"] == "status"  # the channel is still open and nothing else came
    await ws.close()


async def test_core_304_bus_topic_masks_and_filters(rig: Rig) -> None:
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "bus", "filter": {"name": "HELLO*"}})
    await asyncio.sleep(0.1)
    assert rig.inspector.capturing
    async with rig.http() as c:
        r = await c.post("/api/v1/notify", json={"name": "HELLO_BUS", "payload": {"key": SECRET}})
        assert r.status_code == 200
        await c.post("/api/v1/notify", json={"name": "OTHER", "payload": {}})
    frame = await recv_kind(ws, "bus")
    entry = frame["data"]["entry"]
    assert entry["name"] == "HELLO_BUS"
    assert entry["source"] == "remote"
    assert entry["payload"] == {"key": MASK}
    await ws.close()
    for _ in range(50):
        if not rig.inspector.capturing:
            break
        await asyncio.sleep(0.02)
    assert not rig.inspector.capturing


async def test_core_304_dropped_when_behind(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from mm3.core.web import adminws

    admin = adminws._Admin(None, asyncio.get_running_loop())  # type: ignore[arg-type]
    admin.topics["bus"] = adminws._Topic()
    for i in range(QUEUE_FRAMES + 3):
        admin.push("bus", str(i))
    assert len(admin.topics["bus"].queue) == QUEUE_FRAMES
    assert admin.topics["bus"].dropped == 3
    sent: list[str] = []

    class Fake:
        async def send_text(self, text: str) -> None:
            sent.append(text)

    admin.ws = Fake()  # type: ignore[assignment]
    task = asyncio.create_task(admin.sender())
    await asyncio.sleep(0.05)
    task.cancel()
    assert json.loads(sent[-1]) == {"v": 1, "kind": "dropped", "data": {"topic": "bus", "count": 3}}


# --- CORE-299: log paging ----------------------------------------------------------------


async def test_core_299_logs_paged_newest_first(rig: Rig) -> None:
    log = get_logger("keyed", "k")
    for i in range(7):
        log.warning("paged_line", i=i)
    async with rig.http() as c:
        r = await c.get("/api/v1/debug/logs", params={"text": "paged_line", "limit": 3})
        assert r.status_code == 200
        page = r.json()
        assert [x["i"] for x in page["items"]] == [6, 5, 4]
        assert isinstance(page["next"], str)
        r2 = await c.get(
            "/api/v1/debug/logs", params={"text": "paged_line", "limit": 3, "before": page["next"]}
        )
        assert [x["i"] for x in r2.json()["items"]] == [3, 2, 1]
        r3 = await c.get(
            "/api/v1/debug/logs",
            params={"text": "paged_line", "limit": 3, "before": r2.json()["next"]},
        )
        assert [x["i"] for x in r3.json()["items"]] == [0]
        assert r3.json()["next"] is None
        nd = await c.get(
            "/api/v1/debug/logs",
            params={"text": "paged_line", "limit": 2},
            headers={"accept": "application/x-ndjson"},
        )
        assert nd.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in nd.text.splitlines()]
        assert [x["i"] for x in lines] == [6, 5]
        assert 'rel="next"' in nd.headers["link"]
        for params in ({"limit": 0}, {"limit": 501}, {"level": "loud"}, {"colour": "x"}):
            bad = await c.get("/api/v1/debug/logs", params=params)
            assert bad.status_code == 422
            assert bad.json()["code"] == "CORE_API_INVALID_REQUEST"


# --- CORE-318, CORE-116: the bundle from the engine ----------------------------------------


async def test_core_318_engine_bundle(rig: Rig) -> None:
    get_logger("keyed", "k").error("leaky", value=SECRET)
    async with rig.http() as c:
        r = await c.get("/api/v1/debug/bundle")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "mm3-diag-" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        everything = b"".join(zf.read(n) for n in names).decode()
        health = json.loads(zf.read("health.json"))
    assert {
        "manifest.json",
        "packages.txt",
        "config/config.yaml",
        "logs.jsonl",
        "health.json",
        "browser-errors.jsonl",
        "kiosk.json",
    } <= names
    assert manifest["partial"] is False
    assert SECRET not in everything
    assert ENV_SECRET not in everything
    assert {"file": "config/config.yaml", "path": "/modules/0/config/api_key"} in manifest[
        "redacted"
    ]
    assert {h["instance"] for h in health} == {"k", "p"}
    assert "mm3.env" not in " ".join(names)


# --- CORE-311: the local admin socket -----------------------------------------------------


async def test_core_311_socket_serves_api_without_token(rig: Rig) -> None:
    assert stat.S_IMODE(rig.socket.stat().st_mode) == SOCKET_MODE
    client = SocketClient(rig.socket)
    status = await asyncio.to_thread(client.json, "/api/v1/status")
    assert status["engine_build"] == "1.0+t"
    code, _ = await asyncio.to_thread(client.get, "/admin")
    assert code == 404  # no admin pages on the socket
    code, body = await asyncio.to_thread(client.get, "/api/v1/nothing")
    assert code == 404
    assert json.loads(body)["code"] == "CORE_API_NOT_FOUND"
    await rig.gateway.stop()
    assert not rig.socket.exists()  # removed at stop


async def test_core_311_diag_bundle_over_socket(rig: Rig, tmp_path: Path) -> None:
    args = argparse.Namespace(
        diag_command="bundle",
        config=tmp_path / "config.yaml",
        env_file=tmp_path / "mm3.env",
        socket=rig.socket,
        out=tmp_path / "out",
    )
    out = io.StringIO()
    code = await asyncio.to_thread(run_diag, args, registry(), out)
    assert code == 0, out.getvalue()
    assert "bundle from the running engine" in out.getvalue()
    (zip_path,) = (tmp_path / "out").glob("mm3-diag-*.zip")
    with zipfile.ZipFile(zip_path) as zf:
        assert json.loads(zf.read("manifest.json"))["partial"] is False


def test_core_311_diag_bundle_falls_back_to_partial(tmp_path: Path) -> None:
    load(tmp_path, registry())
    args = argparse.Namespace(
        diag_command="bundle",
        config=tmp_path / "config.yaml",
        env_file=tmp_path / "mm3.env",
        socket=tmp_path / "nothing.sock",
        out=tmp_path / "out",
    )
    out = io.StringIO()
    assert run_diag(args, registry(), out) == 0
    assert "partial bundle" in out.getvalue()
    (zip_path,) = (tmp_path / "out").glob("mm3-diag-*.zip")
    with zipfile.ZipFile(zip_path) as zf:
        assert json.loads(zf.read("manifest.json"))["partial"] is True


# --- CORE-312: mm3 diag check ------------------------------------------------------------


class FakeClient(SocketClient):
    def __init__(self, status: dict[str, Any] | None) -> None:
        super().__init__(Path("/run/mm3/admin.sock"))
        self.status = status

    def json(self, target: str) -> Any:
        if self.status is None:
            raise OSError("connection refused")
        return self.status


def outcomes(checks: list[Check]) -> dict[str, str]:
    return {c.name: c.outcome for c in checks}


def test_core_312_engine_down(tmp_path: Path) -> None:
    reg = registry()
    result = load(tmp_path, reg)
    checks = run_checks(result, FakeClient(None), reg, port_free=lambda h, p: True)
    assert [c.name for c in checks] == [
        "config valid",
        "engine reachable on the socket",
        "listener ports",
        "kiosk agent",
        "audio output",
        "MQTT broker",
        "display driver",
        "locale catalogs",
    ]
    o = outcomes(checks)
    assert o["config valid"] == "pass"
    assert o["engine reachable on the socket"] == "fail"
    assert o["listener ports"] == "pass"
    assert o["kiosk agent"] == "skip"
    assert o["MQTT broker"] == "skip"
    assert o["display driver"] == "skip"
    assert o["locale catalogs"] == "pass"
    busy = run_checks(result, FakeClient(None), reg, port_free=lambda h, p: False)
    assert outcomes(busy)["listener ports"] == "fail"
    assert all(c.line().startswith(c.outcome) for c in checks)


def test_core_312_kiosk_and_audio(tmp_path: Path) -> None:
    reg = registry()
    result = load(tmp_path, reg)
    kiosk = {
        "connected": True,
        "chromium": {"state": "started", "restarts": 0, "failed": False},
        "audio": {"sinks": [], "applied": None, "mismatch": True, "failure": None},
    }
    checks = run_checks(result, FakeClient({"kiosk": kiosk}), reg, port_free=lambda h, p: False)
    o = outcomes(checks)
    assert o["engine reachable on the socket"] == "pass"
    assert o["listener ports"] == "pass"  # held by the engine
    assert o["kiosk agent"] == "pass"
    assert o["audio output"] == "fail"
    kiosk["chromium"] = {"state": "failed", "restarts": 5, "failed": True}
    kiosk["audio"] = {"mismatch": False, "failure": None}
    o = outcomes(run_checks(result, FakeClient({"kiosk": kiosk}), reg))
    assert o["kiosk agent"] == "fail"
    assert o["audio output"] == "pass"
    o = outcomes(run_checks(result, FakeClient({"kiosk": None}), reg))
    assert o["kiosk agent"] == "skip"


def test_core_312_catalogs_and_exit_codes(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    for lang in ("en", "it", "fr", "es"):
        (root / lang).mkdir(parents=True)
        keys = {"A": "a", "B": "b"} if lang != "fr" else {"A": "a"}
        (root / lang / "messages.json").write_text(json.dumps(keys))
    problems = catalog_problems({"demo": root, "none": None})
    assert "demo: no compiled de catalog" in problems
    assert "demo: fr lacks 1 key(s)" in problems
    reg = registry()
    load(tmp_path, reg)
    args = argparse.Namespace(
        diag_command="check",
        config=tmp_path / "config.yaml",
        env_file=tmp_path / "mm3.env",
        socket=tmp_path / "nothing.sock",
    )
    out = io.StringIO()
    assert run_diag(args, reg, out) == 1  # the engine is not reachable
    assert "fail  engine reachable on the socket" in out.getvalue()
    broken = argparse.Namespace(diag_command="check", config=None, env_file=None, socket=None)
    assert run_diag(broken, reg, io.StringIO()) == 2
