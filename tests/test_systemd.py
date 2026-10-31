"""sd_notify, loop watchdog and mm3.service (core spec 7.1 and 10.2, CORE-290..292)."""

from __future__ import annotations

import asyncio
import configparser
import io
import os
import re
import socket
import sys
from pathlib import Path

import pytest

from conftest import Listener
from mm3.cli.main import main
from mm3.cli.service_cmd import unit_text
from mm3.core.engine import Engine
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings
from mm3.core.systemd import ServiceNotifier, run_watchdog, watchdog_interval
from mm3.testing import VirtualClock


def engine(notifier: ServiceNotifier, clock: VirtualClock, state_dir: Path) -> Engine:
    return Engine(
        EngineSettings(), registry=Registry(), clock=clock, notifier=notifier, state_dir=state_dir
    )


# --- CORE-290: sd_notify -------------------------------------------------------------------


def test_core_290_no_socket_is_a_no_op() -> None:
    notifier = ServiceNotifier.from_environ({})
    assert not notifier.enabled
    assert not notifier.ready("1 running")
    assert not notifier.stopping()


def test_core_290_ignores_an_address_that_is_neither_path_nor_abstract() -> None:
    assert not ServiceNotifier("relative/path").enabled
    assert not ServiceNotifier("").enabled


def test_core_290_messages_reach_a_path_socket(listener: Listener) -> None:
    notifier = ServiceNotifier(str(listener.path))
    assert notifier.ready("2 running,\n0 failed")
    assert notifier.status("1 running, 1 failed")
    assert notifier.watchdog()
    assert notifier.stopping()
    assert listener.messages() == [
        ["READY=1", "STATUS=2 running, 0 failed"],
        ["STATUS=1 running, 1 failed"],
        ["WATCHDOG=1"],
        ["STOPPING=1"],
    ]


def test_core_290_reloading_carries_monotonic_usec(listener: Listener) -> None:
    ServiceNotifier(str(listener.path)).reloading()
    [[first, second]] = listener.messages()
    assert first == "RELOADING=1"
    assert re.fullmatch(r"MONOTONIC_USEC=\d+", second)


@pytest.mark.skipif(sys.platform != "linux", reason="abstract sockets are Linux only")
def test_core_290_abstract_socket() -> None:
    name = f"mm3-test-{os.getpid()}"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind("\0" + name)
    sock.settimeout(1)
    try:
        assert ServiceNotifier("@" + name).send("READY=1")
        assert sock.recv(64) == b"READY=1"
    finally:
        sock.close()


def test_core_290_a_failed_send_never_raises(tmp_path: Path) -> None:
    notifier = ServiceNotifier(str(tmp_path / "nobody-listens.sock"))
    assert notifier.enabled
    assert notifier.ready() is False
    assert notifier.watchdog() is False


async def test_core_290_engine_sends_ready_status_and_stopping(listener: Listener) -> None:
    eng = engine(ServiceNotifier(str(listener.path)), VirtualClock(), listener.path.parent)
    await eng.start([])
    assert listener.messages() == []  # CORE_READY alone does not tell systemd
    eng.notify_ready()
    await eng.stop()
    assert listener.messages() == [["READY=1", "STATUS=0 running, 0 failed"], ["STOPPING=1"]]


async def test_core_290_stopping_is_sent_once_and_ready_not_after_it(listener: Listener) -> None:
    eng = engine(ServiceNotifier(str(listener.path)), VirtualClock(), listener.path.parent)
    await eng.start([])
    eng.notify_stopping()
    eng.notify_stopping()
    eng.notify_ready()
    await eng.stop()
    assert listener.messages() == [["STOPPING=1"]]


async def test_core_290_reload_sends_reloading_then_ready(listener: Listener) -> None:
    eng = engine(ServiceNotifier(str(listener.path)), VirtualClock(), listener.path.parent)
    await eng.start([])
    eng.notify_reloading()  # before READY=1: nothing to reload yet
    eng.notify_reloaded()
    assert listener.messages() == []
    eng.notify_ready()
    listener.messages()
    eng.notify_reloading()
    eng.notify_reloaded()
    [reloading, ready] = listener.messages()
    assert reloading[0] == "RELOADING=1"
    assert ready == ["READY=1", "STATUS=0 running, 0 failed"]
    await eng.stop()


# --- CORE-291: the loop watchdog -----------------------------------------------------------


