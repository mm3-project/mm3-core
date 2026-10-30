"""mm3-weather: the Open-Meteo provider and the normalised data.

Implements:
    WTH-03, WTH-04, WTH-05: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from helpers import ROME, URL, fixture
from mm3_weather.models import CONDITIONS, WeatherRequest
from mm3_weather.open_meteo import CODES, OpenMeteo
from mm3_weather.providers import load_provider

from mm3.errors import HttpStatusError, TransientError
from mm3.http import Response


class Http:
    """A minimal ``ctx.http`` returning one body."""

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.body, self.status = body, status
        self.params: dict[str, str] = {}

    async def get(self, url: str, **kw: Any) -> Response:
        import json

        assert url == URL
        self.params = kw["params"]
        return Response(self.status, json.dumps(self.body).encode(), url)


def request(**kw: Any) -> WeatherRequest:
    args: dict[str, Any] = {
        "lat": ROME["lat"],
        "lon": ROME["lon"],
        "units": "metric",
        "timezone": "Europe/Rome",
        "days": 5,
        "hours": 8,
    }
    args.update(kw)
    return WeatherRequest(**args)


class Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def debug(self, event: str, **f: Any) -> None:
        self.records.append(("debug", event, f))

    def warning(self, event: str, **f: Any) -> None:
        self.records.append(("warning", event, f))


async def test_wth_04_current_daily_and_hourly_are_normalised() -> None:
    data = await OpenMeteo().fetch(Http(fixture()), request())
    c = data.current
    assert (c.condition, c.is_day, c.temperature) == ("partly_cloudy", True, 18.4)
    assert (c.feels_like, c.humidity, c.wind_speed, c.wind_direction) == (17.1, 62, 14.5, 220)
    assert c.observed_at.isoformat() == "2026-10-30T14:00:00+01:00"
    assert len(data.daily) == 5
    first = data.daily[0]
    assert (first.date, first.condition, first.temp_min, first.temp_max) == (
        "2026-10-30",
        "partly_cloudy",
        11.0,
        19.2,
    )
    assert first.sunrise is not None
    assert first.sunrise.isoformat() == "2026-10-30T06:41:00+01:00"
    assert first.precipitation_probability == 5
    assert data.daily[2].condition == "rain"
    assert data.hourly[0].time.utcoffset() == timedelta(hours=1)
    assert data.hourly[3].is_day is False


async def test_wth_04_times_follow_the_requested_time_zone() -> None:
    data = await OpenMeteo().fetch(Http(fixture()), request(timezone="America/New_York"))
    assert data.current.observed_at.utcoffset() == timedelta(hours=-4)


async def test_wth_04_response_without_optional_fields() -> None:
    data = await OpenMeteo().fetch(Http(fixture("minimal")), request())
    assert data.current.feels_like is None
    assert data.current.humidity is None
    assert data.daily[0].sunrise is None
    assert data.daily[0].precipitation_probability is None
    assert data.hourly[0].precipitation_probability is None


async def test_wth_04_units_are_converted_by_the_provider_request() -> None:
    http = Http(fixture())
    await OpenMeteo().fetch(http, request(units="imperial"))
    assert http.params["temperature_unit"] == "fahrenheit"
    assert http.params["wind_speed_unit"] == "mph"
    assert http.params["precipitation_unit"] == "inch"
    await OpenMeteo().fetch(http, request())
    assert http.params["temperature_unit"] == "celsius"
    assert http.params["wind_speed_unit"] == "kmh"
    assert http.params["forecast_days"] == "5"
    assert http.params["forecast_hours"] == "8"
    assert http.params["timezone"] == "Europe/Rome"


def test_wth_05_every_mapped_condition_is_in_the_closed_set() -> None:
    assert set(CODES.values()) <= set(CONDITIONS)
    assert len(CONDITIONS) == 12


@pytest.mark.parametrize(("code", "condition"), sorted(CODES.items()))
async def test_wth_05_wmo_codes_map_to_conditions(code: int, condition: str) -> None:
    body = fixture()
    body["current"]["weather_code"] = code
    data = await OpenMeteo().fetch(Http(body), request())
    assert data.current.condition == condition


async def test_wth_05_unmapped_code_is_cloudy_and_logged_once_per_refresh() -> None:
    body = fixture()
    body["current"]["weather_code"] = 42
    body["hourly"]["weather_code"] = [42] * 48
    provider = OpenMeteo()
    provider.log = Log()
    data = await provider.fetch(Http(body), request())
    assert data.current.condition == "cloudy"
    assert data.hourly[0].condition == "cloudy"
    assert provider.log.records == [("debug", "condition_unmapped", {"codes": [42]})]


@pytest.mark.parametrize("status", [500, 503, 429, 404])
async def test_wth_08_http_errors_reach_the_module_as_status_errors(status: int) -> None:
    with pytest.raises(HttpStatusError) as caught:
        await OpenMeteo().fetch(Http({}, status), request())
    assert caught.value.status == status


async def test_wth_08_unreadable_answer_is_transient() -> None:
    with pytest.raises(TransientError) as caught:
        await OpenMeteo().fetch(Http({"current": {"time": "2026-10-30T14:00"}}), request())
    assert caught.value.message_key == "WEATHER_UNAVAILABLE"


def test_wth_03_builtin_provider_and_unknown_name() -> None:
    from mm3 import ConfigurationError

    assert load_provider("open_meteo", None) is OpenMeteo  # type: ignore[arg-type]
    assert OpenMeteo.url_prefix == "https://api.open-meteo.com/"
    with pytest.raises(ConfigurationError) as caught:
        load_provider("nope", None)  # type: ignore[arg-type]
    assert (caught.value.field, caught.value.message_key) == ("provider", "CFG_PROVIDER_UNKNOWN")
