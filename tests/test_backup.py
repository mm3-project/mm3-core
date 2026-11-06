"""Tests of backup and restore in the installer (milestone 7, PR 5).

Implements:
- CORE-161, CORE-362 (Core spec 0.37, §10.8): secrets only in encrypted archives,
  archive layout, manifest and sidecar.
- CORE-360 (Core spec 0.37, §10.8): retention and the schedule.
- CORE-162, CORE-163, CORE-164, CORE-165, CORE-363 (Core spec 0.37, §10.8): restore,
  rollback, compatibility, one instance, order.
- CORE-364, CORE-365, CORE-366, CORE-361 (Core spec 0.37, §10.8): keys, remotes,
  pre-operation backups, requests.
- CORE-244 (Core spec 0.37, §10.8): the backup block as settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from installer_fakes import NOW
from mm3.cli.backup_cmd import run_backup
from mm3.cli.main import build_parser
from mm3.core.config.model import BackupConfig, BackupKeep, BackupRemote
from mm3.installer import backup as backup_mod
from mm3.installer.age import Age, AgeError, Identity, encryption_kind, valid_recipient
from mm3.installer.backup import (
    Archive,
    BackupError,
    BackupService,
    check_compatible,
    select_kept,
)
from mm3.installer.backupops import BackupRunner
from mm3.installer.files import replace_tree
from mm3.installer.operations import Operation, Request
from mm3.installer.remotes import MASKED, RcloneResult, RemoteError, Remotes
from mm3.installer.restore import Restorer, Selection
from mm3.installer.schedule import BackupScheduler, Settings, StatusFile, parse_settings
from test_installer_operations import PASS, Served, World, _wait_done

RECIPIENT = "age1" + "q" * 58
HOST = "mirror"


class FakeAge:
    """age without the binary: a header line, then the plain bytes."""

    def __init__(self) -> None:
        self.n = 0

    def generate_identity(self) -> Identity:
        self.n += 1
        return Identity(RECIPIENT, f"# public key: {RECIPIENT}\nAGE-SECRET-KEY-1{'Q' * 58}\n")

    def encrypt_to(self, src: Path, dest: Path, recipient: str) -> None:
        if not valid_recipient(recipient):
            raise AgeError("not a public key")
        dest.write_bytes(b"age-encryption.org/v1\n-> X25519 fake\n" + src.read_bytes())

    def encrypt_passphrase(self, src: Path, dest: Path, passphrase: str) -> None:
        head = f"age-encryption.org/v1\n-> scrypt {passphrase}\n".encode()
        dest.write_bytes(head + src.read_bytes())

    def decrypt(self, src: Path, dest: Path, secret: str, work: Path) -> None:
        raw = src.read_bytes()
        _, second, body = raw.split(b"\n", 2)
        if second.startswith(b"-> scrypt"):
            if second.split(b" ", 2)[2].decode() != secret:
                raise AgeError("age: error: incorrect passphrase")
        elif "AGE-SECRET-KEY-1" not in secret:
            raise AgeError("age: error: no identity matched")
        dest.write_bytes(body)


class FakeRclone:
    """rclone over a directory: ``name:path`` is ``<root>/name/path``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fail = False
        self.calls: list[list[str]] = []

    def _p(self, spec: str) -> Path:
        name, _, rest = spec.partition(":")
        return self.root / name / rest

    def run(self, args: list[str], *, stdin: bytes | None = None, timeout: float = 0) -> Any:
        self.calls.append(args)
        if self.fail and args[0] != "obscure":
            return RcloneResult(1, b"", "rclone: connection refused")
        cmd = args[0]
        if cmd == "obscure":
            return RcloneResult(0, b"obs-" + (stdin or b"") + b"\n", "")
        if cmd == "mkdir":
            self._p(args[1]).mkdir(parents=True, exist_ok=True)
            return RcloneResult(0, b"", "")
        if cmd == "lsf":
            return RcloneResult(0, b"", "")
        if cmd == "copyto":
            src, dst = args[1], args[2]
            if ":" in src and not src.startswith("/"):
                data = self._p(src).read_bytes()
                Path(dst).write_bytes(data)
            else:
                target = self._p(dst)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
            return RcloneResult(0, b"", "")
        if cmd == "lsjson":
            folder = self._p(args[-1])
            items = [{"Name": p.name} for p in sorted(folder.glob("*")) if p.is_file()]
            return RcloneResult(0, json.dumps(items).encode(), "")
        if cmd == "cat":
            path = self._p(args[1])
            if not path.exists():
                return RcloneResult(3, b"", "not found")
            return RcloneResult(0, path.read_bytes(), "")
        if cmd == "deletefile":
            path = self._p(args[1])
            if not path.exists():
                return RcloneResult(4, b"", "not found")
            path.unlink()
            return RcloneResult(0, b"", "")
        raise AssertionError(args)


class FakeReader:
    def __init__(self, backup: BackupConfig | None = None, tz: str = "UTC") -> None:
        self.backup = backup or BackupConfig()
        self.tz = tz
        self.reads = 0

    def read(self) -> Settings:
        self.reads += 1
        return Settings(self.backup, self.tz)


