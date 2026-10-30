"""The mm3.testing kit: ModuleHarness, fakes, virtual time and check_contract()."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

import kit_modules as km
from mm3 import MirrorModule
from mm3.core.supervisor import State
from mm3.errors import HttpConnectionError, HttpTimeoutError, RequestTimeoutError
from mm3.testing import ContractError, ModuleHarness, sample_payload
from mm3.testing.contract import KIT_CHECKS, run_kit_checks
from mm3.testing.loop import DriveError, LoopClock


def weather(**kw: Any) -> ModuleHarness:
    args: dict[str, Any] = {"config": km.CONFIG, "grants": km.GRANTS}
    args.update(kw)
    return ModuleHarness(km.Weather, **args)


# --- harness ----------------------------------------------------------------------


async def test_mod_29_harness_drives_scheduler_http_and_notifications() -> None:
    async with weather() as h:
        h.http.mock("GET", km.URL, json=km.forecast_json(18.4))
        assert h.instance_state is State.RUNNING
        await h.advance(minutes=10)
        assert h.state["temp"] == 18.4
        sent = h.notifications.last("WEATHER_UPDATED")
        assert sent is not None
        assert sent.payload["temp_c"] == 18.4
        assert h.http.calls[-1].params == {"lat": "42.1"}
        assert h.mqtt.last("mirror/weather") is not None
        assert h.health()["fields"]["last_refresh"] == "2026-01-01T12:10:00Z"
        assert h.violations == []


async def test_core_278_virtual_time_moves_sleep_scheduler_and_now() -> None:
    seen: list[datetime] = []

    class Sleeper(MirrorModule):
        name = "sleeper"

        async def start(self) -> None:
            self.ctx.create_task(self.run(), name="run")

        async def run(self) -> None:
            while True:
                await asyncio.sleep(30)
                seen.append(self.ctx.now())

    async with ModuleHarness(Sleeper, timezone="Europe/Rome") as h:
        assert h.now == datetime(2026, 1, 1, 12, tzinfo=h.now.tzinfo)
        await h.advance(seconds=95)
        assert [s - seen[0] for s in seen] == [
            timedelta(0),
            timedelta(seconds=30),
            timedelta(seconds=60),
        ]
        assert seen[0].utcoffset() == timedelta(hours=1)
        await h.advance(to="2026-01-01T13:00:00")
        assert h.now.hour == 13


async def test_core_278_harness_now_argument() -> None:
    class Probe(MirrorModule):
        name = "probe"

    async with ModuleHarness(Probe, now="2026-06-01T08:30:00+00:00") as h:
        ctx = h.managed.context
        assert ctx is not None
        assert ctx.now() == datetime(2026, 6, 1, 8, 30, tzinfo=UTC)


async def test_mod_29_mqtt_inject_goes_through_the_inbox() -> None:
    async with weather() as h:
        assert h.mqtt.subscriptions == ["home/+/temp"]
        count = await h.mqtt.inject("home/kitchen/temp", {"t": 21})
        assert count == 1
        assert h.state["room"] == 21
        assert h.instance is not None
        assert h.instance.seen == ["home/kitchen/temp"]  # type: ignore[attr-defined]
        await h.mqtt.inject("office/temp", {"t": 1})
        assert h.instance.seen == ["home/kitchen/temp"]  # type: ignore[attr-defined]
    assert h.mqtt.subscriptions == []  # cancelled at stop (MOD-22)


async def test_mod_70_grants_apply_as_on_a_mirror() -> None:
    class Rogue(MirrorModule):
        name = "rogue"
        capabilities: ClassVar[set[str]] = {"http.outbound", "mqtt.publish"}

    async with ModuleHarness(Rogue, grants={"http": ["https://ok.example/"]}) as h:
        ctx = h.managed.context
        assert ctx is not None
        with pytest.raises(PermissionError, match="not granted"):
            await ctx.http.get("https://other.example/")
        with pytest.raises(HttpConnectionError):
            await ctx.http.get("https://ok.example/x")
        assert [c.url for c in h.http.unmocked] == ["https://ok.example/x"]
        h.http.mock("GET", "https://ok.example/", raises=HttpTimeoutError("slow"))
        with pytest.raises(HttpTimeoutError):
            await ctx.http.get("https://ok.example/x")
        with pytest.raises(PermissionError, match="for publish"):
            await ctx.mqtt.publish("any/topic", "x")
        with pytest.raises(PermissionError, match=r"mqtt\.subscribe is not declared"):
            await ctx.mqtt.subscribe("a/#", lambda m: asyncio.sleep(0))
        assert h.denied == ["mqtt.subscribe"]


async def test_mod_24_request_and_respond() -> None:
    async with weather() as h:
        assert await h.request("WEATHER_NOW", {}) == {"temp_c": 0.0}
        with pytest.raises(RequestTimeoutError):
            await h.request("UNKNOWN_THING", {}, timeout=1)
        with pytest.raises(ValueError, match="answers model"):
            await h.request("WEATHER_NOW", {"city": 5})

    async with ModuleHarness(km.Asker) as h:
        h.respond("CLOCK_NOW", lambda payload: {"time": "12:00"})
        await h.advance(minutes=1)
        assert h.state["time"] == "12:00"

        async def broken(payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("down")

        h.respond("CLOCK_NOW", broken)
        await h.advance(minutes=1)
        assert "clock_unavailable" in h.logs.events("warning")
        assert h.instance_state is State.RUNNING


async def test_mod_43_notify_checks_consumes_model() -> None:
    async with weather() as h:
        await h.notify("PRESENCE_DETECTED", {"sensor": "hall"})
        assert h.state["sensor"] == "hall"
        with pytest.raises(ValueError, match="consumes model"):
            await h.notify("PRESENCE_DETECTED", {"zone": "hall"})


async def test_mod_27_inputs_locale_page_suspend_reconfigure_restart() -> None:
    class Probe(MirrorModule):
        name = "probe"
        consumes: ClassVar[Any] = {}

        async def on_client_message(self, msg: Any) -> None:
            await self.ctx.send_to_client("echo", {"type": msg.type})

    async with ModuleHarness(Probe, pages=["a", "b"]) as h:
        ctx = h.managed.context
        assert ctx is not None
        assert ctx.page == "a"
        await h.set_page("b")
        assert ctx.page == "b"
        assert h.received.last("CORE_PAGE_CHANGED") is not None
        with pytest.raises(ValueError, match="unknown page"):
            await h.set_page("zzz")
        await h.set_locale("it")
        assert ctx.locale == "it"
        await h.client_message("ping")
        assert h.client_messages[-1].payload == {"type": "ping"}
        await h.suspend()
        assert h.instance_state is State.SUSPENDED
        await h.resume()
        assert h.instance_state is State.RUNNING
        old = h.instance
        await h.reconfigure({})
        await h.restart()
        assert h.instance is not old
        await h.set_grants({"http": ["https://x/"]})
        assert h.managed.spec.grants.http == ("https://x/",)


async def test_core_278_drive_reports_operations_that_never_finish() -> None:
    clock = LoopClock.acquire(datetime(2026, 1, 1, tzinfo=UTC))
    try:
        with pytest.raises(DriveError, match="nothing scheduled"):
            await clock.drive(asyncio.Event().wait())
        with pytest.raises(DriveError, match="more than"):
            await clock.drive(asyncio.sleep(100), limit=10)
        with pytest.raises(ValueError, match="forward"):
            await clock.advance(-1)
    finally:
        clock.release()


async def test_core_278_storage_and_run_blocking_in_real_threads() -> None:
    class Saver(MirrorModule):
        name = "saver"

        async def start(self) -> None:
            await self.ctx.storage.kv_set("n", 1)
            self.ctx.set_state({"n": await self.ctx.storage.kv_get("n")})

    async with ModuleHarness(Saver) as h:
        assert h.state["n"] == 1
        assert h.storage_dir.exists()


async def test_mod_10_harness_rejects_bad_config_and_unusable_module() -> None:
    with pytest.raises(Exception, match="api_key"):
        async with ModuleHarness(km.Weather, config={}):
            pass

    class Future(MirrorModule):
        name = "future"
        api_version = "2.0"

    with pytest.raises(ValueError, match="cannot run"):
        async with ModuleHarness(Future):
            pass


def test_mod_43_sample_payloads() -> None:
    from pydantic import BaseModel, Field

    class Inner(BaseModel):
        n: int = Field(ge=3)

    class Rich(BaseModel):
        when: datetime
        level: str = Field(min_length=2)
        flag: bool
        ratio: float = Field(gt=0)
        items: list[Inner] = Field(min_length=1)
        mode: str | None = None

    sample = sample_payload(Rich)
    assert sample is not None
    Rich.model_validate(sample)
    assert sample_payload(km.Forecast) == {"temp_c": 1.0, "at": "x"}


# --- check_contract -------------------------------------------------------------------


async def test_mod_06_good_module_passes_every_kit_check() -> None:
    async with weather() as h:
        h.http.mock("GET", km.URL, json=km.forecast_json())
        results = await run_kit_checks(h)
        assert [r.id for r in results] == list(KIT_CHECKS)
        failed = {r.id: r.findings for r in results if r.status == "fail"}
        assert failed == {}
        await h.check_contract()


class KvWriter(MirrorModule):
    """Writes through ``ctx.storage`` from a job, as a module that keeps its last data does."""

    name = "kvwriter"

    async def start(self) -> None:
        self.ctx.scheduler.every(60, self._tick, start_now=True)

    async def _tick(self) -> None:
        await self.ctx.storage.kv_set("n", 1)


async def test_mod_06_kv_writes_and_temporary_directory_cleanup_are_not_outside_writes() -> None:
    # the kit's fresh instances and the test's own harness share one virtual clock, so the
    # base instance's job writes while the probes run; rmtree cleanup uses relative names
    async with ModuleHarness(KvWriter) as h:
        await h.check_contract()


async def test_mod_26_asker_handles_missing_and_failing_responders() -> None:
    async with ModuleHarness(km.Asker) as h:
        await h.check_contract()


async def expect_failure(module: type[MirrorModule], mod_id: str, **kw: Any) -> ContractError:
    args: dict[str, Any] = {"config": km.CONFIG, "grants": km.GRANTS}
    if not issubclass(module, km.Weather):
        args = {}
    args.update(kw)
    async with ModuleHarness(module, **args) as h:
        h.http.mock("GET", km.URL, json=km.forecast_json())
        with pytest.raises(ContractError) as info:
            await h.check_contract()
    assert mod_id in info.value.ids, str(info.value)
    return info.value


async def test_mod_06_writes_outside_storage_fail() -> None:
    err = await expect_failure(km.WritesOutside, "MOD-06")
    assert "mm3-kit-outside.txt" in str(err)
    os.remove("/tmp/mm3-kit-outside.txt")  # noqa: S108


async def test_mod_10_io_in_init_fails() -> None:
    await expect_failure(km.IoInInit, "MOD-10")


async def test_mod_11_blocking_callback_fails() -> None:
    await expect_failure(km.Blocks, "MOD-11")


async def test_mod_13_stop_not_idempotent_fails() -> None:
    await expect_failure(km.StopTwiceFails, "MOD-13")


async def test_mod_14_swallowed_cancel_fails() -> None:
    await expect_failure(km.SwallowsCancel, "MOD-14")


async def test_mod_15_module_level_state_fails() -> None:
    await expect_failure(km.GlobalState, "MOD-15")


async def test_mod_16_secret_in_snapshot_logs_and_payloads_fails() -> None:
    err = await expect_failure(km.LeaksSecret, "MOD-16")
    assert {"MOD-42", "MOD-83"} <= set(err.ids)
    assert "not ISO 8601 UTC" in str(err)


async def test_mod_24_answering_undeclared_request_fails() -> None:
    await expect_failure(km.AnswersEverything, "MOD-24")


async def test_mod_25_request_from_on_request_fails() -> None:
    await expect_failure(km.AsksInRequest, "MOD-25")


async def test_mod_26_crash_on_failed_request_fails() -> None:
    await expect_failure(km.CrashesOnRemoteError, "MOD-26")


async def test_mod_27_fragile_hide_show_fails() -> None:
    await expect_failure(km.FragileSuspend, "MOD-27")


async def test_mod_29_undeclared_notification_fails() -> None:
    await expect_failure(km.Undeclared, "MOD-29")


async def test_mod_42_payload_checked_against_models() -> None:
    class BadDeclared(km.Weather):
        name = "kitbaddecl"
        emits: ClassVar[Any] = {"SHOW_ALERT": km.Forecast, "WEATHER_UPDATED": km.Forecast}

    await expect_failure(BadDeclared, "MOD-29")


async def test_mod_43_strict_handler_fails() -> None:
    await expect_failure(km.StrictHandler, "MOD-43")


async def test_mod_70_undeclared_capability_fails() -> None:
    await expect_failure(km.UndeclaredCapability, "MOD-70")


async def test_mod_72_crash_without_grants_fails() -> None:
    await expect_failure(km.CrashesWithoutGrants, "MOD-72")


async def test_mod_72_no_warning_without_grants_fails() -> None:
    class Silent(km.Weather):
        name = "kitsilent"

        async def start(self) -> None:
            self.ctx.scheduler.every(600, self.refresh, name="refresh")

        async def refresh(self) -> None:
            try:
                await self.ctx.http.get(km.URL)
            except (PermissionError, HttpConnectionError):
                return

    await expect_failure(Silent, "MOD-72")


async def test_mod_83_contract_error_lists_ids_and_notes() -> None:
    async with weather() as h:  # no mock: every fresh refresh call is unmocked
        results = await run_kit_checks(h)
    notes = [f for r in results for f in r.findings if f.startswith("note:")]
    assert any("unmocked HTTP call: GET https://api.example.com/forecast" in n for n in notes)
    err = ContractError([results[0]], ["extra"])
    assert err.ids == ["MOD-06"]
    assert "extra" in str(err)


async def test_mod_15_kit_patches_are_restored() -> None:
    init, on_request = km.Weather.__init__, km.Weather.on_request
    async with weather() as h:
        h.http.mock("GET", km.URL, json=km.forecast_json())
        await h.check_contract()
    assert km.Weather.__init__ is init
    assert km.Weather.on_request is on_request
    assert "time" not in vars(asyncio.get_running_loop())