def test_core_291_interval_is_half_of_watchdog_usec() -> None:
    assert watchdog_interval({"WATCHDOG_USEC": "30000000"}) == 15.0
    assert watchdog_interval({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "42"}, pid=42) == 15.0


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"WATCHDOG_USEC": ""},
        {"WATCHDOG_USEC": "0"},
        {"WATCHDOG_USEC": "soon"},
        {"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "7"},
        {"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "x"},
    ],
)
def test_core_291_no_watchdog_when_not_asked_or_not_for_this_process(env: dict[str, str]) -> None:
    assert watchdog_interval(env, pid=42) is None


async def test_core_291_pings_every_half_interval_on_the_engine_clock(listener: Listener) -> None:
    clock = VirtualClock()
    eng = engine(ServiceNotifier(str(listener.path)), clock, listener.path.parent)
    task = eng.start_watchdog({"WATCHDOG_USEC": "30000000"})
    assert task is not None
    await clock.advance(0)
    assert listener.messages() == [["WATCHDOG=1"]]
    await clock.advance(14)
    assert listener.messages() == []
    await clock.advance(1)
    assert listener.messages() == [["WATCHDOG=1"]]
    await clock.advance(30)
    assert listener.messages() == [["WATCHDOG=1"], ["WATCHDOG=1"]]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_core_291_no_task_without_watchdog_usec(tmp_path: Path) -> None:
    eng = engine(ServiceNotifier(None), VirtualClock(), tmp_path)
    assert eng.start_watchdog({}) is None
    assert eng.watchdog_task is None


async def test_core_291_a_blocked_loop_stops_the_ping(listener: Listener) -> None:
    """The ping proves the loop runs: nothing else keeps it alive."""
    clock = VirtualClock()
    notifier = ServiceNotifier(str(listener.path))
    task = asyncio.create_task(run_watchdog(notifier, 15.0, clock))
    await clock.advance(0)
    listener.messages()
    # A loop that is stuck never reaches the sleep: no virtual time passes for the task.
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await clock.advance(60)
    assert listener.messages() == []


# --- CORE-292: mm3.service -----------------------------------------------------------------


def parse_unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(unit_text())
    return parser


def test_core_292_unit_directives() -> None:
    service = parse_unit()["Service"]
    assert service["Type"] == "notify"
    assert service["NotifyAccess"] == "main"
    assert service["WatchdogSec"] == "30s"
    assert service["User"] == "mm3"
    assert service["Group"] == "mm3"
    assert service["ExecStart"] == "/opt/mm3/venv/bin/mm3 run"
    assert service["ExecReload"] == "/bin/kill -HUP $MAINPID"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "2s"
    assert service["TimeoutStartSec"] == "60s"
    assert service["TimeoutStopSec"] == "20s"
    assert service["StateDirectory"] == "mm3"
    assert service["StateDirectoryMode"] == "0750"
    assert service["RuntimeDirectory"] == "mm3"
    assert service["RuntimeDirectoryMode"] == "0755"
    assert service["ReadWritePaths"] == "/etc/mm3"


def test_core_292_unit_carries_the_core_65_hardening() -> None:
    service = parse_unit()["Service"]
    assert service["ProtectSystem"] == "strict"
    assert service["NoNewPrivileges"] == "yes"
    assert service["PrivateTmp"] == "yes"


def test_core_292_unit_is_wanted_by_multi_user_target() -> None:
    assert parse_unit()["Install"]["WantedBy"] == "multi-user.target"


def test_core_292_stop_budget_fits_inside_the_timeout() -> None:
    # 7.1: instances get 10 s to stop; the unit allows 20 s.
    assert parse_unit()["Service"]["TimeoutStopSec"] == "20s"


def test_core_292_the_kiosk_unit_starts_after_the_engine() -> None:
    kiosk = configparser.ConfigParser(strict=False, interpolation=None)
    from mm3.cli.kiosk_cmd import unit_text as kiosk_unit

    kiosk.read_string(kiosk_unit())
    assert "mm3.service" in kiosk["Unit"]["After"].split()
    assert kiosk["Unit"]["Wants"] == "mm3.service"


def test_core_292_mm3_service_unit_prints_the_shipped_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["service", "unit"]) == 0
    assert capsys.readouterr().out == unit_text()


def test_core_292_unit_is_in_the_package() -> None:
    import mm3.core.systemd

    assert (Path(mm3.core.systemd.__file__).parent / "mm3.service").read_text() == unit_text()
    assert io.StringIO(unit_text()).readline().startswith("# MM3 engine")
