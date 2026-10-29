"""Milestone 3, part B: the shell WebSocket (core spec 5.6, CORE-246..252, CORE-271..274)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
import websockets
from pydantic import Field, SecretStr
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from mm3.config import LocalizedText, ModuleConfig
from mm3.core.config import load_config
from mm3.core.config.layout import IMPLICIT_PAGE
from mm3.core.engine import Engine
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings, Timeouts
from mm3.core.web.apps import STATIC_DIR
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.sessions import ADMIN_COOKIE, DISPLAY_COOKIE
from mm3.core.web.view import cached_view, layout_object, shell_view
from mm3.core.web.ws import (
    CLOSE_FRAME_TOO_BIG,
    CLOSE_OVERLOADED,
    CLOSE_PROTOCOL,
    CLOSE_ROLE,
    MAX_FRAME_BYTES,
    OUT_QUEUE_FRAMES,
    PING_INTERVAL_S,
    PING_TIMEOUT_S,
    ClientHub,
    engine_build,
)
from mm3.messages import ClientMessage, Notification
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

RECEIVED: list[tuple[str, Any]] = []


class WeatherConfig(ModuleConfig):
    api_key: SecretStr = Field(
        default=SecretStr("k"), title="API_KEY", json_schema_extra={"secret": True}
    )
    label: LocalizedText = Field(default=LocalizedText("x"), title="LABEL")


class Weather(MirrorModule):
    name = "weather"
    element = "mm3-weather"
    config_model = WeatherConfig

    async def start(self) -> None:
        self.ctx.set_state({"temp": 21})

    async def on_client_message(self, msg: ClientMessage) -> None:
        RECEIVED.append(("client", (self.ctx.instance_id, msg.type, msg.payload, msg.client)))
        if msg.type == "ping":
            await self.ctx.send_to_client("pong", {"n": msg.payload.get("n")}, msg.client)

    async def on_notification(self, n: Notification) -> None:
        RECEIVED.append(("notify", (self.ctx.instance_id, n.name, n.source)))

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        if req.name == "FORECAST":
            return {"days": 3}
        if req.name == "SLOW":
            await asyncio.sleep(1)
        return None


class Clock(MirrorModule):
    name = "clock"
    element = "mm3-clock"
    subscribes: ClassVar[set[str] | None] = {"TIME_*"}


class Hidden(MirrorModule):
    name = "hidden"


CONFIG = """
engine:
  locale: it
modules:
  - module: weather
    id: home
    position: top_left
    config: {label: {en: Weather, it: Meteo}}
  - module: weather
    id: away
    position: top_right
  - module: clock
    position: bottom_bar
  - module: hidden
