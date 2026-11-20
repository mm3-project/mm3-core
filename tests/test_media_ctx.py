"""Milestone 8, step 7: ``ctx.media`` and the fake media service (MOD-100, MOD-101, MOD-103).

Most tests run a module under ``ModuleHarness``, whose ``h.media`` is the fake of the
test kit; the last ones run the same calls against the engine's media and takeover
services, with the fake ``ffmpeg`` of ``ffmpeg_fake``.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

import ffmpeg_fake
from conftest import EngineFactory, settle
from mm3 import MirrorModule
from mm3.core.config import load_config
from mm3.core.media import MediaService
from mm3.core.media import instance as media_instance
from mm3.core.media.files import FileRefused
from mm3.core.media.instance import EngineMediaBackend
from mm3.core.media.takeover import TakeoverService
from mm3.core.registry import Registry
from mm3.core.services.grants import GrantSet
from mm3.core.supervisor import InstanceSpec
from mm3.errors import TransientError
from mm3.media import MediaFile, MediaSource, MediaUrl
from mm3.testing import FakeMedia, ModuleHarness, VirtualClock

# --- modules ---------------------------------------------------------------------------------


class Player(MirrorModule):
    """Declares both media capabilities."""

    name = "player"
    capabilities: ClassVar[set[str]] = {"media.play", "media.takeover"}


class PlayOnly(MirrorModule):
    name = "playonly"
    capabilities: ClassVar[set[str]] = {"media.play"}


class TakeoverOnly(MirrorModule):
    name = "takeoveronly"
    capabilities: ClassVar[set[str]] = {"media.takeover"}


class Plain(MirrorModule):
    """Declares nothing and uses ctx.media."""

    name = "plain"


GRANTS = {"media": ["door", "radio", "holidays", "jingle"]}


def fake_world(h: ModuleHarness) -> FakeMedia:
    m = h.media
    m.add_source("door", "rtsp", audio=False, delivery="mjpeg")
    m.add_source("radio", "stream")
    m.add_source("cam", "camera")
    m.add_source("holidays", "files", path="files/trip")
    m.add_source("jingle", "files", path="files/jingle.mp3")
    m.add_files("holidays", ["a.mp4", "b.jpg", "day 2/"])
    m.add_files("holidays", ["c.png"], path="day 2")
    return m


def ctx(h: ModuleHarness) -> Any:
    assert h.instance is not None
    return h.instance.ctx


# --- MOD-100: sources, URLs and files ---------------------------------------------------------


async def test_mod_100_sources_lists_only_granted_sources_with_their_state() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        h.media.set_state("door", "running")
        sources = ctx(h).media.sources()
        assert [s.name for s in sources] == ["door", "radio", "holidays", "jingle"]
        door = sources[0]
        assert door == MediaSource("door", "rtsp", "mjpeg", False, "running")
        assert sources[1].delivery == "hls"
        assert sources[1].audio is True
        assert sources[2].delivery == "file"


async def test_mod_100_a_wildcard_grant_lists_every_source_and_a_camera_has_no_audio() -> None:
    async with ModuleHarness(Player, grants={"media": ["*"]}) as h:
        fake_world(h)
        names = [s.name for s in ctx(h).media.sources()]
        assert "cam" in names
        cam = next(s for s in ctx(h).media.sources() if s.name == "cam")
        assert (cam.delivery, cam.audio) == ("mjpeg", False)


async def test_mod_100_url_follows_the_delivery_of_the_source() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        media = ctx(h).media
        assert media.url("door") == MediaUrl("mjpeg", "/media/door/stream.mjpg")
        assert media.url("radio") == MediaUrl("hls", "/media/radio/hls/index.m3u8")
        assert media.url("holidays", file="a.mp4") == MediaUrl(
            "file", "/media/files/files/trip/a.mp4"
        )
        assert media.url("jingle") == MediaUrl("file", "/media/files/files/jingle.mp3")


async def test_mod_100_url_percent_encodes_every_part_of_a_file() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        url = ctx(h).media.url("holidays", file="day 2/c.png")
        assert url.url == "/media/files/files/trip/day%202/c.png"


async def test_mod_100_url_and_files_outside_the_grant_raise_permission_error() -> None:
    async with ModuleHarness(Player, grants={"media": ["door"]}) as h:
        fake_world(h)
        media = ctx(h).media
        with pytest.raises(PermissionError):
            media.url("radio")
        with pytest.raises(PermissionError):
            await media.files("holidays")
        # an unknown name is not told apart from one outside the grant
        with pytest.raises(PermissionError):
            media.url("nothing")


@pytest.mark.parametrize(
    ("source", "file"),
    [
        ("nothing", None),  # unknown source
        ("door", "a.mp4"),  # a file on a source that is not of kind files
        ("holidays", None),  # a folder needs a file
        ("holidays", "missing.mp4"),  # a file that does not exist
        ("holidays", "notes.txt"),  # an extension the media folder does not serve
        ("holidays", "../jingle.mp3"),  # outside the source
        ("holidays", "/etc/passwd"),  # not a media file
        ("jingle", "other.mp3"),  # a file on a source that is one file
    ],
)
async def test_mod_100_url_of_an_unknown_source_or_file_is_a_value_error(
    source: str, file: str | None
) -> None:
    async with ModuleHarness(Player, grants={"media": ["*"]}) as h:
        fake_world(h)
        with pytest.raises(ValueError):  # noqa: PT011 - the message is not part of the contract
            ctx(h).media.url(source, file=file)


async def test_mod_100_files_lists_a_folder_and_its_subfolders() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        entries = await ctx(h).media.files("holidays")
        assert [(e.name, e.path, e.kind) for e in entries] == [
            ("a.mp4", "a.mp4", "video"),
            ("b.jpg", "b.jpg", "image"),
            ("day 2", "day 2", "folder"),
        ]
        assert entries[0].size == 1000
        assert entries[2].size is None
        assert entries[0].modified is not None
        assert entries[0].modified.tzinfo is not None
        inner = await ctx(h).media.files("holidays", "day 2")
        assert inner == [MediaFile("c.png", "day 2/c.png", "image", 1000, entries[0].modified)]
        # the path of an entry goes straight back into url()
        assert ctx(h).media.url("holidays", file=inner[0].path).kind == "file"


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("door", ""),  # not a files source
        ("jingle", ""),  # one file, not a folder
        ("holidays", "../other"),  # leaves the source
        ("holidays", "a//b"),  # an empty part
        ("holidays", "nowhere"),  # a folder that does not exist
    ],
)
async def test_mod_100_files_of_something_that_is_not_a_folder_is_a_value_error(
    source: str, path: str
) -> None:
    async with ModuleHarness(Player, grants={"media": ["*"]}) as h:
        fake_world(h)
        with pytest.raises(ValueError):  # noqa: PT011
            await ctx(h).media.files(source, path)


async def test_mod_100_a_folder_that_does_not_answer_is_a_transient_error() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        h.media.list_error = TransientError("the folder did not answer")
        with pytest.raises(TransientError):
            await ctx(h).media.files("holidays")


async def test_mod_100_play_needs_media_play_and_is_reported_as_denied() -> None:
    async with ModuleHarness(Plain, grants=GRANTS) as h:
        with pytest.raises(PermissionError):
            _ = ctx(h).media
        assert "media.play" in h.denied
    async with ModuleHarness(TakeoverOnly, grants=GRANTS) as h:
        fake_world(h)
        with pytest.raises(PermissionError):
            ctx(h).media.sources()
        with pytest.raises(PermissionError):
            ctx(h).media.url("door")
        with pytest.raises(PermissionError):
            await ctx(h).media.files("holidays")
        assert h.denied.count("media.play") == 3


# --- MOD-101: takeover and stop ---------------------------------------------------------------


async def test_mod_101_takeover_returns_a_12_hex_id_and_records_the_start() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        first = await ctx(h).media.takeover("door", duration=20)
        second = await ctx(h).media.takeover("holidays", file="a.mp4")
        assert re.fullmatch(r"[0-9a-f]{12}", first)
        assert first != second
        assert [(t.id, t.source, t.file, t.duration, t.owner) for t in h.media.takeovers] == [
            (first, "door", None, 20, "player_test"),
            (second, "holidays", "a.mp4", None, "player_test"),
        ]
        assert h.media.current == second  # the newest replaces the one before


async def test_mod_101_takeover_needs_media_takeover_and_the_grant() -> None:
    async with ModuleHarness(PlayOnly, grants=GRANTS) as h:
        fake_world(h)
        with pytest.raises(PermissionError):
            await ctx(h).media.takeover("door")
        with pytest.raises(PermissionError):
            await ctx(h).media.stop()
        assert h.denied.count("media.takeover") == 2
        assert h.media.takeovers == []
    async with ModuleHarness(Player, grants={"media": ["door"]}) as h:
        fake_world(h)
        with pytest.raises(PermissionError):
            await ctx(h).media.takeover("radio")
        assert h.media.takeovers == []


@pytest.mark.parametrize("duration", [0, -5, math.nan, math.inf])
async def test_mod_101_a_duration_of_0_or_less_is_a_value_error(duration: float) -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        with pytest.raises(ValueError):  # noqa: PT011
            await ctx(h).media.takeover("door", duration=duration)
        assert h.media.takeovers == []


async def test_mod_101_a_takeover_of_an_unknown_source_or_file_is_a_value_error() -> None:
    async with ModuleHarness(Player, grants={"media": ["*"]}) as h:
        fake_world(h)
        for source, file in (("nothing", None), ("holidays", None), ("holidays", "gone.mp4")):
            with pytest.raises(ValueError):  # noqa: PT011
                await ctx(h).media.takeover(source, file=file)
        assert h.media.takeovers == []


async def test_mod_101_stop_without_an_id_stops_only_the_takeover_this_instance_started() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        # another instance, REST or MQTT started the running one
        await h.media.takeover("other", "door", file=None, duration=None, takeover_id="theirs")
        await ctx(h).media.stop()
        assert h.media.current == "theirs"
        mine = await ctx(h).media.takeover("radio")
        assert h.media.current == mine
        await ctx(h).media.stop()
        assert h.media.current is None
        assert h.media.stops == [None, None]


async def test_mod_101_stop_by_id_changes_nothing_for_another_id() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        mine = await ctx(h).media.takeover("radio")
        await ctx(h).media.stop("0123456789ab")
        assert h.media.current == mine
        await ctx(h).media.stop(mine)
        assert h.media.current is None


async def test_mod_101_stop_by_the_id_of_anothers_takeover_needs_the_grant_of_its_source() -> None:
    async with ModuleHarness(Player, grants={"media": ["door"]}) as h:
        fake_world(h)
        await h.media.takeover("other", "radio", file=None, duration=None, takeover_id="theirs")
        with pytest.raises(PermissionError):
            await ctx(h).media.stop("theirs")
        assert h.media.current == "theirs"
        await h.media.takeover("other", "door", file=None, duration=None, takeover_id="theirs2")
        await ctx(h).media.stop("theirs2")
        assert h.media.current is None


async def test_mod_101_stop_needs_a_media_grant() -> None:
    async with ModuleHarness(Player) as h:
        with pytest.raises(PermissionError):
            await ctx(h).media.stop()


async def test_mod_101_a_takeover_is_not_a_notification_the_module_must_declare() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        await ctx(h).media.takeover("door")
        assert h.violations == []
        assert h.notifications.of("MEDIA_TAKEOVER") == []


# --- MOD-103: the fake media service ----------------------------------------------------------


async def test_mod_103_the_fake_sets_sources_files_and_states_and_ends_a_takeover() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        fake_world(h)
        tid = await ctx(h).media.takeover("door")
        assert h.media.current == tid
        h.media.end()
        assert h.media.current is None
        h.media.set_state("door", "error")
        assert ctx(h).media.sources()[0].state == "error"
        with pytest.raises(ValueError):  # noqa: PT011
            h.media.add_files("door", ["a.mp4"])  # not a files source
        with pytest.raises(ValueError):  # noqa: PT011
            h.media.add_files("holidays", ["notes.txt"])


async def test_mod_103_check_contract_passes_with_media_capabilities() -> None:
    async with ModuleHarness(Player, grants=GRANTS) as h:
        await h.check_contract()


# --- the engine's media behind ctx.media ------------------------------------------------------

CONFIG = """
media:
  sources:
    door: {kind: rtsp, url: "rtsp://cam.lan/ok", audio: false}
    radio: {kind: stream, url: "https://radio.example/ok"}
    holidays: {kind: files, path: files/trip}
    nas: {kind: files, path: shares/nas/photos}
    jingle: {kind: files, path: files/jingle.mp3}
  shares:
    nas: {method: sync, remote: nas, path: media}
  takeover: {duration: 60, max_duration: 600}
