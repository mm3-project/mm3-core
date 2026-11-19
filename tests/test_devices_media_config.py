"""Milestone 8, step 1: the ``devices`` and ``media`` keys, owners, checks and the registry file."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from conftest import EngineFactory
from mm3.core.config import ConfigResult, load_config
from mm3.core.config.diff import TOP_SECTIONS, changed_sections, plan_reload
from mm3.core.config.manager import ConfigManager
from mm3.core.config.records import CODES, MESSAGES, WARNING_CODES
from mm3.core.devices import DeviceRegistry, device_list, images_enabled, resolved
from mm3.core.registry import Registry
from mm3.module import MirrorModule
from test_history import Setup

ADMINUI = Path(__file__).resolve().parents[1] / "src" / "mm3" / "core" / "web" / "adminui"
CAM = "/dev/v4l/by-id/usb-cam-video-index0"


class Vision(MirrorModule):
    name = "vision"


class Clock(MirrorModule):
    name = "clock"
    element = "mm3-clock"


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    reg.add(Vision)
    reg.add(Clock)
    return reg


def load(tmp_path: Path, text: str, registry: Registry) -> ConfigResult:
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "config.yaml").write_text(text)
    return load_config(etc / "config.yaml", registry=registry, env_file=etc / "x.env", environ={})


def codes(result: ConfigResult) -> list[tuple[str, str, str]]:
    return [(r.code, r.severity, r.path) for r in result.records]


VISION = "modules:\n  - module: vision\n"


# --- CORE-388: the devices key ---------------------------------------------------------------


def test_core_388_devices_key_with_defaults(tmp_path: Path, registry: Registry) -> None:
    r = load(
        tmp_path,
        f"devices:\n  usb_cam: {{kind: camera, path: {CAM}}}\n  kin: {{kind: kinect}}\n",
        registry,
    )
    assert r.ok, r.records
    assert r.config is not None
    cam, kin = r.config.devices["usb_cam"], r.config.devices["kin"]
    assert (cam.owner, cam.images, cam.serial) == ("auto", False, None)
    assert (kin.path, kin.serial) == (None, None)
    assert load(tmp_path, "engine: {}\n", registry).config.devices == {}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("entry", "code", "path"),
    [
        ("{kind: camera}", "CORE_CFG_DEVICE_PATH", "/devices/cam"),
        ("{kind: camera, path: /dev/video0}", "CORE_CFG_DEVICE_PATH", "/devices/cam/path"),
        ("{kind: camera, path: dev/v4l/by-id/x}", "CORE_CFG_DEVICE_PATH", "/devices/cam/path"),
        ("{kind: camera, path: /dev/v4l/by-id/}", "CORE_CFG_DEVICE_PATH", "/devices/cam/path"),
        ("{kind: camera, path: /dev/v4l/by-id/../x}", "CORE_CFG_DEVICE_PATH", "/devices/cam/path"),
        ("{kind: kinect, path: /dev/v4l/by-id/x}", "CORE_CFG_INVALID", "/devices/cam/path"),
        (f"{{kind: camera, path: {CAM}, serial: A1}}", "CORE_CFG_INVALID", "/devices/cam/serial"),
        ("{kind: webcam}", "CORE_CFG_CHOICE", "/devices/cam/kind"),
        (f"{{kind: camera, path: {CAM}, images: 'yes'}}", "CORE_CFG_TYPE", "/devices/cam/images"),
        (f"{{kind: camera, path: {CAM}, owner: me}}", "CORE_CFG_CHOICE", "/devices/cam/owner"),
        (f"{{kind: camera, path: {CAM}, size: 2}}", "CORE_CFG_UNKNOWN_KEY", "/devices/cam/size"),
    ],
)
def test_core_388_device_entry_errors(
    tmp_path: Path, registry: Registry, entry: str, code: str, path: str
) -> None:
    r = load(tmp_path, f"devices:\n  cam: {entry}\n", registry)
    assert (code, "error", path) in codes(r)
    assert not r.ok


def test_core_388_device_name_pattern(tmp_path: Path, registry: Registry) -> None:
    r = load(tmp_path, f"devices:\n  Cam-1: {{kind: camera, path: {CAM}}}\n", registry)
    assert [c for c, _, _ in codes(r)] == ["CORE_CFG_PATTERN"]


def test_core_388_devices_are_live_on_reload(tmp_path: Path, registry: Registry) -> None:
    old = load(tmp_path, "engine: {}\n", registry)
    new = load(
        tmp_path, f"devices:\n  cam: {{kind: camera, path: {CAM}, images: true}}\n", registry
    )
    plan = plan_reload(old, new)
    assert plan.devices
    assert not plan.media
    assert not plan.stop
    assert not plan.start
    assert not plan.restart
    assert not plan.engine_restart


# --- CORE-389: owner resolution ---------------------------------------------------------------


def test_core_389_auto_without_vision(tmp_path: Path, registry: Registry) -> None:
    r = load(
        tmp_path, f"devices:\n  b: {{kind: kinect}}\n  a: {{kind: camera, path: {CAM}}}\n", registry
    )
    assert [(d.name, d.owner, d.reason) for d in resolved(r)] == [
        ("a", "media", "auto"),
        ("b", "none", "auto"),
    ]


def test_core_389_auto_with_an_enabled_vision_instance(tmp_path: Path, registry: Registry) -> None:
    text = f"devices:\n  a: {{kind: camera, path: {CAM}}}\n  b: {{kind: kinect}}\n" + VISION
    assert [d.owner for d in resolved(load(tmp_path, text, registry))] == ["vision", "vision"]
    disabled = text + "    disabled: true\n"
    assert [d.owner for d in resolved(load(tmp_path, disabled, registry))] == ["media", "none"]


def test_core_389_explicit_owners(tmp_path: Path, registry: Registry) -> None:
    text = (
        f"devices:\n  a: {{kind: camera, path: {CAM}, owner: none}}\n"
        "  b: {kind: kinect, owner: vision}\n" + VISION
    )
    assert [(d.owner, d.reason) for d in resolved(load(tmp_path, text, registry))] == [
        ("none", "explicit"),
        ("vision", "explicit"),
    ]


def test_core_389_media_cannot_own_a_kinect(tmp_path: Path, registry: Registry) -> None:
    r = load(tmp_path, "devices:\n  k: {kind: kinect, owner: media}\n", registry)
    assert codes(r) == [("CORE_CFG_DEVICE_OWNER", "error", "/devices/k/owner")]


# --- CORE-390: checks ------------------------------------------------------------------------


def test_core_390_same_path_is_a_duplicate(tmp_path: Path, registry: Registry) -> None:
    r = load(
        tmp_path,
        f"devices:\n  a: {{kind: camera, path: {CAM}}}\n  b: {{kind: camera, path: {CAM}}}\n",
        registry,
    )
    assert codes(r) == [("CORE_CFG_DEVICE_DUPLICATE", "error", "/devices/b")]
    assert r.records[0].params == {"device": "b", "other": "a"}
    assert "b" in r.records[0].message
    assert "a" in r.records[0].message


def test_core_390_links_are_resolved_where_the_path_exists(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    by_id, by_path = "/dev/v4l/by-id/usb-cam", "/dev/v4l/by-path/pci-0-usb-0:1"
    target = str(tmp_path / "video0")
    monkeypatch.setattr(os.path, "lexists", lambda p: p in (by_id, by_path))
    monkeypatch.setattr(os.path, "realpath", lambda p, **kw: target if p in (by_id, by_path) else p)
    r = load(
        tmp_path,
        f"devices:\n  a: {{kind: camera, path: {by_id}}}\n  b: {{kind: camera, path: {by_path}}}\n",
        registry,
    )
    assert [c for c, _, _ in codes(r)] == ["CORE_CFG_DEVICE_DUPLICATE"]


def test_core_390_kinect_serials(tmp_path: Path, registry: Registry) -> None:
    same = "devices:\n  a: {kind: kinect, serial: A1}\n  b: {kind: kinect, serial: A1}\n"
    assert [c for c, _, _ in codes(load(tmp_path, same, registry))] == ["CORE_CFG_DEVICE_DUPLICATE"]
    both_bare = "devices:\n  a: {kind: kinect}\n  b: {kind: kinect}\n"
    assert [c for c, _, _ in codes(load(tmp_path, both_bare, registry))] == [
        "CORE_CFG_DEVICE_DUPLICATE"
    ]
    one_bare = "devices:\n  a: {kind: kinect}\n  b: {kind: kinect, serial: A1}\n"
    assert load(tmp_path, one_bare, registry).records == []


def test_core_390_owner_vision_without_vision_is_a_warning(
    tmp_path: Path, registry: Registry
) -> None:
    text = f"devices:\n  a: {{kind: camera, path: {CAM}, owner: vision}}\n"
    r = load(tmp_path, text, registry)
    assert r.ok
    assert codes(r) == [("CORE_CFG_DEVICE_OWNER_ABSENT", "warning", "/devices/a/owner")]
    assert load(tmp_path, text + VISION, registry).records == []


def test_core_390_codes_are_in_the_table() -> None:
    new = {
        "CORE_CFG_DEVICE_PATH",
        "CORE_CFG_DEVICE_OWNER",
        "CORE_CFG_DEVICE_DUPLICATE",
        "CORE_CFG_DEVICE_OWNER_ABSENT",
    }
    assert new <= CODES
    assert all(MESSAGES[c] for c in new)
    assert "CORE_CFG_DEVICE_OWNER_ABSENT" in WARNING_CODES


# --- CORE-391: registry file -----------------------------------------------------------------


def test_core_391_registry_file(tmp_path: Path, registry: Registry) -> None:
    r = load(
        tmp_path,
        f"devices:\n  cam: {{kind: camera, path: {CAM}, images: true}}\n"
        "  kin: {kind: kinect, serial: A1}\n  kin2: {kind: kinect, owner: none}\n",
        registry,
    )
    run = tmp_path / "run"
    reg = DeviceRegistry(run)
    assert reg.write(r)
    doc = json.loads((run / "devices.json").read_text())
    assert doc == {
        "devices": {
            "cam": {
                "kind": "camera",
                "path": CAM,
                "owner": "media",
                "images": True,
                "feed": str(run / "feed" / "cam.sock"),
            },
            "kin": {
                "kind": "kinect",
                "serial": "A1",
                "owner": "none",
                "images": False,
                "feed": str(run / "feed" / "kin.sock"),
            },
            "kin2": {
                "kind": "kinect",
                "owner": "none",
                "images": False,
                "feed": str(run / "feed" / "kin2.sock"),
            },
        }
    }
    assert stat.S_IMODE((run / "devices.json").stat().st_mode) == 0o644
    assert [p.name for p in run.iterdir()] == ["devices.json"]  # no temporary file left


def test_core_391_atomic_replace(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    reg = DeviceRegistry(run)
    assert reg.write(load(tmp_path, "engine: {}\n", registry))
    calls: list[tuple[str, str]] = []
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        calls.append((Path(src).parent.name, Path(dst).name))
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    assert reg.write(load(tmp_path, f"devices:\n  c: {{kind: camera, path: {CAM}}}\n", registry))
    assert calls == [("run", "devices.json")]


def test_core_391_unwritable_is_logged_not_fatal(tmp_path: Path, registry: Registry) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert DeviceRegistry(blocker / "run").write(load(tmp_path, "engine: {}\n", registry)) is False


async def test_core_391_written_again_after_every_reload(
    make_engine: EngineFactory, tmp_path: Path
) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    path = etc / "config.yaml"
    path.write_text(f"devices:\n  cam: {{kind: camera, path: {CAM}}}\n")
    env = etc / "mm3.env"
    env.write_text("")
    env.chmod(0o600)
    engine = make_engine(Vision, Clock)
    result = load_config(path, registry=engine.registry, env_file=env, environ={})
    assert result.ok
    await engine.start(result.instance_specs())
    manager = ConfigManager(engine, result, registry=engine.registry, env_file=env, environ={})
    run = tmp_path / "run"
    reg = DeviceRegistry(run)
    reg.write(result)
    manager.layout_listeners.append(reg.reloaded)
    await manager.start(watch=False)
    try:

        def owner() -> str:
            return str(json.loads((run / "devices.json").read_text())["devices"]["cam"]["owner"])

        assert owner() == "media"
        # Only the instances change: the owner of an auto device follows them.
        path.write_text(path.read_text() + VISION)
        assert await manager.reload("file")
        assert owner() == "vision"
    finally:
        await manager.close()
        await engine.stop()


# --- CORE-392: camera images -----------------------------------------------------------------


def test_core_392_images_off_by_default_and_warning(tmp_path: Path, registry: Registry) -> None:
    off = load(tmp_path, f"devices:\n  cam: {{kind: camera, path: {CAM}}}\n", registry)
    on = load(tmp_path, f"devices:\n  cam: {{kind: camera, path: {CAM}, images: true}}\n", registry)
    assert not images_enabled(off)
    assert images_enabled(on)
    assert MESSAGES  # the core catalog is loaded
    from mm3.core.i18n.catalogs import CORE_CATALOG, Table

    for lang in ("en", "it", "fr", "es", "de"):
        assert "server.allow" in Table(CORE_CATALOG).get(lang)["CORE_ADMIN_DEVICES_IMAGES_WARNING"]


def test_core_392_warning_banner_on_home_and_form() -> None:
    devices = (ADMINUI / "devices.js").read_text()
    assert 'banner("warn", t("CORE_ADMIN_DEVICES_IMAGES_WARNING")' in devices
    home = (ADMINUI / "home.js").read_text()
    assert 'api("GET", "/devices")' in home
    assert "imagesWarning(this.images)" in home
    config = (ADMINUI / "config.js").read_text()
    assert "imagesWarning(imagesOn(values.devices))" in config


# --- CORE-393: admin -------------------------------------------------------------------------


def test_core_393_device_list(tmp_path: Path, registry: Registry) -> None:
    real = tmp_path / "cam0"
    real.write_text("")
    r = load(
        tmp_path,
        f"devices:\n  z: {{kind: kinect}}\n  a: {{kind: camera, path: {CAM}, owner: none}}\n",
        registry,
    )
    rows = device_list(resolved(r))
    assert rows == [
        {
            "name": "a",
            "kind": "camera",
            "path": CAM,
            "serial": None,
            "owner": "none",
            "reason": "explicit",
            "present": os.path.exists(CAM),
            "images": False,
        },
        {
            "name": "z",
            "kind": "kinect",
            "path": None,
            "serial": None,
            "owner": "none",
            "reason": "auto",
            "present": None,
            "images": False,
        },
    ]


def test_core_393_form_and_debug_overview() -> None:
    assert "devices" in TOP_SECTIONS
    draft = (ADMINUI / "draft.js").read_text()
    assert '"backup", "devices", "media"]' in draft
    form = (ADMINUI / "schemaform.js").read_text()
    assert "modelMap(parts, names, node, value)" in form
    debug = (ADMINUI / "debug.js").read_text()
    assert 'api("GET", "/devices")' in debug
    assert "devicesCard(this.devices)" in debug


# --- CORE-398: the media key -----------------------------------------------------------------


MEDIA = f"""
devices:
  usb_cam: {{kind: camera, path: {CAM}}}
