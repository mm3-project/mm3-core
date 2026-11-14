"""Milestone 7, closing step 3: a removal disables the module's enabled instances first.

Implements:
- CORE-358 (Core spec 0.44, §10.7 Installer service): before a removal is forwarded to the
  installer, the engine (admin) or the CLI (through the admin socket) saves a configuration
  history version that sets ``disabled: true`` on every enabled instance of the module; with no
  enabled instance no version is written; the version stays if the removal fails and can be
  undone; with no engine on the socket ``mm3 modules remove`` stops with an error.
- CORE-31 (Core spec 0.5, §6.2 Rules): the instances are found in the main file, in an
  ``!include`` of the whole ``modules`` list and in an ``!include`` of one instance.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from mm3.cli.main import build_parser
from mm3.cli.modules_cmd import run_modules
from mm3.core.config import load_config
from mm3.core.config.editor import ConfigEditor, EditorError
from mm3.core.config.history import ConfigHistory
from mm3.core.config.manager import ConfigManager
from mm3.core.config.removal import disable_ops, module_instances
from mm3.core.diag.secrets import Masker, config_secrets
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.installer import InstallerLink
from mm3.installer.client import Reply
from mm3.module import MirrorModule
from mm3.testing import VirtualClock
from test_admin_catalog import OP_A, PASSPHRASE, ScriptedInstaller
from test_admin_config_api import ADMIN, LAYOUT, SECRET, Keyed, Panel, problem

MAIN = f"""# The mirror
engine:
  locale: en  # language
layout: !include layout.yaml
modules:
  - module: keyed  # the first
    id: k
    config:
      api_key: {SECRET}
  - module: keyed
    id: k2
    disabled: true
    config:
      api_key: {SECRET}
  - module: panel
    id: p
    position: top_left
  - !include extra.yaml
"""
EXTRA = f"module: keyed\nid: k3\nconfig:\n  api_key: {SECRET}\n"


class Spare(MirrorModule):
    name = "spare"


# --- the pure part ---------------------------------------------------------------------------


def docs(**files: Any) -> dict[str, Any]:
    return {name.replace("__", "/").replace("_yaml", ".yaml"): doc for name, doc in files.items()}


def test_core_358_disable_ops_one_add_per_enabled_instance() -> None:
    documents = {
        "config.yaml": {
            "modules": [
                {"module": "keyed", "id": "a"},
                {"module": "keyed", "id": "b", "disabled": True},
                {"module": "keyed"},
                {"module": "keyed", "id": "d", "disabled": False},
                {"module": "other", "id": "e"},
                {"module": {"!env": ["M", "keyed"]}, "id": "f"},
            ]
        }
    }
    ops, ids = disable_ops(documents, "config.yaml", ["keyed"])
    assert ids == ["a", "keyed", "d"]  # the id is the module name without an id key (CORE-202)
    assert ops == [
        {
            "file": "config.yaml",
            "patch": [
                {"op": "add", "path": "/modules/0/disabled", "value": True},
                {"op": "add", "path": "/modules/2/disabled", "value": True},
                {"op": "add", "path": "/modules/3/disabled", "value": True},
            ],
        }
    ]


def test_core_358_no_enabled_instance_no_ops() -> None:
    documents = {"config.yaml": {"modules": [{"module": "keyed", "disabled": True}]}}
    assert disable_ops(documents, "config.yaml", ["keyed"]) == ([], [])
    assert disable_ops(documents, "config.yaml", ["nothing"]) == ([], [])
    assert disable_ops({"config.yaml": {}}, "config.yaml", ["keyed"]) == ([], [])
    assert disable_ops({"config.yaml": None}, "config.yaml", ["keyed"]) == ([], [])


def test_core_31_instances_are_followed_through_includes() -> None:
    documents = {
        "config.yaml": {"modules": {"!include": "mods/all.yaml"}},
        "mods/all.yaml": [
            {"module": "keyed", "id": "a"},
            {"!include": "one.yaml"},
            {"!include": "../elsewhere.yaml"},
        ],
        "mods/one.yaml": {"module": "keyed", "id": "b"},
        "elsewhere.yaml": {"module": "keyed", "id": "c"},
    }
    ops, ids = disable_ops(documents, "config.yaml", ["keyed"])
    assert ids == ["a", "b", "c"]
    assert ops == [
        {"file": "mods/all.yaml", "patch": [{"op": "add", "path": "/0/disabled", "value": True}]},
        {"file": "mods/one.yaml", "patch": [{"op": "add", "path": "/disabled", "value": True}]},
        {"file": "elsewhere.yaml", "patch": [{"op": "add", "path": "/disabled", "value": True}]},
    ]


def test_core_31_an_include_loop_ends_at_the_nesting_limit() -> None:
    documents = {
        "config.yaml": {"modules": [{"!include": "a.yaml"}]},
        "a.yaml": {"!include": "a.yaml"},
    }
    assert module_instances(documents, "config.yaml", ["keyed"]) == []


# --- the engine ------------------------------------------------------------------------------


@dataclass
class Rig:
    root: Path
    history: ConfigHistory
    editor: ConfigEditor
    services: ApiServices
    client: httpx.AsyncClient
    installer: Probe

    def main(self) -> str:
        return (self.root / "config.yaml").read_text()

    def versions(self) -> list[int]:
        return list(self.history.store.versions())


class Probe(ScriptedInstaller):
    """The scripted installer that notes the configuration versions at each operation."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[int]] = []
        self.history: ConfigHistory | None = None

    def request(self, method: str, target: str, body: Any = None, **kw: Any) -> Reply:
        if (method, target) == ("POST", "/v1/operations") and self.history is not None:
            self.seen.append(list(self.history.store.versions()))
        return super().request(method, target, body, **kw)

    def events(self, op_id: str) -> Iterator[dict[str, Any]]:
        yield {"state": "queued", "detail": "queued"}
        yield {"state": "done", "detail": "removed"}


