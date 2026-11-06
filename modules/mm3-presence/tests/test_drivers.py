"""mm3-presence: the built-in display drivers and the driver lookup.

Implements:
    PRS-06: Core spec 0.34, §9.2 Presence and power: module boundary.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import mm3_presence.drivers as drivers
import pytest
from mm3_presence.drivers import CecDriver, DdcutilDriver, KioskDriver, load_driver

from mm3 import ConfigurationError


class Ctx:
    def __init__(self) -> None:
        self.system = SimpleNamespace(calls=[])
        self.log = SimpleNamespace(warning=lambda *a, **k: None)

        async def set_display_power(on: bool) -> None:
            self.system.calls.append(on)

        self.system.set_display_power = set_display_power

    async def run_blocking(self, fn: Any) -> Any:
        return fn()


class Run:
    """A stand-in for ``subprocess.run`` that records commands and answers in turn."""

    def __init__(self, *answers: tuple[int, str] | Exception) -> None:
        self.answers = list(answers)
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.commands.append(command)
        self.inputs.append(kwargs.get("input"))
        answer = self.answers.pop(0) if self.answers else (0, "")
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(returncode=answer[0], stdout=answer[1], stderr="")


def patched(monkeypatch: pytest.MonkeyPatch, *answers: tuple[int, str] | Exception) -> Run:
    run = Run(*answers)
    monkeypatch.setattr(drivers.subprocess, "run", run)
    return run


async def test_prs_06_kiosk_uses_the_engine_hook() -> None:
    ctx = Ctx()
    driver = KioskDriver(ctx, {})  # type: ignore[arg-type]
    await driver.set_power(False)
    await driver.set_power(True)
    assert ctx.system.calls == [False, True]
    assert not driver.supports_brightness
    assert await driver.check() is None


async def test_prs_06_ddcutil_sets_power_and_brightness(monkeypatch: pytest.MonkeyPatch) -> None:
    run = patched(monkeypatch)
    driver = DdcutilDriver(Ctx(), {"display": 2})  # type: ignore[arg-type]
    await driver.set_power(False)
    await driver.set_power(True)
    await driver.set_brightness(35)
    assert run.commands == [
        ["ddcutil", "--display", "2", "setvcp", "0xD6", "4"],
        ["ddcutil", "--display", "2", "setvcp", "0xD6", "1"],
        ["ddcutil", "--display", "2", "setvcp", "0x10", "35"],
    ]


async def test_prs_06_ddcutil_reads_the_brightness(monkeypatch: pytest.MonkeyPatch) -> None:
    patched(monkeypatch, (0, "VCP 10 C 40 80\n"))
    assert await DdcutilDriver(Ctx(), {}).get_brightness() == 50  # type: ignore[arg-type]
    patched(monkeypatch, (1, "nope"))
    assert await DdcutilDriver(Ctx(), {}).get_brightness() is None  # type: ignore[arg-type]


async def test_prs_06_ddcutil_failure_names_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    patched(monkeypatch, (1, "Display not found\nmore"), (1, "No display"))
    driver = DdcutilDriver(Ctx(), {})  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Display not found"):
        await driver.set_power(False)
    assert await driver.check() == "No display"


async def test_prs_06_a_missing_program_is_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    patched(monkeypatch, FileNotFoundError())
    assert await DdcutilDriver(Ctx(), {}).check() == "ddcutil was not found"  # type: ignore[arg-type]


async def test_prs_06_a_slow_program_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    patched(monkeypatch, subprocess.TimeoutExpired("ddcutil", 10))
    reason = await DdcutilDriver(Ctx(), {}).check()  # type: ignore[arg-type]
    assert reason is not None
    assert "10" in reason


async def test_prs_06_cec_sends_on_and_standby(monkeypatch: pytest.MonkeyPatch) -> None:
    run = patched(monkeypatch)
    driver = CecDriver(Ctx(), {"adapter": "/dev/ttyACM0"})  # type: ignore[arg-type]
    await driver.set_power(False)
    await driver.set_power(True)
    assert run.commands[0] == ["cec-client", "-s", "-d", "1", "/dev/ttyACM0"]
    assert run.inputs == ["standby 0\n", "on 0\n"]
    assert not driver.supports_brightness


def test_prs_06_bad_options_are_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as info:
        DdcutilDriver(Ctx(), {"display": 0})  # type: ignore[arg-type]
    assert info.value.message_key == "CFG_DISPLAY_OPTIONS_INVALID"
    with pytest.raises(ConfigurationError):
        KioskDriver(Ctx(), {"anything": 1})  # type: ignore[arg-type]


def test_prs_06_built_in_drivers_are_found_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: Any) -> Any:
        raise AssertionError("the entry point group must not be read")

    monkeypatch.setattr(drivers, "entry_points", boom)
    for name in ("kiosk", "ddcutil", "cec"):
        assert load_driver(name, Ctx(), {}).name == name  # type: ignore[arg-type]


def test_prs_06_plug_ins_come_from_the_entry_point_group(monkeypatch: pytest.MonkeyPatch) -> None:
    class Plugin:
        name = "plug"
        supports_brightness = False

        def __init__(self, ctx: Any, options: dict[str, Any]) -> None:
            self.options = options

    point = SimpleNamespace(name="plug", load=lambda: Plugin)
    monkeypatch.setattr(drivers, "entry_points", lambda group: [point])
    driver = load_driver("plug", Ctx(), {"a": 1})  # type: ignore[arg-type]
    assert isinstance(driver, Plugin)
    assert driver.options == {"a": 1}
    with pytest.raises(ConfigurationError) as info:
        load_driver("other", Ctx(), {})  # type: ignore[arg-type]
    assert info.value.message_key == "CFG_DISPLAY_UNKNOWN"
