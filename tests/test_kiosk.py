"""Milestone 3, part E: kiosk session, agent and display (core spec 6.7)."""

from __future__ import annotations

import asyncio
import io
import json
import stat
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import websockets

from mm3.cli.kiosk_cmd import run_kiosk, unit_text
from mm3.cli.main import main
from mm3.core.config import load_config
from mm3.core.config.manager import ConfigManager
from mm3.core.engine import Engine
from mm3.core.kiosk import (
    CHROMIUM_FLAGS,
    MIN_CAGE,
    CageError,
    Mode,
    Output,
    apply_command,
    cage_version,
    choose_mode,
    chromium_command,
    parse_cage_version,
    parse_outputs,
)
from mm3.core.kiosk.agent import KioskSession
from mm3.core.kiosk.randr import pick_output
from mm3.core.logging import recent_records
from mm3.core.registry import Registry
from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import WebGateway
from mm3.core.web.kiosk import AgentKey, KioskAgents
from mm3.core.web.view import cached_view
from mm3.core.web.ws import ClientHub
from mm3.messages import Notification
from mm3.testing import VirtualClock

# --- CORE-143: cage version and the unit -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("cage version 0.1.5+20240127\n", (0, 1, 5)),
        ("cage 0.2.0", (0, 2, 0)),
        ("0.1.5-1~bpo12", (0, 1, 5)),
    ],
)
def test_core_143_reads_the_leading_version(text: str, version: tuple[int, int, int]) -> None:
    assert parse_cage_version(text) == version


def test_core_143_unreadable_version() -> None:
    with pytest.raises(CageError, match="cannot read"):
        parse_cage_version("cage: unknown option")


def fake_binary(tmp_path: Path, name: str, output: str, code: int = 0) -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_core_143_cage_check(tmp_path: Path) -> None:
    assert cage_version(fake_binary(tmp_path, "cage", "cage version 0.1.5+20240127")) == MIN_CAGE
    with pytest.raises(CageError, match="too old"):
        cage_version(fake_binary(tmp_path, "old", "cage version 0.1.4"))
    with pytest.raises(CageError, match="cannot run"):
        cage_version(str(tmp_path / "missing"))


def test_core_143_unit_file() -> None:
    unit = unit_text()
    for line in (
        "User=mm3-kiosk",
        "SupplementaryGroups=video input render",
        "PAMName=login",
        "TTYPath=/dev/tty1",
        "ReadOnlyPaths=/etc/mm3",
        "InaccessiblePaths=-/var/lib/mm3",
    ):
        assert line in unit.splitlines()
    assert any(
        line.startswith("ExecStart=/usr/bin/cage -- ") and line.endswith("mm3 kiosk session")
        for line in unit.splitlines()
    )
    out = io.StringIO()
    assert run_kiosk(_args("unit"), out) == 0
    assert out.getvalue() == unit


def test_core_143_session_refuses_without_cage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["kiosk", "session", "--key-file", str(tmp_path / "k")]) == 1


# --- CORE-270: Chromium --------------------------------------------------------------------


REQUIRED_FLAGS = (
    "--kiosk",
    "--ozone-platform=wayland",
    "--no-first-run",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--disable-translate",
    "--disable-pinch",
    "--overscroll-history-navigation=0",
    "--autoplay-policy=no-user-gesture-required",
)


def test_core_270_chromium_command(tmp_path: Path) -> None:
    assert set(REQUIRED_FLAGS) <= set(CHROMIUM_FLAGS)
    cmd = chromium_command("http://127.0.0.1:8080/#kiosk=k", tmp_path / "p", "chromium")
    assert cmd[0] == "chromium"
    assert f"--user-data-dir={tmp_path / 'p'}" in cmd
    assert cmd[-1] == "http://127.0.0.1:8080/#kiosk=k"
    out = io.StringIO()
    args = _args("session", print_command=True, profile=tmp_path / "p")
    assert run_kiosk(args, out) == 0
    printed = out.getvalue().split()
    assert printed[-1] == "http://127.0.0.1:8080/#kiosk=<key>"
    assert set(REQUIRED_FLAGS) <= set(printed)