media:
  sources:
    doorbell: {{kind: rtsp, url: "rtsps://user:pw@door.lan/live", audio: false}}
    radio:    {{kind: stream, url: "https://radio.example/live.m3u8"}}
    mirror:   {{kind: camera, device: usb_cam}}
    holidays: {{kind: files, path: "shares/nas/photos/2026"}}
    mine:     {{kind: files, path: "files/"}}
  shares:
    nas:   {{method: sync, remote: nas, path: "media", interval: 60}}
    films: {{method: mount, type: nfs, host: nas.lan, path: /export/films}}
    smb:   {{method: mount, type: smb, host: nas.lan, path: films, remote: nas}}
  limits:   {{processes: 4, transcode: 1, upload_max_mb: 2048}}
  takeover: {{duration: 60, max_duration: 3600}}
  browser_audio: false
"""


def test_core_398_the_example_of_the_spec(tmp_path: Path, registry: Registry) -> None:
    r = load(tmp_path, MEDIA, registry)
    assert r.ok, r.records
    assert r.records == [], r.records
    assert r.config is not None
    m = r.config.media
    assert m.sources["mirror"].has_audio is False
    assert m.sources["radio"].has_audio is True
    assert m.sources["doorbell"].has_audio is False
    assert (m.sources["radio"].delivery, m.sources["radio"].fps, m.sources["radio"].width) == (
        "auto",
        15,
        None,
    )
    assert m.shares["nas"].method == "sync"
    assert m.shares["films"].method == "mount"


def test_core_398_defaults(tmp_path: Path, registry: Registry) -> None:
    r = load(tmp_path, "engine: {}\n", registry)
    assert r.config is not None
    m = r.config.media
    assert (m.sources, m.shares, m.browser_audio) == ({}, {}, False)
    assert (m.limits.processes, m.limits.transcode, m.limits.upload_max_mb) == (4, 1, 2048)
    assert (m.takeover.duration, m.takeover.max_duration) == (60, 3600)
    camera = load(
        tmp_path,
        f"devices:\n  c: {{kind: camera, path: {CAM}}}\nmedia:\n  sources:\n"
        "    v: {kind: camera, device: c, audio: true}\n",
        registry,
    )
    assert camera.config is not None
    assert camera.config.media.sources["v"].has_audio is False


@pytest.mark.parametrize(
    ("text", "code", "path"),
    [
        ("media: {other: 1}", "CORE_CFG_UNKNOWN_KEY", "/media/other"),
        ("media: {limits: {processes: 9}}", "CORE_CFG_RANGE", "/media/limits/processes"),
        ("media: {limits: {transcode: 5}}", "CORE_CFG_RANGE", "/media/limits/transcode"),
        ("media: {limits: {upload_max_mb: 0}}", "CORE_CFG_RANGE", "/media/limits/upload_max_mb"),
        (
            "media: {sources: {Bad: {kind: files, path: files/}}}",
            "CORE_CFG_PATTERN",
            "/media/sources/Bad",
        ),
        ("media: {sources: {s: {kind: tv}}}", "CORE_CFG_CHOICE", "/media/sources/s/kind"),
        ("media: {sources: {s: {kind: rtsp}}}", "CORE_CFG_REQUIRED", "/media/sources/s"),
        ("media: {sources: {s: {kind: files}}}", "CORE_CFG_REQUIRED", "/media/sources/s"),
        ("media: {sources: {s: {kind: camera}}}", "CORE_CFG_REQUIRED", "/media/sources/s"),
        (
            "media: {sources: {s: {kind: files, path: files/, url: 'https://x'}}}",
            "CORE_CFG_INVALID",
            "/media/sources/s/url",
        ),
        (
            "media: {sources: {s: {kind: files, path: files/, fps: 31}}}",
            "CORE_CFG_RANGE",
            "/media/sources/s/fps",
        ),
        (
            "media: {sources: {s: {kind: files, path: files/, width: 1921}}}",
            "CORE_CFG_RANGE",
            "/media/sources/s/width",
        ),
        (
            "media: {sources: {s: {kind: files, path: files/, delivery: dash}}}",
            "CORE_CFG_CHOICE",
            "/media/sources/s/delivery",
        ),
        (
            "media: {shares: {n: {method: copy, remote: r}}}",
            "CORE_CFG_CHOICE",
            "/media/shares/n/method",
        ),
        (
            "media: {shares: {n: {method: sync, remote: r, interval: 4}}}",
            "CORE_CFG_RANGE",
            "/media/shares/n/interval",
        ),
        (
            "media: {shares: {n: {method: mount, type: smb, host: h, path: p}}}",
            "CORE_CFG_INVALID",
            "/media/shares/n",
        ),
        (
            "media: {takeover: {duration: 120, max_duration: 60}}",
            "CORE_CFG_INVALID",
            "/media/takeover/max_duration",
        ),
        ("media: {browser_audio: 'yes'}", "CORE_CFG_TYPE", "/media/browser_audio"),
    ],
)
def test_core_398_media_errors(
    tmp_path: Path, registry: Registry, text: str, code: str, path: str
) -> None:
    r = load(tmp_path, text + "\n", registry)
    assert (code, "error", path) in codes(r), codes(r)


def test_core_398_media_is_live_on_reload(tmp_path: Path, registry: Registry) -> None:
    old = load(tmp_path, "engine: {}\n", registry)
    new = load(tmp_path, "media: {browser_audio: true}\n", registry)
    plan = plan_reload(old, new)
    assert plan.media
    assert not plan.devices
    assert not plan.engine_restart


# --- CORE-399: media checks ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "code", "field"),
    [
        ("{kind: rtsp, url: 'https://cam/live'}", "CORE_CFG_MEDIA_URL", "url"),
        ("{kind: stream, url: 'rtsp://cam/live'}", "CORE_CFG_MEDIA_URL", "url"),
        ("{kind: stream, url: 'ftp://radio/live'}", "CORE_CFG_MEDIA_URL", "url"),
        ("{kind: stream, url: 'not a url'}", "CORE_CFG_MEDIA_URL", "url"),
        ("{kind: camera, device: nope}", "CORE_CFG_MEDIA_DEVICE", "device"),
        ("{kind: camera, device: kin}", "CORE_CFG_MEDIA_DEVICE", "device"),
        ("{kind: files, path: /var/lib/mm3/media/files/a.mp4}", "CORE_CFG_MEDIA_PATH", "path"),
        ("{kind: files, path: files/../secret}", "CORE_CFG_MEDIA_PATH", "path"),
        ("{kind: files, path: photos/a.jpg}", "CORE_CFG_MEDIA_PATH", "path"),
        ("{kind: files, path: files}", "CORE_CFG_MEDIA_PATH", "path"),
        ("{kind: files, path: shares/nas}", "CORE_CFG_MEDIA_PATH", "path"),
        ("{kind: files, path: shares/other/a.jpg}", "CORE_CFG_MEDIA_SHARE", "path"),
    ],
)
def test_core_399_errors(
    tmp_path: Path, registry: Registry, source: str, code: str, field: str
) -> None:
    text = (
        f"devices:\n  cam: {{kind: camera, path: {CAM}}}\n  kin: {{kind: kinect}}\n"
        f"media:\n  shares:\n    nas: {{method: sync, remote: nas}}\n  sources:\n    s: {source}\n"
    )
    r = load(tmp_path, text, registry)
    assert codes(r) == [(code, "error", f"/media/sources/s/{field}")], codes(r)


def test_core_399_url_credentials_stay_out_of_records(tmp_path: Path, registry: Registry) -> None:
    r = load(
        tmp_path,
        "media:\n  sources:\n    s: {kind: rtsp, url: 'https://u:secret@cam/live'}\n",
        registry,
    )
    (rec,) = r.records
    assert "secret" not in json.dumps(rec.as_dict())


def test_core_399_hls_on_a_camera_is_a_warning(tmp_path: Path, registry: Registry) -> None:
    text = (
        f"devices:\n  cam: {{kind: camera, path: {CAM}}}\n"
        "media:\n  sources:\n    s: {kind: camera, device: cam, delivery: hls}\n"
    )
    r = load(tmp_path, text, registry)
    assert r.ok
    assert codes(r) == [("CORE_CFG_MEDIA_DELIVERY", "warning", "/media/sources/s/delivery")]


def test_core_399_share_remotes_are_not_checked_here(tmp_path: Path, registry: Registry) -> None:
    # The remote lives in the installer's rclone.conf (CORE-365); the installer checks it.
    text = "media:\n  shares:\n    nas: {method: sync, remote: no_such_remote}\n"
    assert load(tmp_path, text, registry).records == []


def test_core_399_codes_are_in_the_table() -> None:
    new = {
        "CORE_CFG_MEDIA_URL",
        "CORE_CFG_MEDIA_DEVICE",
        "CORE_CFG_MEDIA_PATH",
        "CORE_CFG_MEDIA_SHARE",
        "CORE_CFG_MEDIA_DELIVERY",
    }
    assert new <= CODES
    assert "CORE_CFG_MEDIA_DELIVERY" in WARNING_CODES


# --- CORE-416: history sections --------------------------------------------------------------


def test_core_416_devices_and_media_are_sections(tmp_path: Path, registry: Registry) -> None:
    assert TOP_SECTIONS[-3:] == ("backup", "devices", "media")
    old = load(tmp_path, "modules: []\n", registry)
    new = load(
        tmp_path,
        "modules: []\n"
        f"devices:\n  cam: {{kind: camera, path: {CAM}}}\nmedia: {{browser_audio: true}}\n",
        registry,
    )
    assert changed_sections(old, new) == ["devices", "media"]


def test_core_416_section_restore(tmp_path: Path) -> None:
    reg = Registry()
    reg.add(Clock)
    s = Setup(tmp_path, reg)
    s.save(f"devices:\n  cam: {{kind: camera, path: {CAM}}}\nmedia: {{browser_audio: true}}\n")
    v2 = s.save(f"devices:\n  cam: {{kind: camera, path: {CAM}, images: true}}\nmedia: {{}}\n")
    assert s.history.store.meta(v2).sections == ["devices", "media"]
    res = s.history.restore(1, "cli:t", section="devices")
    assert res.code == 0, res.message
    assert "images" not in s.text()
    assert "media: {}" in s.text()
    assert s.history.store.meta(res.version).sections == ["devices"]
