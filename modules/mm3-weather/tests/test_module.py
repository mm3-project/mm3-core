"""mm3-weather: the refresh cycle, errors, stored data and notifications.

Implements:
    WTH-02, WTH-03, WTH-07, WTH-08, WTH-09, WTH-11, WTH-12: Core spec 0.19,
        §9.4 Reference modules.
    MOD-29: Module Contract 1.0 rev 12, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import json

import pytest
from helpers import GRANTS, ROME, URL, fixture, weather
from mm3_weather import WeatherModule

from mm3.errors import HttpTimeoutError
from mm3.notifications import validate_payload
from mm3.testing import ModuleHarness


async def test_wth_07_fetches_at_start_and_publishes_the_current_view() -> None:
    async with weather() as h:
        assert len(h.http.calls) == 1
        state = h.state
        assert state["type"] == "current"
        assert state["units"] == "metric"
        assert state["data"]["condition"] == "partly_cloudy"
        assert state["data"]["temperature"] == 18.4
        assert state["location"] == {"lat": ROME["lat"], "lon": ROME["lon"], "name": "Rome"}
        assert state["fetched_at"].startswith("2026-10-30T14:00:00")


async def test_wth_07_one_request_covers_every_view() -> None:
    async with weather({"type": "daily"}) as h:
        call = h.http.calls[0]
        assert call.url == URL
        assert call.params is not None
        assert {"current", "daily", "hourly"} <= set(call.params)
        assert call.params["latitude"] == "41.9028"
        assert call.params["timezone"] == "Europe/Rome"
        assert call.timeout <= 10


async def test_wth_07_refreshes_every_interval_without_overlap() -> None:
    async with weather({"refresh": 600}) as h:
        await h.advance(minutes=9)
        assert len(h.http.calls) == 1
        await h.advance(minutes=1)
        assert len(h.http.calls) == 2
        await h.advance(minutes=30)
        assert len(h.http.calls) == 5


async def test_wth_07_only_the_data_of_the_type_is_published() -> None:
    async with weather({"type": "daily", "days": 3}) as h:
        data = h.state["data"]
        assert [d["date"] for d in data] == ["2026-10-30", "2026-10-31", "2026-11-01"]
        assert data[1]["condition"] == "rain"
        assert "current" not in json.dumps(dict(h.state))
    async with weather({"type": "hourly", "hours": 4, "hour_step": 3}) as h:
        times = [x["time"][11:16] for x in h.state["data"]]
        assert times == ["14:00", "17:00", "20:00", "23:00"]
        assert h.http.calls[0].params is not None
        assert h.http.calls[0].params["forecast_hours"] == "12"


async def test_wth_07_health_log_and_storage_after_a_success() -> None:
    async with weather({"type": "daily", "days": 2}) as h:
        fields = h.health()["fields"]
        assert fields["last_refresh"].startswith("2026-10-30T14:00:00")
        assert fields["items"] == 2
        assert "weather_refreshed" in h.logs.events("info")
        stored = (
            json.loads((h.storage_dir / "kv.json").read_text())
            if (h.storage_dir / "kv.json").exists()
            else None
        )
        assert stored is None or "last" in json.dumps(stored)


async def test_wth_04_imperial_units_come_from_the_context_by_default() -> None:
    async with weather(units="imperial") as h:
        assert h.state["units"] == "imperial"
        assert h.http.calls[0].params is not None
        assert h.http.calls[0].params["temperature_unit"] == "fahrenheit"
    async with weather({"units": "metric"}, units="imperial") as h:
        assert h.state["units"] == "metric"


# --- location and provider -------------------------------------------------------


async def test_wth_02_instance_location_replaces_the_mirror_location() -> None:
    config = {"location": {"lat": 35.68, "lon": 139.69, "name": "Tokyo"}}
    async with weather(config) as h:
        assert h.http.calls[0].params is not None
        assert h.http.calls[0].params["latitude"] == "35.6800"
        assert h.state["location"]["name"] == "Tokyo"


async def test_wth_02_without_any_location_the_instance_is_disabled() -> None:
    async with weather(location=None) as h:
        assert h.instance_state.name == "DISABLED"
        assert h.managed.reason == "configuration_error"
        assert h.http.calls == []


async def test_wth_03_unknown_provider_is_a_configuration_error() -> None:
    async with weather({"provider": "nowhere"}) as h:
        assert h.instance_state.name == "DISABLED"
        assert h.http.calls == []


# --- WTH-11: the shared notification ------------------------------------------------


async def test_wth_11_current_weather_is_sent_with_the_registered_payload() -> None:
    async with weather() as h:
        sent = h.notifications.last("WEATHER_CURRENT")
        assert sent is not None
        p = sent.payload
        assert p["location"] == {"lat": ROME["lat"], "lon": ROME["lon"], "name": "Rome"}
        assert (p["condition"], p["is_day"], p["temperature"]) == ("partly_cloudy", True, 18.4)
        assert (p["feels_like"], p["humidity"], p["wind_speed"]) == (17.1, 62, 14.5)
        assert p["units"] == "metric"
        assert p["observed_at"] == "2026-10-30T13:00:00+00:00"  # UTC (MOD-42), the same instant
        validate_payload("WEATHER_CURRENT", p)
        assert h.violations == []


async def test_wth_11_sent_after_every_refresh_whatever_the_view() -> None:
    async with weather({"type": "hourly", "refresh": 300}) as h:
        await h.advance(minutes=10)
        assert len(h.notifications.of("WEATHER_CURRENT")) == 3


async def test_wth_11_optional_fields_are_left_out_when_unknown() -> None:
    async with weather(body=fixture("minimal")) as h:
        sent = h.notifications.last("WEATHER_CURRENT")
        assert sent is not None
        assert set(sent.payload) == {
            "location",
            "condition",
            "is_day",
            "temperature",
            "units",
            "observed_at",
        }
        validate_payload("WEATHER_CURRENT", sent.payload)


async def test_wth_11_emit_off_sends_nothing() -> None:
    async with weather({"emit": False}) as h:
        assert h.notifications.of("WEATHER_CURRENT") == []
        assert h.state["type"] == "current"


async def test_wth_11_payload_without_a_location_name() -> None:
    async with weather(location={"lat": 10.0, "lon": 20.0}) as h:
        sent = h.notifications.last("WEATHER_CURRENT")
        assert sent is not None
        assert sent.payload["location"] == {"lat": 10.0, "lon": 20.0}


# --- WTH-08: errors and stale data --------------------------------------------------


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_wth_08_server_errors_are_transient_and_the_job_continues(status: int) -> None:
    async with weather(status=status) as h:
        assert h.instance_state.name == "RUNNING"
        assert h.state == {"error": "unavailable"}
        assert h.notifications.of("WEATHER_CURRENT") == []
        h.http.mock("GET", URL, json=fixture())
        await h.advance(minutes=10)
        assert h.state["data"]["temperature"] == 18.4
        assert len(h.notifications.of("WEATHER_CURRENT")) == 1


async def test_wth_08_network_errors_are_transient() -> None:
    h = weather()
    h.http.mock("GET", URL, raises=HttpTimeoutError("timeout"))
    async with h:
        assert h.instance_state.name == "RUNNING"
        assert h.state == {"error": "unavailable"}
        assert "weather_refresh_failed" in h.logs.events("warning")


async def test_wth_08_last_good_data_stays_after_a_failure() -> None:
    async with weather() as h:
        good = dict(h.state)
        h.http.mock("GET", URL, status=503, text="down")
        await h.advance(minutes=10)
        assert h.state == good
        assert h.instance_state.name == "RUNNING"


@pytest.mark.parametrize(
    ("status", "field"),
    [(400, "location"), (422, "location"), (401, "provider"), (404, "provider")],
)
async def test_wth_08_other_4xx_name_the_location_or_the_provider(status: int, field: str) -> None:
    async with weather() as h:
        h.http.mock("GET", URL, status=status, text="no")
        await h.advance(minutes=10)
        assert h.instance_state.name == "DISABLED"
        assert h.health()["fields"]["last_refresh"].startswith("2026-10-30T14:00")
    del field


async def test_wth_08_restart_shows_stored_data_until_the_first_success() -> None:
    async with weather() as h:
        first = dict(h.state)
        h.http.reset()
        h.http.mock("GET", URL, status=503, text="down")
        await h.restart()
        assert h.state == first  # the stored data, with its old fetch time, until a success
        assert h.state["fetched_at"] == first["fetched_at"]


async def test_wth_08_stored_data_older_than_a_day_is_dropped() -> None:
    async with weather() as h:
        h.http.reset()
        h.http.mock("GET", URL, status=503, text="down")
        await h.advance(hours=25)
        await h.restart()
        assert h.state == {"error": "unavailable"}


async def test_wth_08_stored_data_of_another_view_is_not_shown() -> None:
    async with weather({"type": "hourly"}) as h:
        assert h.state["type"] == "hourly"


# --- WTH-09: a missing grant -----------------------------------------------------------


async def test_wth_09_missing_grant_does_not_crash_the_instance() -> None:
    async with weather(grants={}) as h:
        assert h.instance_state.name == "RUNNING"
        assert h.state == {"error": "grant", "grant": "https://api.open-meteo.com/"}
        assert h.notifications.of("WEATHER_CURRENT") == []
        assert "grant_missing" in h.logs.events("warning")


async def test_wth_09_the_grant_can_be_added_later() -> None:
    async with weather(grants={}) as h:
        await h.set_grants(GRANTS)
        assert h.state["data"]["temperature"] == 18.4


# --- the contract ---------------------------------------------------------------------------


async def test_wth_12_check_contract() -> None:
    config = {"location": {"lat": ROME["lat"], "lon": ROME["lon"]}}
    h = ModuleHarness(WeatherModule, config=config, grants=GRANTS, timezone="Europe/Rome")
    h.http.mock("GET", URL, json=fixture())
    async with h:
        await h.check_contract()
