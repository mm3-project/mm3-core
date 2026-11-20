"""Milestone 8, step 5: the full-screen takeover (CORE-410, CORE-411, CORE-412, CORE-418).

The takeover runs on the media service with the fake ``ffmpeg`` of
``ffmpeg_fake`` and a fake ``ffprobe`` that prints the content of the file it
is given, so a test file holding ``12.5`` lasts 12.5 s.
"""

from __future__ import annotations

import asyncio
import io
import os
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest

import ffmpeg_fake
from conftest import EngineFactory, Recorder, settle
from mm3 import MirrorModule
from mm3.core.config import ConfigResult, load_config
from mm3.core.config.diff import ReloadPlan
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging, recent_records
from mm3.core.media import MediaService
from mm3.core.media.command import ffprobe_command, parse_duration
from mm3.core.media.takeover import HOLDER, TakeoverRefused, TakeoverService
from mm3.core.registry import Registry
from mm3.core.services.grants import GrantSet
from mm3.core.settings import EngineSettings
from mm3.core.supervisor import InstanceSpec
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.messages import Notification
from mm3.notifications import validate_payload
from mm3.testing import VirtualClock

FFPROBE = r"""#!{python}
import sys, time
path = sys.argv[-1].removeprefix("file:")
with open(path) as f:
    text = f.read().strip()
if text == "sleep":
    time.sleep(30)
print(text)
"""

CONFIG = """
media:
  sources:
    door: {kind: rtsp, url: "rtsp://cam.lan/ok", audio: false}
    porch: {kind: rtsp, url: "rtsp://cam.lan/ok"}
    ending: {kind: rtsp, url: "rtsp://cam.lan/end", audio: false}
    radio: {kind: stream, url: "https://radio.example/ok"}
    holidays: {kind: files, path: files/trip}
    jingle: {kind: files, path: files/jingle.mp3}
  takeover: {duration: 60, max_duration: 600}
"""


def load(tmp: Path, text: str) -> ConfigResult:
    etc = tmp / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "config.yaml").write_text(text)
    result = load_config(
        etc / "config.yaml", registry=Registry(), env_file=etc / "x.env", environ={}
    )
    assert result.ok, result.records
    return result


