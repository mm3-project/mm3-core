"""Milestone 8, step 3: the local camera feed and camera sources (core 9.5, 9.7)."""

from __future__ import annotations

import asyncio
import grp
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import ffmpeg_fake
from mm3.core.config import ConfigResult, load_config
from mm3.core.config.diff import ReloadPlan
from mm3.core.config.media import MediaSource
from mm3.core.devices import feed_path
from mm3.core.feed import (
    MAX_FRAME_BYTES,
    FeedClosed,
    FeedError,
    FeedHeader,
    FeedReader,
    FeedServer,
    encode_frame,
)
from mm3.core.media import MediaRefused, MediaService
from mm3.core.media.command import ffmpeg_command
from mm3.core.media.service import FEED_RETRY_MAX_S, OWNER_SETTLE_S, RETRY_AFTER_S
from mm3.core.registry import Registry
from mm3.testing import VirtualClock

CAM = "/dev/v4l/by-id/usb-cam-video-index0"


def jpeg(n: int) -> bytes:
    return b"\xff\xd8" + b"frame-%d" % n + b"\xff\xd9"


@pytest.fixture
def run_dir() -> Iterator[Path]:
    """A short runtime directory: a Unix socket path has at most 107 bytes."""
    path = Path(tempfile.mkdtemp(prefix="mm3f", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def until(cond: Any, limit: float = 5.0) -> None:
    async with asyncio.timeout(limit):
        while not cond():  # noqa: ASYNC110 - set by another task or process
            await asyncio.sleep(0.01)


# --- CORE-394, CORE-395: the socket and its framing ---------------------------------------


async def test_core_395_header_line_then_length_prefixed_jpeg(run_dir: Path) -> None:
    path = feed_path(run_dir, "cam")
    server = FeedServer(path, FeedHeader("cam"), group=None)
    await server.start()
    try:
        reader = await FeedReader.connect(path, "cam")
        assert reader.header == FeedHeader("cam", 320, 240, 15, "jpeg")  # the defaults
        await until(lambda: server.readers == 1)
        server.publish(jpeg(1))
        assert await reader.frame() == jpeg(1)
        await reader.close()
    finally:
        await server.close()
    assert not path.exists()


async def test_core_395_wire_bytes(run_dir: Path) -> None:
    path = feed_path(run_dir, "cam")
    server = FeedServer(path, FeedHeader("cam", 640, 480, 30), group=None)
    await server.start()
    try:
        r, w = await asyncio.open_unix_connection(str(path))
        line = await r.readuntil(b"\n")
        assert line == b'{"device":"cam","width":640,"height":480,"fps":30,"format":"jpeg"}\n'
        await until(lambda: server.readers == 1)
        server.publish(jpeg(7))
        assert await r.readexactly(4) == len(jpeg(7)).to_bytes(4, "big")
        assert await r.readexactly(len(jpeg(7))) == jpeg(7)
        w.close()
    finally:
        await server.close()


async def test_core_395_reader_sees_the_owner_close(run_dir: Path) -> None:
    path = feed_path(run_dir, "cam")
    server = FeedServer(path, FeedHeader("cam"), group=None)
    await server.start()
    reader = await FeedReader.connect(path, "cam")
    await until(lambda: server.readers == 1)
    await server.close()
    with pytest.raises(FeedClosed):
        await reader.frame()
    await reader.close()


async def test_core_394_socket_mode_0660_with_its_group(run_dir: Path) -> None:
    path = feed_path(run_dir, "cam")
    assert path == run_dir / "feed" / "cam.sock"
    mine = grp.getgrgid(os.getgid()).gr_name
    server = FeedServer(path, FeedHeader("cam"), group=mine)
    await server.start()
    try:
        st = os.stat(path)
        assert stat.S_ISSOCK(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o660
        assert st.st_gid == os.getgid()
    finally:
        await server.close()


async def test_core_394_a_stale_socket_is_replaced_but_not_another_file(run_dir: Path) -> None:
    path = feed_path(run_dir, "cam")
    first = FeedServer(path, FeedHeader("cam"), group=None)
    await first.start()
    first._server.close()  # type: ignore[union-attr]  # the owner died: the socket stays
    second = FeedServer(path, FeedHeader("cam"), group=None)
    await second.start()
    await second.close()
    path.write_bytes(b"not a socket")
    with pytest.raises(FeedError):
        await FeedServer(path, FeedHeader("cam"), group=None).start()
    assert path.read_bytes() == b"not a socket"


@pytest.mark.parametrize(
    "fields",
    [
        {"width": 641},
        {"height": 481},
        {"fps": 31},
        {"fps": 0},
        {"width": True},
        {"format": "png"},
        {"device": "Cam!"},
    ],
)
def test_core_395_header_limits(fields: dict[str, Any]) -> None:
    with pytest.raises(FeedError):
        FeedHeader(**{"device": "cam", **fields})


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"[]\n",
        b'{"device":"cam","width":320,"height":240,"fps":15}\n',
        b'{"device":"cam","width":320,"height":240,"fps":15,"format":"jpeg","x":1}\n',
    ],
)
def test_core_395_header_must_have_the_five_keys(line: bytes) -> None:
    with pytest.raises(FeedError):
        FeedHeader.decode(line)


def test_core_395_frames_are_jpegs_of_a_bounded_size() -> None:
    with pytest.raises(FeedError):
        encode_frame(b"GIF89a")
    with pytest.raises(FeedError):
        encode_frame(b"\xff\xd8" + b"x" * MAX_FRAME_BYTES)


async def test_core_395_a_slow_reader_gets_the_newest_frame(run_dir: Path) -> None:
    path = feed_path(run_dir, "cam")
    server = FeedServer(path, FeedHeader("cam"), group=None)
    await server.start()
    try:
        reader = await FeedReader.connect(path, "cam")
        await until(lambda: server.readers == 1)
        for n in range(1, 6):  # five frames before the reader takes any
            server.publish(jpeg(n))
        assert await reader.frame() == jpeg(5)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await reader.frame()  # the older four were dropped, not queued
        await reader.close()
    finally:
        await server.close()


async def test_core_395_owner_learns_when_readers_come_and_go(run_dir: Path) -> None:
    counts: list[int] = []
    path = feed_path(run_dir, "cam")
    server = FeedServer(path, FeedHeader("cam"), on_readers=counts.append, group=None)
    await server.start()
    try:
        server.publish(jpeg(1))  # no reader: nothing is sent or kept
        a = await FeedReader.connect(path, "cam")
        b = await FeedReader.connect(path, "cam")
        await until(lambda: counts == [1, 2])
        await a.close()
        await b.close()
        await until(lambda: counts == [1, 2, 1, 0])
    finally:
        await server.close()


async def test_core_395_reader_refuses_a_feed_that_breaks_the_format(run_dir: Path) -> None:
    path = run_dir / "feed" / "cam.sock"
    with pytest.raises(FeedError):
        await FeedReader.connect(path, "cam")  # no socket
    path.parent.mkdir(parents=True)
    sends: list[bytes] = []

    async def owner(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(sends.pop(0))
        await w.drain()
        await r.read()
        w.close()

    server = await asyncio.start_unix_server(owner, path=str(path))
    try:
        sends.append(FeedHeader("other").encode())
        with pytest.raises(FeedError):
            await FeedReader.connect(path, "cam")
        sends.append(b'{"device":"cam","width":1280,"height":720,"fps":15,"format":"jpeg"}\n')
        with pytest.raises(FeedError):
            await FeedReader.connect(path, "cam")
        sends.append(FeedHeader("cam").encode() + (0).to_bytes(4, "big"))
        reader = await FeedReader.connect(path, "cam")
        with pytest.raises(FeedError):
            await reader.frame()  # a length of 0
        await reader.close()
        sends.append(FeedHeader("cam").encode() + encode_frame(jpeg(1))[:4] + b"GIF89a-xxxxx")
        reader = await FeedReader.connect(path, "cam")
        with pytest.raises(FeedError):
            await reader.frame()  # not a JPEG
        await reader.close()
    finally:
        server.close()
        await server.wait_closed()


# --- CORE-402, CORE-396: camera sources ---------------------------------------------------


def load(tmp: Path, owner: str, images: bool = True) -> ConfigResult:
    etc = tmp / "etc"
    etc.mkdir(exist_ok=True)
    flag = "true" if images else "false"
    (etc / "config.yaml").write_text(
        f"""
devices:
  cam: {{kind: camera, path: {CAM}, owner: {owner}, images: {flag}}}
media:
  sources:
    mirror: {{kind: camera, device: cam, fps: 10, width: 320}}
    a: {{kind: rtsp, url: "rtsp://cam.lan/ok", audio: false}}
    b: {{kind: rtsp, url: "rtsp://cam.lan/ok", audio: false, fps: 5}}
  limits: {{processes: 2}}
"""
    )
    result = load_config(
        etc / "config.yaml", registry=Registry(), env_file=etc / "x.env", environ={}
    )
    assert result.ok, result.records
    return result


@dataclass
class Media:
    service: MediaService
    clock: VirtualClock
    log: Path
    tmp: Path
    run: Path

    def runs(self) -> list[dict[str, Any]]:
        return ffmpeg_fake.runs(self.log)

    def state(self) -> tuple[str, str | None]:
        s = next(x for x in self.service.status()["sources"] if x["name"] == "mirror")
        return s["state"], s["reason"]

    async def reload(self, owner: str, images: bool = True) -> None:
        await self.service.reloaded(
            load(self.tmp, owner, images), ReloadPlan(media=True, devices=True)
        )


async def make(tmp: Path, run: Path, owner: str, monkeypatch: pytest.MonkeyPatch) -> Media:
    log = tmp / "ffmpeg.log"
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
    exe = ffmpeg_fake.install(tmp / "bin")
    clock = VirtualClock()
    service = MediaService(run, tmp / "state", clock, ffmpeg=str(exe))
    service.configure(load(tmp, owner))
    return Media(service, clock, log, tmp, run)


@pytest.fixture
async def vision(
    tmp_path: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Media]:
    m = await make(tmp_path, run_dir, "vision", monkeypatch)
    try:
        yield m
    finally:
        await m.service.close()


@pytest.fixture
async def owned(
    tmp_path: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Media]:
    m = await make(tmp_path, run_dir, "media", monkeypatch)
    try:
        yield m
    finally:
        await m.service.close()


class Owner:
    """A stand-in for ``mm3-vision``: offers the feed and captures while read."""

    def __init__(self, run: Path) -> None:
        self.server = FeedServer(
            feed_path(run, "cam"), FeedHeader("cam"), on_readers=self._readers, group=None
        )
        self.readers = 0
        self._task: asyncio.Task[None] | None = None

    def _readers(self, n: int) -> None:
        self.readers = n
        if n and self._task is None:
            self._task = asyncio.create_task(self._capture())
        elif not n and self._task is not None:
            self._task.cancel()
            self._task = None

    async def _capture(self) -> None:
        n = 0
        while True:
            n += 1
            self.server.publish(jpeg(n))
            await asyncio.sleep(0.02)

    async def start(self) -> Owner:
        await self.server.start()
        return self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self.server.close()


async def frames(viewer: Any, n: int, limit: float = 5.0) -> list[bytes]:
    out: list[bytes] = []
    async with asyncio.timeout(limit):
        async for frame in viewer.frames():
            out.append(frame)
            if len(out) >= n:
                break
    return out


async def test_core_402_vision_camera_relays_the_feed_without_ffmpeg(vision: Media) -> None:
    owner = await Owner(vision.run).start()
    try:
        viewer = await vision.service.open_mjpeg("mirror")
        got = await frames(viewer, 3)
        assert all(f.startswith(b"\xff\xd8") for f in got)
        assert vision.runs() == []  # no ffmpeg
        assert vision.service.status()["children"] == []  # a relay is not a child
        assert vision.state() == ("running", None)
        viewer.close()
    finally:
        await owner.stop()


async def test_core_402_a_relay_does_not_count_against_the_processes(vision: Media) -> None:
    owner = await Owner(vision.run).start()
    try:
        await vision.service.open_mjpeg("a")
        await vision.service.open_mjpeg("b")  # limits.processes: 2, both used
        assert len(vision.service.status()["children"]) == 2
        viewer = await vision.service.open_mjpeg("mirror")
        assert await frames(viewer, 1)
    finally:
        await owner.stop()


async def test_core_402_missing_feed_is_idle_feed_503_and_never_error(vision: Media) -> None:
    for _ in range(8):  # more than the 5 failures that make a child's source an error
        with pytest.raises(MediaRefused) as refused:
            await vision.service.open_mjpeg("mirror")
        assert (refused.value.status, refused.value.reason) == (503, "feed")
        assert refused.value.retry_after == RETRY_AFTER_S
    assert vision.state() == ("idle", "feed")
    owner = await Owner(vision.run).start()  # Vision comes back: no reload needed
    try:
        viewer = await vision.service.open_mjpeg("mirror")
        assert await frames(viewer, 1)
        assert vision.state() == ("running", None)
    finally:
        await owner.stop()


async def test_core_402_lost_feed_keeps_viewers_and_connects_again(vision: Media) -> None:
    owner = await Owner(vision.run).start()
    viewer = await vision.service.open_mjpeg("mirror")
    assert await frames(viewer, 1)
    await owner.stop()  # Vision restarts
    await until(lambda: vision.state() == ("idle", "feed"))
    with pytest.raises(MediaRefused) as refused:
        await vision.service.open_mjpeg("mirror")  # a new viewer waits for the feed
    assert (refused.value.status, refused.value.retry_after) == (503, RETRY_AFTER_S)
    assert vision.service.sources["mirror"].viewers == 1  # the open viewer stays
    for _ in range(3):  # the relay keeps trying, at most every 5 s
        await vision.clock.advance(FEED_RETRY_MAX_S)
    owner = await Owner(vision.run).start()
    try:
        await vision.clock.advance(FEED_RETRY_MAX_S)
        await until(lambda: vision.state() == ("running", None))
        assert await frames(viewer, 2)  # the same viewer gets frames again
        assert vision.state()[0] != "error"
    finally:
        await owner.stop()


async def test_core_402_relay_leaves_the_feed_30s_after_the_last_viewer(vision: Media) -> None:
    owner = await Owner(vision.run).start()
    try:
        viewer = await vision.service.open_mjpeg("mirror")
        assert await frames(viewer, 1)
        await until(lambda: owner.readers == 1)
        viewer.close()
        await vision.clock.advance(29)
        assert owner.readers == 1
        await vision.clock.advance(2)
        await until(lambda: owner.readers == 0)  # CORE-395: the owner stops sending
        assert vision.state() == ("idle", None)
    finally:
        await owner.stop()


async def test_core_402_images_off_sends_nothing_even_from_the_feed(vision: Media) -> None:
    await vision.reload("vision", images=False)
    owner = await Owner(vision.run).start()
    try:
        with pytest.raises(MediaRefused) as refused:
            await vision.service.open_mjpeg("mirror")
        assert (refused.value.status, refused.value.reason) == (403, "images")
        assert owner.readers == 0  # CORE-392: the feed is not even opened
    finally:
        await owner.stop()


def test_core_402_media_camera_reads_the_v4l2_device() -> None:
    s = MediaSource.model_validate({"kind": "camera", "device": "cam", "fps": 10, "width": 320})
    argv = ffmpeg_command("ffmpeg", s, "mjpeg", device_path=CAM)
    assert argv[argv.index("-f") + 1] == "v4l2"
    assert argv[argv.index("-i") + 1] == CAM
    assert argv[argv.index("-vf") + 1] == "fps=10,scale=320:-2"
    assert argv[-3:] == ["-f", "image2pipe", "pipe:1"]
    assert "-protocol_whitelist" not in argv
    with pytest.raises(ValueError, match="device"):
        ffmpeg_command("ffmpeg", s, "mjpeg")


async def test_core_402_media_owned_camera_runs_an_ffmpeg_child(owned: Media) -> None:
    viewer = await owned.service.open_mjpeg("mirror")
    assert await frames(viewer, 1)
    (run,) = owned.runs()
    assert run["argv"][run["argv"].index("-i") + 1] == CAM
    assert [c["source"] for c in owned.service.status()["children"]] == ["mirror"]
    viewer.close()


async def test_core_402_owner_none_is_unavailable(owned: Media) -> None:
    await owned.reload("none")
    assert owned.state() == ("unavailable", "owner")
    with pytest.raises(MediaRefused) as refused:
        await owned.service.open_mjpeg("mirror")
    assert refused.value.status == 503
    assert owned.runs() == []


async def test_core_396_change_to_vision_releases_the_device_within_5s(owned: Media) -> None:
    viewer = await owned.service.open_mjpeg("mirror")
    assert await frames(viewer, 1)
    pid = owned.service.status()["children"][0]["pid"]
    loop = asyncio.get_running_loop()
    started = loop.time()
    await owned.reload("vision")
    assert loop.time() - started < 5.0
    assert owned.service.status()["children"] == []
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the child is gone: the device is free for Vision
    assert await frames(viewer, 1) == []  # its viewer ends; the player opens it again
    owner = await Owner(owned.run).start()
    try:
        again = await owned.service.open_mjpeg("mirror")
        assert await frames(again, 1)
        assert len(owned.runs()) == 1  # relayed, not opened again
    finally:
        await owner.stop()


async def test_core_396_change_to_media_opens_the_device_5s_after_the_reload(
    vision: Media,
) -> None:
    owner = await Owner(vision.run).start()
    viewer = await vision.service.open_mjpeg("mirror")
    assert await frames(viewer, 1)
    await vision.reload("media")
    await until(lambda: owner.readers == 0)  # the relay left the feed
    await owner.stop()  # Vision lets the device go
    opening = asyncio.create_task(vision.service.open_mjpeg("mirror"))
    await vision.clock.advance(OWNER_SETTLE_S - 0.5)
    assert not opening.done()
    assert vision.runs() == []  # not opened before the old owner had 5 s
    await vision.clock.advance(1.0)
    again = await asyncio.wait_for(opening, 5)
    assert await frames(again, 1)
    (run,) = vision.runs()
    assert run["argv"][run["argv"].index("-i") + 1] == CAM