def registry() -> Registry:
    reg = Registry()
    reg.add(Keyed, distribution="MM3_Keyed")  # the installer says mm3-keyed (PEP 503)
    reg.add(Panel, distribution="mm3-panel")
    reg.add(Spare, distribution="mm3-spare")
    return reg


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    configure_logging("info", stream=io.StringIO(), json=True)
    root = tmp_path / "etc"
    root.mkdir()
    (root / "config.yaml").write_text(MAIN)
    (root / "layout.yaml").write_text(LAYOUT)
    (root / "extra.yaml").write_text(EXTRA)
    env = tmp_path / "mm3.env"
    env.write_text("")
    env.chmod(0o600)
    reg = registry()
    result = load_config(root / "config.yaml", registry=reg, env_file=env, environ={})
    assert result.ok, result.records
    history = ConfigHistory(
        root / "config.yaml", env_file=env, registry=reg, state_dir=tmp_path / "state", environ={}
    )
    with history.store.lock():
        history.record_disk()
    secrets = config_secrets(result, env, environ={})
    editor = ConfigEditor(history, lambda: Masker(secrets))
    engine = Engine(
        EngineSettings(
            timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
        ),
        registry=reg,
        clock=VirtualClock(),
        state_dir=tmp_path / "state",
    )
    manager = ConfigManager(engine, result, registry=reg, env_file=env, environ={})
    services = ApiServices(engine, manager, None, None, None, "1.0+t", editor=editor)
    probe = Probe()
    probe.history = history
    services.installer = InstallerLink(probe)  # type: ignore[arg-type]
    auth = WebAuth.open(tmp_path / "web")
    token = auth.tokens.new("cfg")
    app = protect(admin_app(auth, api=services), lambda: [], "admin")
    await engine.start(result.instance_specs())
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(
        transport=transport, base_url=ADMIN, headers={"authorization": f"Bearer {token}"}
    ) as client:
        try:
            yield Rig(root, history, editor, services, client, probe)
        finally:
            await engine.stop()


async def remove(rig: Rig, name: str = "mm3-keyed") -> httpx.Response:
    body = {"action": "remove", "name": name, "passphrase": PASSPHRASE}
    return await rig.client.post("/api/v1/installer/operations", json=body)


def test_core_358_the_editor_disables_every_enabled_instance(rig: Rig) -> None:
    saved = rig.editor.disable_instances(["keyed"], "127.0.0.1")
    assert saved is not None
    assert saved["version"] == 2
    assert saved["instances"] == ["k", "k3"]  # k2 was disabled already
    main = rig.main()
    first, second = main.split("- module: keyed\n")  # the second instance starts the split
    assert "- module: keyed  # the first" in first
    assert first.count("disabled: true") == 1  # k is now disabled; k2 keeps its own
    assert second.count("disabled: true") == 1
    assert "# The mirror" in main  # comments and the rest are kept
    assert "layout: !include layout.yaml" in main
    assert "- !include extra.yaml" in main
    assert "disabled: true" in (rig.root / "extra.yaml").read_text()
    assert "disabled" not in main.split("module: panel")[1]  # the panel is another package
    meta = rig.history.store.meta(2)
    assert meta.author == "127.0.0.1"
    assert meta.sections == ["modules/k", "modules/k3"]
    assert f"api_key: {SECRET}" in main  # no secret was masked into the file


