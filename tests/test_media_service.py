"""Milestone 8, step 2: the media service, its children and the delivery routes (core 9.7)."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import ffmpeg_fake
from mm3.cli.diag_cmd import ffmpeg_check
from mm3.core.config import ConfigResult, load_config
from mm3.core.config.diff import ReloadPlan
from mm3.core.config.media import MediaSource
from mm3.core.diag.secrets import MASK, Masker, config_secrets, url_credentials
from mm3.core.logging import configure_logging, recent_records
from mm3.core.media import MediaRefused, MediaService
from mm3.core.media.command import (
    CA_FILE,
    Codecs,
    delivery,
    ffmpeg_command,
    mask_url_credentials,
    parse_codecs,
    unplayable,
)
from mm3.core.media.folder import list_files, open_file, split_media_path
from mm3.core.media.service import (
    FAILURES_FOR_ERROR,
    HLS_LEASE_S,
    IDLE_STOP_S,
    RETRY_AFTER_S,
)
from mm3.core.registry import Registry
from mm3.core.web.apps import admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.testing import VirtualClock

MIRROR = "http://127.0.0.1:8080"
LOOPBACK = ("127.0.0.1", 50000)


def src(**kw: Any) -> MediaSource:
    return MediaSource.model_validate(kw)


# --- CORE-400: delivery --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"kind": "rtsp", "url": "rtsp://cam/x", "audio": False}, "mjpeg"),
        ({"kind": "rtsp", "url": "rtsp://cam/x"}, "hls"),
        ({"kind": "stream", "url": "https://radio/live.m3u8"}, "hls"),
        ({"kind": "stream", "url": "https://radio/x", "audio": False}, "hls"),
        ({"kind": "rtsp", "url": "rtsp://cam/x", "delivery": "hls", "audio": False}, "hls"),
        ({"kind": "stream", "url": "https://x/y", "delivery": "mjpeg"}, "mjpeg"),
        ({"kind": "camera", "device": "cam"}, "mjpeg"),
        ({"kind": "camera", "device": "cam", "delivery": "hls"}, "mjpeg"),
        ({"kind": "files", "path": "files/a"}, "file"),
    ],
)
def test_core_400_auto_delivery(fields: dict[str, Any], expected: str) -> None:
    assert delivery(src(**fields)) == expected


# --- CORE-401, CORE-403: command lines --------------------------------------------------------


def test_core_400_mjpeg_command_writes_jpeg_frames(tmp_path: Path) -> None:
    s = src(kind="rtsp", url="rtsp://cam.lan/live", audio=False, fps=10, width=640)
    argv = ffmpeg_command("/usr/bin/ffmpeg", s, "mjpeg")
    assert argv[0] == "/usr/bin/ffmpeg"
    assert argv[argv.index("-vf") + 1] == "fps=10,scale=640:-2"
    assert argv[-3:] == ["-f", "image2pipe", "pipe:1"]
    assert "-an" in argv
    assert argv[argv.index("-c:v") + 1] == "mjpeg"
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"


def test_core_400_hls_command_2s_fmp4_list_of_6_in_the_runtime_folder(tmp_path: Path) -> None:
    out = tmp_path / "run" / "media" / "radio"
    argv = ffmpeg_command("ffmpeg", src(kind="stream", url="https://r.example/l.m3u8"), "hls", out)
    opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("-hls")}
    assert opt["-hls_time"] == "2"
    assert opt["-hls_list_size"] == "6"
    assert opt["-hls_segment_type"] == "fmp4"
    assert opt["-hls_segment_filename"] == str(out / "seg%d.m4s")
    assert "delete_segments" in opt["-hls_flags"]
    assert argv[-1] == str(out / "index.m3u8")


def test_core_401_copy_by_default(tmp_path: Path) -> None:
    argv = ffmpeg_command("ffmpeg", src(kind="rtsp", url="rtsp://c/x"), "hls", tmp_path)
    assert argv[argv.index("-c") + 1] == "copy"
    assert "-c:v" not in argv
    assert argv.count("-map") == 2  # video and audio
    silent = ffmpeg_command(
        "ffmpeg", src(kind="stream", url="http://c/x", audio=False), "hls", tmp_path
    )
    assert "-an" in silent
    assert silent.count("-map") == 1


def test_core_401_transcode_vaapi_or_libx264_veryfast_and_aac(tmp_path: Path) -> None:
    s = src(kind="rtsp", url="rtsp://c/x", transcode=True, width=1280)
    node = Path("/dev/dri/renderD128")
    va = ffmpeg_command("ffmpeg", s, "hls", tmp_path, encoder="vaapi", render_node=node)
    assert va[va.index("-vaapi_device") + 1] == str(node)
    assert va.index("-vaapi_device") < va.index("-i")
    assert va[va.index("-c:v") + 1] == "h264_vaapi"
    assert va[va.index("-vf") + 1] == "format=nv12,hwupload,scale_vaapi=w=1280:h=-2"
    assert va[va.index("-c:a") + 1] == "aac"
    sw = ffmpeg_command("ffmpeg", s, "hls", tmp_path, encoder="x264")
    assert sw[sw.index("-c:v") + 1] == "libx264"
    assert sw[sw.index("-preset") + 1] == "veryfast"
    assert sw[sw.index("-vf") + 1] == "scale=1280:-2"
    assert "-vaapi_device" not in sw
    assert "-force_key_frames" in sw  # segments of 2 s


@pytest.mark.parametrize(
    ("url", "whitelist", "tls"),
    [
        ("https://r.example/live.m3u8", "https,tls,tcp,crypto", True),
        ("http://r.example/live.mp3", "http,tcp,crypto", False),
        ("rtsps://cam.lan/s", "rtsps,rtsp,rtp,srtp,udp,tcp,tls", True),
        ("rtsp://cam.lan/s", "rtsp,rtp,udp,tcp", False),
    ],
)
def test_core_403_tls_verified_and_no_other_scheme(url: str, whitelist: str, tls: bool) -> None:
    kind = "rtsp" if url.startswith("rtsp") else "stream"
    argv = ffmpeg_command("ffmpeg", src(kind=kind, url=url), "mjpeg")
    before = argv[: argv.index("-i")]
    assert before[before.index("-protocol_whitelist") + 1] == whitelist
    if tls:
        assert before[before.index("-tls_verify") + 1] == "1"
        assert before[before.index("-ca_file") + 1] == CA_FILE
    else:
        assert "-tls_verify" not in before
    # A plain scheme never reaches an encrypted one or a local file, and the other way round.
    assert "file" not in whitelist.split(",")
    if url.startswith("https"):
        assert "http" not in whitelist.split(",")
    if url.startswith("http:"):
        assert "https" not in whitelist.split(",")


# --- CORE-401: codecs --------------------------------------------------------------------------

STDERR = """ffmpeg version 7.1
Input #0, rtsp, from 'rtsp://cam/x':
  Stream #0:0: Video: hevc (Main), yuvj420p(pc), 2560x1440, 25 fps
  Stream #0:1: Audio: pcm_alaw, 8000 Hz, mono