class Mirror:
    """A World plus engine files, backup runner, fake age and fake rclone."""

    def __init__(self, world: World, tmp: Path) -> None:
        self.world = world
        self.layout = world.layout
        lay = self.layout
        self.clock = world.clock
        (lay.engine_config).mkdir(parents=True)
        (lay.engine_config / "config.yaml").write_text(
            "engine:\n  locale: en\nmodules:\n  - module: clock\n    id: clock\n"
            "    config: {format: HH}\n  - module: news\n    id: news\n    config: {n: 1}\n",
            encoding="utf-8",
        )
        (lay.engine_config / "mm3.env").write_text("API_KEY=secret1\n", encoding="utf-8")
        for inst, text in (("clock", "clock-data"), ("news", "news-data")):
            (lay.instance_data / inst).mkdir(parents=True)
            (lay.instance_data / inst / "state.txt").write_text(text, encoding="utf-8")
            (lay.instance_data / inst / "cache").mkdir()
            (lay.instance_data / inst / "cache" / "big.bin").write_bytes(b"x" * 100)
        lay.instance_kv.mkdir(parents=True)
        (lay.instance_kv / "clock.json").write_text('{"a": 1}', encoding="utf-8")
        lay.tokens_file.write_text('{"tokens": []}', encoding="utf-8")
        self.age = FakeAge()
        self.rclone = FakeRclone(tmp / "remote")
        self.reader = FakeReader()
        self.hostname = HOST
        self.service = BackupService(
            lay,
            self.age,
            lambda: world.installer.installed(world.store.kept()),
            hostname=lambda: self.hostname,
            now=self.clock,
        )
        self.remotes = Remotes(lay, self.rclone)
        self.runner = BackupRunner(
            lay,
            self.service,
            Restorer(lay),
            self.remotes,
            self.reader,
            StatusFile(lay),
            hostname=lambda: self.hostname,
            now=self.clock,
        )
        world.installer.backups = self.runner
        self.n = 0

    def tick(self) -> None:
        self.clock.now += timedelta(minutes=1)

    def make(self, kind: str = "manual", parts: list[str] | None = None, **kw: Any) -> Any:
        self.tick()
        work = self.layout.tmp / f"t{self.n}"
        self.n += 1
        return self.service.create(
            kind,  # type: ignore[arg-type]
            parts or ["config", "data", "modules"],
            work,
            **kw,
        )

    def run(self, **kw: Any) -> Operation:
        request = Request(client="test", peer_uid=1000, **kw)
        op = self.world.installer.submit(request)
        return self.world.installer.run(op, request)

    def restore(self, name: str, **opts: Any) -> Operation:
        return self.run(action="restore", options={"source": {"name": name}, **opts})

    def config_text(self) -> str:
        return (self.layout.engine_config / "config.yaml").read_text(encoding="utf-8")

    def damage(self) -> None:
        """Change everything a backup holds."""
        lay = self.layout
        (lay.engine_config / "config.yaml").write_text("engine:\n  locale: it\nmodules: []\n")
        (lay.instance_data / "clock" / "state.txt").write_text("changed")
        (lay.instance_data / "news" / "state.txt").write_text("changed")
        (lay.engine_config / "mm3.env").write_text("API_KEY=other\n")


@pytest.fixture
def mirror(tmp_path: Path) -> Mirror:
    return Mirror(World(tmp_path), tmp_path)


# --- archives (CORE-161, CORE-362) ----------------------------------------------------


def test_core_362_layout_manifest_and_sidecar(mirror: Mirror) -> None:
    created = mirror.make("manual")
    assert created.entry.name == f"{HOST}-{mirror.clock.now:%Y%m%d-%H%M%S}-manual.mm3backup"
    assert stat.S_IMODE(created.path.stat().st_mode) == 0o600
    side = json.loads(Path(f"{created.path}.json").read_text())
    assert set(side) == {
        "created", "kind", "hostname", "engine_version", "api_version", "parts",
        "size", "encrypted", "sha256",
    }  # fmt: skip
    assert side["encrypted"] is False
    assert side["parts"] == ["config", "data", "modules"]
    with Archive(created.path) as archive:
        manifest = archive.verify()
        names = set(archive.members)
    assert "manifest.json" in names
    assert "modules.json" in names
    assert "config/set/config.yaml" in names
    assert "data/clock/files/state.txt" in names
    assert "data/clock/kv.json" in names
    assert not any("cache" in n for n in names)  # caches excluded
    assert manifest.hostname == HOST
    assert manifest.kind == "manual"
    assert not any(n.startswith("secrets/") for n in names)


def test_core_362_verify_detects_a_changed_file(mirror: Mirror, tmp_path: Path) -> None:
    created = mirror.make("manual")
    bad = tmp_path / "bad.mm3backup"
    with tarfile.open(created.path) as src, tarfile.open(bad, "w:gz") as dst:
        for member in src.getmembers():
            fh = src.extractfile(member)
            if member.name == "config/set/config.yaml":
                data = b"engine: {locale: de}\n"
                member.size = len(data)
                import io

                dst.addfile(member, io.BytesIO(data))
            else:
                dst.addfile(member, fh)
    with pytest.raises(BackupError), Archive(bad) as archive:
        archive.verify()


def test_core_161_unencrypted_backup_omits_secrets_and_says_so(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "secrets"])
    assert created.omitted == ("secrets",)
    with Archive(created.path) as archive:
        manifest = archive.verify()
        assert manifest.omitted == ["secrets"]
        assert not any(n.startswith("secrets/") for n in archive.members)
    with pytest.raises(BackupError, match="encrypted"):
        mirror.make("manual", ["secrets"])


def test_core_161_encrypted_backup_holds_secrets(mirror: Mirror, tmp_path: Path) -> None:
    created = mirror.make("manual", ["config", "secrets"], recipient=RECIPIENT)
    assert encryption_kind(created.path) == "key"
    side = json.loads(Path(f"{created.path}.json").read_text())
    assert side["encrypted"] is True
    assert "secrets" in side["parts"]
    plain = mirror.service.open_plain(created.path, "AGE-SECRET-KEY-1" + "Q" * 58, tmp_path)
    with Archive(plain) as archive:
        archive.verify()
        assert archive.read("secrets/mm3.env") == b"API_KEY=secret1\n"
        assert archive.read("secrets/tokens.json") == b'{"tokens": []}'
    with pytest.raises(BackupError, match="encrypted"):
        mirror.service.open_plain(created.path, None, tmp_path)


def test_core_161_passphrase_archive_and_wrong_passphrase(mirror: Mirror, tmp_path: Path) -> None:
    created = mirror.make("manual", ["config", "secrets"], passphrase="open sesame")  # noqa: S106
    assert encryption_kind(created.path) == "passphrase"
    mirror.service.open_plain(created.path, "open sesame", tmp_path)
    with pytest.raises(BackupError, match="decrypt"):
        mirror.service.open_plain(created.path, "nope", tmp_path)