def _args(command: str, **extra: Any) -> Any:
    import argparse

    defaults = {
        "kiosk_command": command,
        "url": "http://127.0.0.1:8080",
        "key_file": Path("/nonexistent"),
        "profile": None,
        "chromium": "chromium",
        "print_command": False,
    }
    return argparse.Namespace(**{**defaults, **extra})


# --- CORE-144, CORE-145: outputs and modes ------------------------------------------------------

RANDR = [
    {
        "name": "HDMI-A-1",
        "enabled": True,
        "modes": [
            {"width": 3840, "height": 2160, "refresh": 30.0, "preferred": True, "current": False},
            {"width": 1920, "height": 1080, "refresh": 60.0, "preferred": False, "current": True},
            {"width": 1920, "height": 1080, "refresh": 50.0, "preferred": False, "current": False},
            {"width": 1280, "height": 1024, "refresh": 60.0, "preferred": False, "current": False},
            {"width": 1280, "height": 720, "refresh": 60.0, "preferred": False, "current": False},
        ],
        "transform": "normal",
        "scale": 1.0,
    },
    {"name": "DP-1", "enabled": False, "modes": []},
]


def test_core_144_parse_outputs() -> None:
    hdmi, dp = parse_outputs(json.dumps(RANDR))
    assert hdmi.name == "HDMI-A-1"
    assert hdmi.current == Mode(1920, 1080, 60.0)
    assert hdmi.preferred == Mode(3840, 2160, 30.0)
    assert len(hdmi.modes) == 5
    assert dp.current is None
    assert not dp.enabled
    assert hdmi.as_dict()["current"] == {"width": 1920, "height": 1080, "refresh": 60.0}
    assert pick_output([hdmi, dp], None) is hdmi
    assert pick_output([hdmi, dp], "DP-1") is dp
    assert pick_output([hdmi, dp], "VGA-1") is None
    with pytest.raises(ValueError, match="list"):
        parse_outputs("{}")


@pytest.mark.parametrize(
    ("want", "mode"),
    [
        ((1920, 1080, None), Mode(1920, 1080, 60.0)),  # exact, highest refresh
        ((1920, 1080, 50), Mode(1920, 1080, 50.0)),  # exact, closest refresh
        ((2560, 1440, None), Mode(1920, 1080, 60.0)),  # same 16:9, largest not above
        ((1280, 960, None), Mode(1280, 720, 60.0)),  # no 4:3: largest not above
        ((640, 480, None), Mode(1280, 720, 60.0)),  # all above: the smallest
        ((None, None, None), Mode(3840, 2160, 30.0)),  # preferred mode
    ],
)
def test_core_145_closest_mode(want: tuple[Any, Any, Any], mode: Mode) -> None:
    hdmi = parse_outputs(json.dumps(RANDR))[0]
    assert choose_mode(hdmi, *want) == mode


def test_core_145_no_modes() -> None:
    assert choose_mode(Output("X"), 1920, 1080) is None


def test_core_143_apply_command() -> None:
    hdmi = parse_outputs(json.dumps(RANDR))[0]
    assert apply_command(hdmi, Mode(1920, 1080, 60.0), 90, 1.5) == [
        "wlr-randr",
        "--output",
        "HDMI-A-1",
        "--on",
        "--mode",
        "1920x1080@60Hz",
        "--transform",
        "90",
        "--scale",
        "1.5",
    ]


# --- CORE-268: the agent key ------------------------------------------------------------------


def test_core_268_agent_key_file(tmp_path: Path) -> None:
    key = AgentKey(tmp_path / "run")
    path = key.write()
    assert path.read_text().strip() == key.value
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert key.valid(f"Bearer {key.value}")
    assert not key.valid("Bearer nope")
    assert not key.valid(key.value)
    assert not key.valid(None)