"""


class Mirror:
    def __init__(self, engine: Engine, hub: ClientHub, gateway: WebGateway, auth: WebAuth):
        self.engine = engine
        self.hub = hub
        self.gateway = gateway
        self.auth = auth

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.gateway.mirror.port}/ws"

    @property
    def admin_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.gateway.admin.port}/admin/preview/ws"

    @property
    def admin_origin(self) -> str:
        return f"http://127.0.0.1:{self.gateway.admin.port}"


def free_ports() -> EngineSettings:
    return EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    ).model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )


@pytest.fixture(autouse=True)
def _received() -> None:
    RECEIVED.clear()


@pytest.fixture
async def mirror(tmp_path: Path) -> AsyncIterator[Mirror]:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.yaml").write_text(CONFIG)
    registry = Registry()
    for cls in (Weather, Clock, Hidden):
        registry.add(cls)
    result = load_config(etc / "config.yaml", registry=registry, env_file=etc / "none.env")
    assert result.ok, result.records
    settings = free_ports()
    engine = Engine(
        settings,
        registry=registry,
        clock=VirtualClock(),
        state_dir=tmp_path / "state",
    )
    auth = WebAuth.open(tmp_path / "state")
    hub = ClientHub(cached_view(lambda: result, registry), auth, "1.0+test", hello_timeout=0.5)
    engine.attach_clients(hub)
    gateway = WebGateway(settings, tmp_path / "state", hub=hub)
    await engine.start(result.instance_specs())
    await gateway.start()
    await asyncio.sleep(0.05)
    RECEIVED.clear()  # CORE_READY and other start-up traffic
    try:
        yield Mirror(engine, hub, gateway, auth)
    finally:
        await gateway.stop()
        await engine.stop()


def hello(role: str = "browser", **extra: Any) -> str:
    data = {
        "protocol": 1,
        "client_id": str(uuid.uuid4()),
        "role": role,
        "viewport": [1920, 1080],
        "dpr": 1,
        **extra,
    }
    return json.dumps({"v": 1, "kind": "hello", "data": data})


async def recv(ws: ClientConnection, wait: float = 2.0) -> dict[str, Any]:
    async with asyncio.timeout(wait):
        frame: dict[str, Any] = json.loads(await ws.recv())
    return frame


async def until(ws: ClientConnection, kind: str, wait: float = 2.0) -> dict[str, Any]:
    async with asyncio.timeout(wait):
        while True:
            frame = await recv(ws, wait)
            if frame["kind"] == kind:
                return frame


async def connect(
    url: str, role: str = "browser", *, headers: dict[str, str] | None = None, **extra: Any
) -> tuple[ClientConnection, dict[str, Any], list[dict[str, Any]]]:
    ws = await websockets.connect(url, additional_headers=headers or {})
    await ws.send(hello(role, **extra))
    welcome = await recv(ws)
    states = [await recv(ws) for _ in welcome["data"]["instances"]]
    return ws, welcome, states


async def closed_with(ws: ClientConnection) -> int | None:
    async def drain() -> None:
        while True:
            await ws.recv()

    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(drain(), 3)
    return ws.close_code


async def eventually(check: Callable[[], bool], wait: float = 2.0) -> None:
    async with asyncio.timeout(wait):
        while not check():  # noqa: ASYNC110 - polls engine state, no event to wait on
            await asyncio.sleep(0.01)


def display_cookie(m: Mirror) -> str:
    rec = m.auth.tokens.verify(m.auth.tokens.new("hall", "display"), "display")
    assert rec is not None
    return f"{DISPLAY_COOKIE}={m.auth.sessions.create(rec)}"


# --- CORE-246: handshake --------------------------------------------------------------


async def test_core_246_welcome_then_one_state_per_instance(mirror: Mirror) -> None:
    ws, welcome, states = await connect(mirror.ws_url)
    async with ws:
        data = welcome["data"]
        assert welcome["v"] == 1
        assert set(data) == {
            "engine_build",
            "client_id",
            "role",
            "interactive",
            "layout",
            "page",
            "pages",
            "locale",
            "instances",
        }
        assert (data["engine_build"], data["role"], data["interactive"]) == (
            "1.0+test",
            "browser",
            False,
        )
        assert (data["page"], data["pages"], data["locale"]) == (
            IMPLICIT_PAGE,
            [{"id": IMPLICIT_PAGE, "title": IMPLICIT_PAGE}],
            "it",
        )
        ids = [i["id"] for i in data["instances"]]
        assert ids == ["home", "away", "clock"]  # backend-only "hidden" has no element
        home = data["instances"][0]
        assert home == {
            "id": "home",
            "module": "weather",
            "element": "mm3-weather",
            "config": {"label": "Meteo"},
            "catalog": None,
        }
        assert [(s["kind"], s["instance"]) for s in states] == [
            ("state", "home"),
            ("state", "away"),
            ("state", "clock"),
        ]
        assert states[0]["data"] == {"state": {"temp": 21}, "delta": False}
        assert states[2]["data"] == {"state": {}, "delta": False}


@pytest.mark.parametrize(
    "first",
    [
        json.dumps({"v": 1, "kind": "notify", "data": {}}),
        json.dumps({"v": 1, "kind": "hello", "data": {"protocol": 2}}),
        json.dumps({"v": 2, "kind": "hello", "data": {}}),
        "not json",
        hello(client_id="nope"),
        hello(role="admin"),
        hello(viewport=[0, 10]),
        hello(dpr=0),
    ],
)
async def test_core_246_bad_first_frame_closes_4400(mirror: Mirror, first: str) -> None:
    async with websockets.connect(mirror.ws_url) as ws:
        await ws.send(first)
        assert await closed_with(ws) == CLOSE_PROTOCOL


async def test_core_246_secrets_never_reach_the_browser(mirror: Mirror) -> None:
    ws, welcome, _ = await connect(mirror.ws_url)
    async with ws:
        text = json.dumps(welcome)
    assert "api_key" not in text
    assert '"k"' not in text


# --- CORE-247: roles ------------------------------------------------------------------


async def test_core_247_browser_is_interactive_only_with_a_display_session(
    mirror: Mirror,
) -> None:
    ws, welcome, _ = await connect(mirror.ws_url, headers={"Cookie": display_cookie(mirror)})
    async with ws:
        assert welcome["data"]["interactive"] is True


async def test_core_247_kiosk_needs_the_current_key(mirror: Mirror) -> None:
    async with websockets.connect(mirror.ws_url) as ws:
        await ws.send(hello("kiosk", key="wrong"))
        assert await closed_with(ws) == CLOSE_ROLE
    key = mirror.hub.kiosk_keys.issue()
    ws, welcome, _ = await connect(mirror.ws_url, "kiosk", key=key)
    async with ws:
        assert (welcome["data"]["role"], welcome["data"]["interactive"]) == ("kiosk", True)
    mirror.hub.kiosk_keys.issue()  # a new Chromium launch invalidates the old key
    async with websockets.connect(mirror.ws_url) as ws:
        await ws.send(hello("kiosk", key=key))
        assert await closed_with(ws) == CLOSE_ROLE


async def test_core_247_preview_refused_on_the_mirror_listener(mirror: Mirror) -> None:
    async with websockets.connect(mirror.ws_url) as ws:
        await ws.send(hello("preview"))
        assert await closed_with(ws) == CLOSE_ROLE


# --- CORE-273: endpoints, hello timeout, engine_build ---------------------------------


async def test_core_273_preview_endpoint_needs_session_and_origin(mirror: Mirror) -> None:
    token = mirror.auth.tokens.new("me")
    rec = mirror.auth.tokens.verify(token, "admin")
    assert rec is not None
    cookie = {"Cookie": f"{ADMIN_COOKIE}={mirror.auth.sessions.create(rec)}"}
    origin = {"Origin": mirror.admin_origin}
    for headers, role in (
        (origin, "preview"),  # no session
        (cookie, "preview"),  # no Origin
        ({**cookie, "Origin": "http://evil.example"}, "preview"),
        ({**cookie, **origin}, "browser"),  # wrong role for this endpoint
    ):
        async with websockets.connect(mirror.admin_ws_url, additional_headers=headers) as ws:
            await ws.send(hello(role))
            assert await closed_with(ws) == CLOSE_ROLE, headers
    ws, welcome, _ = await connect(mirror.admin_ws_url, "preview", headers={**cookie, **origin})
    async with ws:
        assert (welcome["data"]["role"], welcome["data"]["interactive"]) == ("preview", True)


async def test_core_273_hello_timeout(mirror: Mirror) -> None:
    async with websockets.connect(mirror.ws_url) as ws:
        assert await closed_with(ws) == CLOSE_PROTOCOL  # hello_timeout is 0.5 s here


def test_core_273_engine_build(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("1")
    first = engine_build(tmp_path)
    version, digest = first.split("+")
    assert len(digest) == 12
    assert int(digest, 16) >= 0
    assert engine_build(tmp_path) == first
    (tmp_path / "a.js").write_text("2")
    assert engine_build(tmp_path) != first
    assert engine_build(STATIC_DIR).startswith(version + "+")


# --- CORE-274: layout object ----------------------------------------------------------


async def test_core_274_layout_object(mirror: Mirror) -> None:
    ws, welcome, _ = await connect(mirror.ws_url)
    async with ws:
        layout = welcome["data"]["layout"]
    assert set(layout) == {"viewport", "cell", "pages"}
    assert layout["viewport"] == [1920, 1080]
    assert len(layout["cell"]) == 2
    (placements,) = layout["pages"].values()
    home = next(p for p in placements if p["instance"] == "home")
    assert set(home) == {
        "instance",
        "col",
        "row",
        "width",
        "height",
        "z",
        "preset",
        "stack_index",
        "stack_size",
        "area",
    }
    assert home["preset"] == "top_left"
    assert len(home["area"]) == 2


def test_core_274_layout_object_matches_the_engine_layout(tmp_path: Path) -> None:
    etc = tmp_path
    (etc / "config.yaml").write_text(CONFIG)
    registry = Registry()
    for cls in (Weather, Clock, Hidden):
        registry.add(cls)
    result = load_config(etc / "config.yaml", registry=registry, env_file=etc / "none.env")
    assert result.layout is not None
    obj = layout_object(result.layout)
    assert json.loads(json.dumps(obj)) == obj
    view = shell_view(result, registry)
    assert view.layout == obj


# --- CORE-248: inbound limits ---------------------------------------------------------


async def test_core_248_non_interactive_frames_are_dropped(mirror: Mirror) -> None:
    ws, _, _ = await connect(mirror.ws_url)
    async with ws:
        await ws.send(json.dumps({"v": 1, "kind": "client", "instance": "home", "data": {}}))
        await ws.send(
            json.dumps({"v": 1, "kind": "notify", "instance": "home", "data": {"name": "X"}})
        )
        await ws.send(json.dumps({"v": 1, "kind": "log", "data": {"message": "boom"}}))
        await eventually(lambda: mirror.hub.clients()[0]["dropped_inbound"] == 2)
    assert RECEIVED == []


async def test_core_248_frame_over_512_kib_closes_1009(mirror: Mirror) -> None:
    ws, _, _ = await connect(mirror.ws_url, headers={"Cookie": display_cookie(mirror)})
    async with ws:
        await ws.send("x" * (MAX_FRAME_BYTES + 1))
        assert await closed_with(ws) == CLOSE_FRAME_TOO_BIG


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        ({"kind": "notify", "instance": "home", "data": {"name": "CORE_READY"}}, "reserved_name"),
        ({"kind": "notify", "instance": "nope", "data": {"name": "X"}}, "unknown_instance"),
        ({"kind": "client", "instance": "hidden", "data": {"type": "x"}}, "unknown_instance"),
        (
            {"kind": "notify", "instance": "home", "data": {"name": "PAGE_SET", "payload": {}}},
            "invalid_payload",
        ),
        ({"kind": "bogus", "instance": "home", "data": {}}, "invalid_frame"),
        ({"kind": "client", "instance": "home", "data": {"type": 5}}, "invalid_frame"),
        ({"kind": "hello", "data": {}}, "invalid_frame"),
    ],
)
async def test_core_248_refused_frames_get_an_error(
    mirror: Mirror, frame: dict[str, Any], code: str
) -> None:
    ws, _, _ = await connect(mirror.ws_url, headers={"Cookie": display_cookie(mirror)})
    async with ws:
        await ws.send(json.dumps({"v": 1, "ref": "r1", **frame}))
        err = await until(ws, "error")
    assert err["data"]["code"] == code
    assert err["data"]["message_key"].startswith("CORE_WS_")
    assert err["data"]["ref"] == "r1"
    assert RECEIVED == []


async def test_core_248_element_notification_source_and_new_trace(mirror: Mirror) -> None:
    ws, _, _ = await connect(mirror.ws_url, headers={"Cookie": display_cookie(mirror)})
    async with ws:
        await ws.send(
            json.dumps(
                {"v": 1, "kind": "notify", "instance": "home", "data": {"name": "HELLO_THERE"}}
            )
        )
        await eventually(lambda: ("notify", ("away", "HELLO_THERE", "weather/home")) in RECEIVED)
    # Both halves of the sender are excluded (CORE-21): "home" never gets it.
    assert ("notify", ("home", "HELLO_THERE", "weather/home")) not in RECEIVED


# --- CORE-271: client and state frames ------------------------------------------------


async def test_core_271_client_frames_both_ways(mirror: Mirror) -> None:
    ws, welcome, _ = await connect(mirror.ws_url, headers={"Cookie": display_cookie(mirror)})
    async with ws:
        cid = welcome["data"]["client_id"]
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "kind": "client",
                    "instance": "home",
                    "data": {"type": "ping", "payload": {"n": 7}},
                }
            )
        )
        pong = await until(ws, "client")
    assert pong == {
        "v": 1,
        "kind": "client",
        "instance": "home",
        "data": {"type": "pong", "payload": {"n": 7}},
    }
    assert [("client", ("home", "ping", {"n": 7}, cid))] == RECEIVED


async def test_core_271_state_and_delta(mirror: Mirror) -> None:
    ws, _, _ = await connect(mirror.ws_url)
    async with ws:
        ctx = mirror.engine.supervisor.get("home").context
        assert ctx is not None
        ctx.patch_state({"wind": 3})
        frame = await until(ws, "state")
        assert frame["data"] == {"state": {"wind": 3}, "delta": True}
    ws, _, states = await connect(mirror.ws_url)
    async with ws:
        assert states[0]["data"] == {"state": {"temp": 21, "wind": 3}, "delta": False}


async def test_core_271_notify_to_browsers_lists_matching_instances(mirror: Mirror) -> None:
    ws, _, _ = await connect(mirror.ws_url)
    async with ws:
        mirror.engine.supervisor.emit("CORE_LOCALE_CHANGED", {"locale": "de"})
        frame = await until(ws, "notify")
        assert frame["data"]["instances"] == ["away", "home"]  # clock subscribes TIME_* only
        assert frame["data"]["notification"]["name"] == "CORE_LOCALE_CHANGED"
        assert frame["data"]["notification"]["source"] == "core"
        ctx = mirror.engine.supervisor.get("away").context
        assert ctx is not None
        await ctx.send_notification("TIME_TICK", {})
        frame = await until(ws, "notify")
        assert frame["data"]["instances"] == ["clock", "home"]  # not the sender "away"


# --- CORE-223, CORE-272: requests from the shell --------------------------------------


async def test_core_223_request_reply_and_timeout(mirror: Mirror) -> None:
    ws, _, _ = await connect(mirror.ws_url, headers={"Cookie": display_cookie(mirror)})
    async with ws:
        req = {"rid": "a", "name": "FORECAST", "payload": {}, "target": "away", "timeout": 2}
        await ws.send(json.dumps({"v": 1, "kind": "request", "instance": "home", "data": req}))
        reply = await until(ws, "reply")
        assert reply == {
            "v": 1,
            "kind": "reply",
            "instance": "home",
            "data": {"rid": "a", "payload": {"days": 3}},
        }
        req = {"rid": 2, "name": "SLOW", "payload": {}, "target": "away", "timeout": 0.2}
        await ws.send(json.dumps({"v": 1, "kind": "request", "instance": "home", "data": req}))
        reply = await until(ws, "reply", wait=3)
        assert reply["data"] == {
            "rid": 2,
            "error": {"code": "timeout", "message_key": "REQUEST_TIMEOUT"},
        }
        bad = {"rid": 3, "name": "FORECAST", "timeout": 120}
        await ws.send(json.dumps({"v": 1, "kind": "request", "instance": "home", "data": bad}))
        err = await until(ws, "error")
        assert err["data"]["code"] == "invalid_frame"


# --- CORE-249: outbound limits --------------------------------------------------------


async def test_core_249_state_frames_coalesce_and_full_queue_closes_1013(
    mirror: Mirror,
) -> None:
    ws, _, _ = await connect(mirror.ws_url)
    async with ws:
        await eventually(lambda: len(mirror.hub.clients()) == 1)
        hub = mirror.hub
        client = next(iter(hub._clients.values()))
        client.ready.clear()
        sender_hold = asyncio.Event()

        # Stop the sender from draining while frames are queued.
        orig_ready = client.ready
        client.ready = sender_hold
        for i in range(5):
            hub.send_state("home", {"temp": i}, delta=False)
        hub.send_state("home", {"wind": 1}, delta=True)
        states = [f for f in client.queue if f["kind"] == "state"]
        assert len(states) == 1
        assert states[0]["data"] == {"state": {"temp": 4, "wind": 1}, "delta": False}
        for i in range(OUT_QUEUE_FRAMES):
            hub.send_client("home", "x", {"i": i}, None)
        assert client.closing == CLOSE_OVERLOADED
        client.ready = orig_ready
        orig_ready.set()
        assert await closed_with(ws) == CLOSE_OVERLOADED


async def test_core_99_preview_gets_no_client_frames(mirror: Mirror) -> None:
    token = mirror.auth.tokens.new("me")
    rec = mirror.auth.tokens.verify(token, "admin")
    assert rec is not None
    headers = {
        "Cookie": f"{ADMIN_COOKIE}={mirror.auth.sessions.create(rec)}",
        "Origin": mirror.admin_origin,
    }
    pv, _, _ = await connect(mirror.admin_ws_url, "preview", headers=headers)
    br, _, _ = await connect(mirror.ws_url)
    async with pv, br:
        mirror.hub.send_client("home", "hi", {}, None)
        assert (await until(br, "client"))["data"]["type"] == "hi"
        with pytest.raises(TimeoutError):
            await until(pv, "client", wait=0.3)


# --- CORE-251: liveness ---------------------------------------------------------------


def test_core_251_ping_every_15_s_drop_after_30_s(mirror: Mirror) -> None:
    assert PING_INTERVAL_S == 15
    assert PING_INTERVAL_S + PING_TIMEOUT_S == 30
    for listener in (mirror.gateway.mirror, mirror.gateway.admin):
        config = listener._server.config
        assert (config.ws_ping_interval, config.ws_ping_timeout) == (15, 15)
        assert config.ws_max_size == MAX_FRAME_BYTES


# --- CORE-114: browser log frames -----------------------------------------------------


async def test_core_114_log_frames_are_logged_from_any_client(
    mirror: Mirror, tmp_path: Path
) -> None:
    ws, _, _ = await connect(mirror.ws_url)
    async with ws:
        frame = {
            "v": 1,
            "kind": "log",
            "instance": "home",
            "data": {"level": "error", "message": "boom", "stack": "at x", "user_agent": "UA"},
        }
        await ws.send(json.dumps(frame))
        log = tmp_path / "log.jsonl"

        def logged() -> bool:
            return any(
                json.loads(line).get("event") == "browser_log"
                for line in log.read_text().splitlines()
                if line.strip()
            )

        await eventually(logged)
    rec = next(
        json.loads(line)
        for line in log.read_text().splitlines()
        if line.strip() and json.loads(line).get("event") == "browser_log"
    )
    assert (rec["source"], rec["message"], rec["stack"]) == ("browser", "boom", "at x")


async def test_core_254_display_cookie_renewed_on_connect(mirror: Mirror) -> None:
    cookie = display_cookie(mirror)
    ws = await websockets.connect(mirror.ws_url, additional_headers={"Cookie": cookie})
    async with ws:
        assert ws.response is not None
        renewed = ws.response.headers.get("set-cookie", "")
    assert renewed.startswith(cookie + ";")
    assert "Max-Age=34560000" in renewed
    assert "HttpOnly" in renewed
    assert "SameSite=Strict" in renewed
    plain = await websockets.connect(mirror.ws_url)
    async with plain:
        assert plain.response is not None
        assert "set-cookie" not in plain.response.headers