Output #0, hls, to 'index.m3u8':
  Stream #0:0: Video: hevc (Main)
""".splitlines()


def test_core_401_input_codecs_are_read_until_the_output() -> None:
    assert parse_codecs(STDERR) == Codecs("hevc", "pcm_alaw", complete=True)
    assert parse_codecs(STDERR[:3]) == Codecs("hevc", None, complete=False)
    radio = ["Input #0, mp3, from 'x':", "  Stream #0:0: Audio: mp3, 44100 Hz", "Output #0, x"]
    assert parse_codecs(radio) == Codecs(None, "mp3", complete=True)


def test_core_401_browser_playable_is_h264_with_aac_mp3_or_opus() -> None:
    assert not unplayable(Codecs("h264", "aac"), audio=True)
    assert not unplayable(Codecs(None, "opus"), audio=True)
    assert not unplayable(Codecs("h264", "mp3"), audio=True)
    assert unplayable(Codecs("hevc", "aac"), audio=True)
    assert unplayable(Codecs("h264", "pcm_alaw"), audio=True)
    assert not unplayable(Codecs("h264", "pcm_alaw"), audio=False)  # the sound is dropped


# --- CORE-397, CORE-403: credentials -------------------------------------------------------


def test_core_403_url_credentials_are_masked_in_lines() -> None:
    line = "[rtsp @ 0x1] rtsp://admin:s3cret@cam.lan:554/live: 401 Unauthorized"
    assert (
        mask_url_credentials(line)
        == f"[rtsp @ 0x1] rtsp://{MASK}@cam.lan:554/live: 401 Unauthorized"
    )
    assert (
        mask_url_credentials("https://host/x and rtsps://u@h/y")
        == f"https://host/x and rtsps://{MASK}@h/y"
    )


def test_core_403_url_credentials_are_secrets_of_the_configuration(tmp_path: Path) -> None:
    assert url_credentials(["rtsp://admin:s3cret@cam/x", None, "https://h/x", "http://u@h"]) == {
        "s3cret",
        "admin:s3cret",
    }
    result = load(
        tmp_path,
        "media:\n  sources:\n    door: {kind: rtsp, url: 'rtsp://admin:s3cret@cam.lan/x'}\n",
    )
    secrets = config_secrets(result, environ={})
    assert {"s3cret", "admin:s3cret"} <= secrets
    assert Masker(secrets).mask_text("user admin:s3cret") == f"user {MASK}"


# --- CORE-404: the media folder ------------------------------------------------------------


def test_core_404_paths_start_with_files_or_shares_name() -> None:
    assert split_media_path("files/a.jpg") == ["files", "a.jpg"]
    assert split_media_path("shares/nas/x/a.mp4") == ["shares", "nas", "x", "a.mp4"]
    for bad in ("/files/a", "files/../x", "shares/", "other/a", "files//a", "files\\a", ""):
        assert split_media_path(bad) is None, bad


def test_core_404_only_listed_extensions_and_never_through_a_link(tmp_path: Path) -> None:
    root = tmp_path / "media"
    (root / "files" / "trip" / "day2").mkdir(parents=True)
    (root / "files" / "trip" / "b.jpg").write_bytes(b"b")
    (root / "files" / "trip" / "A.PNG").write_bytes(b"a")
    (root / "files" / "trip" / "notes.txt").write_text("x")
    (root / "files" / "trip" / "day2" / "c.mp4").write_bytes(b"c")
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    (root / "files" / "trip" / "link.jpg").symlink_to(outside)
    (root / "files" / "linked").symlink_to(root / "files" / "trip")
    assert list_files(root, "files/trip") == [
        "files/trip/A.PNG",
        "files/trip/b.jpg",
        "files/trip/day2/c.mp4",
    ]
    assert list_files(root, "files/trip/b.jpg") == ["files/trip/b.jpg"]
    assert list_files(root, "files/linked") == []
    assert list_files(root, "files/missing") == []
    one = open_file(root, "files/trip/day2/c.mp4")
    assert one is not None
    assert one.content_type == "video/mp4"
    assert one.size == 1
    os.close(one.fd)
    assert open_file(root, "files/trip/link.jpg") is None
    assert open_file(root, "files/linked/b.jpg") is None
    assert open_file(root, "files/trip/notes.txt") is None
    assert open_file(root, "files/trip/day2") is None


# --- the service with a fake ffmpeg -----------------------------------------------------------


def load(tmp_path: Path, text: str) -> ConfigResult:
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "config.yaml").write_text(text)
    result = load_config(
        etc / "config.yaml", registry=Registry(), env_file=etc / "x.env", environ={}
    )
    assert result.ok, result.records
    return result


CONFIG = """
devices:
  cam: {kind: camera, path: /dev/v4l/by-id/usb-cam-video-index0}