def _group_warnings() -> list[dict[str, Any]]:
    return [r for r in recent_records() if r.get("event") == "kiosk_key_group"]


def test_core_268_missing_group_warns_and_keeps_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_group(name: str) -> Any:
        raise KeyError(name)

    monkeypatch.setattr("mm3.core.web.kiosk.grp.getgrnam", no_group)
    before = len(_group_warnings())
    key = AgentKey(tmp_path / "run")
    path = key.write()
    assert path.read_text().strip() == key.value
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    warnings = _group_warnings()
    assert len(warnings) == before + 1
    assert warnings[-1]["group"] == "mm3-kiosk"
    assert warnings[-1]["level"] == "warning"


def test_core_268_failed_group_change_warns_and_keeps_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Group:
        gr_gid = 4242

    def refuse(*_args: Any) -> None:
        raise PermissionError("not a member")

    monkeypatch.setattr("mm3.core.web.kiosk.grp.getgrnam", lambda name: Group())
    monkeypatch.setattr("mm3.core.web.kiosk.os.chown", refuse)
    before = len(_group_warnings())
    key = AgentKey(tmp_path / "run")
    path = key.write()
    assert path.exists()
    warnings = _group_warnings()
    assert len(warnings) == before + 1
    assert warnings[-1]["group"] == "mm3-kiosk"
    assert warnings[-1]["reason"] == "chown_failed"


def test_core_268_group_change_ok_logs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Group:
        gr_gid = 4242

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr("mm3.core.web.kiosk.grp.getgrnam", lambda name: Group())
    monkeypatch.setattr("mm3.core.web.kiosk.os.chown", lambda *a: calls.append(a))
    before = len(_group_warnings())
    AgentKey(tmp_path / "run").write()
    assert len(_group_warnings()) == before
    assert calls
    assert calls[0][1:] == (-1, 4242)


# --- the agent against a running gateway --------------------------------------------------------


class FakeProcess:
    def __init__(self) -> None:
        self.exited = asyncio.Event()
        self.code = 0

    async def wait(self) -> int:
        await self.exited.wait()
        return self.code

    def terminate(self) -> None:
        self.exited.set()


class FakeRunner:
    def __init__(self) -> None:
        self.randr = json.loads(json.dumps(RANDR))
        self.commands: list[list[str]] = []
        self.spawned: list[tuple[list[str], FakeProcess]] = []
        self.fail_apply = False

    async def run(self, cmd: Sequence[str]) -> tuple[int, str, str]:
        self.commands.append(list(cmd))
        if cmd[1:] == ["--json"]:
            return 0, json.dumps(self.randr), ""
        if self.fail_apply:
            return 1, "", "cannot set mode"
        mode = cmd[cmd.index("--mode") + 1]
        size, refresh = mode.removesuffix("Hz").split("@")
        width, height = (int(x) for x in size.split("x"))
        for m in self.randr[0]["modes"]:
            m["current"] = (m["width"], m["height"], m["refresh"]) == (
                width,
                height,
                float(refresh),
            )
        return 0, "", ""

    async def spawn(self, cmd: Sequence[str]) -> FakeProcess:
        proc = FakeProcess()
        self.spawned.append((list(cmd), proc))
        return proc


class Rig:
    def __init__(self, gateway: WebGateway, agents: KioskAgents, hub: ClientHub) -> None:
        self.gateway = gateway
        self.agents = agents
        self.hub = hub
        self.applied: list[tuple[int, int] | None] = []
        self.display: dict[str, Any] = {"width": 1920, "height": 1080, "rotation": 90, "scale": 1.0}

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.gateway.mirror.port}"