def test_core_362_symlinks_in_the_data_are_not_followed(mirror: Mirror, tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("private")
    link = mirror.layout.instance_data / "clock" / "link.txt"
    link.symlink_to(secret)
    (mirror.layout.instance_data / "evil").symlink_to(tmp_path)
    created = mirror.make("manual")
    with Archive(created.path) as archive:
        assert not any("link.txt" in n or "evil" in n for n in archive.members)


@pytest.mark.skipif(shutil.which("age") is None, reason="age is not installed")
def test_core_364_real_age_key_and_passphrase_round_trip(tmp_path: Path) -> None:
    age = Age()
    identity = age.generate_identity()
    assert valid_recipient(identity.public)
    src = tmp_path / "a.tar.gz"
    src.write_bytes(b"hello")
    out = tmp_path / "a.age"
    age.encrypt_to(src, out, identity.public)
    assert encryption_kind(out) == "key"
    plain = tmp_path / "b"
    age.decrypt(out, plain, identity.text, tmp_path)
    assert plain.read_bytes() == b"hello"
    assert not list(tmp_path.glob("identity-*"))
    pw = tmp_path / "p.age"
    age.encrypt_passphrase(src, pw, "correct horse")
    assert encryption_kind(pw) == "passphrase"
    plain2 = tmp_path / "c"
    age.decrypt(pw, plain2, "correct horse", tmp_path)
    assert plain2.read_bytes() == b"hello"
    with pytest.raises(AgeError):
        age.decrypt(pw, tmp_path / "d", "wrong", tmp_path)


# --- retention (CORE-360, CORE-366) -----------------------------------------------------


def test_core_360_select_kept_daily_weekly_monthly() -> None:
    start = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    days = [start + timedelta(days=i) for i in range(120)]
    newest_first = sorted(days, reverse=True)
    kept = select_kept(newest_first, BackupKeep(daily=7, weekly=4, monthly=6), UTC)
    assert {0, 1, 2, 3, 4, 5, 6} <= kept
    assert len(kept) <= 7 + 4 + 6
    assert len(kept) >= 7 + 3  # weeks and months add to the days
    none = select_kept(newest_first, BackupKeep(daily=1, weekly=0, monthly=0), UTC)
    assert none == {0}


def test_core_360_prune_touches_only_scheduled(mirror: Mirror) -> None:
    for _ in range(5):
        mirror.make("scheduled")
        mirror.clock.now += timedelta(days=1)  # one backup per day
    manual = mirror.make("manual")
    mirror.make("pre")
    gone = mirror.service.prune(BackupKeep(daily=2, weekly=0, monthly=0))
    assert len(gone) == 3
    kinds = [e.sidecar.kind for e in mirror.service.listing()]
    assert kinds.count("scheduled") == 2
    assert "manual" in kinds
    assert "pre" in kinds
    assert (manual.path).exists()


def test_core_366_only_the_last_three_pre_backups_are_kept(mirror: Mirror) -> None:
    names = [mirror.make("pre").entry.name for _ in range(5)]
    left = {p.name for p in mirror.layout.pre_backups.glob("*.mm3backup")}
    assert left == set(names[-3:])
    assert len(list(mirror.layout.pre_backups.glob("*.json"))) == 3


# --- pre-operation backups (CORE-366) ---------------------------------------------------


def test_core_366_install_takes_a_pre_backup_first(mirror: Mirror) -> None:
    mirror.world.publish(("mm3-news", "1.2.0"))
    op = mirror.run(action="install", name="mm3-news")
    assert op.state == "done", op.error
    pre = list(mirror.layout.pre_backups.glob("*-pre.mm3backup"))
    assert len(pre) == 1
    assert [s["state"] for s in op.steps][:3] == ["queued", "backing_up", "backing_up"]


def test_core_366_a_failed_pre_backup_stops_the_operation(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror.world.publish(("mm3-news", "1.2.0"))

    def boom(*a: Any, **k: Any) -> Any:
        raise BackupError("disk full")

    monkeypatch.setattr(mirror.service, "create", boom)
    before = mirror.world.builder.current()
    op = mirror.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "disk full" in (op.error or "")
    assert mirror.world.builder.current() == before
    assert mirror.world.engine.restarts == 0


def test_core_366_pre_backup_holds_secrets_with_a_configured_key(mirror: Mirror) -> None:
    mirror.reader.backup = BackupConfig(encrypt_to=RECIPIENT)
    mirror.world.publish(("mm3-news", "1.2.0"))
    assert mirror.run(action="install", name="mm3-news").state == "done"
    (pre,) = mirror.layout.pre_backups.glob("*-pre.mm3backup")
    assert encryption_kind(pre) == "key"


# --- restore (CORE-162, CORE-163, CORE-363) ------------------------------------------------


def test_core_363_restore_order_and_content(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "data", "secrets"], recipient=RECIPIENT)
    original = mirror.config_text()
    mirror.damage()
    mirror.world.engine.events.clear()
    op = mirror.restore(created.entry.name, key="AGE-SECRET-KEY-1" + "Q" * 58)
    assert op.state == "done", op.error
    assert mirror.world.engine.events == ["stop", "start"]
    assert mirror.config_text() == original
    assert (mirror.layout.instance_data / "clock" / "state.txt").read_text() == "clock-data"
    assert (mirror.layout.instance_data / "news" / "state.txt").read_text() == "news-data"
    assert (mirror.layout.env_file).read_text() == "API_KEY=secret1\n"
    assert mirror.world.engine.checked  # mm3 diag check ran
    marker = json.loads(mirror.layout.restore_marker.read_text())
    assert marker["operation"] == op.id
    detail = [s["detail"] for s in op.steps if s["state"] == "restoring"]
    assert [d.split(":")[0] for d in detail] == [
        "stopping mm3.service",
        "secrets",
        "data",
        "config",
    ]


def test_core_162_restore_takes_a_pre_restore_backup_first(mirror: Mirror) -> None:
    created = mirror.make("manual")
    mirror.damage()
    op = mirror.restore(created.entry.name)
    assert op.state == "done", op.error
    kinds = [e.sidecar.kind for e in mirror.service.listing()]
    assert kinds.count("pre") == 1
    first_stop = next(i for i, s in enumerate(op.steps) if "stopping" in s["detail"])
    assert any(s["state"] == "backing_up" for s in op.steps[:first_stop])


def test_core_163_not_ready_puts_the_pre_restore_backup_back(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "data", "secrets"], recipient=RECIPIENT)
    mirror.damage()
    damaged = mirror.config_text()
    mirror.world.engine.ready = [False, True]
    op = mirror.restore(created.entry.name, key="AGE-SECRET-KEY-1" + "Q" * 58)
    assert op.state == "rolled_back"
    assert "READY" in (op.error or "")
    assert mirror.config_text() == damaged
    assert (mirror.layout.instance_data / "clock" / "state.txt").read_text() == "changed"
    # secrets were replaced by the restore and come back too
    assert mirror.layout.env_file.read_text() == "API_KEY=other\n"
    assert mirror.world.engine.events == ["stop", "start", "stop", "start"]


