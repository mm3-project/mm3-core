"""Milestone 6, part A: the REST base of the admin API (core spec 9.1)."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from mm3.core.config import load_config
from mm3.core.config.manager import ConfigManager
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging, get_logger, log_levels, recent_records
from mm3.core.pages import PageService
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.core.supervisor import State
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.kiosk import AgentKey, KioskAgents
from mm3.core.web.problems import CODES, ApiError, problem
from mm3.core.web.view import cached_view
from mm3.core.web.ws import ClientHub
from mm3.errors import TransientError
from mm3.messages import Notification
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"
LOOPBACK = ("127.0.0.1", 50000)


class Panel(MirrorModule):
    name = "panel"
    element = "mm3-panel"


class Answerer(MirrorModule):
    """A backend that answers requests and records notifications."""

    name = "answerer"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.seen: list[Notification] = []

    async def on_notification(self, n: Notification) -> None:
        self.seen.append(n)

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        if req.name == "TIME_NOW":
            return {"by": self.ctx.instance_id, "echo": req.payload}
        if req.name == "FAIL_TRANSIENT":
            raise TransientError("ANSWER_OFFLINE")
        if req.name == "SLOW":
            await asyncio.sleep(5)
        return None


CONFIG = """
pages:
  list:
    - id: a
    - id: b
      title: {en: Bravo}
modules:
  - module: panel
    id: one
    position: top_left
    positions: {b: top_right}
    pages: [a, b]
  - module: panel
    id: two
    position: bottom_bar
    pages: [b]
  - module: answerer
    id: ans
