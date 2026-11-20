"""Milestone 8, step 6b: network shares by rclone sync and by mount (CORE-406, CORE-407).

The installer side runs over fakes: a settings reader, an rclone that records
its arguments and an ``systemctl`` that records unit actions. The generator
``mm3-media-mounts`` is loaded from the package with its paths pointed at
``tmp_path``. The polkit rule is evaluated with node, as in CORE-359.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import runpy
import shutil
import stat
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm3.cli.installer_cmd import generator_text, polkit_text
from mm3.core.config import load_config
from mm3.core.config.loader import node_to_json, rt_load
from mm3.core.config.media import MediaConfig
from mm3.core.engine import Engine
from mm3.core.media import MediaService
from mm3.core.media.shares import ShareWatch
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.problems import ApiError
from mm3.installer.operations import Request
from mm3.installer.paths import Layout
from mm3.installer.remotes import RcloneResult
from mm3.installer.schedule import Settings, parse_settings
from mm3.installer.shares import Shares, SharesScheduler, path_ok, unit_names
from mm3.testing import VirtualClock
from test_installer_operations import World

ADMIN = "http://127.0.0.1:8081"


def shares_of(text: str) -> dict[str, Any]:
    return dict(MediaConfig.model_validate({"shares": node_to_json(rt_load(text))}).shares)


class Reader:
    def __init__(self) -> None:
        self.shares: dict[str, Any] = {}

    def read(self) -> Settings:
        return Settings(shares=self.shares)


@dataclass
class FakeRclone:
    calls: list[list[str]] = field(default_factory=list)
    fds: list[tuple[int, ...]] = field(default_factory=list)
    size: int = 10
    fail_sync: bool = False
    write: dict[str, bytes] = field(default_factory=lambda: {"a.mp4": b"x"})

    def run(
        self, args: list[str], *, pass_fds: tuple[int, ...] = (), timeout: float = 3600.0
    ) -> RcloneResult:
        self.calls.append(args)
        self.fds.append(pass_fds)
        if args[0] == "reveal":
            return RcloneResult(0, b"pw-" + args[1].encode() + b"\n", "")
        if args[0] == "size":
            return RcloneResult(0, json.dumps({"count": 1, "bytes": self.size}).encode(), "")
        if args[0] == "sync":
            if self.fail_sync:
                return RcloneResult(1, b"", "connection refused")
            dest = Path(args[-1])  # /proc/self/fd/<n> of this process
            for name, data in self.write.items():
                (dest / name).write_bytes(data)
            return RcloneResult(0, b"", "")
        return RcloneResult(2, b"", "unexpected")


@dataclass
class FakeUnits:
    actions: list[str] = field(default_factory=list)
    failed_units: set[str] = field(default_factory=set)

    def reload(self) -> bool:
        self.actions.append("reload")
        return True

    def start(self, unit: str) -> bool:
        self.actions.append(f"start {unit}")
        return True

    def stop(self, unit: str) -> bool:
        self.actions.append(f"stop {unit}")
        return True

    def failed(self, unit: str) -> bool:
        return unit in self.failed_units


class Mirror:
    def __init__(self, tmp: Path) -> None:
        self.world = World(tmp)
        self.layout: Layout = self.world.layout
        self.reader = Reader()
        self.rclone = FakeRclone()
        self.units = FakeUnits()
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        self.shares = Shares(
            self.layout, self.reader, self.rclone, self.units, now=lambda: self.now
        )
        self.world.installer.shares = self.shares
        (self.layout.engine_state / "media" / "shares").mkdir(parents=True)
        self.layout.rclone_conf.write_text(
            "[nas]\ntype = smb\nhost = nas.lan\nuser = me\npass = OBSC\ndomain = HOME\n\n"
            "[dav]\ntype = webdav\nurl = https://dav.lan\n\n[s3]\ntype = s3\n",
            encoding="utf-8",
        )

    @property
    def share_dir(self) -> Path:
        return self.layout.engine_state / "media" / "shares"

    def states(self) -> dict[str, dict[str, Any]]:
        return {s["name"]: s for s in self.shares.listing()}

    def sync(self, name: str) -> Any:
        request = Request(action="media_sync", name=name, client="test")
        op = self.world.installer.submit(request)
        return self.world.installer.run(op, request)


@pytest.fixture
def mirror(tmp_path: Path) -> Mirror:
    return Mirror(tmp_path)


# --- CORE-407: the generator --------------------------------------------------------------


def generator(tmp: Path) -> dict[str, Any]:
    code = tmp / "gen.py"
    code.write_text(generator_text(), encoding="utf-8")
    gen: dict[str, Any] = runpy.run_path(str(code))["main"].__globals__
    state = tmp / "mounts"
    state.mkdir(exist_ok=True)
    gen["STATE"] = str(state)
    gen["WANTED"] = str(state / "shares.json")
    gen["OWNER"] = getpass.getuser()
    return gen


def test_core_407_generator_makes_units_named_after_the_mount_point(tmp_path: Path) -> None:
    gen = generator(tmp_path)
    wanted = Path(gen["WANTED"])
    wanted.write_text(
        json.dumps(
            [
                {"name": "nas", "type": "smb", "host": "nas.lan", "path": "Media/Film 100%"},
                {"name": "pics", "type": "nfs", "host": "10.0.0.2", "path": "/export/pics"},
            ]
        )
    )
    os.chmod(wanted, 0o600)
    out = tmp_path / "out"
    out.mkdir()
    assert gen["main"](["gen", str(out)]) == 0
    mount = (out / "var-lib-mm3-media-shares-nas.mount").read_text()
    assert "What=//nas.lan/Media/Film 100%%\n" in mount
    assert "Where=/var/lib/mm3/media/shares/nas\n" in mount
    assert "Type=cifs\n" in mount
    assert (
        "Options=ro,nosuid,nodev,noexec,_netdev,uid=mm3,gid=mm3,file_mode=0444,dir_mode=0555,"
        f"credentials={gen['STATE']}/nas.cred\n"
    ) in mount
    assert "TimeoutSec=10s\n" in mount
    nfs = (out / "var-lib-mm3-media-shares-pics.mount").read_text()
    assert "What=10.0.0.2:/export/pics\n" in nfs
    assert "Type=nfs\n" in nfs
    assert "Options=ro,nosuid,nodev,noexec,_netdev,soft,timeo=100,retrans=3\n" in nfs
    auto = (out / "var-lib-mm3-media-shares-pics.automount").read_text()
    assert "TimeoutIdleSec=600\n" in auto
    link = out / "remote-fs.target.wants" / "var-lib-mm3-media-shares-pics.automount"
    assert os.readlink(link) == "../var-lib-mm3-media-shares-pics.automount"


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "Bad", "type": "nfs", "host": "h", "path": "/x"},
        {"name": "a-b", "type": "nfs", "host": "h", "path": "/x"},
        {"name": "ok", "type": "ftp", "host": "h", "path": "/x"},
        {"name": "ok", "type": "nfs", "host": "h;rm", "path": "/x"},
        {"name": "ok", "type": "nfs", "host": "h", "path": "/x\nExecStart=/bin/sh"},
        {"name": "ok", "type": "smb", "host": "h", "path": "a\\b"},
        {"name": "ok", "type": "smb", "host": "h", "path": " lead"},
        "not an object",
    ],
)
def test_core_407_generator_skips_a_bad_entry(tmp_path: Path, entry: Any) -> None:
    gen = generator(tmp_path)
    Path(gen["WANTED"]).write_text(json.dumps([entry]))
    os.chmod(gen["WANTED"], 0o600)
    out = tmp_path / "out"
    out.mkdir()
    assert gen["main"](["gen", str(out)]) == 0
    assert list(out.iterdir()) == []


def test_core_407_generator_trusts_only_a_file_of_the_installer(tmp_path: Path) -> None:
    gen = generator(tmp_path)
    wanted = Path(gen["WANTED"])
    good = [{"name": "pics", "type": "nfs", "host": "h", "path": "/p"}]
    wanted.write_text(json.dumps(good))
    os.chmod(wanted, 0o666)  # noqa: S103 - writable by others: ignored
    out = tmp_path / "out"
    out.mkdir()
    gen["main"](["gen", str(out)])
    assert list(out.iterdir()) == []
    os.chmod(wanted, 0o600)
    wanted.rename(tmp_path / "real.json")
    wanted.symlink_to(tmp_path / "real.json")  # a link: not followed
    gen["main"](["gen", str(out)])
    assert list(out.iterdir()) == []
    gen["OWNER"] = "root" if os.getuid() != 0 else "nobody"
    wanted.unlink()
    (tmp_path / "real.json").rename(wanted)
    gen["main"](["gen", str(out)])
    assert list(out.iterdir()) == []


def test_core_407_generator_is_standalone_and_exits_zero_without_a_file(tmp_path: Path) -> None:
    text = generator_text()
    assert text.startswith("#!/usr/bin/python3 -IS\n")
    assert "import mm3" not in text
    assert "from mm3" not in text
    gen = generator(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    assert gen["main"](["gen", str(out)]) == 0
    assert gen["main"](["gen"]) == 0


def test_core_407_unit_names_and_paths() -> None:
    assert unit_names("nas") == (
        "var-lib-mm3-media-shares-nas.mount",
        "var-lib-mm3-media-shares-nas.automount",
    )
    assert path_ok("Media/Film 2024")
    for bad in ("", " x", "x ", "a\\b", "a\tb", "a\x7fb"):
        assert not path_ok(bad), bad


# --- CORE-407: the polkit rule ------------------------------------------------------------

_HEAD = "let rules=[];const polkit={Result:{YES:'yes',NOT_HANDLED:'no'},addRule:f=>rules.push(f)};"
_TAIL = (
    "const a=(id,u,v)=>({id,lookup:k=>k==='unit'?u:v});"
    "const s=n=>({user:n});"
    "const M='org.freedesktop.systemd1.manage-units';"
    "const run=(x,y)=>rules[0](x,y);"
)
_P = "var-lib-mm3-media-shares-"
_CASES = [
    (f"run(a(M,'{_P}nas.mount','start'),s('mm3-install'))", "yes"),
    (f"run(a(M,'{_P}nas.automount','stop'),s('mm3-install'))", "yes"),
    (f"run(a(M,'{_P}nas.automount','restart'),s('mm3-install'))", "yes"),
    (f"run(a(M,'{_P}nas.mount','enable'),s('mm3-install'))", "no"),
    (f"run(a(M,'{_P}nas.service','start'),s('mm3-install'))", "no"),
    (f"run(a(M,'{_P}nas.mount.d','start'),s('mm3-install'))", "no"),
    (f"run(a(M,'{_P}Nas.mount','start'),s('mm3-install'))", "no"),
    (f"run(a(M,'{_P}.mount','start'),s('mm3-install'))", "no"),
    ("run(a(M,'var-lib-other.mount','start'),s('mm3-install'))", "no"),
    ("run(a(M,'mm3-media-nas.mount','start'),s('mm3-install'))", "no"),
    (f"run(a(M,'{_P}nas.mount','start'),s('mm3'))", "no"),
    ("run(a('org.freedesktop.systemd1.reload-daemon'),s('mm3-install'))", "yes"),
    ("run(a('org.freedesktop.systemd1.reload-daemon'),s('mm3'))", "no"),
    ("run(a('org.freedesktop.systemd1.manage-unit-files'),s('mm3-install'))", "no"),
    ("run(a('org.freedesktop.systemd1.set-environment'),s('mm3-install'))", "no"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_407_polkit_rule_allows_reload_and_only_the_share_units() -> None:
    cases = ",".join(c[0] for c in _CASES)
    script = _HEAD + polkit_text() + ";" + _TAIL + f"console.log(JSON.stringify([{cases}]))"
    out = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", "-e", script], capture_output=True, text=True, check=True
    )
    answers = json.loads(out.stdout)
    assert answers == [c[1] for c in _CASES], [
        (c[0], got) for c, got in zip(_CASES, answers, strict=True) if c[1] != got
    ]


# --- CORE-407: mounts in the installer ----------------------------------------------------


def test_core_407_mounts_are_data_files_in_the_installer_state(mirror: Mirror) -> None:
    mirror.reader.shares = shares_of(
        "nas: {method: mount, type: smb, host: nas.lan, path: Media, remote: nas}\n"
        "pics: {method: mount, type: nfs, host: 10.0.0.2, path: /export/pics}\n"
    )
    assert mirror.shares.tick() == []
    mounts = mirror.layout.state / "mounts"
    assert json.loads((mounts / "shares.json").read_text()) == [
        {"name": "nas", "type": "smb", "host": "nas.lan", "path": "Media"},
        {"name": "pics", "type": "nfs", "host": "10.0.0.2", "path": "/export/pics"},
    ]
    cred = mounts / "nas.cred"
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    assert cred.read_text() == "username=me\npassword=pw-OBSC\ndomain=HOME\n"
    assert not (mounts / "pics.cred").exists()
    assert mirror.units.actions == [
        "reload",
        "start var-lib-mm3-media-shares-nas.automount",
        "start var-lib-mm3-media-shares-pics.automount",
    ]
    assert {n: (s["state"], s["error"]) for n, s in mirror.states().items()} == {
        "nas": ("ok", None),
        "pics": ("ok", None),
    }
    # Nothing changed: no reload, and the password is revealed only once.
    mirror.units.actions.clear()
    mirror.shares.tick()
    assert mirror.units.actions == []
    assert sum(1 for c in mirror.rclone.calls if c[0] == "reveal") == 1
    # The installer never writes a unit file.
    assert not any(p.suffix in (".mount", ".automount") for p in mirror.layout.state.rglob("*"))


def test_core_407_a_removed_share_stops_its_units_and_loses_its_credentials(
    mirror: Mirror,
) -> None:
    mirror.reader.shares = shares_of(
        "nas: {method: mount, type: smb, host: nas.lan, path: Media, remote: nas}\n"
    )
    mirror.shares.tick()
    mirror.units.actions.clear()
    mirror.reader.shares = {}
    mirror.shares.tick()
    assert mirror.units.actions == [
        "reload",
        "stop var-lib-mm3-media-shares-nas.automount",
        "stop var-lib-mm3-media-shares-nas.mount",
    ]
    assert not (mirror.layout.state / "mounts" / "nas.cred").exists()
    assert mirror.shares.listing() == []


def test_core_407_remote_path_and_mount_errors(mirror: Mirror) -> None:
    mirror.reader.shares = shares_of(
        "a: {method: mount, type: smb, host: h, path: x, remote: missing}\n"
        "b: {method: mount, type: smb, host: h, path: x, remote: dav}\n"
        "c: {method: mount, type: nfs, host: h, path: ' x'}\n"
        "d: {method: mount, type: nfs, host: h, path: /d}\n"
    )
    mirror.units.failed_units.add("var-lib-mm3-media-shares-d.mount")
    mirror.shares.tick()
    assert {n: (s["state"], s["error"]) for n, s in mirror.states().items()} == {
        "a": ("error", "remote"),
        "b": ("error", "remote"),
        "c": ("error", "path"),
        "d": ("error", "mount"),
    }
    wanted = json.loads((mirror.layout.state / "mounts" / "shares.json").read_text())
    assert [e["name"] for e in wanted] == ["d"]  # only valid shares get a unit
    mirror.units.failed_units.clear()
    mirror.shares.tick()
    assert mirror.states()["d"]["state"] == "ok"


def test_core_407_reloads_once_at_the_installer_start(mirror: Mirror) -> None:
    mirror.shares.tick()
    assert mirror.units.actions == ["reload"]
    mirror.shares.tick()
    assert mirror.units.actions == ["reload"]


# --- CORE-406: sync -----------------------------------------------------------------------


def test_core_406_sync_copies_into_a_folder_the_engine_cannot_write(mirror: Mirror) -> None:
    mirror.reader.shares = shares_of("film: {method: sync, remote: dav, path: /Film}\n")
    assert mirror.shares.tick() == ["film"]  # a new share is due at once
    op = mirror.sync("film")
    assert op.state == "done", op.error
    dest = mirror.share_dir / "film"
    st = dest.lstat()
    assert stat.S_ISDIR(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o755
    assert st.st_uid == os.getuid()
    assert (dest / "a.mp4").read_bytes() == b"x"
    size_call = next(c for c in mirror.rclone.calls if c[0] == "size")
    sync_call = next(c for c in mirror.rclone.calls if c[0] == "sync")
    assert size_call[-1] == sync_call[-2] == "dav:Film"
    for call in (size_call, sync_call):
        include = call[call.index("--include") + 1]
        assert include.startswith("*.{")
        assert {"mp4", "avif"} <= set(include[3:-1].split(","))
        assert "--ignore-case" in call
    assert "--skip-links" in sync_call
    assert "--delete-excluded" in sync_call
    fds = mirror.rclone.fds[mirror.rclone.calls.index(sync_call)]
    assert sync_call[-1] == f"/proc/self/fd/{fds[0]}"
    got = mirror.states()["film"]
    assert (got["state"], got["error"], got["size"]) == ("ok", None, 10)
    assert got["last_sync"] == "2026-11-20T12:00:00Z"
    assert mirror.shares.due() == []
    mirror.now += timedelta(minutes=60)
    assert mirror.shares.due() == ["film"]


def test_core_406_a_link_planted_by_the_engine_is_never_written_through(
    mirror: Mirror, tmp_path: Path
) -> None:
    target = tmp_path / "etc-like"
    target.mkdir()
    (mirror.share_dir / "film").symlink_to(target)
    mirror.reader.shares = shares_of("film: {method: sync, remote: dav, path: Film}\n")
    mirror.shares.tick()
    assert mirror.sync("film").state == "done"
    assert list(target.iterdir()) == []
    assert not (mirror.share_dir / "film").is_symlink()
    assert (mirror.share_dir / "film" / "a.mp4").exists()


def test_core_406_wrong_remote_too_large_and_failed_runs(mirror: Mirror) -> None:
    mirror.reader.shares = shares_of(
        "a: {method: sync, remote: missing}\n"
        "b: {method: sync, remote: s3}\n"
        "c: {method: sync, remote: dav, max_size_gb: 0.000000001}\n"
        "d: {method: sync, remote: dav}\n"
    )
    mirror.shares.tick()
    mirror.rclone.fail_sync = True
    for name in "abcd":
        assert mirror.sync(name).state == "failed"
    assert {n: (s["state"], s["error"]) for n, s in mirror.states().items()} == {
        "a": ("error", "remote"),
        "b": ("error", "remote"),
        "c": ("error", "too_large"),
        "d": ("error", "failed"),
    }
    assert not (mirror.share_dir / "c").exists()  # nothing copied above the limit


def test_core_406_removing_a_share_removes_its_copy_but_not_mount_points(
    mirror: Mirror,
) -> None:
    mirror.reader.shares = shares_of("film: {method: sync, remote: dav}\n")
    mirror.shares.tick()
    assert mirror.sync("film").state == "done"
    other = mirror.share_dir / "engine_made"
    other.mkdir()
    os.chmod(other, 0o755)  # noqa: S103 - a folder like a mount point
    mirror.reader.shares = {}
    if os.getuid() == 0:
        os.chown(other, 65534, 65534)  # not the installer's: a mount point, say
    mirror.shares.tick()
    assert not (mirror.share_dir / "film").exists()
    if os.getuid() == 0:
        assert other.exists()


def test_core_406_a_due_sync_waits_while_the_installer_is_busy(mirror: Mirror) -> None:
    mirror.reader.shares = shares_of(
        "a: {method: sync, remote: dav}\nb: {method: sync, remote: dav}\n"
    )
    submitted: list[str] = []
    busy = [True]

    def submit(name: str) -> bool:
        if busy[0]:
            return False
        submitted.append(name)
        return True

    sched = SharesScheduler(mirror.shares, submit)
    assert sched.tick() == []
    busy[0] = False
    assert sched.tick() == ["a", "b"]


def test_core_406_shares_come_from_the_running_config() -> None:
    files = {
        "config.yaml": ("engine: {locale: en}\nmedia:\n  shares: !include shares.yaml\n"),
        "shares.yaml": "nas: {method: sync, remote: dav, interval: 15}\n",
    }
    got = parse_settings(files)
    assert list(got.shares) == ["nas"]
    assert got.shares["nas"].interval == 15  # type: ignore[union-attr]
    bad = parse_settings({"config.yaml": "media:\n  shares: {x: {method: sync}}\n"})
    assert bad.shares == {}


async def test_core_406_installer_routes_list_and_start_a_sync(mirror: Mirror) -> None:
    from mm3.installer.client import InstallerClient
    from mm3.installer.server import InstallerServer

    mirror.reader.shares = shares_of("film: {method: sync, remote: dav}\n")
    mirror.shares.tick()
    server = InstallerServer(
        mirror.world.installer,
        mirror.world.auth,
        peer=lambda _s: 1000,
        shares=SharesScheduler(mirror.shares, lambda _n: False),
    )
    path = mirror.layout.socket
    task = asyncio.create_task(server.serve(path, refresh=False))
    for _ in range(100):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    client = InstallerClient(path, timeout=10)
    try:
        listed = await asyncio.to_thread(client.request, "GET", "/v1/media/shares")
        assert listed.status == 200
        assert [s["name"] for s in listed.body["shares"]] == ["film"]
        missing = await asyncio.to_thread(client.request, "POST", "/v1/media/shares/nope/sync")
        assert missing.status == 404
        started = await asyncio.to_thread(
            client.request, "POST", "/v1/media/shares/film/sync", {"client": "1.2.3.4"}
        )
        assert started.status == 202
        for _ in range(200):
            op = (
                await asyncio.to_thread(
                    client.request, "GET", f"/v1/operations/{started.body['id']}"
                )
            ).body
            if op["state"] in ("done", "failed"):
                break
            await asyncio.sleep(0.02)
        assert (op["action"], op["state"]) == ("media_sync", "done")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- CORE-406: the engine side ------------------------------------------------------------


def media_with(tmp: Path, text: str) -> MediaService:
    etc = tmp / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "config.yaml").write_text(text)
    result = load_config(
        etc / "config.yaml", registry=Registry(), env_file=etc / "x.env", environ={}
    )
    assert result.ok, result.records
    media = MediaService(tmp / "run", tmp / "state", VirtualClock())
    media.configure(result)
    return media


CONFIG = """
media:
  shares:
    nas: {method: mount, type: nfs, host: h, path: /p}
    dav: {method: sync, remote: dav}
  sources:
    film: {kind: files, path: shares/nas/film}
    other: {kind: files, path: shares/dav/all}
    local: {kind: files, path: files/local}