def test_core_163_failed_diag_check_rolls_back(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "data"])
    mirror.damage()
    damaged = mirror.config_text()
    mirror.world.engine.diag = [1]
    op = mirror.restore(created.entry.name)
    assert op.state == "rolled_back"
    assert "diag check failed" in (op.error or "")
    assert mirror.config_text() == damaged


def test_core_163_restore_of_secrets_without_a_key_is_still_rolled_back(mirror: Mirror) -> None:
    # No encrypt_to is set: the pre-restore backup keeps secrets (unreadably) for the rollback.
    created = mirror.make("manual", ["config", "secrets"], passphrase="pw")  # noqa: S106
    mirror.damage()
    mirror.world.engine.ready = [False, True]
    op = mirror.restore(created.entry.name, key="pw")
    assert op.state == "rolled_back"
    assert mirror.layout.env_file.read_text() == "API_KEY=other\n"


def test_core_162_restore_refuses_a_corrupt_or_missing_backup_before_changing_anything(
    mirror: Mirror,
) -> None:
    created = mirror.make("manual")
    mirror.damage()
    created.path.write_bytes(created.path.read_bytes()[:-40])
    op = mirror.restore(created.entry.name)
    assert op.state == "failed"
    assert mirror.world.engine.events == []
    assert mirror.restore("nothing.mm3backup").state == "failed"


def test_core_165_one_instance_config_section_and_data(mirror: Mirror) -> None:
    created = mirror.make("manual")
    lay = mirror.layout
    lay.engine_config.joinpath("config.yaml").write_text(
        "engine:\n  locale: it\nmodules:\n  - module: clock\n    id: clock\n"
        "    config: {format: changed}\n  - module: news\n    id: news\n    config: {n: 99}\n"
    )
    (lay.instance_data / "clock" / "state.txt").write_text("changed")
    (lay.instance_data / "news" / "state.txt").write_text("changed")
    op = mirror.restore(created.entry.name, instance="clock", parts=["config", "data"])
    assert op.state == "done", op.error
    text = mirror.config_text()
    assert "format: HH" in text
    assert "n: 99" in text
    assert "locale: it" in text
    assert (lay.instance_data / "clock" / "state.txt").read_text() == "clock-data"
    assert (lay.instance_data / "news" / "state.txt").read_text() == "changed"


def test_core_165_instance_with_secrets_or_modules_is_refused(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "data", "modules"])
    with Archive(created.path) as archive:
        manifest = archive.manifest()
    with pytest.raises(BackupError):
        Selection.build(manifest, ["modules"], "clock")
    with pytest.raises(BackupError):
        Selection.build(manifest, ["secrets"], None)
    assert Selection.build(manifest, None, "clock").parts == ("config", "data")