CONFIG = "engine:\n  locale: en\nmodules: []\n"


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    (tmp_path / "config.yaml").write_text(CONFIG)
    registry = Registry()
    result = load_config(tmp_path / "config.yaml", registry=registry, env_file=tmp_path / "e")
    assert result.ok, result.records
    settings = EngineSettings().model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    hub = ClientHub(cached_view(lambda: result, registry), WebAuth.open(tmp_path), "1.0+t")
    key = AgentKey(tmp_path / "run")
    key.write()
    holder: dict[str, Any] = {}

    async def applied(mode: tuple[int, int] | None) -> None:
        holder["rig"].applied.append(mode)

    agents = KioskAgents(
        key, hub, lambda: holder["rig"].display, applied, VirtualClock(), hello_timeout=1
    )
    gateway = WebGateway(settings, tmp_path, hub=hub, agents=agents)
    await gateway.start()
    holder["rig"] = Rig(gateway, agents, hub)
    try:
        yield holder["rig"]
    finally:
        await gateway.stop()


async def eventually(check: Any, wait: float = 3.0) -> None:
    async with asyncio.timeout(wait):
        while not check():  # noqa: ASYNC110 - polls agent state, no event to wait on
            await asyncio.sleep(0.01)


async def no_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


def session(rig: Rig, runner: FakeRunner, tmp_path: Path, **extra: Any) -> KioskSession:
    return KioskSession(
        rig.base,
        rig.agents.key.value,
        "0.1.5",
        tmp_path / "profile",
        runner=runner,
        **extra,
    )


async def test_core_268_endpoint_refuses_wrong_key_and_bad_hello(rig: Rig) -> None:
    url = rig.base.replace("http", "ws") + "/ws/kiosk-agent"
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer x"}) as ws:
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
        assert ws.close_code == 4403
    headers = {"Authorization": f"Bearer {rig.agents.key.value}"}
    async with websockets.connect(url, additional_headers=headers) as ws:
        await ws.send(json.dumps({"v": 1, "kind": "hello", "data": {"protocol": 2}}))
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
        assert ws.close_code == 4400


