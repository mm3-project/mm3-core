"""Milestone 7, part 3: the engine hooks for presence and power (CORE-52, CORE-53, CORE-312)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from mm3.cli.diag_cmd import _display_driver_check
from mm3.errors import TransientError
from mm3.module import MirrorModule
from mm3.testing import ModuleHarness

# --- modules that use ctx.system ------------------------------------------------------------


class Power(MirrorModule):
    """Declares system.power and does what the test tells it."""

    name = "powerful"
    capabilities: ClassVar[set[str]] = {"system.power"}


class NoPower(MirrorModule):
    """Uses ctx.system without declaring system.power."""

    name = "nopower"


def instance(h: ModuleHarness) -> Any:
    assert h.instance is not None
    return h.instance


async def test_core_53_suspend_and_resume_follow_the_requests() -> None:
    async with ModuleHarness(Power) as h:
        assert not h.suspended
        instance(h).ctx.system.request_suspend("test")
        await h.settle()
        assert h.suspended
        instance(h).ctx.system.request_resume("test")
        await h.settle()
        assert not h.suspended


async def test_core_53_the_last_request_of_an_instance_wins() -> None:
    async with ModuleHarness(Power) as h:
        system = instance(h).ctx.system
        system.request_suspend("a")
        system.request_suspend("b")
        system.request_resume("c")
        await h.settle()
        assert not h.suspended
        power = h.engine.supervisor.power
        assert power.holders == {"powerful_test"}
        assert power.suspenders == frozenset()


async def test_core_53_a_holder_beats_a_suspender() -> None:
    async with ModuleHarness(Power) as h:
        power = h.engine.supervisor.power
        power.request_suspend("one", "test")
        power.request_resume("two", "test")
        await h.settle()
        assert not h.suspended
        power.release("two")
        await h.settle()
        assert h.suspended
        power.release("one")
        power.release("one")  # nothing to drop: no change, no error
        await h.settle()
        assert not h.suspended


async def test_core_53_a_stopped_instance_drops_its_request() -> None:
    async with ModuleHarness(Power) as h:
        instance(h).ctx.system.request_suspend("test")
        await h.settle()
        assert h.suspended
        await h.clock.drive(h.engine.supervisor.remove("powerful_test"))
        await h.settle()
        assert not h.suspended
        assert h.engine.supervisor.power.suspenders == frozenset()


async def test_core_52_needs_the_capability() -> None:
    async with ModuleHarness(NoPower) as h:
        ctx = instance(h).ctx
        with pytest.raises(PermissionError):
            _ = ctx.system
        assert h.denied == ["system.power"]
        assert not h.suspended


async def test_core_346_the_display_goes_through_the_kiosk_hook() -> None:
    async with ModuleHarness(Power) as h:
        await instance(h).ctx.system.set_display_power(False)
        await instance(h).ctx.system.set_display_power(True)
        assert h.system.display_calls == [False, True]
        h.system.error = TransientError("CORE_DISPLAY_NO_KIOSK")
        with pytest.raises(TransientError):
            await instance(h).ctx.system.set_display_power(False)


async def test_core_346_without_a_kiosk_agent_the_display_is_unavailable() -> None:
    async with ModuleHarness(Power) as h:
        power = h.engine.supervisor.power
        power.display = None
        with pytest.raises(TransientError) as info:
            await instance(h).ctx.system.set_display_power(False)
        assert info.value.message_key == "CORE_DISPLAY_UNAVAILABLE"


# --- CORE-312: mm3 diag check, display driver -----------------------------------------------


class Client:
    def __init__(self, health: Any = None, error: Exception | None = None) -> None:
        self.health = health
        self.error = error
        self.asked: list[str] = []

    def json(self, target: str) -> Any:
        self.asked.append(target)
        if self.error is not None:
            raise self.error
        return self.health


def config(*modules: tuple[str, bool]) -> Any:
    return SimpleNamespace(
        instances=[
            SimpleNamespace(id=f"m{i}", module=m, enabled=on) for i, (m, on) in enumerate(modules)
        ]
    )


def check(result: Any, client: Client, status: Any = None) -> Any:
    return _display_driver_check(result, client, {"ok": True} if status is None else status)  # type: ignore[arg-type]


def test_core_312_skips_without_a_presence_module() -> None:
    assert check(config(("clock", True)), Client()).outcome == "skip"
    assert check(config(("presence", False)), Client()).outcome == "skip"
    assert check(None, Client()).outcome == "skip"


def test_core_312_skips_when_the_engine_is_not_reachable() -> None:
    result = _display_driver_check(config(("presence", True)), Client(), None)  # type: ignore[arg-type]
    assert (result.outcome, result.detail) == ("skip", "engine not reachable")


def test_core_312_passes_with_a_working_driver() -> None:
    client = Client({"state": "RUNNING", "fields": {"driver": "ddcutil"}})
    result = check(config(("clock", True), ("presence", True)), client)
    assert (result.outcome, result.detail) == ("pass", "ddcutil")
    assert client.asked == ["/api/v1/modules/m1/health"]


def test_core_312_fails_with_the_driver_reason() -> None:
    health = {"state": "RUNNING", "fields": {"driver": "ddcutil", "driver_error": "No display"}}
    result = check(config(("presence", True)), Client(health))
    assert (result.outcome, result.detail) == ("fail", "ddcutil: No display")


def test_core_312_fails_when_the_module_failed() -> None:
    result = check(config(("presence", True)), Client({"state": "FAILED"}))
    assert result.outcome == "fail"


def test_core_312_skips_when_the_module_is_not_running_or_unreadable() -> None:
    assert check(config(("presence", True)), Client({"state": "DISABLED"})).outcome == "skip"
    assert check(config(("presence", True)), Client(error=OSError("gone"))).outcome == "skip"