def test_core_358_no_enabled_instance_writes_no_version(rig: Rig) -> None:
    assert rig.editor.instances_to_disable(["spare"]) == []
    assert rig.editor.disable_instances(["spare"], "127.0.0.1") is None
    assert rig.versions() == [1]
    assert rig.editor.disable_instances(["keyed"], "me") is not None
    assert rig.editor.instances_to_disable(["keyed"]) == []
    assert rig.editor.disable_instances(["keyed"], "me") is None  # the second time: nothing
    assert rig.versions() == [1, 2]


def test_core_358_a_result_that_does_not_validate_writes_nothing(rig: Rig) -> None:
    (rig.root / "extra.yaml").write_text("module: keyed\nid: p\n")  # a duplicate id
    with pytest.raises(EditorError) as raised:
        rig.editor.disable_instances(["keyed"], "me")
    assert raised.value.code in ("CORE_API_DISK_INVALID", "CORE_API_CONFIG_INVALID")
    assert rig.versions() == [1]


async def test_core_358_the_version_comes_before_the_installer_operation(rig: Rig) -> None:
    r = await remove(rig)
    assert r.status_code == 202, r.text
    assert r.json() == {"id": OP_A}
    assert rig.installer.seen == [[1, 2]]  # the version existed when the operation was sent
    method, target, body, _ = rig.installer.calls[-1]
    assert (method, target) == ("POST", "/v1/operations")
    assert body is not None
    assert body["action"] == "remove"
    assert body["name"] == "mm3-keyed"
    meta = rig.history.store.meta(2)
    assert meta.origin == "admin"
    assert meta.author == "cfg (bearer) 127.0.0.1"
    assert rig.main().count("disabled: true") == 2  # k and k2


async def test_core_358_the_version_can_be_undone(rig: Rig) -> None:
    assert (await remove(rig)).status_code == 202
    etag = (await rig.client.get("/api/v1/config")).json()["etag"]
    undo = await rig.client.post("/api/v1/config/undo", headers={"If-Match": etag})
    assert undo.status_code == 200, undo.text
    assert undo.json()["version"] == 3
    assert rig.main() == MAIN
    assert (rig.root / "extra.yaml").read_text() == EXTRA


async def test_core_358_the_version_stays_when_the_installer_refuses(rig: Rig) -> None:
    rig.installer.replies[("POST", "/v1/operations")] = Reply(403, {"error": "bad passphrase"})
    r = await remove(rig)
    problem(r, 403, "CORE_API_REAUTH_FAILED")
    assert rig.versions() == [1, 2]
    assert "disabled: true" in rig.main()


@pytest.mark.parametrize(
    ("status", "code", "extra"),
    [
        ({"busy": True}, "CORE_API_INSTALLER_BUSY", 409),
        ({"locked_until": "2026-11-14T12:00:00+00:00"}, "CORE_API_INSTALLER_LOCKED", 423),
    ],
)
async def test_core_358_a_busy_or_locked_installer_changes_nothing(
    rig: Rig, status: dict[str, Any], code: str, extra: int
) -> None:
    rig.installer.status.update(status)
    r = await remove(rig)
    problem(r, extra, code)
    assert rig.versions() == [1]
    assert rig.main() == MAIN
    assert rig.installer.seen == []  # nothing was sent


async def test_core_358_an_unreachable_installer_changes_nothing(rig: Rig) -> None:
    rig.installer.down = True
    problem(await remove(rig), 503, "CORE_API_INSTALLER_UNAVAILABLE")
    assert rig.versions() == [1]


async def test_core_358_no_instance_no_version(rig: Rig) -> None:
    for package in ("mm3-spare", "mm3-unknown"):  # no instance; no module from that package
        assert (await remove(rig, package)).status_code == 202
    assert rig.versions() == [1]
    assert rig.installer.seen == [[1], [1]]


async def test_core_358_only_removals_disable_instances(rig: Rig) -> None:
    for action in ("update", "install"):
        body = {"action": action, "name": "mm3-keyed", "passphrase": PASSPHRASE}
        r = await rig.client.post("/api/v1/installer/operations", json=body)
        assert r.status_code == 202, r.text
    assert rig.versions() == [1]


async def test_core_358_the_distribution_name_is_compared_canonically(rig: Rig) -> None:
    assert (await remove(rig, "MM3.Keyed")).status_code == 202
    assert rig.versions() == [1, 2]


# --- the CLI ---------------------------------------------------------------------------------


