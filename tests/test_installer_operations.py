"""Tests of the installer's operations, staged venvs, children, protocol and CLI.

Implements:
- CORE-131, CORE-134, CORE-135, CORE-136 (Core spec 0.33, §10.7 Installer service):
  listed packages only, staged build, rollback, operations logged and kept.
- CORE-124, CORE-126, CORE-352, CORE-353 (Core spec 0.33, §10.6 Module catalog).
- CORE-354 (Core spec 0.33, §10.7): frozen pins, conflicts, removal of packages
  only the module used.
- CORE-355 (Core spec 0.35, §10.7): the protocol and its answers.
- CORE-357 (Core spec 0.33, §10.7): unit and polkit rule shipped and printed.
- CORE-359 (Core spec 0.35, §10.8): capabilities and write paths of the unit.
- CORE-367, CORE-369 (Core spec 0.37, §10.7), CORE-368 (Core spec 0.35): children without rights,
  installer venv and build user, root-only setup.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from installer_fakes import (
    BASE,
    NOW,
    Clock,
    FakeBuilder,
    FakeEngine,
    FakeFetcher,
    FakeVerification,
    entry,
    index_bytes,
    make_wheel,
    release,
    wheel_name,
)
from mm3.cli.installer_cmd import polkit_text, run_installer, unit_text
from mm3.cli.main import build_parser
from mm3.cli.modules_cmd import run_modules
from mm3.installer.auth import Auth
from mm3.installer.client import InstallerClient
from mm3.installer.files import make_read_only
from mm3.installer.operations import Installer, ModuleRecord, Operation, Request
from mm3.installer.paths import Layout
from mm3.installer.runner import DirectRunner, SetprivRunner, drop_argv
from mm3.installer.server import InstallerServer
from mm3.installer.store import CatalogStore
from mm3.installer.venv import Dist, VenvBuilder, conflicts, only_required_by

SCRIPT = Path(__file__).resolve().parents[1] / "install" / "mm3-install.sh"
PASS = "correct horse battery"  # noqa: S105 - test passphrase


class World:
    """An installer over fakes in a temporary layout."""

    def __init__(self, tmp: Path) -> None:
        self.layout = Layout.under(tmp)
        self.layout.state.mkdir(parents=True)
        self.clock = Clock()
        self.fetcher = FakeFetcher()
        self.verification = FakeVerification()
        self.store = CatalogStore(self.layout, self.fetcher, self.verification, now=self.clock)
        self.builder = FakeBuilder(self.layout)
        self.engine = FakeEngine()
        self.old = self.builder.seed("20261101T000000Z", BASE)
        self.installer = Installer(
            self.layout,
            self.store,
            self.fetcher,
            self.verification,
            self.builder,
            self.engine,
            now=self.clock,
        )
        self.auth = Auth(self.layout, now=self.clock)
        self.auth.set_passphrase(PASS)
        self.wheels: dict[tuple[str, str], bytes] = {}

    def publish(self, *releases: tuple[str, str], serial: int = 10, api: str = ">=1.0,<2") -> None:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for name, version in releases:
            data = self.wheels.setdefault((name, version), make_wheel(name, version))
            by_name.setdefault(name, []).append(release(name, version, data, api=api))
            self.fetcher.serve_release(name, version, data)
        self.fetcher.serve_index(
            index_bytes([entry(n, r) for n, r in by_name.items()], serial=serial)
        )
        self.store.refresh()

    def run(self, **kw: Any) -> Operation:
        request = Request(client="test", peer_uid=1000, **kw)
        op = self.installer.submit(request)
        return self.installer.run(op, request)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def states(op: Operation) -> list[str]:
    out: list[str] = []
    for step in op.steps:
        if not out or out[-1] != step["state"]:
            out.append(step["state"])
    return out


# --- operations -------------------------------------------------------------------


def test_core_134_install_builds_switches_restarts_and_checks(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    op = world.run(action="install", name="mm3-news")
    assert op.state == "done", op.error
    assert states(op) == [
        "queued",
        "verifying",
        "building",
        "switching",
        "restarting",
        "checking",
        "done",
    ]
    assert (op.from_, op.to) == (None, "1.2.0")
    current = world.builder.current()
    assert current is not None
    assert current != world.old
    assert "mm3-news" in world.builder.freezes[current.name]
    pins = world.builder.requirements[-1]
    assert "mm3-core==0.9.0" in pins
    assert "pydantic==2.9.0" in pins
    assert not any(p.startswith("pip==") for p in pins)
    assert pins[-1].endswith(wheel_name("mm3-news", "1.2.0"))
    assert world.engine.restarts == 1
    assert world.engine.checked == [current.name]
    rec = world.installer.records(current)["mm3-news"]
    assert rec == ModuleRecord("mm3-news", "1.2.0", "catalog", wheel_name("mm3-news", "1.2.0"))
    assert (world.layout.wheels / rec.wheel).is_file()  # type: ignore[operator]


def test_core_352_sha_mismatch_aborts_before_building(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    url = next(u for u in world.fetcher.files if u.endswith(".whl"))
    world.fetcher.files[url] = b"tampered"
    op = world.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "SHA-256" in (op.error or "")
    assert world.builder.requirements == []
    assert world.engine.restarts == 0


def test_core_352_provenance_failure_aborts_before_building(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.verification.wheel_ok = False
    op = world.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "attestation" in (op.error or "")
    assert world.verification.wheels[0][1].repository == "example/mm3-news"
    assert world.builder.requirements == []


def test_core_131_unlisted_name_is_refused(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    op = world.run(action="install", name="mm3-unknown")
    assert op.state == "failed"
    assert "not in the catalog" in (op.error or "")


def test_core_124_incompatible_release_is_refused(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"), api=">=2,<3")
    op = world.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "compatible" in (op.error or "")
    op = world.run(action="install", name="mm3-news", version="1.2.0")
    assert op.state == "failed"
    assert "Module API" in (op.error or "")


def test_core_351_expired_index_is_refreshed_once_before_an_install(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.clock.now = NOW + timedelta(days=8)
    op = world.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "expired" in (op.error or "")


def test_core_135_not_ready_rolls_back(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.engine.ready = [False, True]
    op = world.run(action="install", name="mm3-news")
    assert op.state == "rolled_back"
    assert "READY" in (op.error or "")
    assert world.builder.current() == world.old
    assert world.engine.restarts == 2
    assert world.builder.kept() == [world.old]  # the failed venv is removed


def test_core_135_failed_diag_check_rolls_back(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.engine.diag = [1]
    op = world.run(action="install", name="mm3-news")
    assert op.state == "rolled_back"
    assert "diag check" in (op.error or "")
    assert world.builder.current() == world.old


def test_core_135_rollback_reports_an_engine_still_down(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.engine.ready = [False, False]
    op = world.run(action="install", name="mm3-news")
    assert op.state == "rolled_back"
    assert "still not READY" in (op.error or "")


def test_core_354_changed_shared_package_aborts(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.builder.override = {"pydantic": Dist("pydantic", "2.10.0")}
    op = world.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "pydantic 2.9.0 would become 2.10.0" in (op.error or "")
    assert world.builder.current() == world.old
    assert world.engine.restarts == 0


def test_core_134_failed_import_test_keeps_the_engine_running(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.builder.fail_import = True
    op = world.run(action="install", name="mm3-news")
    assert op.state == "failed"
    assert "import test" in (op.error or "")
    assert world.builder.kept() == [world.old]


def test_core_354_update_replaces_the_module_and_keeps_pins(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"), ("mm3-news", "1.3.0"))
    assert world.run(action="install", name="mm3-news", version="1.2.0").state == "done"
    op = world.run(action="update", name="mm3-news")
    assert op.state == "done", op.error
    assert (op.from_, op.to) == ("1.2.0", "1.3.0")
    pins = world.builder.requirements[-1]
    assert not any("1.2.0" in p for p in pins)
    op = world.run(action="update", name="mm3-news")
    assert op.state == "failed"
    assert "already the newest" in (op.error or "")


def test_core_354_install_twice_and_update_missing_are_refused(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    assert world.run(action="install", name="mm3-news").state == "done"
    assert "use update" in (world.run(action="install", name="mm3-news").error or "")
    assert "not installed" in (world.run(action="update", name="mm3-other").error or "")


def test_core_354_remove_drops_packages_only_the_module_used(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.builder.extra_deps["mm3-news"] = [Dist("feedparser", "6.0"), Dist("sgmllib3k", "1.0")]
    assert world.run(action="install", name="mm3-news").state == "done"
    current = world.builder.current()
    assert current is not None
    freeze = world.builder.freezes[current.name]
    freeze["mm3-news"] = Dist("mm3-news", "1.2.0", ("feedparser",), True)
    freeze["feedparser"] = Dist("feedparser", "6.0", ("sgmllib3k",))
    op = world.run(action="remove", name="mm3-news")
    assert op.state == "done", op.error
    pins = world.builder.requirements[-1]
    assert not any(p.startswith(("feedparser", "sgmllib3k", "mm3-news", "mm3_news")) for p in pins)
    assert "pydantic==2.9.0" in pins
    assert world.installer.records(world.builder.current()) == {}


def test_core_354_only_required_by_keeps_shared_dependencies() -> None:
    freeze = {
        "mod": Dist("mod", "1", ("a", "b; python_version > '3'", "c; extra == 'x'")),
        "a": Dist("a", "1", ("shared",)),
        "b": Dist("b", "1"),
        "c": Dist("c", "1"),
        "shared": Dist("shared", "1"),
        "other": Dist("other", "1", ("shared",)),
    }
    assert only_required_by(freeze, "mod") == {"a", "b"}


def test_core_354_conflicts_list_removed_and_changed_packages() -> None:
    old = {
        "a": Dist("a", "1"),
        "b": Dist("b", "1"),
        "mod": Dist("mod", "1"),
        "pip": Dist("pip", "1"),
    }
    new = {"a": Dist("a", "2"), "mod": Dist("mod", "2"), "pip": Dist("pip", "2")}
    assert conflicts(old, new, {"mod"}) == ["a 1 would become 2", "b 1 would be removed"]


def test_core_135_restore_previous_and_its_rollback(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    assert world.run(action="install", name="mm3-news").state == "done"
    newer = world.builder.current()
    op = world.run(action="restore_previous")
    assert op.state == "done"
    assert world.builder.current() == world.old
    assert (op.from_, op.to) == (newer.name, world.old.name)  # type: ignore[union-attr]
    world.engine.diag = [1]
    op = world.run(action="restore_previous")
    assert op.state == "rolled_back"
    assert world.builder.current() == world.old


def test_core_135_three_venvs_are_kept(world: World) -> None:
    world.publish(("mm3-news", "1.0.0"))
    assert world.run(action="install", name="mm3-news").state == "done"
    for i in range(1, 5):
        world.publish(*[("mm3-news", f"1.{j}.0") for j in range(i + 1)], serial=10 + i)
        assert world.run(action="update", name="mm3-news").state == "done"
    assert len(world.builder.kept()) == 3


def test_core_353_unlisted_local_wheel_is_recorded_as_local(world: World, tmp_path: Path) -> None:
    wheel = tmp_path / wheel_name("mm3-mine", "0.1.0")
    wheel.write_bytes(make_wheel("mm3-mine", "0.1.0"))
    op = world.run(action="install", name="mm3-mine", source="local", path=str(wheel))
    assert op.state == "done", op.error
    rec = world.installer.records(world.builder.current())["mm3-mine"]
    assert rec.origin == "local"
    # A later rebuild installs it again from the kept wheel, not from PyPI.
    world.publish(("mm3-news", "1.2.0"))
    assert world.run(action="install", name="mm3-news").state == "done"
    pins = world.builder.requirements[-1]
    assert any(p.endswith(wheel_name("mm3-mine", "0.1.0")) for p in pins)
    assert not any(p.startswith("mm3-mine==") for p in pins)


def test_core_353_unlisted_from_pypi_is_recorded_as_pypi(world: World) -> None:
    world.fetcher.serve_release("mm3-extra", "2.0.0", make_wheel("mm3-extra", "2.0.0"))
    op = world.run(action="install", name="mm3-extra", version="2.0.0", source="pypi")
    assert op.state == "done", op.error
    assert world.installer.records(world.builder.current())["mm3-extra"].origin == "pypi"


def test_core_355_installed_modules_and_origin(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"), ("mm3-clock", "1.0.0"))
    assert world.run(action="install", name="mm3-news").state == "done"
    current = world.builder.current()
    assert current is not None
    world.builder.freezes[current.name]["mm3-clock"] = Dist("mm3-clock", "1.0.0", (), True)
    world.builder.freezes[current.name]["mm3-local"] = Dist("mm3-local", "1.0.0", (), True)
    rows = world.installer.installed(world.store.kept())
    assert {r["name"]: r["origin"] for r in rows} == {
        "mm3-clock": "catalog",
        "mm3-local": "pypi",
        "mm3-news": "catalog",
    }


def test_core_355_operations_are_kept_and_interrupted_ones_fail(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    done = world.run(action="install", name="mm3-news")
    stuck = Operation(
        "abcdef0123456789", "install", "x", "test", 0, "catalog", NOW.isoformat(), "building"
    )
    world.installer._save(stuck)
    again = Installer(
        world.layout, world.store, world.fetcher, world.verification, world.builder, world.engine
    )
    assert again.get(done.id).state == "done"  # type: ignore[union-attr]
    assert again.get(stuck.id).state == "failed"  # type: ignore[union-attr]
    lines = world.installer.events_path(done.id).read_text().splitlines()
    assert json.loads(lines[-1])["state"] == "done"


def test_core_136_every_operation_is_logged(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from mm3.installer import operations

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        operations._log, "info", lambda event, **kw: seen.append({"event": event, **kw})
    )
    world.publish(("mm3-news", "1.2.0"))
    world.run(action="install", name="mm3-news")
    record = next(r for r in seen if r["event"] == "installer_operation")
    assert record["client"] == "test"
    assert record["package"] == "mm3-news"
    assert record["to_version"] == "1.2.0"
    assert record["outcome"] == "done"


# --- staged venvs and children -------------------------------------------------------


def test_core_367_children_run_as_mm3_build_without_rights() -> None:
    argv = drop_argv("mm3-build", ["/usr/bin/python3", "-m", "venv", "/x"])
    assert argv[:8] == [
        "/usr/bin/setpriv",
        "--reuid=mm3-build",
        "--regid=mm3-build",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--",
    ]
    # Changing the bounding set needs CAP_SETPCAP, which the unit does not grant.
    assert not any(a.startswith("--bounding-set") for a in argv)
    assert "--init-groups" in drop_argv("mm3", ["mm3", "diag", "check"])
    with pytest.raises(ValueError, match="only as"):
        SetprivRunner().run(["true"], user="root", home=Path("/"))


def test_core_367_child_environment_carries_no_installer_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mm3.installer.runner import child_env

    monkeypatch.setenv("MM3_SECRET", "x")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = child_env(Path("/home/build"), None)
    assert "MM3_SECRET" not in env
    assert env["HOME"] == "/home/build"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"


def test_core_367_make_read_only_removes_every_write_bit(tmp_path: Path) -> None:
    (tmp_path / "v" / "lib").mkdir(parents=True)
    (tmp_path / "v" / "lib" / "m.py").write_text("x")
    (tmp_path / "v" / "bin").mkdir()
    (tmp_path / "v" / "bin" / "python").symlink_to("/usr/bin/python3")
    os.chmod(tmp_path / "v" / "lib" / "m.py", 0o4777)  # noqa: S103 - removed below
    make_read_only(tmp_path / "v", None)
    for path in (tmp_path / "v").rglob("*"):
        if not path.is_symlink():
            assert stat.S_IMODE(path.lstat().st_mode) & 0o7222 == 0, path
    assert stat.S_IMODE(Path("/usr/bin/python3").resolve().stat().st_mode) & 0o200


def _real_wheel(tmp: Path, name: str, version: str) -> Path:
    path = tmp / wheel_name(name, version)
    data = make_wheel(name, version)
    # Fill RECORD so pip accepts the wheel.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as out:
        names = src.namelist()
        for n in names:
            out.writestr(n, src.read(n))
    path.write_bytes(buf.getvalue())
    return path


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is required")
def test_core_134_real_staged_venv_is_read_only_and_listed(tmp_path: Path) -> None:
    layout = Layout.under(tmp_path)
    runner = DirectRunner()
    builder = VenvBuilder(layout, runner, python=sys.executable)
    wheel = _real_wheel(tmp_path, "mm3-demo", "1.0.0")
    venv = builder.build([str(wheel)], NOW)
    assert venv.parent == layout.venvs
    assert venv.name == "20261106T120000Z"
    pip_call = runner.calls[1][1]
    assert "--only-binary=:all:" in pip_call
    assert all(u == "mm3-build" for u, _ in runner.calls)
    target = next(venv.rglob("mm3_demo/__init__.py"))
    assert stat.S_IMODE(target.stat().st_mode) & 0o222 == 0
    freeze = builder.freeze(venv)
    assert freeze["mm3-demo"].module
    assert freeze["mm3-demo"].version == "1.0.0"
    from mm3.installer.venv import BuildError

    with pytest.raises(BuildError, match="import test"):
        builder.import_test(venv, "mm3-demo")  # mm3 itself is not in this venv
    builder.switch(venv)
    assert builder.current() == venv.resolve()
    with pytest.raises(BuildError, match="pip install"):
        builder.build(["mm3-no-such-package==0"], NOW + timedelta(seconds=1))
    assert len(builder.kept()) == 1


# --- protocol ----------------------------------------------------------------------


class Served:
    def __init__(self, world: World, uid: int) -> None:
        self.world = world
        self.uid = uid
        self.server = InstallerServer(world.installer, world.auth, peer=lambda _s: self.uid)
        self.path = world.layout.socket
        self.client = InstallerClient(self.path, timeout=10)

    async def call(self, method: str, target: str, body: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self.client.request, method, target, body)


@pytest.fixture
async def served(world: World) -> AsyncIterator[Served]:
    s = Served(world, uid=1000)
    task = asyncio.create_task(s.server.serve(s.path, refresh=False))
    for _ in range(100):
        if s.path.exists():
            break
        await asyncio.sleep(0.01)
    yield s
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait_done(s: Served, op_id: str) -> dict[str, Any]:
    for _ in range(200):
        reply = await s.call("GET", f"/v1/operations/{op_id}")
        if reply.body["state"] in ("done", "failed", "rolled_back"):
            return reply.body  # type: ignore[no-any-return]
        await asyncio.sleep(0.02)
    raise AssertionError("operation did not end")


async def test_core_355_status_and_socket_mode(served: Served) -> None:
    assert stat.S_IMODE(served.path.stat().st_mode) == 0o660
    reply = await served.call("GET", "/v1/status")
    assert reply.status == 200
    assert set(reply.body) == {"busy", "locked_until", "previous", "installed", "catalog"}
    assert reply.body["catalog"] is None
    assert reply.body["previous"] == []


async def test_core_355_install_over_the_socket_and_events(served: Served) -> None:
    served.world.publish(("mm3-news", "1.2.0"))
    body = {"action": "install", "name": "mm3-news", "passphrase": PASS, "client": "10.0.0.5"}
    reply = await served.call("POST", "/v1/operations", body)
    assert reply.status == 202, reply.body
    op = await _wait_done(served, reply.body["id"])
    assert op["state"] == "done"
    assert op["to"] == "1.2.0"
    assert op["client"] == "10.0.0.5"
    events = await asyncio.to_thread(lambda: list(served.client.events(reply.body["id"])))
    assert events[0]["state"] == "queued"
    assert events[-1]["state"] == "done"
    listed = await served.call("GET", "/v1/operations")
    assert listed.body["operations"][0]["id"] == reply.body["id"]
    status = await served.call("GET", "/v1/status")
    assert status.body["catalog"]["serial"] == 10
    assert status.body["previous"] == ["20261101T000000Z"]


async def test_core_132_wrong_passphrase_is_403_then_423(served: Served) -> None:
    body = {"action": "install", "name": "mm3-news", "passphrase": "nope"}
    for _ in range(4):
        assert (await served.call("POST", "/v1/operations", body)).status == 403
    assert (await served.call("POST", "/v1/operations", body)).status == 423
    status = await served.call("GET", "/v1/status")
    assert status.body["locked_until"] is not None


async def test_core_355_second_operation_is_409(served: Served) -> None:
    served.world.installer._running = Operation(  # an operation in progress
        "0" * 16, "install", "x", "t", 0, "catalog", NOW.isoformat()
    )
    body = {"action": "install", "name": "mm3-news", "passphrase": PASS}
    reply = await served.call("POST", "/v1/operations", body)
    assert reply.status == 409


async def test_core_353_unlisted_sources_need_root(served: Served, tmp_path: Path) -> None:
    body = {"action": "install", "name": "mm3-x", "source": "pypi"}
    reply = await served.call("POST", "/v1/operations", body)
    assert reply.status == 403
    assert "root" in reply.error
    served.uid = 0
    wheel = tmp_path / wheel_name("mm3-mine", "0.1.0")
    wheel.write_bytes(make_wheel("mm3-mine", "0.1.0"))
    body = {"action": "install", "name": "mm3-mine", "source": "local", "path": str(wheel)}
    reply = await served.call("POST", "/v1/operations", body)
    assert reply.status == 202, reply.body  # root, unlisted: no passphrase
    assert (await _wait_done(served, reply.body["id"]))["state"] == "done"


async def test_core_355_bad_requests_are_400(served: Served) -> None:
    for body in (
        {"action": "explode"},
        {"action": "install"},
        {"action": "install", "name": "../etc"},
        {"action": "update", "name": "mm3-x", "version": "1.0"},
        {"action": "restore_previous", "name": "mm3-x"},
    ):
        reply = await served.call("POST", "/v1/operations", body)
        assert reply.status == 400, body
    assert (await served.call("GET", "/v1/nothing")).status == 404
    assert (await served.call("GET", "/v1/operations/" + "f" * 16)).status == 404


async def test_core_369_auth_setup_only_from_root(served: Served) -> None:
    reply = await served.call(
        "POST", "/v1/auth/passphrase", {"passphrase": "a new long passphrase"}
    )
    assert reply.status == 403
    served.uid = 0
    reply = await served.call(
        "POST", "/v1/auth/passphrase", {"passphrase": "a new long passphrase"}
    )
    assert reply.status == 204
    reply = await served.call("POST", "/v1/auth/totp", {})
    assert reply.status == 200
    assert reply.body["uri"].startswith("otpauth://")
    reply = await served.call("POST", "/v1/auth/totp", {"code": "x"})
    assert reply.status == 403
    assert (await served.call("DELETE", "/v1/auth/totp")).status == 204


async def test_core_355_refresh_answers_502_on_failure(served: Served) -> None:
    reply = await served.call("POST", "/v1/catalog/refresh")
    assert reply.status == 502
    assert "404" in reply.error
    served.world.fetcher.serve_index(index_bytes([]))
    reply = await served.call("POST", "/v1/catalog/refresh")
    assert reply.status == 200
    assert reply.body["catalog"]["serial"] == 10


# --- shipped files and the CLI -----------------------------------------------------------


def test_core_359_installer_unit_has_only_its_rights() -> None:
    unit = unit_text()
    caps = "CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER CAP_SETUID CAP_SETGID"
    assert f"AmbientCapabilities={caps}" in unit
    assert f"CapabilityBoundingSet={caps}" in unit
    for line in (
        "User=mm3-install",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/etc/mm3 /var/lib/mm3 /opt/mm3",
        "StateDirectory=mm3-install",
        "RuntimeDirectory=mm3-install",
    ):
        assert line in unit.splitlines(), line


def test_core_368_installer_runs_from_its_own_venv() -> None:
    assert "ExecStart=/opt/mm3/installer/bin/mm3 installer serve" in unit_text().splitlines()


def test_core_357_polkit_rule_allows_only_mm3_service_verbs() -> None:
    rule = polkit_text()
    assert 'subject.user === "mm3-install"' in rule
    assert 'action.lookup("unit") === "mm3.service"' in rule
    assert all(f'verb === "{v}"' in rule for v in ("start", "stop", "restart"))
    node = shutil.which("node")
    if node is None:
        return
    script = (
        "let rules=[];const polkit={Result:{YES:'yes',NOT_HANDLED:'no'},addRule:f=>rules.push(f)};"
        + rule
        + ";const a=(u,v)=>({id:'org.freedesktop.systemd1.manage-units',lookup:k=>k==='unit'?u:v});"
        "const s=n=>({user:n});"
        "console.log(JSON.stringify([rules[0](a('mm3.service','restart'),s('mm3-install')),"
        "rules[0](a('ssh.service','restart'),s('mm3-install')),"
        "rules[0](a('mm3.service','enable'),s('mm3-install')),"
        "rules[0](a('mm3.service','stop'),s('mm3'))]))"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)  # noqa: S603
    assert json.loads(out.stdout) == ["yes", "no", "no", "no"]


def test_core_357_cli_prints_unit_and_rule() -> None:
    parser = build_parser()
    for cmd, text in (("unit", unit_text()), ("polkit", polkit_text())):
        out = io.StringIO()
        assert run_installer(parser.parse_args(["installer", cmd]), out=out) == 0
        assert out.getvalue() == text


def test_core_368_install_script_builds_the_installer_venv_and_users() -> None:
    script = SCRIPT.read_text()
    assert '"${pip[@]}" "mm3-core[installer]==$version"' in script
    # Built in place and switched by symlink: a venv cannot be moved.
    assert "new=$root/$stamp" in script
    assert 'mv -T "$link.new" "$link"' in script
    assert "installer.new" not in script
    assert "--no-create-home --home-dir /nonexistent" in script
    assert 'usermod --append --groups "$SVC_GROUP" "$INSTALLER_USER"' in script
    assert 'chown "$INSTALLER_USER:" "$OPT/venvs"' in script
    assert "50-mm3-installer.rules" in script
    assert "polkitd" in script


def test_core_368_installer_accounts_are_created(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool, body in {
        "id": "#!/bin/sh\nexit 1\n",
        "useradd": f'#!/bin/sh\necho "useradd $*" >> {log}\n',
        "usermod": f'#!/bin/sh\necho "usermod $*" >> {log}\n',
    }.items():
        (bin_dir / tool).write_text(body)
        (bin_dir / tool).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [
            shutil.which("bash") or "/bin/bash",
            "-c",
            f'source "{SCRIPT}"\nensure_installer_accounts',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert any("useradd --system --user-group --home-dir /var/lib/mm3-install" in c for c in calls)
    assert any("mm3-build" in c and "--no-create-home" in c for c in calls)
    assert "usermod --append --groups mm3 mm3-install" in calls


async def test_core_353_cli_install_follows_the_operation(served: Served) -> None:
    served.world.publish(("mm3-news", "1.2.0"))
    args = build_parser().parse_args(
        [
            "modules",
            "install",
            "mm3-news",
            "--yes",
            "--socket",
            str(served.path),
            "--state-dir",
            str(served.world.layout.engine_state),
        ]
    )
    out = io.StringIO()
    code = await asyncio.to_thread(
        run_modules,
        args,
        out=out,
        ask=lambda _p: "y",
        secret=lambda p: PASS if "passphrase" in p else "",
    )
    text = out.getvalue()
    assert code == 0, text
    assert "capabilities: http.outbound" in text
    assert "Done: mm3-news - -> 1.2.0" in text


async def test_core_125_cli_search_info_and_list(served: Served) -> None:
    served.world.publish(("mm3-news", "1.2.0"))
    state = str(served.world.layout.engine_state)
    out = io.StringIO()
    args = argparse.Namespace(
        modules_command="search", text=["news"], category=None, state_dir=state, socket=served.path
    )
    assert await asyncio.to_thread(run_modules, args, out=out) == 0
    assert "mm3-news" in out.getvalue()
    assert "1.2.0" in out.getvalue()
    out = io.StringIO()
    args = argparse.Namespace(
        modules_command="info", name="mm3-news", state_dir=state, socket=served.path
    )
    assert await asyncio.to_thread(run_modules, args, out=out) == 0
    assert "check pass" in out.getvalue()
    out = io.StringIO()
    args = argparse.Namespace(modules_command="list", state_dir=state, socket=served.path)
    assert await asyncio.to_thread(run_modules, args, out=out) == 0


def test_core_353_cli_without_installer_exits_2(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["modules", "refresh", "--socket", str(tmp_path / "none.sock")]
    )
    out = io.StringIO()
    assert run_modules(args, out=out) == 2
    assert "does not answer" in out.getvalue()


def test_core_357_ready_means_restart_job_done_and_unit_active(tmp_path: Path) -> None:
    from mm3.installer.service import SystemdEngine

    class Engine(SystemdEngine):
        def __init__(self, restart_rc: int, states: list[str]) -> None:
            super().__init__(Layout.under(tmp_path), DirectRunner(), sleep=lambda _s: None)
            self.restart_rc, self.states, self.calls = restart_rc, states, []

        def _systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[0] == "restart":
                return subprocess.CompletedProcess(args, self.restart_rc, "", "denied")
            return subprocess.CompletedProcess(args, 0, self.states.pop(0), "")

    ok = Engine(0, ["activating", "active"])
    assert ok.restart() is True
    assert ok.calls[0] == ("restart",)  # blocking: waits for READY=1
    assert Engine(1, []).restart() is False  # job failed (TimeoutStartSec, polkit)
    assert Engine(0, ["failed"]).restart() is False


def test_core_357_diag_check_runs_as_the_engine_user(tmp_path: Path) -> None:
    from mm3.installer.service import SystemdEngine

    class Recorder(DirectRunner):
        def run(self, argv, *, user, home, env=None, timeout=900.0):  # type: ignore[no-untyped-def]
            self.calls.append((user, list(argv)))
            from mm3.installer.runner import Completed

            return Completed(1, "fail")

    runner = Recorder()
    layout = Layout.under(tmp_path)
    result = SystemdEngine(layout, runner).diag_check(Path("/opt/mm3/venvs/x"))
    assert result.returncode == 1
    user, argv = runner.calls[0]
    assert user == "mm3"
    assert argv == [
        "/opt/mm3/venvs/x/bin/mm3",
        "diag",
        "check",
        "--socket",
        str(layout.admin_socket),
    ]


# --- core spec 0.36: wheel check and rebuilds before 1.0 -----------------------------


def test_core_371_installer_runs_the_wheel_check_after_the_import_test(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    op = world.run(action="install", name="mm3-news")
    assert op.state == "done"
    assert world.builder.checked == [wheel_name("mm3-news", "1.2.0")]
    world.builder.fail_check = True
    world.publish(("mm3-news", "1.2.0"), ("mm3-news", "1.3.0"), serial=11)
    op = world.run(action="update", name="mm3-news")
    assert op.state == "failed"
    assert "module check" in (op.error or "")
    assert world.builder.current().name != "x"  # type: ignore[union-attr]


def test_core_371_wheel_check_argv(tmp_path: Path) -> None:
    class Recorder(DirectRunner):
        def run(self, argv, *, user, home, env=None, timeout=900.0):  # type: ignore[no-untyped-def]
            from mm3.installer.runner import Completed

            self.calls.append((user, list(argv)))
            return Completed(0, "")

    runner = Recorder()
    builder = VenvBuilder(Layout.under(tmp_path), runner)
    builder.static_check(Path("/opt/mm3/venvs/v"), Path("/srv/x/m.whl"))
    user, argv = runner.calls[0]
    assert user == "mm3-build"
    assert argv == [
        "/opt/mm3/venvs/v/bin/mm3",
        "module",
        "check",
        "--wheel",
        "/srv/x/m.whl",
        "--only",
        "static",
    ]


def test_core_372_interim_rebuild_uses_no_deps_then_constraints(tmp_path: Path) -> None:
    from mm3.installer.runner import Completed
    from mm3.installer.venv import core_is_interim, wheel_requirements

    wheel = tmp_path / wheel_name("mm3-news", "1.2.0")
    wheel.write_bytes(
        make_wheel(
            "mm3-news",
            "1.2.0",
            requires=[
                "mm3-core>=1.0,<2",
                "feedparser>=6",
                "tzdata; sys_platform == 'win32'",
                "pytest; extra == 'test'",
            ],
        )
    )
    assert wheel_requirements(wheel) == ["feedparser>=6"]
    assert core_is_interim(BASE) is True
    assert core_is_interim({"mm3-core": Dist("mm3-core", "1.0.0")}) is False

    class Recorder(DirectRunner):
        def run(self, argv, *, user, home, env=None, timeout=900.0):  # type: ignore[no-untyped-def]
            self.calls.append((user, list(argv)))
            if "-c" in argv:
                self.constraints = Path(argv[argv.index("-c") + 1]).read_text()
            return Completed(0, "")

    runner = Recorder()
    builder = VenvBuilder(Layout.under(tmp_path), runner)
    pins = ["mm3-core==0.9.0", "pydantic==2.9.0", str(wheel)]
    builder.build(pins, NOW, interim=True, wheel=wheel)
    first, second = runner.calls[1][1], runner.calls[2][1]
    assert "--no-deps" in first
    assert first[-3:] == pins
    assert "--no-deps" not in second
    assert second[-1] == "feedparser>=6"
    assert runner.constraints == "mm3-core==0.9.0\npydantic==2.9.0\n"
    runner.calls.clear()
    builder.build(pins, NOW + timedelta(seconds=1), interim=False, wheel=wheel)
    assert len(runner.calls) == 2
    assert "--no-deps" not in runner.calls[1][1]


def test_core_372_operation_builds_interim_while_core_is_0x(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    assert world.run(action="install", name="mm3-news").state == "done"
    assert world.builder.builds[-1]["interim"] is True
    assert world.builder.builds[-1]["wheel"].name == wheel_name("mm3-news", "1.2.0")


# --- security review fixes (core spec 0.37) ------------------------------------------


def test_core_367_freeze_reads_metadata_without_running_python(tmp_path: Path) -> None:
    from mm3.installer.venv import BuildError, read_freeze

    site = tmp_path / "v" / "lib" / "python3.13" / "site-packages"
    good = site / "mm3_news-1.2.0.dist-info"
    good.mkdir(parents=True)
    (good / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: mm3-news\nVersion: 1.2.0\nRequires-Dist: feedparser\n"
    )
    (good / "entry_points.txt").write_text("[mm3.modules]\nnews = mm3_news:Module\n")
    plain = site / "pydantic-2.9.0.dist-info"
    plain.mkdir()
    (plain / "METADATA").write_text("Name: pydantic\nVersion: 2.9.0\n")
    linked = site / "evil-1.dist-info"
    linked.symlink_to(good)  # a symlinked dist-info is ignored
    (tmp_path / "v" / "bin").mkdir()
    (tmp_path / "v" / "bin" / "python").write_text(
        "#!/bin/sh\necho must-not-run > /dev/null\nexit 1\n"
    )
    freeze = read_freeze(tmp_path / "v")
    assert set(freeze) == {"mm3-news", "pydantic"}
    assert freeze["mm3-news"].module
    assert freeze["mm3-news"].requires == ("feedparser",)
    assert not freeze["pydantic"].module
    bad = site / "x-1.dist-info"
    bad.mkdir()
    (bad / "METADATA").write_text("Name: --extra-index-url=https://evil/simple\nVersion: 1\n")
    with pytest.raises(BuildError, match="invalid project name"):
        read_freeze(tmp_path / "v")
    (bad / "METADATA").write_text("Name: x\nVersion: 1 --no-binary=:all:\n")
    with pytest.raises(BuildError, match="invalid version"):
        read_freeze(tmp_path / "v")


def test_core_367_direct_url_requirements_are_refused(tmp_path: Path) -> None:
    from mm3.installer.venv import BuildError, wheel_requirements

    wheel = tmp_path / wheel_name("mm3-url", "1.0.0")
    wheel.write_bytes(make_wheel("mm3-url", "1.0.0", requires=["mm3-core>=1.0", "requests>=2"]))
    assert wheel_requirements(wheel) == ["requests>=2"]
    wheel.write_bytes(
        make_wheel("mm3-url", "1.0.0", requires=["evil @ https://evil.example/e.whl"])
    )
    with pytest.raises(BuildError, match="direct URL"):
        wheel_requirements(wheel)


def test_core_367_surviving_build_process_stops_the_build(tmp_path: Path) -> None:
    from mm3.installer.venv import BuildError

    class Survivor(DirectRunner):
        def reap(self, user: str) -> list[int]:
            return [4242]

    builder = VenvBuilder(Layout.under(tmp_path), Survivor(), python=sys.executable)
    wheel = _real_wheel(tmp_path, "mm3-demo", "1.0.0")
    with pytest.raises(BuildError, match="survived the build"):
        builder.build([str(wheel)], NOW)
    assert builder.kept() == []


def test_core_367_processes_of_reads_proc(tmp_path: Path) -> None:
    from mm3.installer.runner import processes_of

    for pid, uid in ((10, 999), (11, 1000), (12, 999)):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (tmp_path / "self").mkdir()
    assert sorted(processes_of(999, tmp_path)) == [10, 12]


def test_core_367_status_never_starts_children(world: World) -> None:
    world.publish(("mm3-news", "1.2.0"))
    world.run(action="install", name="mm3-news")
    runner = DirectRunner()
    real = VenvBuilder(world.layout, runner)
    current = world.builder.current()
    assert current is not None
    site = current / "lib" / "python3.13" / "site-packages" / "mm3_news-1.2.0.dist-info"
    site.mkdir(parents=True)
    (site / "METADATA").write_text("Name: mm3-news\nVersion: 1.2.0\n")
    (site / "entry_points.txt").write_text("[mm3.modules]\nn = m:M\n")
    assert real.freeze(current)["mm3-news"].module
    assert runner.calls == []


async def test_core_355_non_ascii_content_length_is_400(served: Served) -> None:
    def raw() -> bytes:
        import socket as sk

        with sk.socket(sk.AF_UNIX, sk.SOCK_STREAM) as c:
            c.connect(str(served.path))
            c.sendall("POST /v1/operations HTTP/1.1\r\nContent-Length: ²\r\n\r\n".encode())
            return c.recv(200)

    answer = await asyncio.to_thread(raw)
    assert answer.startswith(b"HTTP/1.1 400")
