"""mm3-weather: configuration rules.

Implements:
    WTH-01, WTH-02: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from mm3_weather import WeatherConfig, WeatherModule
from pydantic import ValidationError

from mm3.core.config.records import code_for_pydantic


def codes(config: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(ValidationError) as caught:
        WeatherConfig.model_validate(config)
    return [(code_for_pydantic(e)[0], str(e["loc"][-1])) for e in caught.value.errors()]


def test_wth_01_full_module_with_the_http_capability() -> None:
    assert WeatherModule.element == "mm3-weather"
    assert WeatherModule.capabilities == {"http.outbound"}
    assert set(WeatherModule.emits) == {"WEATHER_CURRENT"}


def test_wth_02_defaults() -> None:
    c = WeatherConfig.model_validate({})
    assert (c.type, c.provider, c.location, c.units) == ("current", "open_meteo", None, None)
    assert c.refresh == timedelta(minutes=10)
    assert (c.days, c.hours, c.hour_step, c.emit) == (5, 8, 1, True)
    assert (c.show_feels_like, c.show_humidity, c.show_wind) == (True, False, False)
    assert (c.show_precipitation, c.show_sun) == (False, False)


def test_wth_02_type_is_a_closed_set() -> None:
    assert codes({"type": "weekly"}) == [("CORE_CFG_CHOICE", "type")]
    assert codes({"units": "kelvin"}) == [("CORE_CFG_CHOICE", "units")]


def test_wth_02_refresh_is_at_least_five_minutes() -> None:
    assert WeatherConfig.model_validate({"refresh": 300}).refresh == timedelta(minutes=5)
    assert codes({"refresh": 299}) == [("CORE_CFG_RANGE", "refresh")]


@pytest.mark.parametrize(
    ("field", "low", "high"), [("days", 1, 16), ("hours", 1, 48), ("hour_step", 1, 6)]
)
def test_wth_02_ranges(field: str, low: int, high: int) -> None:
    assert getattr(WeatherConfig.model_validate({field: low}), field) == low
    assert getattr(WeatherConfig.model_validate({field: high}), field) == high
    assert codes({field: low - 1}) == [("CORE_CFG_RANGE", field)]
    assert codes({field: high + 1}) == [("CORE_CFG_RANGE", field)]


def test_wth_02_location_with_a_name() -> None:
    c = WeatherConfig.model_validate(
        {"location": {"lat": 45.0, "lon": 9.0, "name": {"en": "Milan", "it": "Milano"}}}
    )
    assert c.location is not None
    assert (c.location.lat, c.location.lon) == (45.0, 9.0)
    assert codes({"location": {"lat": 95, "lon": 0}})[0][0] == "CORE_CFG_RANGE"