async def test_core_277_agent_applies_display_and_launches(rig: Rig, tmp_path: Path) -> None:
    runner = FakeRunner()
    agent = session(rig, runner, tmp_path, sleep=no_sleep)
    task = asyncio.create_task(agent.run())
    try:
        await eventually(lambda: rig.agents.status.chromium == "started")
        status = rig.agents.status
        assert status.connected
        assert status.cage_version == "0.1.5"
        assert status.applied == {
            "output": "HDMI-A-1",
            "width": 1920,
            "height": 1080,
            "refresh": 60.0,
            "rotation": 90,
            "scale": 1.0,
        }
        assert status.viewport == (1080, 1920)
        assert status.dpr == 1.0
        assert not status.mismatch
        assert rig.applied[-1] == (1920, 1080)
        assert next(c for c in runner.commands if "--mode" in c)[5] == "1920x1080@60Hz"
        (cmd, _), *_ = runner.spawned
        url = cmd[-1]
        assert url.startswith(rig.base + "/#kiosk=")
        assert rig.hub.kiosk_keys.valid(url.split("#kiosk=", 1)[1])
        # CORE-143: a changed display block is applied live.
        rig.display = {"width": 2560, "height": 1440, "rotation": 0, "scale": 2.0}
        await rig.agents.display_changed()
        await eventually(lambda: rig.agents.status.viewport == (960, 540))
        assert rig.agents.status.mismatch  # 2560 x 1440 is not offered (CORE-145)
        assert rig.agents.status.applied is not None
        assert rig.agents.status.applied["width"] == 1920
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_core_277_failure_is_reported(rig: Rig, tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.fail_apply = True
    agent = session(rig, runner, tmp_path, sleep=no_sleep)
    task = asyncio.create_task(agent.run())
    try:
        await eventually(lambda: rig.agents.status.failure == "cannot set mode")
        rig.display = {"output": "VGA-9"}
        await rig.agents.display_changed()
        await eventually(lambda: rig.agents.status.failure == "output VGA-9 not found")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_core_269_restart_with_backoff_then_give_up(rig: Rig, tmp_path: Path) -> None:
    runner = FakeRunner()
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    agent = session(rig, runner, tmp_path, sleep=sleep, now=lambda: 0.0)
    task = asyncio.create_task(agent.run())
    try:
        for n in range(1, 7):
            await eventually(lambda n=n: len(runner.spawned) == n)
            keys = {c[-1] for c, _ in runner.spawned}
            assert len(keys) == n  # a new kiosk key for every launch (CORE-268)
            runner.spawned[-1][1].terminate()
        await eventually(lambda: rig.agents.status.failed)
        assert rig.agents.status.chromium == "failed"
        assert rig.agents.status.restarts == 5
        assert [d for d in delays if d != 10.0][:5] == [1.0, 2.0, 4.0, 8.0, 16.0]
        assert len(runner.spawned) == 6
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_core_269_engine_asks_for_a_restart(rig: Rig, tmp_path: Path) -> None:
    runner = FakeRunner()
    agent = session(rig, runner, tmp_path, sleep=no_sleep)
    task = asyncio.create_task(agent.run())
    clock = rig.agents.clock
    assert isinstance(clock, VirtualClock)
    watchdog = asyncio.create_task(rig.agents.watchdog())
    try:
        await eventually(lambda: rig.agents.status.chromium == "started")
        first = runner.spawned[0][1]
        await clock.advance(61)
        await eventually(lambda: first.exited.is_set())
        await eventually(lambda: len(runner.spawned) == 2)
    finally:
        watchdog.cancel()
        task.cancel()
        for t in (watchdog, task):
            with pytest.raises(asyncio.CancelledError):
                await t


async def test_core_268_waits_for_the_mirror_listener(tmp_path: Path) -> None:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds != 10.0:  # the output poller
            delays.append(seconds)
        if len(delays) >= 4:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    agent = KioskSession(
        "http://127.0.0.1:9", "k", "0.1.5", tmp_path, runner=FakeRunner(), sleep=sleep
    )
    with pytest.raises(asyncio.CancelledError):
        await agent.run()
    assert delays[:3] == [1.0, 2.0, 4.0]


# --- CORE-146: the applied viewport -------------------------------------------------------------


async def test_core_146_applied_mode_relayouts_and_emits(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "engine:\n  locale: en\ndisplay:\n  width: 1920\n  height: 1080\nmodules: []\n"
    )
    registry = Registry()
    result = load_config(tmp_path / "config.yaml", registry=registry, env_file=tmp_path / "e")
    engine = Engine(EngineSettings(), registry=registry, state_dir=tmp_path / "s")
    seen: list[Notification] = []

    class Rec:
        def deliver(self, n: Notification, exclude_instance: str | None) -> None:
            seen.append(n)

    engine.bus.add_endpoint(Rec())
    manager = ConfigManager(engine, result, registry=registry, env_file=tmp_path / "e")
    plans: list[Any] = []

    async def listener(new: Any, plan: Any) -> None:
        plans.append(plan)

    manager.layout_listeners.append(listener)
    await engine.start(result.instance_specs())
    try:
        assert result.layout is not None
        assert result.layout.viewport.assumed
        assert await manager.set_applied((1920, 1080)) is False  # same size: no event
        assert manager.current.layout is not None
        assert not manager.current.layout.viewport.assumed
        assert await manager.set_applied((1280, 720)) is True
        assert manager.current.layout is not None
        assert (manager.current.layout.viewport.width, manager.current.layout.viewport.height) == (
            1280,
            720,
        )
        await asyncio.sleep(0.05)
        changed = [n for n in seen if n.name == "CORE_DISPLAY_CHANGED"]
        assert [dict(n.payload) for n in changed] == [
            {"viewport": [1280, 720], "scale": 1.0, "rotation": 0}
        ]
        assert [p.display for p in plans] == [True]
        assert await manager.set_applied((1280, 720)) is False
    finally:
        await engine.stop()


def test_core_270_print_command_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["kiosk", "session", "--print-command"]) == 0
    assert "--kiosk" in capsys.readouterr().out