media:
  sources:
    door: {kind: rtsp, url: "rtsp://admin:s3cret@cam.lan/ok", audio: false}
    door2: {kind: rtsp, url: "rtsp://cam.lan/ok", audio: false}
    broken: {kind: rtsp, url: "rtsp://cam.lan/fail", audio: false}
    ending: {kind: rtsp, url: "rtsp://cam.lan/end", audio: false}
    radio: {kind: stream, url: "https://radio.example/ok"}
    hevc: {kind: rtsp, url: "rtsp://cam.lan/hevc"}
    hevc_tc: {kind: rtsp, url: "rtsp://cam.lan/hevc", transcode: true}
    tc2: {kind: rtsp, url: "rtsp://cam.lan/ok", transcode: true}
    mirror: {kind: camera, device: cam}
    adts: {kind: rtsp, url: "rtsp://cam.lan/adts"}
    holidays: {kind: files, path: files/trip}
  limits: {processes: 3, transcode: 1}
"""


@dataclass
class Media:
    service: MediaService
    clock: VirtualClock
    log: Path
    events: list[dict[str, Any]]
    tmp: Path

    def runs(self) -> list[dict[str, Any]]:
        return ffmpeg_fake.runs(self.log)


@pytest.fixture
async def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Media]:
    log = tmp_path / "ffmpeg.log"
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
    exe = ffmpeg_fake.install(tmp_path / "bin")
    clock = VirtualClock()
    events: list[dict[str, Any]] = []
    service = MediaService(
        tmp_path / "run",
        tmp_path / "state",
        clock,
        lambda name, payload: events.append({"name": name, **payload}),
        ffmpeg=str(exe),
        render_node=tmp_path / "no-render-node",
        playlist_wait=5.0,
    )
    service.configure(load(tmp_path, CONFIG))
    try:
        yield Media(service, clock, log, events, tmp_path)
    finally:
        await service.close()


async def frames(viewer: Any, n: int) -> list[bytes]:
    out: list[bytes] = []
    async with asyncio.timeout(5):
        async for frame in viewer.frames():
            out.append(frame)
            if len(out) >= n:
                break
    return out


async def until(cond: Any, limit: float = 5.0) -> None:
    """Poll a condition that a real child process makes true."""
    async with asyncio.timeout(limit):
        while not cond():  # noqa: ASYNC110 - the condition is set by another process
            await asyncio.sleep(0.02)


def state(m: Media, name: str) -> dict[str, Any]:
    return next(s for s in m.service.status()["sources"] if s["name"] == name)


async def test_core_397_child_starts_on_first_viewer_and_stops_30s_after_the_last(
    media: Media,
) -> None:
    assert media.runs() == []
    assert state(media, "door")["state"] == "idle"
    a = await media.service.open_mjpeg("door")
    b = await media.service.open_mjpeg("door")
    got = await frames(a, 2)
    assert all(f.startswith(b"\xff\xd8") and f.endswith(b"\xff\xd9") for f in got)
    await frames(b, 1)
    assert len(media.runs()) == 1  # one child for both viewers
    assert state(media, "door") | {} == {
        "name": "door",
        "state": "running",
        "viewers": 2,
        "reason": None,
    }
    child = media.service.status()["children"][0]
    assert child["source"] == "door"
    assert child["transcode"] is False
    a.close()
    b.close()
    await media.clock.advance(IDLE_STOP_S - 2)
    assert media.service.status()["children"]
    await media.clock.advance(3)
    await until(lambda: not media.service.status()["children"])
    assert state(media, "door")["state"] == "idle"


async def test_core_397_children_run_with_nice_10(media: Media) -> None:
    viewer = await media.service.open_mjpeg("door")
    await frames(viewer, 1)
    viewer.close()
    assert media.runs()[0]["nice"] == os.nice(0) + 10


async def test_core_397_limit_of_processes_answers_503_retry_after_5(media: Media) -> None:
    viewers = [await media.service.open_mjpeg(n) for n in ("door", "door2", "ending")]
    with pytest.raises(MediaRefused) as refused:
        await media.service.hls_file("radio", "10.0.0.2", "index.m3u8")
    assert (refused.value.status, refused.value.retry_after) == (503, RETRY_AFTER_S)
    assert RETRY_AFTER_S == 5
    for v in viewers:
        v.close()


async def test_core_401_at_most_limits_transcode_transcoding_children(media: Media) -> None:
    await media.service.hls_file("hevc_tc", "10.0.0.2", "index.m3u8")
    with pytest.raises(MediaRefused) as refused:
        await media.service.hls_file("tc2", "10.0.0.2", "index.m3u8")
    assert (refused.value.status, refused.value.reason) == (503, "transcode_limit")
    assert refused.value.retry_after == 5
    assert media.service.status()["children"][0]["transcode"] is True


async def test_core_401_transcode_falls_back_to_libx264_without_a_render_node(media: Media) -> None:
    await media.service.hls_file("hevc_tc", "10.0.0.2", "index.m3u8")
    argv = media.runs()[0]["argv"]
    assert "libx264" in argv
    assert "h264_vaapi" not in argv


async def test_core_401_transcode_uses_vaapi_on_the_render_node(media: Media) -> None:
    node = media.tmp / "renderD128"
    node.write_bytes(b"")
    media.service.render_node = node
    await media.service.hls_file("hevc_tc", "10.0.0.2", "index.m3u8")
    argv = media.runs()[0]["argv"]
    assert "h264_vaapi" in argv
    assert argv[argv.index("-vaapi_device") + 1] == str(node)


async def test_core_401_unplayable_copy_source_is_error_codec(media: Media) -> None:
    with pytest.raises(MediaRefused) as refused:
        await media.service.hls_file("hevc", "10.0.0.2", "index.m3u8")
    assert refused.value.status in (503, 404)
    await until(lambda: state(media, "hevc")["state"] == "error")
    assert state(media, "hevc")["reason"] == "codec"
    assert len(media.runs()) == 1  # not restarted
    with pytest.raises(MediaRefused) as again:
        await media.service.hls_file("hevc", "10.0.0.2", "index.m3u8")
    assert (again.value.status, again.value.reason) == (503, "error")


async def test_core_401_adts_aac_is_copied_through_the_filter_without_a_failure(
    media: Media,
) -> None:
    playlist = await media.service.hls_file("adts", "10.0.0.2", "index.m3u8")
    assert playlist.exists()
    first, second = (r["argv"] for r in media.runs())
    assert "-bsf:a" not in first
    assert second[second.index("-bsf:a") + 1] == "aac_adtstoasc"
    assert not media.service.sources["adts"].failures
    assert state(media, "adts")["state"] == "running"


async def test_core_400_hls_files_from_the_runtime_folder_and_removed_after(media: Media) -> None:
    playlist = await media.service.hls_file("radio", "10.0.0.2", "index.m3u8")
    folder = media.tmp / "run" / "media" / "radio"
    assert playlist == folder / "index.m3u8"
    assert "#EXTM3U" in playlist.read_text()
    init = await media.service.hls_file("radio", "10.0.0.2", "init.mp4")
    assert init.read_bytes() == b"init"
    with pytest.raises(MediaRefused) as bad:
        await media.service.hls_file("radio", "10.0.0.2", "../x")
    assert bad.value.status == 404
    assert state(media, "radio")["viewers"] == 1
    await media.clock.advance(HLS_LEASE_S + 1)
    assert state(media, "radio")["viewers"] == 0
    assert folder.exists()
    await media.clock.advance(IDLE_STOP_S + 1)
    await until(lambda: not folder.exists())
    assert not media.service.status()["children"]


async def test_core_400_hls_lease_renewed_by_every_request(media: Media) -> None:
    await media.service.hls_file("radio", "10.0.0.2", "index.m3u8")
    for _ in range(5):
        await media.clock.advance(HLS_LEASE_S - 2)
        await media.service.hls_file("radio", "10.0.0.2", "index.m3u8")
    await media.clock.advance(IDLE_STOP_S)
    assert media.service.status()["children"]  # still watched


async def test_core_397_failures_back_off_and_become_error_until_reload(
    media: Media, tmp_path: Path
) -> None:
    viewer = await media.service.open_mjpeg("broken")
    for n in range(1, FAILURES_FOR_ERROR):
        await until(lambda n=n: len(media.runs()) == n and media.service.sources["broken"].restart)
        await media.clock.advance(min(60, 2 ** (n - 1)))
    await until(lambda: state(media, "broken")["state"] == "error")
    assert len(media.runs()) == FAILURES_FOR_ERROR
    assert state(media, "broken")["reason"] == "failed"
    assert await frames(viewer, 1) == []  # the viewer ends
    with pytest.raises(MediaRefused):
        await media.service.open_mjpeg("broken")
    await media.service.reloaded(load(tmp_path, CONFIG), ReloadPlan())
    assert state(media, "broken")["state"] == "idle"


async def test_core_397_backoff_doubles(media: Media) -> None:
    await media.service.open_mjpeg("broken")
    await until(lambda: media.service.sources["broken"].restart is not None)
    await media.clock.advance(0.9)
    assert len(media.runs()) == 1
    await media.clock.advance(0.2)
    await until(lambda: len(media.runs()) == 2 and media.service.sources["broken"].restart)
    await media.clock.advance(1.5)
    assert len(media.runs()) == 2
    await media.clock.advance(0.6)
    await until(lambda: len(media.runs()) == 3)


async def test_core_397_stream_that_ends_ends_its_viewers(media: Media) -> None:
    viewer = await media.service.open_mjpeg("ending")
    got = await frames(viewer, 10)
    assert 1 <= len(got) <= 3
    assert state(media, "ending")["state"] == "idle"
    assert len(media.runs()) == 1


async def test_core_397_stderr_to_debug_log_with_credentials_masked(media: Media) -> None:
    configure_logging("debug")
    viewer = await media.service.open_mjpeg("door")
    await frames(viewer, 1)
    viewer.close()
    lines = [r for r in recent_records() if r.get("event") == "media_ffmpeg"]
    assert lines
    assert all(r["level"] == "debug" for r in lines)
    text = " ".join(str(r["line"]) for r in lines)
    assert "s3cret" not in text
    assert f"rtsp://{MASK}@cam.lan/ok" in text
    configure_logging("info")


async def test_core_400_camera_follows_images(media: Media, tmp_path: Path) -> None:
    with pytest.raises(MediaRefused) as refused:
        await media.service.open_mjpeg("mirror")
    assert (refused.value.status, refused.value.reason) == (403, "images")
    assert media.runs() == []  # CORE-392: nothing is opened while images are off
    on = CONFIG.replace("usb-cam-video-index0}", "usb-cam-video-index0, images: true}")
    await media.service.reloaded(load(tmp_path, on), ReloadPlan(media=True, devices=True))
    viewer = await media.service.open_mjpeg("mirror")  # owned by media: no Vision instance
    assert (await frames(viewer, 1))[0].startswith(b"\xff\xd8")
    viewer.close()


async def test_core_398_reload_stops_changed_and_removed_sources(
    media: Media, tmp_path: Path
) -> None:
    door = await media.service.open_mjpeg("door")
    radio = await media.service.open_mjpeg("door2")
    await frames(door, 1)
    await frames(radio, 1)
    changed = (
        CONFIG.replace("fps", "fps")
        .replace(
            'door2: {kind: rtsp, url: "rtsp://cam.lan/ok", audio: false}',
            'door2: {kind: rtsp, url: "rtsp://cam.lan/ok", audio: false, fps: 5}',
        )
        .replace('    ending: {kind: rtsp, url: "rtsp://cam.lan/end", audio: false}\n', "")
    )
    await media.service.reloaded(load(tmp_path, changed), ReloadPlan(media=True))
    assert await frames(radio, 5) == []  # door2 changed: its viewer ends
    assert "ending" not in media.service.sources
    assert [c["source"] for c in media.service.status()["children"]] == ["door"]
    assert (await frames(door, 1))[0].startswith(b"\xff\xd8")


async def test_core_397_without_ffmpeg_sources_are_unavailable(tmp_path: Path) -> None:
    service = MediaService(tmp_path / "run", tmp_path / "state", VirtualClock(), ffmpeg=None)
    service.ffmpeg = None
    service.configure(load(tmp_path, CONFIG))
    assert {s["name"]: (s["state"], s["reason"]) for s in service.status()["sources"]}["door"] == (
        "unavailable",
        "ffmpeg",
    )
    with pytest.raises(MediaRefused) as refused:
        await service.open_mjpeg("door")
    assert refused.value.status == 503
    assert (tmp_path / "state" / "media" / "files").is_dir()  # CORE-404
    await service.close()


async def test_core_412_state_event_on_change_at_most_once_a_second(media: Media) -> None:
    media.events.clear()
    viewer = await media.service.open_mjpeg("door")
    await frames(viewer, 1)
    first = len(media.events)
    assert first == 1
    assert media.events[0]["name"] == "CORE_MEDIA_CHANGED"
    assert media.events[0]["takeover"] is None
    door = next(s for s in media.events[0]["sources"] if s["name"] == "door")
    assert door == {"name": "door", "state": "running", "viewers": 1}
    viewer.close()  # a change inside the same second waits
    await asyncio.sleep(0.05)
    assert len(media.events) == first
    await media.clock.advance(1.0)
    assert len(media.events) == first + 1
    last = next(s for s in media.events[-1]["sources"] if s["name"] == "door")
    assert last["viewers"] == 0
    await media.clock.advance(5.0)
    assert len(media.events) == first + 1  # nothing changed


def test_core_412_payload_matches_the_registry() -> None:
    from mm3.notifications import validate_payload

    validate_payload(
        "CORE_MEDIA_CHANGED",
        {"takeover": None, "sources": [{"name": "a", "state": "idle", "viewers": 0}]},
    )


# --- CORE-400: routes ---------------------------------------------------------------------------


@dataclass
class Routes:
    mirror: httpx.AsyncClient
    admin: httpx.AsyncClient
    media: Media
    token: str


@pytest.fixture
async def routes(media: Media) -> AsyncIterator[Routes]:
    auth = WebAuth.open(media.tmp / "web")
    token = auth.tokens.new("t")
    mirror = protect(mirror_app(auth, media=media.service), lambda: [], "mirror")
    admin = protect(admin_app(auth, media=media.service), lambda: [], "admin")
    files = media.tmp / "state" / "media" / "files" / "trip"
    files.mkdir(parents=True)
    (files / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    (files / "x.txt").write_text("no")
    t1 = httpx.ASGITransport(app=mirror, client=LOOPBACK)
    t2 = httpx.ASGITransport(app=admin, client=LOOPBACK)
    async with (
        httpx.AsyncClient(transport=t1, base_url=MIRROR) as m,
        httpx.AsyncClient(transport=t2, base_url="http://127.0.0.1:8081") as a,
    ):
        yield Routes(m, a, media, token)


async def test_core_400_files_route_with_ranges_and_content_type(routes: Routes) -> None:
    r = await routes.mirror.get("/media/files/files/trip/clip.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert len(r.content) == 1024
    part = await routes.mirror.get(
        "/media/files/files/trip/clip.mp4", headers={"range": "bytes=10-19"}
    )
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 10-19/1024"
    assert part.content == bytes(range(10, 20))
    tail = await routes.mirror.get(
        "/media/files/files/trip/clip.mp4", headers={"range": "bytes=-4"}
    )
    assert tail.content == bytes(range(252, 256))
    bad = await routes.mirror.get(
        "/media/files/files/trip/clip.mp4", headers={"range": "bytes=5000-"}
    )
    assert bad.status_code == 416
    assert bad.headers["content-range"] == "bytes */1024"
    assert (await routes.mirror.get("/media/files/files/trip/x.txt")).status_code == 404
    assert (await routes.mirror.get("/media/files/files/../etc/passwd")).status_code == 404
    # The CSP of CORE-63 is unchanged on media routes.
    assert "media-src 'self'" in r.headers["content-security-policy"]


async def test_core_400_mjpeg_route_is_multipart_jpeg(routes: Routes) -> None:
    # httpx's ASGI transport reads a whole body, so this uses a stream that ends.
    async with asyncio.timeout(10):
        r = await routes.mirror.get("/media/ending/stream.mjpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "multipart/x-mixed-replace; boundary=mm3frame"
    parts = [p for p in r.content.split(b"--mm3frame\r\n") if p]
    assert parts
    for part in parts:
        head, _, body = part.partition(b"\r\n\r\n")
        assert b"Content-Type: image/jpeg" in head
        length = int(head.split(b"Content-Length: ")[1])
        assert body[:length].startswith(b"\xff\xd8")
        assert body[:length].endswith(b"\xff\xd9")
    assert state(routes.media, "ending")["viewers"] == 0


async def test_core_400_hls_route_and_refusals(routes: Routes) -> None:
    r = await routes.mirror.get("/media/radio/hls/index.m3u8")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    seg = await routes.mirror.get("/media/radio/hls/init.mp4")
    assert seg.status_code == 200
    assert seg.headers["content-type"] == "video/mp4"
    assert (await routes.mirror.get("/media/nope/hls/index.m3u8")).status_code == 404
    assert (await routes.mirror.get("/media/door/hls/index.m3u8")).status_code == 404  # MJPEG
    assert (await routes.mirror.get("/media/mirror/stream.mjpg")).status_code == 403
    for name in ("door", "door2"):
        routes.media.service.sources[name].mjpeg_viewers = 0
    await routes.media.service.open_mjpeg("door")
    await routes.media.service.open_mjpeg("door2")
    full = await routes.mirror.get("/media/ending/stream.mjpg")
    assert full.status_code == 503
    assert full.headers["retry-after"] == "5"


async def test_core_400_admin_preview_routes_need_a_session(routes: Routes) -> None:
    path = "/admin/preview/media/files/files/trip/clip.mp4"
    assert (await routes.admin.get(path)).status_code == 401
    ok = await routes.admin.get(path, headers={"authorization": f"Bearer {routes.token}"})
    assert ok.status_code == 200
    assert len(ok.content) == 1024
    assert (
        await routes.mirror.get("/admin/preview/media/files/files/trip/clip.mp4")
    ).status_code == 404


# --- CORE-397: mm3 diag check -------------------------------------------------------------------


def test_core_397_diag_check_fails_without_ffmpeg_when_sources_exist(tmp_path: Path) -> None:
    with_sources = load(tmp_path, CONFIG)
    assert ffmpeg_check(with_sources, lambda _: None).outcome == "fail"
    assert ffmpeg_check(with_sources, lambda _: "/usr/bin/ffmpeg").outcome == "pass"
    assert ffmpeg_check(load(tmp_path, "engine: {}\n"), lambda _: None).outcome == "skip"
    assert ffmpeg_check(None, lambda _: None).outcome == "skip"


# --- with the real ffmpeg ------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@needs_ffmpeg
async def test_core_401_real_ffmpeg_remuxes_h264_aac_to_hls_and_mjpeg(tmp_path: Path) -> None:
    import functools
    import http.server
    import subprocess
    import threading
    import time

    www = tmp_path / "www"
    www.mkdir()
    exe = shutil.which("ffmpeg")
    assert exe is not None
    await asyncio.to_thread(
        subprocess.run,
        [
            exe, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=f=440",
            "-t", "20", "-g", "25", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(www / "clip.ts"),
        ],
        check=True,
    )  # fmt: skip

    class Slow(http.server.SimpleHTTPRequestHandler):
        """Sends the clip at about real time, as a live stream arrives."""

        def copyfile(self, source: Any, outputfile: Any) -> None:
            while chunk := source.read(8192):
                outputfile.write(chunk)
                time.sleep(0.1)

    handler = functools.partial(Slow, directory=str(www))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/clip.ts"
    config = (
        f"media:\n  sources:\n    clip: {{kind: stream, url: '{url}'}}\n"
        f"    view: {{kind: stream, url: '{url}', delivery: mjpeg, width: 160}}\n"
    )
    service = MediaService(tmp_path / "run", tmp_path / "state", VirtualClock(), playlist_wait=10)
    service.configure(load(tmp_path, config))
    try:
        playlist = await service.hls_file("clip", "10.0.0.2", "index.m3u8")
        text = playlist.read_text()
        assert "#EXT-X-MAP" in text
        assert ".m4s" in text
        assert state_of(service, "clip") == "running"  # h264 + aac: playable, copied
        viewer = await service.open_mjpeg("view")
        got = await frames(viewer, 2)
        assert all(f[:2] == b"\xff\xd8" for f in got)
        viewer.close()
    finally:
        await service.close()
        server.shutdown()
    assert not (tmp_path / "run" / "media" / "clip").exists()


def state_of(service: MediaService, name: str) -> str:
    return str(next(s for s in service.status()["sources"] if s["name"] == name)["state"])
