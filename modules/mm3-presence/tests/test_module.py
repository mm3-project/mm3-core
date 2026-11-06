"""mm3-presence: states, sensors, schedule, control surface, Home Assistant, drivers.

Implements:
    PRS-01, PRS-02, PRS-03, PRS-04, PRS-05, PRS-06, PRS-07, PRS-08: Core spec 0.34,
        §9.2 Presence and power: module boundary.
    CORE-53: Core spec 0.34, §9.2 Presence and power: module boundary.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from helpers import RecordingDriver, presence, states
from mm3_presence import PresenceModule

from mm3 import ConfigurationError

MQTT = [{"topic": "home/pir", "field": "occupancy"}]


async def test_prs_01_starts_on_and_tells_everyone() -> None:
    async with presence() as h:
        assert states(h) == [("on", "start")]
        assert h.system.display_calls == [True]
        assert not h.suspended


async def test_prs_01_a_second_instance_is_refused() -> None:
    async with presence() as h:
        assert h.instance is not None
        ctx = h.instance.ctx
        # A second instance of the same engine: the same <state>/modules directory.
        other = SimpleNamespace(
            instance_id="presence_two",
            storage=SimpleNamespace(path=ctx.storage.path.parent / "presence_two"),
        )
        second = PresenceModule(other, PresenceModule.config_model())  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError) as info:
            await second.start()
        assert info.value.message_key == "CFG_SINGLE_INSTANCE"
        # The running one is not disturbed, and a restart of it is allowed.
        await h.restart()
        assert h.instance_state.value == "RUNNING"


async def test_prs_04_dims_then_turns_off_and_suspends(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = RecordingDriver()
    cfg = {"idle_after": "5m", "off_after": "15m", "idle_brightness": 20, "brightness": 80}
    async with presence(cfg, driver=driver, monkeypatch=monkeypatch) as h:
        await h.advance(minutes=5)
        assert states(h)[-1] == ("idle", "timeout")
        assert ("brightness", 20) in driver.calls
        await h.advance(minutes=10)
        assert states(h)[-1] == ("off", "timeout")
        assert driver.calls[-1] == ("power", False)
        assert h.suspended


async def test_prs_04_a_touch_wakes_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = RecordingDriver()
    cfg = {"idle_after": "5m", "off_after": "15m", "brightness": 80}
    async with presence(cfg, driver=driver, monkeypatch=monkeypatch) as h:
        await h.advance(minutes=15)
        assert h.suspended
        await h.notify("CORE_INTERACTION", {"kind": "touch"})
        assert states(h)[-1] == ("on", "presence")
        assert not h.suspended
        assert driver.calls[-2:] == [("power", True), ("brightness", 80)]


async def test_prs_03_touch_can_be_disabled() -> None:
    async with presence({"off_after": "10m", "touch": False}) as h:
        await h.advance(minutes=10)
        await h.notify("CORE_INTERACTION", {"kind": "touch"})
        assert states(h)[-1] == ("off", "timeout")


async def test_prs_03_held_presence_keeps_the_display_on() -> None:
    async with presence({"off_after": "10m"}) as h:
        await h.notify("PRESENCE_DETECTED", {"sensor": "pir"}, sender="x")
        await h.advance(minutes=60)
        assert states(h) == [("on", "start")]
        await h.notify("PRESENCE_CLEARED", {"sensor": "pir"}, sender="x")
        await h.advance(minutes=10)
        assert states(h)[-1] == ("off", "timeout")


async def test_prs_03_mqtt_json_field() -> None:
    async with presence({"off_after": "10m", "mqtt": MQTT}) as h:
        await h.advance(minutes=10)
        assert h.suspended
        await h.mqtt.inject("home/pir", {"occupancy": False})
        assert h.suspended
        await h.mqtt.inject("home/pir", {"occupancy": True})
        assert states(h)[-1] == ("on", "presence")
        await h.advance(minutes=60)
        assert states(h)[-1] == ("on", "presence")
        await h.mqtt.inject("home/pir", {"occupancy": False})
        await h.advance(minutes=10)
        assert states(h)[-1] == ("off", "timeout")


async def test_prs_03_mqtt_plain_text_and_value() -> None:
    cfg = {"off_after": "10m", "mqtt": [{"topic": "home/door", "value": "open"}]}
    async with presence(cfg) as h:
        await h.advance(minutes=10)
        await h.mqtt.inject("home/door", "closed")
        assert h.suspended
        await h.mqtt.inject("home/door", "OPEN")
        assert not h.suspended


async def test_prs_05_schedule_turns_off_and_on() -> None:
    cfg = {"schedule": [{"off": "23:00", "on": "06:30"}]}
    async with presence(cfg) as h:
        await h.notify("PRESENCE_DETECTED", {"sensor": "pir"}, sender="x")
        await h.advance(to="2026-10-30T23:00:00+01:00")
        assert states(h)[-1] == ("off", "schedule")
        assert h.suspended
        await h.notify("CORE_INTERACTION", {"kind": "touch"})
        assert states(h)[-1] == ("off", "schedule")
        await h.advance(to="2026-10-31T06:30:00+01:00")
        assert states(h)[-1] == ("on", "schedule")
        assert not h.suspended


async def test_prs_05_wake_in_schedule() -> None:
    cfg = {"schedule": [{"off": "23:00", "on": "06:30"}], "wake_in_schedule": True}
    async with presence(cfg) as h:
        await h.advance(to="2026-10-30T23:00:00+01:00")
        assert states(h)[-1] == ("off", "schedule")
        await h.notify("CORE_INTERACTION", {"kind": "touch"})
        assert states(h)[-1] == ("on", "presence")


async def test_prs_05_starting_inside_a_window_is_off() -> None:
    cfg = {"schedule": [{"off": "11:00", "on": "13:00"}]}
    async with presence(cfg) as h:
        assert states(h) == [("off", "schedule")]
        assert h.suspended
        assert h.system.display_calls == [False]


async def test_prs_05_days_are_start_days() -> None:
    cfg = {"schedule": [{"off": "23:00", "on": "06:00", "days": ["sat"]}]}
    async with presence(cfg) as h:  # Friday
        await h.notify("PRESENCE_DETECTED", {"sensor": "pir"}, sender="x")
        await h.advance(to="2026-10-30T23:30:00+01:00")
        assert not h.suspended
        await h.advance(to="2026-10-31T23:00:00+01:00")
        assert h.suspended


async def test_prs_08_routes() -> None:
    async with presence() as h:
        status, body = await h.route("GET", "state")
        assert status == 200
        assert body["state"] == "on"
        assert body["driver"] == "kiosk"
        status, body = await h.route("POST", "display/off")
        assert (status, body["state"], body["reason"]) == (200, "off", "command")
        assert h.suspended
        status, body = await h.route("POST", "display/on")
        assert (status, body["state"], body["reason"]) == (200, "on", "command")
        assert not h.suspended


async def test_prs_08_home_assistant_entities() -> None:
    async with presence() as h:
        ents = {k[1]: e for k, e in h.mqtt.entities.items()}
        assert set(ents) == {"display", "presence", "state"}
        assert ents["display"].state == "ON"
        await h.mqtt.command("presence_test", "display", "OFF")
        assert ents["display"].state == "OFF"
        assert ents["state"].state == "off"
        await h.mqtt.command("presence_test", "display", "ON")
        assert ents["display"].state == "ON"


async def test_prs_08_brightness_entity_only_with_a_dimmable_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = RecordingDriver()
    async with presence(driver=driver, monkeypatch=monkeypatch) as h:
        assert ("presence_test", "brightness") in h.mqtt.entities
        await h.mqtt.command("presence_test", "brightness", "42")
        assert ("brightness", 42) in driver.calls


async def test_prs_08_no_bridge_is_not_an_error() -> None:
    h = presence()
    h.mqtt.enabled = False
    async with h:
        assert states(h) == [("on", "start")]
        assert not h.mqtt.entities


async def test_prs_06_driver_failure_is_health_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = RecordingDriver()
    async with presence({"off_after": "10m"}, driver=driver, monkeypatch=monkeypatch) as h:
        driver.fail = True
        await h.advance(minutes=10)
        assert states(h)[-1] == ("off", "timeout")
        assert h.health()["fields"]["driver_error"] == "boom"
        assert h.health()["fields"]["driver"] == "recording"


async def test_prs_06_check_reason_goes_to_health(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = RecordingDriver()
    driver.reason = "no monitor"
    async with presence(driver=driver, monkeypatch=monkeypatch) as h:
        assert h.health()["fields"]["driver_error"] == "no monitor"


async def test_prs_06_unknown_driver_is_a_configuration_error() -> None:
    async with presence({"display": "nope"}) as h:
        assert h.instance_state.value == "DISABLED"
        assert "CFG_DISPLAY_UNKNOWN" in h.health()["last_error"]["message"]


async def test_prs_02_idle_must_be_below_off() -> None:
    with pytest.raises(ValueError, match="CFG_IDLE_BELOW_OFF"):
        async with presence({"idle_after": "20m", "off_after": "10m"}):
            pass


async def test_core_53_the_module_is_a_suspender_while_off() -> None:
    async with presence() as h:
        power = h.engine.supervisor.power
        assert power.holders == {"presence_test"}
        await h.route("POST", "display/off")
        assert power.suspenders == {"presence_test"}
        assert h.suspended


async def test_prs_02_a_topic_without_a_grant_is_skipped_not_fatal() -> None:
    async with presence({"off_after": "10m", "mqtt": MQTT}, grants={}) as h:
        assert h.instance_state.value == "RUNNING"
        assert h.logs.at_least("warning")
        fields = h.health()["fields"]
        assert fields["mqtt_topics_without_grant"] == ["home/pir"]
        assert "mqtt_error" in fields
        await h.advance(minutes=10)
        assert states(h)[-1] == ("off", "timeout")
        await h.notify("CORE_INTERACTION", {"kind": "touch"})
        assert states(h)[-1] == ("on", "presence")


async def test_mod_contract_checks_pass() -> None:
    async with presence({"mqtt": MQTT}) as h:
        await h.check_contract()


async def test_prs_07_state_is_broadcast_at_start_and_on_every_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = {"idle_after": "5m", "off_after": "15m", "schedule": [{"off": "13:00", "on": "14:00"}]}
    async with presence(cfg, driver=RecordingDriver(), monkeypatch=monkeypatch) as h:
        await h.advance(minutes=5)
        await h.advance(minutes=10)
        await h.notify("CORE_INTERACTION", {"kind": "touch"})
        await h.route("POST", "display/off")
        await h.route("POST", "display/on")
        await h.notify("PRESENCE_DETECTED", {"sensor": "pir"}, sender="x")  # keeps it on
        await h.advance(to="2026-10-30T13:00:00+01:00")
        await h.advance(to="2026-10-30T14:00:00+01:00")
        assert states(h) == [
            ("on", "start"),
            ("idle", "timeout"),
            ("off", "timeout"),
            ("on", "presence"),
            ("off", "command"),
            ("on", "command"),
            ("off", "schedule"),
            ("on", "schedule"),
        ]
        assert not h.violations