@dataclass
class FakeAdmin:
    """The admin socket of an engine: the routes the CLI uses, on the real editor."""

    rig: Rig
    path: Path = Path("/run/mm3/admin.sock")
    down: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    def request(
        self,
        method: str,
        target: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        if self.down:
            raise OSError(f"cannot connect to {self.path}")
        self.calls.append((method, target))
        editor = self.rig.editor
        try:
            if (method, target) == ("GET", "/api/v1/installed-modules"):
                answer: Any = [
                    {"name": "keyed", "distribution": "MM3_Keyed"},
                    {"name": "panel", "distribution": "mm3-panel"},
                    {"name": "spare", "distribution": "mm3-spare"},
                ]
            elif (method, target) == ("GET", "/api/v1/config"):
                answer = editor.view()
            elif (method, target) == ("POST", "/api/v1/config/patch"):
                answer = editor.patch(body["base"], body["files"], body["ops"])
            elif (method, target) == ("PUT", "/api/v1/config"):
                answer = editor.save(
                    (headers or {})["If-Match"], body["base"], body["files"], "cli"
                )
            else:
                raise AssertionError(f"unscripted {method} {target}")
        except EditorError as exc:
            return 409, json.dumps({"code": exc.code, "detail": str(exc)}).encode()
        return 200, json.dumps(answer).encode()


@dataclass
class Cli:
    rig: Rig
    admin: FakeAdmin
    out: io.StringIO = field(default_factory=io.StringIO)

    def run(self, name: str = "mm3-keyed", *, answer: str = "y", yes: bool = True) -> int:
        argv = ["modules", "remove", name, "--socket", "/x/installer.sock"]
        args = build_parser().parse_args(argv + (["--yes"] if yes else []))
        return run_modules(
            args,
            out=self.out,
            ask=lambda _p: answer,
            secret=lambda p: PASSPHRASE if "passphrase" in p else "",
        )


@pytest.fixture
def cli(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Cli:
    admin = FakeAdmin(rig)
    rig.installer.status["installed"] = [{"name": "mm3-keyed", "version": "1.0.0"}]
    rig.installer.operations.append(
        {"id": OP_A, "state": "done", "name": "mm3-keyed", "from": "1.0.0", "to": None}
    )
    monkeypatch.setattr("mm3.cli.modules_cmd.SocketClient", lambda _path: admin)
    monkeypatch.setattr("mm3.cli.modules_cmd.InstallerClient", lambda *_a, **_k: rig.installer)
    return Cli(rig, admin)


async def test_core_358_cli_disables_the_instances_then_removes(rig: Rig, cli: Cli) -> None:
    code = await asyncio.to_thread(cli.run)
    text = cli.out.getvalue()
    assert code == 0, text
    assert "2 enabled instance(s) will be disabled, not deleted: k, k3" in text
    assert "Saved configuration version 2" in text
    assert "Done: mm3-keyed 1.0.0" in text
    assert rig.installer.seen == [[1, 2]]  # the version was saved before the operation
    (body,) = [b for m, t, b, _ in rig.installer.calls if (m, t) == ("POST", "/v1/operations")]
    assert body is not None
    assert body["action"] == "remove"
    assert body["passphrase"] == PASSPHRASE
    assert rig.history.store.meta(2).author == "cli"
    assert rig.main().count("disabled: true") == 2
    assert SECRET not in text


async def test_core_358_cli_stops_without_an_engine(rig: Rig, cli: Cli) -> None:
    cli.admin.down = True
    code = await asyncio.to_thread(cli.run)
    text = cli.out.getvalue()
    assert code == 2, text
    assert "does not answer" in text
    assert "Nothing was changed" in text
    assert rig.installer.seen == []
    assert rig.versions() == [1]


async def test_core_358_cli_changes_nothing_when_not_confirmed(rig: Rig, cli: Cli) -> None:
    code = await asyncio.to_thread(cli.run, answer="n", yes=False)
    assert code == 1
    assert rig.installer.seen == []
    assert rig.versions() == [1]


@pytest.mark.parametrize("status", [{"busy": True}, {"locked_until": "2026-11-14T12:00:00+00:00"}])
async def test_core_358_cli_changes_nothing_when_the_installer_cannot_start(
    rig: Rig, cli: Cli, status: dict[str, Any]
) -> None:
    rig.installer.status.update(status)
    code = await asyncio.to_thread(cli.run)
    assert code == 1, cli.out.getvalue()
    assert rig.installer.seen == []
    assert rig.versions() == [1]


async def test_core_358_cli_refuses_a_module_that_is_not_installed(rig: Rig, cli: Cli) -> None:
    code = await asyncio.to_thread(cli.run, "mm3-nothing")
    assert code == 1
    assert "is not installed" in cli.out.getvalue()
    assert rig.versions() == [1]


async def test_core_358_cli_without_instances_writes_no_version(rig: Rig, cli: Cli) -> None:
    rig.installer.status["installed"] = [{"name": "mm3-spare", "version": "1.0.0"}]
    code = await asyncio.to_thread(cli.run, "mm3-spare")
    assert code == 0, cli.out.getvalue()
    assert "disabled, not deleted" not in cli.out.getvalue()
    assert rig.versions() == [1]
    assert rig.installer.seen == [[1]]