class Holds:
    """A stand-in for the page service's pause holds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def pause_hold(self, holder: str, seconds: float | None = None) -> None:
        self.calls.append(("pause", holder))

    def resume(self, holder: str) -> None:
        self.calls.append(("resume", holder))


@dataclass
class Shells:
    sent: list[dict[str, Any] | None] = field(default_factory=list)

    def control_takeover(self, takeover: dict[str, Any] | None) -> None:
        self.sent.append(takeover)


@dataclass
class World:
    media: MediaService
    takeover: TakeoverService
    clock: VirtualClock
    holds: Holds
    shells: Shells
    events: list[dict[str, Any]]
    tmp: Path


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fake_ffprobe(bin_dir: Path) -> Path:
    import sys

    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "ffprobe"
    exe.write_text(FFPROBE.replace("{python}", sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


@pytest.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(tmp_path / "ffmpeg.log"))
    exe = ffmpeg_fake.install(tmp_path / "bin")
    clock = VirtualClock()
    events: list[dict[str, Any]] = []
    media = MediaService(
        tmp_path / "run",
        tmp_path / "state",
        clock,
        lambda name, payload: events.append({"name": name, **payload}),
        ffmpeg=str(exe),
        render_node=tmp_path / "none",
    )
    media.configure(load(tmp_path, CONFIG))
    root = media.root
    write(root, "files/trip/a.mp4", "12.5")
    write(root, "files/trip/b.jpg", "picture")
    write(root, "files/trip/day 2/c.mp4", "N/A")
    write(root, "files/trip/slow.mp4", "sleep")
    write(root, "files/jingle.mp3", "3")
    holds, shells = Holds(), Shells()
    takeover = TakeoverService(
        media,
        pages=holds,
        shells=shells,
        ffprobe=str(fake_ffprobe(tmp_path / "bin")),
        probe_timeout=0.5,
    )
    try:
        yield World(media, takeover, clock, holds, shells, events, tmp_path)
    finally:
        await takeover.close()
        await media.close()


def note(name: str, payload: dict[str, Any], source: str = "core") -> Notification:
    nid = uuid4()
    return Notification(
        id=nid, trace=nid, ts=VirtualClock().wall(), name=name, source=source, payload=payload
    )


# --- CORE-410: one takeover, its ends --------------------------------------------------------


async def test_core_410_newest_wins_and_holds_the_media_pause(world: World) -> None:
    first = await world.takeover.start("door")
    second = await world.takeover.start("radio", duration=30)
    assert world.takeover.current is not None
    assert world.takeover.current.id == second
    assert first != second
    assert world.holds.calls == [("pause", HOLDER)]  # replaced, not released in between
    assert [s["src"] if s else None for s in world.shells.sent] == [
        "/media/door/stream.mjpg",
        "/media/radio/hls/index.m3u8",
    ]
    await world.clock.advance(30)
    assert world.takeover.current is None
    assert world.holds.calls == [("pause", HOLDER), ("resume", HOLDER)]
    assert world.shells.sent[-1] is None


async def test_core_410_ends_by_duration_default_media_takeover_duration(world: World) -> None:
    await world.takeover.start("door")
    t = world.takeover.current
    assert t is not None
    assert t.until - t.started == timedelta(seconds=60)
    await world.clock.advance(59)
    assert world.takeover.current is t
    await world.clock.advance(1)
    assert world.takeover.current is None


async def test_core_410_a_file_ends_after_its_length_read_with_ffprobe(world: World) -> None:
    await world.takeover.start("holidays", file="a.mp4", duration=60)
    t = world.takeover.current
    assert t is not None
    assert t.until - t.started == timedelta(seconds=12.5)
    assert world.shells.sent[-1] == {
        "id": t.id,
        "src": "/media/files/files/trip/a.mp4",
        "fit": "contain",
        "until": world.takeover.shell_object()["until"],  # type: ignore[index]
    }
    await world.clock.advance(12.5)
    assert world.takeover.current is None


async def test_core_410_until_is_the_earlier_end(world: World) -> None:
    await world.takeover.start("holidays", file="a.mp4", duration=5)
    t = world.takeover.current
    assert t is not None
    assert t.until - t.started == timedelta(seconds=5)


@pytest.mark.parametrize("file", ["b.jpg", "day 2/c.mp4", "slow.mp4"])
async def test_core_410_images_and_unread_lengths_end_by_duration(world: World, file: str) -> None:
    await world.takeover.start("holidays", file=file, duration=40)
    t = world.takeover.current
    assert t is not None
    assert t.until - t.started == timedelta(seconds=40)
    if file == "day 2/c.mp4":
        assert world.takeover.shell_object()["src"] == "/media/files/files/trip/day%202/c.mp4"  # type: ignore[index]


async def test_core_410_a_stream_whose_child_exits_0_ends_the_takeover(world: World) -> None:
    await world.takeover.start("ending")
    viewer = await world.media.open_mjpeg("ending")
    async with asyncio.timeout(5):
        async for _ in viewer.frames():
            pass
    async with asyncio.timeout(5):
        while world.takeover.current is not None:  # noqa: ASYNC110 - ended by a real child
            await asyncio.sleep(0.02)
    viewer.close()
    assert world.shells.sent[-1] is None


async def test_core_410_a_touch_on_the_kiosk_ends_it(world: World) -> None:
    await world.takeover.start("door")
    world.takeover.deliver(note("CORE_INTERACTION", {"kind": "touch"}), None)
    await settle()
    assert world.takeover.current is None
    assert world.holds.calls[-1] == ("resume", HOLDER)


async def test_core_410_a_reload_that_removes_or_changes_the_source_ends_it(
    world: World,
) -> None:
    await world.takeover.start("door")
    same = load(world.tmp, CONFIG)
    await world.media.reloaded(same, ReloadPlan())
    await world.takeover.reloaded(same, ReloadPlan())
    assert world.takeover.current is not None  # nothing changed for door
    changed = load(
        world.tmp, CONFIG.replace('cam.lan/ok", audio: false', 'cam2.lan/ok", audio: false', 1)
    )
    await world.media.reloaded(changed, ReloadPlan())
    await world.takeover.reloaded(changed, ReloadPlan())
    assert world.takeover.current is None


async def test_core_410_display_power_is_not_touched_and_preview_url_is_not_used(
    world: World,
) -> None:
    await world.takeover.start("porch")
    obj = world.takeover.shell_object()
    assert obj is not None
    assert obj["src"] == "/media/porch/hls/index.m3u8"
    assert not obj["src"].startswith("/admin/")


# --- CORE-411: refusals, ids, stops ----------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"source": "nope"}, "unknown"),
        ({"source": "door", "duration": 0}, "duration"),
        ({"source": "door", "duration": -5}, "duration"),
        ({"source": "door", "file": "a.mp4"}, "file"),
        ({"source": "holidays", "file": "missing.mp4"}, "file"),
        ({"source": "holidays", "file": "../jingle.mp3"}, "file"),
        ({"source": "holidays", "file": "notes.txt"}, "file"),
        ({"source": "holidays"}, "file_required"),
    ],
)
async def test_core_411_refused_takeovers(world: World, args: dict[str, Any], reason: str) -> None:
    source = args.pop("source")
    with pytest.raises(TakeoverRefused) as err:
        await world.takeover.start(source, **args)
    assert err.value.reason == reason
    assert world.takeover.current is None
    assert world.holds.calls == []


async def test_core_411_duration_is_shortened_to_max_duration(world: World) -> None:
    await world.takeover.start("door", duration=5000)
    t = world.takeover.current
    assert t is not None
    assert t.until - t.started == timedelta(seconds=600)


async def test_core_411_a_files_source_on_one_file_needs_no_file(world: World) -> None:
    await world.takeover.start("jingle")
    t = world.takeover.current
    assert t is not None
    assert t.until - t.started == timedelta(seconds=3)
    assert world.takeover.shell_object()["src"] == "/media/files/files/jingle.mp3"  # type: ignore[index]


async def test_core_411_a_source_in_error_still_starts(world: World) -> None:
    world.media.sources["door"].state = "error"
    assert await world.takeover.start("door")


async def test_core_411_ids_are_12_hex_unless_given(world: World) -> None:
    tid = await world.takeover.start("door")
    assert len(tid) == 12
    assert int(tid, 16) >= 0
    assert await world.takeover.start("door", takeover_id="bell-1") == "bell-1"


async def test_core_411_stop_by_id_and_without(world: World) -> None:
    await world.takeover.start("door", takeover_id="a")
    assert await world.takeover.stop("b") is False
    assert world.takeover.current is not None
    assert await world.takeover.stop("a") is True
    assert await world.takeover.stop() is False  # nothing runs
    await world.takeover.start("door")
    assert await world.takeover.stop() is True


async def test_core_411_a_module_stops_only_its_own_takeover(world: World) -> None:
    await world.takeover.start("door", owner="rest")
    assert await world.takeover.stop(owner="cam") is False
    await world.takeover.start("door", owner="cam")
    assert await world.takeover.stop(owner="other") is False
    assert await world.takeover.stop(owner="cam") is True


async def test_core_411_select_options_are_the_sources_that_start_without_a_file(
    world: World,
) -> None:
    assert world.takeover.select_options() == ["door", "porch", "ending", "radio", "jingle"]
    assert world.takeover.playing is None
    await world.takeover.start("radio")
    assert world.takeover.playing == "radio"


def supervisor(capabilities: set[str], media: tuple[str, ...]) -> Any:
    cls = SimpleNamespace(capabilities=capabilities)
    inst = SimpleNamespace(spec=SimpleNamespace(module="bell", grants=GrantSet(media=media)))
    reg = SimpleNamespace(get=lambda name: SimpleNamespace(cls=cls) if name == "bell" else None)

    def get(iid: str) -> Any:
        if iid != "b":
            raise KeyError(iid)
        return inst

    return SimpleNamespace(get=get, registry=reg)


@pytest.mark.parametrize(
    ("caps", "grant", "started"),
    [
        ({"media.takeover"}, ("door",), True),
        ({"media.takeover"}, ("*",), True),
        ({"media.takeover"}, ("radio",), False),
        ({"media.play"}, ("door",), False),
        (set(), ("*",), False),
    ],
)
async def test_core_411_a_module_needs_media_takeover_and_the_source_grant(
    world: World, caps: set[str], grant: tuple[str, ...], started: bool
) -> None:
    configure_logging("info", stream=io.StringIO(), json=True)
    world.takeover.supervisor = supervisor(caps, grant)
    world.takeover.deliver(note("MEDIA_TAKEOVER", {"source": "door"}, "bell/b"), None)
    await settle()
    t = world.takeover.current
    assert (t is not None) is started
    if started:
        assert t is not None
        assert t.owner == "b"
    else:
        assert any(r.get("event") == "media_takeover_denied" for r in recent_records())


async def test_core_411_media_stop_from_a_module_needs_the_grant_of_the_running_source(
    world: World,
) -> None:
    world.takeover.supervisor = supervisor({"media.takeover"}, ("door",))
    await world.takeover.start("radio", owner="rest", takeover_id="r")
    world.takeover.deliver(note("MEDIA_STOP", {"id": "r"}, "bell/b"), None)
    await settle()
    assert world.takeover.current is not None  # radio is not granted
    world.takeover.deliver(note("MEDIA_STOP", {}, "mqtt"), None)
    await settle()
    assert world.takeover.current is None


async def test_core_411_commands_from_mqtt_and_rest_are_dropped_with_a_warning(
    world: World,
) -> None:
    configure_logging("info", stream=io.StringIO(), json=True)
    world.takeover.deliver(note("MEDIA_TAKEOVER", {"source": "nope"}, "mqtt"), None)
    await settle()
    assert world.takeover.current is None
    assert any(r.get("event") == "media_takeover_dropped" for r in recent_records())
    world.takeover.deliver(
        note("MEDIA_TAKEOVER", {"source": "door", "duration": 10, "id": "x"}, "remote"), None
    )
    await settle()
    t = world.takeover.current
    assert t is not None
    assert (t.id, t.owner) == ("x", "rest")


# --- CORE-412: the state --------------------------------------------------------------------


async def test_core_412_state_carries_the_takeover(world: World) -> None:
    await world.takeover.start("holidays", file="a.mp4", takeover_id="t1")
    body = world.media.status()
    assert body["takeover"]["id"] == "t1"
    assert body["takeover"]["file"] == "a.mp4"
    assert body["takeover"]["started"].endswith("Z")
    validate_payload("CORE_MEDIA_CHANGED", world.media.payload())
    sent = [e for e in world.events if e["name"] == "CORE_MEDIA_CHANGED"]
    assert sent
    assert sent[-1]["takeover"]["source"] == "holidays"
    await world.clock.advance(13)
    await world.clock.advance(1)
    assert world.media.payload()["takeover"] is None
    assert world.events[-1]["takeover"] is None


# --- the ffprobe command line ----------------------------------------------------------------


def test_core_410_ffprobe_reads_the_open_descriptor_only() -> None:
    argv = ffprobe_command("/usr/bin/ffprobe", 7)
    assert argv[0] == "/usr/bin/ffprobe"
    assert argv[-1] == "file:/proc/self/fd/7"
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-show_entries") + 1] == "format=duration"


@pytest.mark.parametrize(
    ("text", "value"),
    [("12.500000\n", 12.5), ("N/A\n", None), ("", None), ("0\n", None), ("nan\n", None)],
)
def test_core_410_parse_duration(text: str, value: float | None) -> None:
    assert parse_duration(text) == value


@pytest.mark.skipif(not os.path.exists("/proc/self/fd"), reason="needs /proc")
async def test_core_410_real_ffprobe_reads_a_file_length(tmp_path: Path) -> None:
    import shutil

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg is not installed")
    media = MediaService(tmp_path / "run", tmp_path / "state", VirtualClock())
    media.configure(load(tmp_path, CONFIG))
    out = media.root / "files" / "trip" / "tone.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(out)
    )
    assert await proc.wait() == 0
    takeover = TakeoverService(media, ffprobe=ffprobe)
    try:
        await takeover.start("holidays", file="tone.m4a")
        t = takeover.current
        assert t is not None
        assert abs((t.until - t.started).total_seconds() - 2.0) < 0.2
    finally:
        await takeover.close()
        await media.close()


# --- CORE-411: REST --------------------------------------------------------------------------

ADMIN = "http://127.0.0.1:8081"


async def test_core_411_rest_post_answers_202_and_delete_204(world: World, tmp_path: Path) -> None:
    engine = Engine(
        EngineSettings(), registry=Registry(), clock=world.clock, state_dir=tmp_path / "e"
    )
    auth = WebAuth.open(tmp_path / "web")
    services = ApiServices(engine, media=world.media, takeover=world.takeover)
    app = protect(admin_app(auth, api=services), lambda: [], "admin")
    await engine.start([])
    token = auth.tokens.new("t")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5))
    headers = {"authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ADMIN, headers=headers) as c:
            r = await c.post("/api/v1/media/takeover", json={"source": "door", "id": "d1"})
            assert (r.status_code, r.json()) == (202, {"id": "d1"})
            assert world.takeover.current is not None
            assert world.takeover.current.owner == "rest"
            got = (await c.get("/api/v1/media")).json()
            assert got["takeover"]["id"] == "d1"
            for body in (
                {"source": "nope"},
                {"source": "door", "duration": 0},
                {"source": "holidays"},
                {"source": "door", "extra": 1},
            ):
                r = await c.post("/api/v1/media/takeover", json=body)
                assert r.status_code == 422, body
                assert r.json()["code"] == "CORE_API_INVALID_REQUEST"
            assert (await c.delete("/api/v1/media/takeover?id=other")).status_code == 204
            assert world.takeover.current is not None
            assert (await c.delete("/api/v1/media/takeover")).status_code == 204
            assert world.takeover.current is None
            assert (await c.delete("/api/v1/media/takeover")).status_code == 204
    finally:
        await engine.stop()


# --- CORE-411: from a module and from MQTT, on a running engine ------------------------------


class Bell(MirrorModule):
    name = "bell"
    capabilities: ClassVar[set[str]] = {"media.takeover"}

    async def start(self) -> None:
        await self.ctx.send_notification("MEDIA_TAKEOVER", {"source": "door", "id": "ring"})


async def test_core_411_a_module_starts_a_takeover_on_the_bus(
    make_engine: EngineFactory, world: World
) -> None:
    engine = make_engine(Bell)
    world.takeover.supervisor = engine.supervisor
    engine.bus.add_endpoint(world.takeover)
    await engine.start([InstanceSpec("b", "bell", grants=GrantSet(media=("door",)))])
    await settle()
    t = world.takeover.current
    assert t is not None
    assert (t.id, t.owner) == ("ring", "b")


async def test_core_411_from_mqtt_only_when_imported(
    make_engine: EngineFactory, world: World, recorder: Recorder
) -> None:
    from mm3.core.settings import MqttSettings
    from mqtt_fake import FakeBroker

    broker = FakeBroker()
    engine = make_engine(
        mqtt=MqttSettings.model_validate(
            {"url": "mqtt://broker", "base_topic": "mm3/t", "import": ["MEDIA_*"]}
        )
    )
    engine.mqtt.connection._factory = broker.factory
    engine.mqtt.connection._rand = lambda: 0.5
    engine.bus.add_endpoint(world.takeover)
    await engine.start([])
    await settle()
    broker.inject("mm3/t/notify/MEDIA_TAKEOVER", '{"source": "door"}')
    await settle()
    await settle()
    t = world.takeover.current
    assert t is not None
    assert t.owner == "mqtt"
    broker.inject("mm3/t/notify/MEDIA_STOP", "{}")
    await settle()
    await settle()
    assert world.takeover.current is None