"""


async def test_core_406_sources_under_a_failed_share_are_unavailable(tmp_path: Path) -> None:
    media = media_with(tmp_path, CONFIG)
    answers: list[list[dict[str, Any]]] = [
        [{"name": "nas", "state": "error", "error": "mount"}, {"name": "dav", "state": "ok"}]
    ]

    async def read() -> list[dict[str, Any]]:
        if not answers:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "away")
        return answers.pop(0)

    watch = ShareWatch(media, read)
    await watch.check()
    status = {s["name"]: (s["state"], s["reason"]) for s in media.status()["sources"]}
    assert status == {
        "film": ("unavailable", "share"),
        "other": ("idle", None),
        "local": ("idle", None),
    }
    await watch.check()  # the installer is away: the last answer stands
    assert media.status()["sources"][0]["reason"] == "share"
    answers.append([{"name": "nas", "state": "ok"}])
    await watch.check()
    assert {s["state"] for s in media.status()["sources"]} == {"idle"}
    await media.close()


async def test_core_406_nothing_is_read_without_shares(tmp_path: Path) -> None:
    media = media_with(tmp_path, "media: {}\n")
    calls: list[int] = []

    async def read() -> list[dict[str, Any]]:
        calls.append(1)
        return []

    await ShareWatch(media, read).check()
    assert calls == []
    await media.close()


class FakeLink:
    def __init__(self) -> None:
        self.synced: list[tuple[str, str]] = []

    async def shares(self) -> list[dict[str, Any]]:
        return [{"name": "dav", "method": "sync", "state": "ok", "last_sync": None,
                 "size": 3, "error": None}]  # fmt: skip

    async def sync_share(self, name: str, client: str) -> str:
        self.synced.append((name, client))
        return "0123456789abcdef"


@pytest.fixture
async def admin(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, FakeLink]]:
    media = media_with(tmp_path, CONFIG)
    clock = VirtualClock()
    engine = Engine(EngineSettings(), registry=Registry(), clock=clock, state_dir=tmp_path / "e")
    auth = WebAuth.open(tmp_path / "web")
    link = FakeLink()
    services = ApiServices(engine, media=media, installer=link)  # type: ignore[arg-type]
    app = protect(admin_app(auth, api=services), lambda: [], "admin")
    await engine.start([])
    token = auth.tokens.new("t")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5))
    headers = {"authorization": f"Bearer {token}", "origin": ADMIN}
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ADMIN, headers=headers) as c:
            yield c, link
    finally:
        await engine.stop()
        await media.close()


async def test_core_406_engine_forwards_the_share_routes(
    admin: tuple[httpx.AsyncClient, FakeLink],
) -> None:
    c, link = admin
    r = await c.get("/api/v1/media/shares")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "dav"
    r = await c.post("/api/v1/media/shares/dav/sync")
    assert (r.status_code, r.json()) == (202, {"id": "0123456789abcdef"})
    assert link.synced[0][0] == "dav"
    r = await c.post("/api/v1/media/shares/nope/sync")
    assert (r.status_code, r.json()["code"]) == (404, "CORE_API_NOT_FOUND")