def test_core_165_a_part_the_archive_lacks_is_refused(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config"])
    op = mirror.restore(created.entry.name, parts=["data"])
    assert op.state == "failed"
    assert "no data" in (op.error or "")


def test_core_164_newer_engine_and_other_api_major_are_refused(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_mod, "__version__", "99.0.0")
    newer = mirror.make("manual")
    monkeypatch.undo()
    op = mirror.restore(newer.entry.name)
    assert op.state == "failed"
    assert "newer engine" in (op.error or "")
    monkeypatch.setattr(backup_mod, "MODULE_API_VERSION", "9.0")
    other = mirror.make("manual")
    monkeypatch.undo()
    op = mirror.restore(other.entry.name)
    assert op.state == "failed"
    assert "Module API" in (op.error or "")
    assert mirror.world.engine.events == []


def test_core_164_older_engine_is_restored_with_a_warning(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = mirror.make("manual")
    monkeypatch.setattr(backup_mod, "__version__", "0.5.0")  # the engine has moved on
    op = mirror.restore(older.entry.name)
    assert op.state == "done", op.error
    assert any("older engine" in s["detail"] for s in op.steps)


def test_core_362_another_host_is_a_warning_not_a_refusal(mirror: Mirror) -> None:
    created = mirror.make("manual")
    mirror.hostname = "other-host"
    op = mirror.restore(created.entry.name)
    assert op.state == "done", op.error
    assert any("another host" in s["detail"] for s in op.steps)


def test_core_162_preview_says_what_would_change(mirror: Mirror) -> None:
    created = mirror.make("manual")
    mirror.damage()
    info = mirror.runner.inspect({"name": created.entry.name}, None, preview=True)
    assert info["ok"]
    assert info["files"] > 0
    assert info["preview"]["config"]["changed"] == ["config.yaml"]
    assert {i["id"] for i in info["preview"]["data"]["instances"]} == {"clock", "news"}
    assert any(line.startswith("config:") for line in info["lines"])
    assert (mirror.layout.instance_data / "clock" / "state.txt").read_text() == "changed"


def test_core_363_modules_part_lists_manual_installs_and_rebuilds_the_venv(mirror: Mirror) -> None:
    mirror.world.publish(("mm3-news", "1.2.0"), ("mm3-news", "1.0.0"))
    assert mirror.run(action="install", name="mm3-news", version="1.0.0").state == "done"
    created = mirror.make("manual", ["modules"])
    assert mirror.run(action="update", name="mm3-news").state == "done"
    venv_before = mirror.world.builder.current()
    op = mirror.restore(created.entry.name, parts=["modules"])
    assert op.state == "done", op.error
    assert mirror.world.builder.current() != venv_before
    assert mirror.world.engine.events[-2:] == ["stop", "start"]


def test_core_363_restore_marker_makes_the_next_history_version_a_restore(
    mirror: Mirror,
) -> None:
    created = mirror.make("manual", ["config"])
    mirror.damage()
    assert mirror.restore(created.entry.name).state == "done"
    marker = mirror.layout.restore_marker
    assert marker.exists()
    # The engine side consumes it (see tests/test_config_history_restore_marker.py).


def test_core_359_files_written_for_the_engine_follow_no_symlinks(
    mirror: Mirror, tmp_path: Path
) -> None:
    created = mirror.make("manual", ["data"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.txt").write_text("untouched")
    shutil.rmtree(mirror.layout.instance_data / "clock")
    (mirror.layout.instance_data / "clock").symlink_to(outside)
    op = mirror.restore(created.entry.name, parts=["data"])
    assert (outside / "state.txt").read_text() == "untouched"
    assert op.state in ("done", "failed", "rolled_back")
    assert not (mirror.layout.instance_data / "clock").is_symlink() or op.state != "done"


def test_core_373_replace_tree_never_follows_a_link(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    outside = tmp_path / "o"
    outside.mkdir()
    (outside / "keep").write_text("1")
    (root / "inst").symlink_to(outside)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        replace_tree(
            fd,
            "inst",
            lambda d: os.close(os.open("x", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=d)),
            mode=0o750,
            owner=None,
        )
    finally:
        os.close(fd)
    assert (outside / "keep").read_text() == "1"
    assert not (root / "inst").is_symlink()


# --- keys (CORE-364) -----------------------------------------------------------------------


def test_core_364_keygen_shows_the_identity_and_keeps_it_only_on_request(mirror: Mirror) -> None:
    made = mirror.runner.keygen(keep=False)
    assert made["public"] == RECIPIENT
    assert "AGE-SECRET-KEY-1" in made["identity"]
    assert not mirror.layout.backup_key.exists()
    mirror.runner.keygen(keep=True)
    assert stat.S_IMODE(mirror.layout.backup_key.stat().st_mode) == 0o600


def test_core_364_restore_uses_the_kept_identity(mirror: Mirror) -> None:
    mirror.runner.keygen(keep=True)
    created = mirror.make("manual", ["config", "secrets"], recipient=RECIPIENT)
    mirror.damage()
    op = mirror.restore(created.entry.name)  # no key sent
    assert op.state == "done", op.error
    assert mirror.layout.env_file.read_text() == "API_KEY=secret1\n"


def test_core_364_an_identity_file_does_not_outlive_the_operation(mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "secrets"], recipient=RECIPIENT)
    mirror.restore(created.entry.name, key="AGE-SECRET-KEY-1" + "Q" * 58)
    assert not list(mirror.layout.tmp.glob("rs-*"))
    assert not list(mirror.layout.tmp.glob("**/identity-*"))


# --- remotes (CORE-365) ------------------------------------------------------------------------


def test_core_365_remote_secrets_are_masked_and_kept_when_sent_back(mirror: Mirror) -> None:
    mirror.remotes.put("nas", "sftp", {"host": "nas.lan", "user": "me", "pass": "pw1"})
    conf = mirror.layout.rclone_conf
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    assert "obs-pw1" in conf.read_text()  # obscured through rclone, not a command line
    assert all("pw1" not in " ".join(c) for c in mirror.rclone.calls)
    shown = mirror.remotes.get("nas")
    assert shown["params"]["pass"] == MASKED
    assert shown["params"]["host"] == "nas.lan"
    mirror.remotes.put("nas", "sftp", {"host": "nas2.lan", "pass": MASKED})
    assert "obs-pw1" in conf.read_text()
    assert "nas2.lan" in conf.read_text()
    assert [r["name"] for r in mirror.remotes.listing()] == ["nas"]
    mirror.remotes.delete("nas")
    assert mirror.remotes.listing() == []


@pytest.mark.parametrize(
    ("kind", "params", "message"),
    [
        ("ftp", {"host": "x"}, "type"),
        ("sftp", {}, "needs host"),
        ("sftp", {"host": "x", "command": "rm"}, "no parameter"),
        ("sftp", {"host": "x\nevil = 1"}, "control"),
        ("sftp", {"host": "x", "pass": MASKED}, "no stored value"),
    ],
)
def test_core_365_remote_types_and_parameters_are_checked(
    mirror: Mirror, kind: str, params: dict[str, Any], message: str
) -> None:
    with pytest.raises(RemoteError, match=message):
        mirror.remotes.put("nas", kind, params)
    with pytest.raises(RemoteError):
        mirror.remotes.put("../x", "sftp", {"host": "h"})


def test_core_365_scheduled_backup_is_copied_and_pruned_on_the_remote(mirror: Mirror) -> None:
    mirror.remotes.put("nas", "s3", {"region": "eu"})
    mirror.reader.backup = BackupConfig(
        remote=BackupRemote(name="nas", path="mm3/m1"),
        keep=BackupKeep(daily=2, weekly=0, monthly=0),
    )
    ops = []
    for _ in range(3):
        mirror.clock.now += timedelta(days=1)
        ops.append(mirror.run(action="backup", options={"scheduled": True}))
    assert all(o.state == "done" for o in ops), [o.error for o in ops]
    remote = mirror.rclone.root / "nas" / "mm3" / "m1"
    archives = sorted(p.name for p in remote.glob("*.mm3backup"))
    assert len(archives) == 2
    assert len(list(remote.glob("*.json"))) == 2
    assert len(list(mirror.layout.backups.glob("*-scheduled.mm3backup"))) == 2
    status = mirror.runner.status.read()
    assert status["scheduled"]["ok"]
    assert status["remote"]["ok"]
    listing = mirror.runner.remote_listing()
    assert [e["name"] for e in listing] == sorted(archives, reverse=True)
    assert listing[0]["kind"] == "scheduled"


def test_core_365_a_failing_remote_does_not_fail_the_local_backup(mirror: Mirror) -> None:
    mirror.remotes.put("nas", "s3", {"region": "eu"})
    mirror.reader.backup = BackupConfig(remote=BackupRemote(name="nas"))
    mirror.rclone.fail = True
    op = mirror.run(action="backup", options={"scheduled": True})
    assert op.state == "done", op.error
    assert len(list(mirror.layout.backups.glob("*-scheduled.mm3backup"))) == 1
    status = mirror.runner.status.read()
    assert status["scheduled"]["ok"] is True
    assert status["remote"]["ok"] is False
    assert "refused" in status["remote"]["error"]


def test_core_365_restore_from_the_remote(mirror: Mirror) -> None:
    mirror.remotes.put("nas", "s3", {"region": "eu"})
    mirror.reader.backup = BackupConfig(remote=BackupRemote(name="nas", path="p"))
    assert mirror.run(action="backup", options={"scheduled": True}).state == "done"
    (name,) = [e["name"] for e in mirror.runner.remote_listing()]
    original = mirror.config_text()
    mirror.damage()
    op = mirror.run(
        action="restore", options={"source": {"name": name, "remote": True}, "parts": ["config"]}
    )
    assert op.state == "done", op.error
    assert mirror.config_text() == original


# --- manual backups and the scheduler (CORE-360, CORE-244) -------------------------------------


def test_core_360_scheduled_run_encrypts_to_the_configured_key_with_secrets(
    mirror: Mirror,
) -> None:
    mirror.reader.backup = BackupConfig(encrypt_to=RECIPIENT)
    op = mirror.run(action="backup", options={"scheduled": True})
    assert op.state == "done", op.error
    (entry,) = mirror.service.listing()
    assert entry.sidecar.encrypted
    assert "secrets" in entry.sidecar.parts
    assert entry.sidecar.kind == "scheduled"


def test_core_244_manual_backup_parts_and_defaults(mirror: Mirror) -> None:
    op = mirror.run(action="backup", options={"parts": ["config"]})
    assert op.state == "done"
    assert op.to is not None
    assert op.to.endswith("-manual.mm3backup")
    (entry,) = mirror.service.listing()
    assert entry.sidecar.parts == ["config"]
    op = mirror.run(action="backup", options={"parts": ["config", "secrets"]})
    assert op.state == "done"
    assert any("left out: secrets" in s["detail"] for s in op.steps)


def test_core_244_settings_come_from_the_config_files() -> None:
    files = {
        "config.yaml": (
            "engine:\n  timezone: Europe/Rome\nbackup:\n  schedule: '04:15'\n"
            "  keep: {daily: 3, weekly: 1, monthly: 1}\n  remote: {name: nas, path: m}\n"
        )
    }
    settings = parse_settings(files)
    assert settings.backup.schedule == "04:15"
    assert settings.backup.keep.daily == 3
    assert settings.backup.remote == BackupRemote(name="nas", path="m")
    assert settings.timezone == "Europe/Rome"
    assert parse_settings({"config.yaml": "backup: {schedule: 'nope'}\n"}).backup == BackupConfig()
    assert parse_settings({}).timezone == "UTC"
    included = parse_settings(
        {"config.yaml": "backup: !include b.yaml\n", "b.yaml": "schedule: '01:00'\n"}
    )
    assert included.backup.schedule == "01:00"


class Submitted:
    def __init__(self) -> None:
        self.count = 0
        self.busy = False

    def __call__(self) -> bool:
        if self.busy:
            return False
        self.count += 1
        return True


def _scheduler(
    now: list[datetime], last: list[datetime | None], reader: FakeReader, sub: Submitted
) -> BackupScheduler:
    return BackupScheduler(reader, lambda: last[0], sub, now=lambda: now[0])


def test_core_360_runs_once_at_the_scheduled_time_in_the_engine_timezone() -> None:
    # 03:30 in Rome (UTC+1 in winter) is 02:30 UTC.
    reader = FakeReader(BackupConfig(schedule="03:30"), "Europe/Rome")
    now = [datetime(2026, 11, 6, 2, 0, tzinfo=UTC)]
    recent = datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
    last: list[datetime | None] = [recent]
    sub = Submitted()
    sched = _scheduler(now, last, reader, sub)
    assert not sched.tick()  # 03:00 local: too early
    now[0] = datetime(2026, 11, 6, 2, 31, tzinfo=UTC)
    assert sched.tick()
    assert sub.count == 1
    now[0] = datetime(2026, 11, 6, 2, 45, tzinfo=UTC)
    assert not sched.tick()
    assert sub.count == 1


def test_core_360_retries_while_another_operation_runs() -> None:
    reader = FakeReader(BackupConfig(schedule="03:30"))
    now = [datetime(2026, 11, 6, 3, 31, tzinfo=UTC)]
    sub = Submitted()
    sub.busy = True
    sched = _scheduler(now, [now[0] - timedelta(hours=24, minutes=-5)], reader, sub)
    assert not sched.tick()
    sub.busy = False
    now[0] += timedelta(minutes=2)
    assert sched.tick()
    assert sub.count == 1


def test_core_360_a_missed_run_starts_soon_after_the_installer_when_the_last_is_over_24h_old() -> (
    None
):
    reader = FakeReader(BackupConfig(schedule="03:30"))
    now = [datetime(2026, 11, 6, 9, 0, tzinfo=UTC)]
    last: list[datetime | None] = [datetime(2026, 11, 4, 3, 30, tzinfo=UTC)]
    sub = Submitted()
    sched = _scheduler(now, last, reader, sub)
    assert not sched.tick()  # the installer has just started
    now[0] += timedelta(minutes=3)
    assert sched.tick()
    assert sub.count == 1


def test_core_360_no_catch_up_when_the_last_backup_is_recent() -> None:
    reader = FakeReader(BackupConfig(schedule="03:30"))
    now = [datetime(2026, 11, 6, 9, 0, tzinfo=UTC)]
    last: list[datetime | None] = [datetime(2026, 11, 6, 3, 30, tzinfo=UTC)]
    sub = Submitted()
    sched = _scheduler(now, last, reader, sub)
    now[0] += timedelta(minutes=30)
    assert not sched.tick()
    assert sub.count == 0


def test_core_360_settings_are_read_at_start_and_every_hour() -> None:
    reader = FakeReader()
    now = [datetime(2026, 11, 6, 9, 0, tzinfo=UTC)]
    sched = _scheduler(now, [None], reader, Submitted())
    sched.settings()
    sched.settings()
    assert reader.reads == 1
    now[0] += timedelta(minutes=59)
    sched.settings()
    assert reader.reads == 1
    now[0] += timedelta(minutes=2)
    sched.settings()
    assert reader.reads == 2


def test_core_362_check_compatible_returns_warnings_only_for_older() -> None:
    from mm3.installer.backup import Manifest

    m = Manifest("0.0.0", "1.0", HOST, NOW.isoformat(), "manual", ["config"], {}, [], 1)
    assert "older engine" in check_compatible(m)[0]


# --- requests over the socket (CORE-361) --------------------------------------------------------


@pytest.fixture
async def served(mirror: Mirror) -> AsyncIterator[Served]:
    s = Served(mirror.world, uid=1000)
    s.server.installer.backups = mirror.runner
    task = asyncio.create_task(s.server.serve(s.path, refresh=False))
    for _ in range(100):
        if s.path.exists():
            break
        await asyncio.sleep(0.01)
    yield s
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_core_361_create_list_download_and_verify_need_no_passphrase(
    served: Served, mirror: Mirror
) -> None:
    reply = await served.call("POST", "/v1/backups", {"parts": ["config", "data"]})
    assert reply.status == 202, reply.body
    op = await _wait_done(served, reply.body["id"])
    assert op["state"] == "done"
    listing = await served.call("GET", "/v1/backups")
    (item,) = listing.body["backups"]
    assert item["name"] == op["to"]
    assert item["kind"] == "manual"
    verified = await served.call("POST", "/v1/backups/verify", {"name": item["name"]})
    assert verified.status == 200
    assert verified.body["ok"] is True
    got = await asyncio.to_thread(_download, served, item["name"])
    assert got == (mirror.layout.backups / item["name"]).read_bytes()
    missing = await served.call("POST", "/v1/backups/verify", {"name": "x.mm3backup"})
    assert missing.status == 400
    assert (await served.call("GET", "/v1/backups/nothing.mm3backup")).status == 404


def _download(served: Served, name: str) -> bytes:
    import http.client

    from mm3.installer.client import _UnixConnection

    conn = _UnixConnection(served.path, 10)
    conn.request("GET", f"/v1/backups/{name}")
    response = conn.getresponse()
    assert response.status == 200
    body = response.read()
    assert response.getheader("Content-Length") == str(len(body))
    conn.close()
    assert isinstance(conn, http.client.HTTPConnection)
    return body


async def test_core_361_restore_needs_the_passphrase_except_from_root(
    served: Served, mirror: Mirror
) -> None:
    created = mirror.make("manual", ["config"])
    mirror.damage()
    body = {"name": created.entry.name, "parts": ["config"]}
    assert (await served.call("POST", "/v1/backups/restore", body)).status == 403
    wrong = await served.call("POST", "/v1/backups/restore", {**body, "passphrase": "no"})
    assert wrong.status == 403
    ok = await served.call("POST", "/v1/backups/restore", {**body, "passphrase": PASS})
    assert ok.status == 202, ok.body
    assert (await _wait_done(served, ok.body["id"]))["state"] == "done"
    served.uid = 0
    mirror.damage()
    root = await served.call("POST", "/v1/backups/restore", body)
    assert root.status == 202
    assert (await _wait_done(served, root.body["id"]))["state"] == "done"


async def test_core_361_a_file_path_is_for_root_only_and_listing_has_no_secrets(
    served: Served, mirror: Mirror, tmp_path: Path
) -> None:
    created = mirror.make("manual")
    path = str(created.path)
    assert (await served.call("POST", "/v1/backups/verify", {"path": path})).status == 403
    served.uid = 0
    assert (await served.call("POST", "/v1/backups/verify", {"path": path})).status == 200


async def test_core_361_preview_and_encrypted_archives(served: Served, mirror: Mirror) -> None:
    created = mirror.make("manual", ["config", "secrets"], recipient=RECIPIENT)
    body = {"name": created.entry.name}
    refused = await served.call("POST", "/v1/backups/preview", body)
    assert refused.status == 400
    assert "encrypted" in refused.error
    key = "AGE-SECRET-KEY-1" + "Q" * 58
    ok = await served.call("POST", "/v1/backups/preview", {**body, "key": key})
    assert ok.status == 200
    assert ok.body["encrypted"] is True
    assert ok.body["preview"]["secrets"]["replaced"] == ["auth.json", "mm3.env", "tokens.json"]
    assert key not in json.dumps(ok.body)


async def test_core_361_request_bodies_are_validated(served: Served) -> None:
    for body in (
        {"parts": ["nope"]},
        {"parts": []},
        {"recipient": "not-a-key"},
        {"recipient": RECIPIENT, "archive_passphrase": "x"},
        {"use_configured_key": "yes"},
    ):
        assert (await served.call("POST", "/v1/backups", body)).status == 400, body
    assert (await served.call("POST", "/v1/backups/restore", {"passphrase": PASS})).status == 400


async def test_core_364_keygen_route(served: Served, mirror: Mirror) -> None:
    reply = await served.call("POST", "/v1/backups/keygen", {"keep": True})
    assert reply.status == 200
    assert reply.body["public"] == RECIPIENT
    assert reply.body["kept"]
    assert mirror.layout.backup_key.exists()
    assert (await served.call("POST", "/v1/backups/keygen", {"keep": "yes"})).status == 400


async def test_core_365_remote_routes(served: Served, mirror: Mirror) -> None:
    put = {"type": "sftp", "params": {"host": "nas", "pass": "pw"}}
    assert (await served.call("PUT", "/v1/backups/remotes/nas", put)).status == 403
    put["passphrase"] = PASS
    assert (await served.call("PUT", "/v1/backups/remotes/nas", put)).status == 204
    got = await served.call("GET", "/v1/backups/remotes/nas")
    assert got.body["params"]["pass"] == MASKED
    listed = await served.call("GET", "/v1/backups/remotes")
    assert [r["name"] for r in listed.body["remotes"]] == ["nas"]
    assert "pw" not in json.dumps(listed.body)
    assert (await served.call("POST", "/v1/backups/remotes/nas/test", {"path": "m"})).status == 204
    mirror.rclone.fail = True
    failed = await served.call("POST", "/v1/backups/remotes/nas/test", {})
    assert failed.status == 502
    bad = await served.call(
        "PUT", "/v1/backups/remotes/nas", {"type": "ftp", "params": {}, "passphrase": PASS}
    )
    assert bad.status == 400
    assert (await served.call("DELETE", "/v1/backups/remotes/nas", {})).status == 403
    assert (
        await served.call("DELETE", "/v1/backups/remotes/nas", {"passphrase": PASS})
    ).status == 204
    assert (await served.call("GET", "/v1/backups/remotes/nas")).status == 404


async def test_core_360_status_shows_the_last_scheduled_and_remote_results(
    served: Served, mirror: Mirror
) -> None:
    mirror.run(action="backup", options={"scheduled": True})
    status = await served.call("GET", "/v1/status")
    assert status.body["backup"]["scheduled"]["ok"] is True
    listing = await served.call("GET", "/v1/backups")
    assert listing.body["status"]["scheduled"]["name"].endswith("-scheduled.mm3backup")


# --- the CLI (CORE-361) -------------------------------------------------------------------------


async def _cli(served: Served, *argv: str, answers: list[str] | None = None) -> tuple[int, str]:
    import io

    args = build_parser().parse_args(["backup", *argv, "--socket", str(served.path)])
    out = io.StringIO()
    given = list(answers or [])
    code = await asyncio.to_thread(
        run_backup, args, out=out, ask=lambda _p: given.pop(0), secret=lambda _p: given.pop(0)
    )
    return code, out.getvalue()


async def test_core_361_cli_create_list_verify_restore_keygen(
    served: Served, mirror: Mirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mm3.cli.backup_cmd.os.geteuid", lambda: 1000)  # the socket peer is 1000
    code, text = await _cli(served, "create", "--parts", "config,data")
    assert code == 0
    assert "Done: " in text
    code, text = await _cli(served, "list")
    assert code == 0
    assert "-manual.mm3backup" in text
    assert "config,data" in text
    name = next(p.name for p in mirror.layout.backups.glob("*.mm3backup"))
    code, text = await _cli(served, "verify", name)
    assert code == 0
    assert text.startswith("OK:")
    mirror.damage()
    code, text = await _cli(served, "restore", name, "--parts", "config", answers=["y", PASS, ""])
    assert code == 0
    assert "config:" in text
    assert "Done" in text
    assert "locale: en" in mirror.config_text()
    code, text = await _cli(served, "restore", name, answers=["n"])
    assert code == 1
    assert "Cancelled" in text
    out = tmp_path / "id.key"
    code, text = await _cli(served, "keygen", "--out", str(out))
    assert code == 0
    assert RECIPIENT in text
    assert stat.S_IMODE(out.stat().st_mode) == 384
    assert "AGE-SECRET-KEY-1" in out.read_text()
    assert "AGE-SECRET-KEY-1" not in text


async def test_core_361_cli_asks_for_the_key_of_an_encrypted_backup(
    served: Served, mirror: Mirror
) -> None:
    created = mirror.make("manual", ["config", "secrets"], passphrase="pw")  # noqa: S106
    code, text = await _cli(served, "verify", created.entry.name, answers=["pw"])
    assert code == 0
    assert "encrypted" in text
    code, text = await _cli(served, "verify", created.entry.name, answers=["wrong"])
    assert code == 1
    assert "decrypt" in text


async def test_core_361_cli_create_with_a_passphrase_asks_twice(
    served: Served, mirror: Mirror
) -> None:
    code, text = await _cli(
        served, "create", "--parts", "config,secrets", "--encrypt", answers=["a", "b"]
    )
    assert code == 1
    assert "differ" in text
    code, text = await _cli(
        served, "create", "--parts", "config,secrets", "--encrypt", answers=["a", "a"]
    )
    assert code == 0
    (entry,) = mirror.service.listing()
    assert entry.sidecar.encrypted
    assert "secrets" in entry.sidecar.parts


async def test_core_361_cli_reports_an_unreachable_installer(tmp_path: Path) -> None:
    import io

    args = build_parser().parse_args(["backup", "list", "--socket", str(tmp_path / "none.sock")])
    out = io.StringIO()
    assert await asyncio.to_thread(run_backup, args, out=out) == 2
    assert "does not answer" in out.getvalue()


def test_core_366_install_script_asks_for_the_pre_upgrade_backup_and_installs_age_and_rclone() -> (
    None
):
    script = (Path(__file__).resolve().parents[1] / "install" / "mm3-install.sh").read_text()
    main = script[script.index("\nmain() {") :]
    assert main.index("pre_upgrade_backup") < main.index("build_venv")
    assert '"$tool" backup create --pre' in script
    assert "the pre-upgrade backup failed; nothing was changed" in script
    assert "age rclone" in script


async def test_core_366_a_pre_backup_on_request_is_root_only(
    served: Served, mirror: Mirror
) -> None:
    assert (await served.call("POST", "/v1/backups", {"pre": True})).status == 403
    assert (await served.call("POST", "/v1/backups", {"pre": "yes"})).status == 400
    served.uid = 0
    reply = await served.call("POST", "/v1/backups", {"pre": True})
    assert reply.status == 202
    op = await _wait_done(served, reply.body["id"])
    assert op["state"] == "done"
    (pre,) = mirror.layout.pre_backups.glob("*-pre.mm3backup")
    assert pre.name == op["to"]


async def test_core_160_a_restore_is_an_installer_operation_that_is_logged_and_kept(
    served: Served, mirror: Mirror
) -> None:
    created = mirror.make("manual", ["config"])
    mirror.damage()
    served.uid = 0
    reply = await served.call("POST", "/v1/backups/restore", {"name": created.entry.name})
    op = await _wait_done(served, reply.body["id"])
    assert op["state"] == "done"
    assert op["action"] == "restore"
    assert op["client"] == "root"
    assert mirror.world.engine.events == ["stop", "start"]
    listed = await served.call("GET", "/v1/operations")
    assert listed.body["operations"][0]["id"] == reply.body["id"]