"""


@dataclass
class Engineered:
    media: MediaService
    takeover: TakeoverService
    backend: EngineMediaBackend
    root: Path


@pytest.fixture
async def engineered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Engineered]:
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(tmp_path / "ffmpeg.log"))
    exe = ffmpeg_fake.install(tmp_path / "bin")
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.yaml").write_text(CONFIG)
    result = load_config(
        etc / "config.yaml", registry=Registry(), env_file=etc / "x.env", environ={}
    )
    assert result.ok, result.records
    media = MediaService(
        tmp_path / "run",
        tmp_path / "state",
        VirtualClock(),
        lambda name, payload: None,
        ffmpeg=str(exe),
        render_node=tmp_path / "none",
    )
    media.configure(result)
    for rel in (
        "files/trip/a.mp4",
        "files/trip/b.jpg",
        "files/trip/day 2/c.mp4",
        "files/jingle.mp3",
    ):
        path = media.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("12")
    takeover = TakeoverService(media, ffprobe=None)
    try:
        yield Engineered(media, takeover, EngineMediaBackend(media, takeover), media.root)
    finally:
        await takeover.close()
        await media.close()


Script = Callable[[Any], Awaitable[Any]]


class Scripted(MirrorModule):
    """Runs ``script(ctx)`` in ``start()`` and keeps what it returned or raised."""

    name = "scripted"
    capabilities: ClassVar[set[str]] = {"media.play", "media.takeover"}
    script: ClassVar[Script]
    outcome: ClassVar[list[Any]] = []

    async def start(self) -> None:
        try:
            Scripted.outcome.append(await Scripted.script(self.ctx))
        except Exception as exc:
            Scripted.outcome.append(exc)


async def run_script(
    make_engine: EngineFactory, e: Engineered, script: Script, grants: dict[str, Any] | None = None
) -> Any:
    Scripted.script = script
    Scripted.outcome = []
    engine = make_engine(Scripted)
    engine.supervisor.media = e.backend
    e.takeover.supervisor = engine.supervisor
    engine.bus.add_endpoint(e.takeover)
    spec = InstanceSpec("s", "scripted", grants=GrantSet.from_config(grants or {"media": ["*"]}))
    await engine.start([spec])
    await settle()
    assert len(Scripted.outcome) == 1
    return Scripted.outcome[0]


async def test_mod_100_the_engine_lists_its_sources_and_builds_their_urls(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    async def script(ctx: Any) -> Any:
        sources = {s.name: s for s in ctx.media.sources()}
        return (
            sources["door"],
            sources["radio"].delivery,
            sources["holidays"].kind,
            ctx.media.url("door"),
            ctx.media.url("radio"),
            ctx.media.url("holidays", file="day 2/c.mp4"),
            ctx.media.url("jingle"),
        )

    out = await run_script(make_engine, engineered, script)
    door, radio, kind, u_door, u_radio, u_file, u_jingle = out
    assert (door.kind, door.delivery, door.audio, door.state) == ("rtsp", "mjpeg", False, "idle")
    assert (radio, kind) == ("hls", "files")
    assert u_door.url == "/media/door/stream.mjpg"
    assert u_radio.url == "/media/radio/hls/index.m3u8"
    assert u_file == MediaUrl("file", "/media/files/files/trip/day%202/c.mp4")
    assert u_jingle.url == "/media/files/files/jingle.mp3"


async def test_mod_100_the_engine_looks_up_a_file_of_files_but_not_one_under_a_share(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    async def script(ctx: Any) -> Any:
        try:
            ctx.media.url("holidays", file="gone.mp4")
        except ValueError:
            missing = True
        else:
            missing = False
        # a share may be a hung network mount: ctx.media.url() never touches it
        return missing, ctx.media.url("nas", file="2026/x.jpg").url

    missing, share_url = await run_script(make_engine, engineered, script)
    assert missing is True
    assert share_url == "/media/files/shares/nas/photos/2026/x.jpg"


async def test_mod_100_the_engine_lists_a_folder_of_files(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    async def script(ctx: Any) -> Any:
        return await ctx.media.files("holidays"), await ctx.media.files("holidays", "day 2")

    top, inner = await run_script(make_engine, engineered, script)
    assert [(e.name, e.kind) for e in top] == [
        ("a.mp4", "video"),
        ("b.jpg", "image"),
        ("day 2", "folder"),
    ]
    assert top[0].size == 2
    assert [(e.path, e.kind) for e in inner] == [("day 2/c.mp4", "video")]


async def test_mod_100_the_engine_answers_a_hung_folder_with_a_transient_error(
    make_engine: EngineFactory, engineered: Engineered, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hung(fn: Any, *args: Any) -> Any:
        raise FileRefused("unavailable", "The folder did not answer in 5 s")

    monkeypatch.setattr(media_instance, "run_folder", hung)

    async def script(ctx: Any) -> Any:
        return await ctx.media.files("holidays")

    out = await run_script(make_engine, engineered, script)
    assert isinstance(out, TransientError)


async def test_mod_101_a_module_takeover_starts_on_the_engine_owned_by_the_instance(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    async def script(ctx: Any) -> Any:
        return await ctx.media.takeover("door", duration=30)

    tid = await run_script(make_engine, engineered, script)
    await settle()
    current = engineered.takeover.current
    assert current is not None
    assert (current.id, current.source, current.owner) == (tid, "door", "s")


async def test_mod_101_a_takeover_and_a_stop_in_one_handler_keep_their_order(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    async def script(ctx: Any) -> Any:
        await ctx.media.takeover("holidays", file="a.mp4")
        await ctx.media.stop()

    await run_script(make_engine, engineered, script)
    await asyncio.sleep(0)
    await settle()
    assert engineered.takeover.current is None


async def test_mod_101_stop_from_a_module_never_ends_the_takeover_of_rest(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    await engineered.takeover.start("door", owner="rest")

    async def script(ctx: Any) -> Any:
        await ctx.media.stop()

    await run_script(make_engine, engineered, script)
    await settle()
    current = engineered.takeover.current
    assert current is not None
    assert current.owner == "rest"


async def test_mod_101_the_engine_refuses_the_stop_by_id_of_a_source_outside_the_grant(
    make_engine: EngineFactory, engineered: Engineered
) -> None:
    started = await engineered.takeover.start("radio", owner="rest")

    async def script(ctx: Any) -> Any:
        await ctx.media.stop(started)

    out = await run_script(make_engine, engineered, script, grants={"media": ["door"]})
    assert isinstance(out, PermissionError)
    current = engineered.takeover.current
    assert current is not None
    assert current.id == started