"""


@dataclass
class Api:
    client: httpx.AsyncClient
    engine: Engine
    pages: PageService
    agents: KioskAgents
    token: str
    clock: VirtualClock
    services: ApiServices

    @property
    def bearer(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path, headers=self.bearer)

    async def post(self, path: str, body: Any = None) -> httpx.Response:
        return await self.client.post(path, headers=self.bearer, json=body)

    async def put(self, path: str, body: Any) -> httpx.Response:
        return await self.client.put(path, headers=self.bearer, json=body)

    def ans(self) -> Answerer:
        obj = self.engine.supervisor.get("ans").obj
        assert isinstance(obj, Answerer)
        return obj


def is_problem(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    assert isinstance(body["title"], str)
    assert isinstance(body["detail"], str)
    return cast(dict[str, Any], body)


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    registry = Registry()
    registry.add(Panel)
    registry.add(Answerer)
    (tmp_path / "config.yaml").write_text(CONFIG)
    result = load_config(tmp_path / "config.yaml", registry=registry, env_file=tmp_path / "x.env")
    assert result.ok, result.records
    clock = VirtualClock()
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    )
    engine = Engine(settings, registry=registry, clock=clock, state_dir=tmp_path / "state")
    auth = WebAuth.open(tmp_path / "web")
    hub = ClientHub(cached_view(lambda: result, registry), auth, "1.0+t")
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    manager = ConfigManager(engine, result, registry=registry, env_file=tmp_path / "x.env")
    key = AgentKey(tmp_path / "run")
    key.write()

    async def applied(mode: tuple[int, int] | None) -> None:
        return None

    agents = KioskAgents(key, hub, dict, applied, clock)
    services = ApiServices(engine, manager, pages, hub, agents, "1.0+t")
    app = protect(admin_app(auth, hub, api=services), lambda: [], "admin")
    await engine.start(result.instance_specs())
    await pages.start()
    await pages.idle()
    token = auth.tokens.new("test")
    transport = httpx.ASGITransport(app=app, client=LOOPBACK)
    async with httpx.AsyncClient(transport=transport, base_url=ADMIN) as client:
        try:
            yield Api(client, engine, pages, agents, token, clock, services)
        finally:
            await pages.close()
            await engine.stop()


# --- CORE-298: errors ----------------------------------------------------------------


def test_core_298_codes_are_the_spec_list() -> None:
    assert CODES["CORE_API_REQUEST_TIMEOUT"] == (504, "Request timed out")
    assert len(CODES) == 17
    with pytest.raises(KeyError):
        ApiError("CORE_API_TEAPOT", "no")
    body = json.loads(problem("CORE_API_CONFIG_INVALID", "bad", errors=[{"line": 1}]).body)
    assert body["errors"] == [{"line": 1}]


async def test_core_298_auth_errors_are_problems(api: Api) -> None:
    r = await api.client.get("/api/v1/status")
    is_problem(r, 401, "CORE_API_UNAUTHORIZED")
    assert r.headers["www-authenticate"] == "Bearer"
    bad = await api.client.get("/api/v1/status", headers={"authorization": "Bearer mm3_nope"})
    is_problem(bad, 401, "CORE_API_UNAUTHORIZED")
    is_problem(await api.client.get("/api/v1/nothing"), 401, "CORE_API_UNAUTHORIZED")


async def test_core_298_unknown_route_and_invalid_body(api: Api) -> None:
    is_problem(await api.get("/api/v1/nothing"), 404, "CORE_API_NOT_FOUND")
    r = await api.put("/api/v1/kiosk/audio", {"volume": 200})
    body = is_problem(r, 422, "CORE_API_INVALID_REQUEST")
    assert "volume" in body["detail"]
    is_problem(await api.put("/api/v1/kiosk/audio", {"loud": 1}), 422, "CORE_API_INVALID_REQUEST")


async def test_core_298_unavailable_while_stopping(api: Api) -> None:
    api.engine.notify_stopping()
    is_problem(await api.get("/api/v1/status"), 503, "CORE_API_UNAVAILABLE")


async def test_core_298_without_services_every_route_is_not_found(tmp_path: Path) -> None:
    auth = WebAuth.open(tmp_path)
    token = auth.tokens.new("t")
    app = protect(admin_app(auth), lambda: [], "admin")
    transport = httpx.ASGITransport(app=app, client=LOOPBACK)
    async with httpx.AsyncClient(transport=transport, base_url=ADMIN) as c:
        r = await c.get("/api/v1/status", headers={"authorization": f"Bearer {token}"})
        is_problem(r, 404, "CORE_API_NOT_FOUND")
        # Pages outside /api keep their plain errors.
        assert (await c.get("/admin/nothing")).status_code == 404


# --- CORE-313: OpenAPI ---------------------------------------------------------------


async def test_core_313_openapi_json_only_with_auth(api: Api) -> None:
    is_problem(await api.client.get("/api/openapi.json"), 401, "CORE_API_UNAUTHORIZED")
    r = await api.get("/api/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/v1/status" in paths
    assert "/api/v1/request" in paths
    assert not any(p.startswith("/api/{") for p in paths)
    for page in ("/docs", "/redoc", "/api/docs"):
        assert (await api.get(page)).status_code == 404


# --- CORE-305: status ----------------------------------------------------------------


async def test_core_305_status_contents(api: Api) -> None:
    await api.clock.advance(timedelta(seconds=12))
    r = await api.get("/api/v1/status")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {
        "version",
        "engine_build",
        "started_at",
        "uptime_s",
        "suspended",
        "config",
        "page",
        "clients",
        "kiosk",
        "mqtt",
    }
    assert s["engine_build"] == "1.0+t"
    assert s["uptime_s"] >= 12
    assert s["suspended"] is False
    assert s["config"] == {"version": 0, "disk_valid": True}
    assert s["page"] == {"id": "a", "index": 0, "count": 2}
    assert s["clients"] == []
    assert s["kiosk"] is None  # no agent has connected since start
    assert s["mqtt"] is None  # no broker is configured


async def test_core_305_clients_and_kiosk_object(api: Api) -> None:
    class Hub:
        def clients(self) -> list[dict[str, Any]]:
            return [
                {
                    "client_id": "c1",
                    "role": "kiosk",
                    "address": "127.0.0.1",
                    "user_agent": "Chromium",
                    "since": "2026-11-04T20:00:00+00:00",
                    "queued": 0,
                }
            ]

    st = api.agents.status
    st.seen = True
    st.connected = False
    st.cage_version = "0.1.5"
    st.applied = {"output": "HDMI-A-1", "width": 1920, "height": 1080}
    st.viewport = (1920, 1080)
    st.dpr = 1.0
    st.chromium = "started"
    st.sinks = [{"name": "hdmi", "kind": "hdmi", "available": True}]
    st.audio = {"sink": "hdmi", "volume": 40, "mute": False, "matched": True}
    st.audio_failure = "pactl_failed"
    real_hub = api.services.hub
    api.services.hub = cast(Any, Hub())
    try:
        s = (await api.get("/api/v1/status")).json()
    finally:
        api.services.hub = real_hub
    assert s["clients"] == [
        {
            "role": "kiosk",
            "address": "127.0.0.1",
            "user_agent": "Chromium",
            "since": "2026-11-04T20:00:00+00:00",
        }
    ]
    k = s["kiosk"]
    assert k["connected"] is False  # last reported values after the agent has gone
    assert k["cage_version"] == "0.1.5"
    assert k["display"] == {
        "applied": st.applied,
        "viewport": [1920, 1080],
        "dpr": 1.0,
        "mismatch": False,
    }
    assert k["chromium"] == {"state": "started", "restarts": 0, "failed": False}
    assert k["audio"]["failure"] == "pactl_failed"
    assert k["audio"]["sinks"] == st.sinks


# --- CORE-315 and CORE-113: instances ------------------------------------------------


async def test_core_315_modules_entries(api: Api) -> None:
    r = await api.get("/api/v1/modules")
    assert r.status_code == 200
    mods = r.json()
    assert [m["id"] for m in mods] == ["one", "two", "ans"]
    one = mods[0]
    assert set(one) == {
        "id",
        "module",
        "version",
        "state",
        "reason",
        "hidden",
        "pages",
        "position",
        "positions",
        "health",
    }
    assert one["module"] == "panel"
    assert one["state"] == "RUNNING"
    assert one["hidden"] == []
    assert one["pages"] == ["a", "b"]
    assert one["position"] == "top_left"
    assert one["positions"] == {"b": "top_right"}
    assert one["health"]["instance"] == "one"
    two = mods[1]
    assert two["hidden"] == ["page"]
    assert two["state"] == "SUSPENDED"
    assert mods[2]["pages"] == "all"


async def test_core_113_health_route(api: Api) -> None:
    r = await api.get("/api/v1/modules/ans/health")
    assert r.status_code == 200
    assert r.json()["instance"] == "ans"
    assert r.json()["state"] == "RUNNING"
    is_problem(await api.get("/api/v1/modules/nope/health"), 404, "CORE_API_NOT_FOUND")


async def test_core_315_show_hide_idempotent(api: Api) -> None:
    r = await api.post("/api/v1/modules/one/hide")
    assert r.status_code == 200
    assert r.json()["hidden"] == ["manual"]
    again = await api.post("/api/v1/modules/one/hide")
    assert again.status_code == 200
    assert again.json()["hidden"] == ["manual"]
    shown = await api.post("/api/v1/modules/one/show")
    assert shown.json()["hidden"] == []
    assert (await api.post("/api/v1/modules/one/show")).json()["hidden"] == []
    # A backend-only instance: no visibility, nothing changes.
    ans = await api.post("/api/v1/modules/ans/hide")
    assert ans.status_code == 200
    assert ans.json()["hidden"] == []
    assert ans.json()["state"] == "RUNNING"


async def test_core_315_unknown_instance_is_not_found(api: Api) -> None:
    for action in ("show", "hide", "restart", "stop", "start"):
        r = await api.post(f"/api/v1/modules/nope/{action}")
        is_problem(r, 404, "CORE_API_NOT_FOUND")
    r = await api.put("/api/v1/modules/nope/log-level", {"level": "debug"})
    is_problem(r, 404, "CORE_API_NOT_FOUND")


async def test_core_240_runtime_stop_and_start(api: Api) -> None:
    r = await api.post("/api/v1/modules/ans/stop")
    assert r.status_code == 200
    assert r.json()["state"] == "STOPPED"
    assert r.json()["reason"] == "runtime"
    again = await api.post("/api/v1/modules/ans/stop")
    assert again.status_code == 200
    assert again.json()["state"] == "STOPPED"
    started = await api.post("/api/v1/modules/ans/start")
    assert started.json()["state"] == "RUNNING"
    assert started.json()["reason"] is None
    assert (await api.post("/api/v1/modules/ans/start")).json()["state"] == "RUNNING"


async def test_core_240_config_change_clears_runtime_stop(api: Api) -> None:
    inst = api.engine.supervisor.get("ans")
    await inst.runtime_stop()
    assert inst.runtime_stopped
    await inst.reconfigure({})
    assert inst.state is State.RUNNING
    assert inst.reason is None


async def test_core_315_start_restarts_disabled(api: Api) -> None:
    inst = api.engine.supervisor.get("ans")
    await inst.stop()
    inst.state = State.DISABLED
    r = await api.post("/api/v1/modules/ans/start")
    assert r.json()["state"] == "RUNNING"
    restarted = await api.post("/api/v1/modules/ans/restart")
    assert restarted.json()["state"] == "RUNNING"
    assert restarted.json()["health"]["restarts"] == 2


# --- CORE-112: log levels -----------------------------------------------------------


@pytest.fixture
def levels() -> Any:
    configure_logging("info", stream=io.StringIO(), json=True)
    yield log_levels()
    for key in list(log_levels()._overrides):
        log_levels().set(key[0], key[1], None, timedelta(0))
    configure_logging("info", stream=io.StringIO(), json=True)


async def test_core_112_instance_level_raised_and_expires(api: Api, levels: Any) -> None:
    now = [datetime(2026, 11, 4, 12, 0, tzinfo=UTC)]
    levels.now = lambda: now[0]
    r = await api.put("/api/v1/modules/ans/log-level", {"level": "debug"})
    assert r.status_code == 200
    assert r.json() == {"level": "debug", "expires_at": "2026-11-04T12:30:00+00:00"}
    log = get_logger("answerer", "ans")
    log.debug("ans_debug_on")
    get_logger("answerer", "other").debug("other_debug_dropped")
    events = [rec["event"] for rec in recent_records()]
    assert "ans_debug_on" in events
    assert "other_debug_dropped" not in events
    now[0] += timedelta(minutes=31)
    log.debug("ans_debug_expired")
    assert "ans_debug_expired" not in [rec["event"] for rec in recent_records()]
    assert levels.get("instance", "ans").level == "info"


async def test_core_112_lower_level_does_not_expire_and_null_resets(api: Api, levels: Any) -> None:
    r = await api.put("/api/v1/modules/ans/log-level", {"level": "error"})
    assert r.json() == {"level": "error", "expires_at": None}
    get_logger("answerer", "ans").warning("ans_warning_dropped")
    assert "ans_warning_dropped" not in [rec["event"] for rec in recent_records()]
    reset = await api.put("/api/v1/modules/ans/log-level", {"level": None})
    assert reset.json() == {"level": "info", "expires_at": None}
    get_logger("answerer", "ans").warning("ans_warning_back")
    assert "ans_warning_back" in [rec["event"] for rec in recent_records()]
    missing = await api.put("/api/v1/modules/ans/log-level", {})
    is_problem(missing, 422, "CORE_API_INVALID_REQUEST")
    bad = await api.put("/api/v1/modules/ans/log-level", {"level": "loud"})
    is_problem(bad, 422, "CORE_API_INVALID_REQUEST")


async def test_core_112_engine_loggers(api: Api, levels: Any) -> None:
    r = await api.get("/api/v1/debug/log-level")
    assert r.status_code == 200
    loggers = {e["logger"]: e for e in r.json()}
    assert {"bus", "supervisor", "web", "api"} <= set(loggers)
    assert loggers["bus"] == {
        "logger": "bus",
        "default": "info",
        "level": "info",
        "expires_at": None,
    }
    set_ = await api.put("/api/v1/debug/log-level/bus", {"level": "debug"})
    assert set_.json()["level"] == "debug"
    assert set_.json()["expires_at"] is not None
    get_logger(subsystem="bus").debug("bus_debug_on")
    get_logger(subsystem="web").debug("web_debug_dropped")
    events = [rec["event"] for rec in recent_records()]
    assert "bus_debug_on" in events
    assert "web_debug_dropped" not in events
    listed = {e["logger"]: e for e in (await api.get("/api/v1/debug/log-level")).json()}
    assert listed["bus"]["level"] == "debug"
    unknown = await api.put("/api/v1/debug/log-level/nope", {"level": "debug"})
    is_problem(unknown, 404, "CORE_API_NOT_FOUND")


# --- CORE-306: kiosk ----------------------------------------------------------------


async def test_core_306_kiosk_without_agent(api: Api) -> None:
    r = await api.get("/api/v1/kiosk")
    assert r.status_code == 200
    k = r.json()
    assert k["connected"] is False
    assert k["outputs"] == []
    assert k["sinks"] == []
    put = await api.put("/api/v1/kiosk/audio", {"volume": 30})
    is_problem(put, 409, "CORE_API_NO_KIOSK")


async def test_core_306_runtime_audio(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send(kind: str, data: Any) -> bool:
        sent.append((kind, dict(data)))
        return True

    monkeypatch.setattr(api.agents, "send", send)
    st = api.agents.status
    st.seen = st.connected = True
    st.outputs = [{"name": "HDMI-A-1", "modes": []}]
    st.sinks = [{"name": "alsa_output.hdmi", "kind": "hdmi", "available": True}]
    r = await api.put("/api/v1/kiosk/audio", {"volume": 25, "mute": True})
    assert r.status_code == 200
    assert sent == [("audio", {"sink": "auto", "volume": 25, "mute": True})]
    body = r.json()
    assert body["outputs"] == st.outputs
    assert body["sinks"] == st.sinks
    is_problem(
        await api.put("/api/v1/kiosk/audio", {"volume": True}), 422, "CORE_API_INVALID_REQUEST"
    )
    is_problem(
        await api.put("/api/v1/kiosk/audio", {"volume": -1}), 422, "CORE_API_INVALID_REQUEST"
    )


# --- CORE-308: notify ---------------------------------------------------------------


async def test_core_308_notify(api: Api) -> None:
    r = await api.post("/api/v1/notify", {"name": "HELLO_THERE", "payload": {"x": 1}})
    assert r.status_code == 200
    await asyncio.sleep(0.05)
    seen = [n for n in api.ans().seen if n.name == "HELLO_THERE"]
    assert len(seen) == 1
    assert seen[0].source == "remote"
    assert seen[0].payload == {"x": 1}


async def test_core_308_notify_refusals(api: Api) -> None:
    r = await api.post("/api/v1/notify", {"name": "CORE_READY", "payload": {}})
    is_problem(r, 403, "CORE_API_RESERVED_NAME")
    bad = await api.post("/api/v1/notify", {"name": "PAGE_SET", "payload": {"page": 3}})
    is_problem(bad, 422, "CORE_API_PAYLOAD_INVALID")
    big = await api.post("/api/v1/notify", {"name": "BIG", "payload": {"x": "a" * 270_000}})
    is_problem(big, 413, "CORE_API_TOO_LARGE")
    target = await api.post("/api/v1/notify", {"name": "HELLO", "target": "nope"})
    is_problem(target, 404, "CORE_API_NOT_FOUND")


# --- CORE-314 and CORE-223: request -------------------------------------------------


async def test_core_314_request_reply(api: Api) -> None:
    r = await api.post(
        "/api/v1/request", {"name": "TIME_NOW", "payload": {"q": 1}, "target": "ans"}
    )
    assert r.status_code == 200
    assert r.json() == {"payload": {"by": "ans", "echo": {"q": 1}}}


async def test_core_314_error_reply_is_a_reply(api: Api) -> None:
    r = await api.post("/api/v1/request", {"name": "FAIL_TRANSIENT", "target": "ans"})
    assert r.status_code == 200
    assert r.json() == {"error": {"code": "transient", "message_key": "ANSWER_OFFLINE"}}


async def test_core_314_timeout_is_504(api: Api) -> None:
    r = await api.post("/api/v1/request", {"name": "NOBODY_ANSWERS", "timeout": 0.1})
    is_problem(r, 504, "CORE_API_REQUEST_TIMEOUT")
    out = await api.post("/api/v1/request", {"name": "X", "timeout": 61})
    is_problem(out, 422, "CORE_API_INVALID_REQUEST")
    reserved = await api.post("/api/v1/request", {"name": "CORE_X"})
    is_problem(reserved, 403, "CORE_API_RESERVED_NAME")
    target = await api.post("/api/v1/request", {"name": "TIME_NOW", "target": "nope"})
    is_problem(target, 404, "CORE_API_NOT_FOUND")


# --- CORE-236: pages ----------------------------------------------------------------


async def test_core_236_pages(api: Api) -> None:
    r = await api.get("/api/v1/pages")
    assert r.json() == {
        "current": "a",
        "pages": [
            {"id": "a", "title": "a", "cycle": True},
            {"id": "b", "title": "Bravo", "cycle": True},
        ],
        "paused": [],
    }
    assert (await api.post("/api/v1/pages/next")).json()["current"] == "b"
    assert (await api.post("/api/v1/pages/next")).json()["current"] == "a"
    assert (await api.post("/api/v1/pages/prev")).json()["current"] == "b"
    assert (await api.post("/api/v1/pages/a")).json()["current"] == "a"
    is_problem(await api.post("/api/v1/pages/zz"), 404, "CORE_API_NOT_FOUND")
    status = (await api.get("/api/v1/status")).json()
    assert status["page"] == {"id": "a", "index": 0, "count": 2}


async def test_core_336_pause_and_resume(api: Api) -> None:
    held = await api.post("/api/v1/pages/pause", {"for": 30})
    assert held.status_code == 200
    assert held.json()["paused"] == ["rest"]
    assert (await api.get("/api/v1/pages")).json()["paused"] == ["rest"]
    assert (await api.post("/api/v1/pages/resume")).json()["paused"] == []
    assert (await api.post("/api/v1/pages/resume")).json()["paused"] == []  # no hold: no change
    assert (await api.post("/api/v1/pages/pause")).json()["paused"] == ["rest"]
    assert (await api.post("/api/v1/pages/resume")).json()["paused"] == []
    for bad in ({"for": 0}, {"for": 3601}, {"for": "x"}, {"until": 5}):
        is_problem(await api.post("/api/v1/pages/pause", bad), 422, "CORE_API_INVALID_REQUEST")


async def test_shell_reload(api: Api) -> None:
    r = await api.post("/api/v1/shell/reload")
    assert r.status_code == 200
    assert r.json() == {"clients": 0}
