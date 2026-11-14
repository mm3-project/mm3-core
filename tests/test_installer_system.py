"""Tests of the installer's restart and reboot routes (CORE-382).

Implements:
- CORE-382 (Core spec 0.42, §9.1): ``POST /v1/system/<action>`` answers 202, waits, runs the
  command through the polkit rule, and refuses with 409 while busy or without a kiosk unit.
- CORE-357 (Core spec 0.42, §10.7) and CORE-359 (Core spec 0.42, §10.8): the polkit rule lets
  ``mm3-install`` restart the kiosk unit and reboot, and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mm3.cli.installer_cmd import polkit_text
from mm3.installer.client import InstallerClient
from mm3.installer.operations import Request
from mm3.installer.server import InstallerServer
from mm3.installer.system import ACTIONS, SystemdActions
from test_installer_operations import PASS, World


class FakeSystem:
    def __init__(self, *, kiosk: bool = True, ok: bool = True) -> None:
        self.kiosk = kiosk
        self.ok = ok
        self.runs: list[str] = []

    def kiosk_installed(self) -> bool:
        return self.kiosk

    def run(self, action: str) -> bool:
        self.runs.append(action)
        return self.ok


class SystemServed:
    def __init__(self, world: World, system: FakeSystem, delay: float) -> None:
        self.world = world
        self.system = system
        self.server = InstallerServer(
            world.installer, world.auth, peer=lambda _s: 1000, system=system, system_delay=delay
        )
        self.path = world.layout.socket
        self.client = InstallerClient(self.path, timeout=10)

    async def post(self, action: str, body: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(
            self.client.request, "POST", f"/v1/system/{action}", body or {}
        )


async def _serve(world: World, system: FakeSystem, delay: float) -> AsyncIterator[SystemServed]:
    s = SystemServed(world, system, delay)
    task = asyncio.create_task(s.server.serve(s.path, refresh=False))
    for _ in range(100):
        if s.path.exists():
            break
        await asyncio.sleep(0.01)
    yield s
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
async def served(world: World) -> AsyncIterator[SystemServed]:
    async for s in _serve(world, FakeSystem(), 0.05):
        yield s


@pytest.fixture
async def waiting(world: World) -> AsyncIterator[SystemServed]:
    """A long delay, so the action stays waiting to run."""
    async for s in _serve(world, FakeSystem(), 30.0):
        yield s


async def _until(check: Any) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.mark.parametrize("action", ACTIONS)
async def test_core_382_answers_202_first_then_runs_after_the_delay(
    served: SystemServed, action: str, tmp_path: Path
) -> None:
    reply = await served.post(action, {"client": "token ci (10.0.0.9)"})
    assert reply.status == 202, reply.body
    assert reply.body["action"] == action
    assert reply.body["requested_at"].endswith("+00:00")
    assert served.system.runs == []  # the answer came before the command
    await _until(lambda: served.system.runs == [action])
    await _until(lambda: not served.world.installer.busy)
    lines = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    mine = [r for r in lines if r.get("event") == "system_action"]
    assert len(mine) == 1
    assert mine[0]["level"] == "warning"
    assert mine[0]["client"] == "token ci (10.0.0.9)"
    assert mine[0]["action"] == action


async def test_core_382_a_failed_command_is_logged_and_frees_the_installer(
    world: World, tmp_path: Path
) -> None:
    system = FakeSystem(ok=False)
    async for s in _serve(world, system, 0.01):
        assert (await s.post("reboot")).status == 202
        await _until(lambda: not world.installer.busy and system.runs == ["reboot"])
        text = (tmp_path / "log.jsonl").read_text()
        assert "system_action_failed" in text


async def test_core_382_the_client_defaults_to_the_peer_and_must_be_text(
    served: SystemServed, tmp_path: Path
) -> None:
    assert (await served.post("restart-engine", {"client": 5})).status == 400
    assert (await served.post("restart-engine", {"client": "x" * 201})).status == 400
    assert not served.world.installer.busy
    assert (await served.post("restart-engine")).status == 202
    await _until(lambda: served.system.runs == ["restart-engine"])
    text = (tmp_path / "log.jsonl").read_text()
    assert '"client": "uid 1000"' in text


async def test_core_382_a_second_action_while_one_waits_is_409_busy(
    waiting: SystemServed,
) -> None:
    assert (await waiting.post("restart-engine")).status == 202
    assert waiting.world.installer.busy
    second = await waiting.post("reboot")
    assert second.status == 409
    assert second.body["code"] == "busy"
    status = await asyncio.to_thread(waiting.client.request, "GET", "/v1/status")
    assert status.body["busy"] is True
    assert waiting.system.runs == []


async def test_core_382_an_operation_blocks_the_actions_and_they_block_operations(
    waiting: SystemServed,
) -> None:
    world = waiting.world
    request = Request(action="restore_previous", client="test", peer_uid=1000)
    op = world.installer.submit(request)  # an operation is running, never cut short
    busy = await waiting.post("reboot")
    assert busy.status == 409
    assert busy.body["code"] == "busy"
    assert waiting.system.runs == []
    world.installer.run(op, request)
    assert (await waiting.post("reboot")).status == 202
    refused = await asyncio.to_thread(
        waiting.client.request,
        "POST",
        "/v1/operations",
        {"action": "restore_previous", "passphrase": PASS},
    )
    assert refused.status == 409  # a waiting system action holds the installer


async def test_core_382_restart_kiosk_without_the_unit_is_409_no_kiosk(world: World) -> None:
    async for s in _serve(world, FakeSystem(kiosk=False), 0.01):
        reply = await s.post("restart-kiosk")
        assert reply.status == 409
        assert reply.body["code"] == "no_kiosk"
        assert not world.installer.busy
        assert (await s.post("restart-engine")).status == 202  # the engine needs no kiosk


async def test_core_382_unknown_action_is_404(served: SystemServed) -> None:
    assert (await served.post("poweroff")).status == 404


async def test_core_382_without_system_actions_the_routes_are_404(world: World) -> None:
    server = InstallerServer(world.installer, world.auth, peer=lambda _s: 1000)
    path = world.layout.socket
    task = asyncio.create_task(server.serve(path, refresh=False))
    await _until(path.exists)
    client = InstallerClient(path, timeout=10)
    reply = await asyncio.to_thread(client.request, "POST", "/v1/system/reboot", {})
    assert reply.status == 404
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_core_382_systemd_actions_run_only_these_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        out = "mm3-kiosk.service  enabled  enabled\n" if "list-unit-files" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    system = SystemdActions()
    assert system.kiosk_installed()
    for action in ACTIONS:
        assert system.run(action)
    tails = [c[2:] for c in calls if "list-unit-files" not in c]
    assert tails == [
        ["restart", "mm3.service"],
        ["restart", "mm3-kiosk.service"],
        ["reboot"],
    ]
    assert all(c[0] == "/usr/bin/systemctl" and "--no-ask-password" in c for c in calls[1:])
    with pytest.raises(KeyError):
        system.run("poweroff")


def test_core_382_a_missing_kiosk_unit_or_failing_systemctl_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1 if "restart" in argv else 0, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    system = SystemdActions()
    assert system.kiosk_installed() is False  # 0 units listed
    assert system.run("restart-engine") is False

    def timeout(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert system.run("reboot") is False
    assert system.kiosk_installed() is False


_HEAD = "let rules=[];const polkit={Result:{YES:'yes',NOT_HANDLED:'no'},addRule:f=>rules.push(f)};"
_TAIL = (
    "const a=(id,u,v)=>({id,lookup:k=>k==='unit'?u:v});"
    "const s=n=>({user:n});"
    "const M='org.freedesktop.systemd1.manage-units';"
    "const run=(x,y)=>rules[0](x,y);"
)


_CASES = [
    ("restart mm3.service", "run(a(M,'mm3.service','restart'),s('mm3-install'))", "yes"),
    ("start mm3.service", "run(a(M,'mm3.service','start'),s('mm3-install'))", "yes"),
    ("stop mm3.service", "run(a(M,'mm3.service','stop'),s('mm3-install'))", "yes"),
    ("restart kiosk", "run(a(M,'mm3-kiosk.service','restart'),s('mm3-install'))", "yes"),
    ("stop kiosk", "run(a(M,'mm3-kiosk.service','stop'),s('mm3-install'))", "no"),
    ("start kiosk", "run(a(M,'mm3-kiosk.service','start'),s('mm3-install'))", "no"),
    ("restart ssh", "run(a(M,'ssh.service','restart'),s('mm3-install'))", "no"),
    ("enable mm3", "run(a(M,'mm3.service','enable'),s('mm3-install'))", "no"),
    ("reboot", "run(a('org.freedesktop.login1.reboot'),s('mm3-install'))", "yes"),
    (
        "reboot with sessions",
        "run(a('org.freedesktop.login1.reboot-multiple-sessions'),s('mm3-install'))",
        "yes",
    ),
    ("poweroff", "run(a('org.freedesktop.login1.power-off'),s('mm3-install'))", "no"),
    ("halt", "run(a('org.freedesktop.login1.halt'),s('mm3-install'))", "no"),
    ("another user: reboot", "run(a('org.freedesktop.login1.reboot'),s('mm3'))", "no"),
    ("another user: kiosk", "run(a(M,'mm3-kiosk.service','restart'),s('mm3-kiosk'))", "no"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_359_polkit_rule_covers_kiosk_restart_and_reboot_only() -> None:
    cases = ",".join(c[1] for c in _CASES)
    script = _HEAD + polkit_text() + ";" + _TAIL + f"console.log(JSON.stringify([{cases}]))"
    out = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", "-e", script], capture_output=True, text=True, check=True
    )
    answers = json.loads(out.stdout)
    assert answers == [c[2] for c in _CASES], [
        (c[0], got) for c, got in zip(_CASES, answers, strict=True) if c[2] != got
    ]


def test_core_359_polkit_rule_names_both_logind_reboot_actions() -> None:
    rule = polkit_text()
    assert "org.freedesktop.login1.reboot" in rule
    assert "org.freedesktop.login1.reboot-multiple-sessions" in rule
    assert "mm3-kiosk.service" in rule
