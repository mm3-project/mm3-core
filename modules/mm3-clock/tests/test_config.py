"""mm3-clock: the module's metadata and configuration rules.

Implements:
    CLK-01, CLK-02, CLK-03, CLK-06: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from typing import Any

import pytest
from mm3_clock import ClockConfig, ClockModule
from pydantic import ValidationError

from mm3.core.config.records import code_for_pydantic


def codes(config: dict[str, Any]) -> list[tuple[str, str]]:
    """The (code, field) of every error a config produces."""
    with pytest.raises(ValidationError) as caught:
        ClockConfig.model_validate(config)
    return [(code_for_pydantic(e)[0], str(e["loc"][-1])) for e in caught.value.errors()]


def test_clk_01_front_end_only_module() -> None:
    assert ClockModule.element == "mm3-clock"
    assert ClockModule.min_size_px == (180, 80)
    assert ClockModule.scripts == ["clock.js"]
    assert "start" not in vars(ClockModule)
    assert "on_notification" not in vars(ClockModule)
    assert not getattr(ClockModule, "capabilities", None)


def test_clk_02_defaults() -> None:
    c = ClockConfig.model_validate({})
    assert (c.display, c.show_seconds, c.time_format) == ("digital", True, "locale")
    assert (c.show_date, c.date_style, c.show_week) == (True, "long", False)
    assert (c.timezone, c.zones, c.face) == (None, [], "ticks")
    assert (c.sun, c.moon, c.location) == (False, False, None)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("display", "round"), ("time_format", "13"), ("date_style", "tiny"), ("face", "roman")],
)
def test_clk_02_values_outside_the_sets_are_choice_errors(field: str, bad: str) -> None:
    assert codes({field: bad}) == [("CORE_CFG_CHOICE", field)]


@pytest.mark.parametrize(("given", "stored"), [(12, "12"), (24, "24"), ("12", "12")])
def test_clk_02_yaml_numbers_for_time_format(given: object, stored: str) -> None:
    assert ClockConfig.model_validate({"time_format": given}).time_format == stored


def test_clk_02_unknown_time_zone() -> None:
    assert codes({"timezone": "Mars/Olympus"}) == [("CORE_CFG_TIMEZONE", "timezone")]


def test_clk_03_zones_hold_at_most_four() -> None:
    zone = {"timezone": "Asia/Tokyo"}
    assert len(ClockConfig.model_validate({"zones": [zone] * 4}).zones) == 4
    assert codes({"zones": [zone] * 5}) == [("CORE_CFG_LENGTH", "zones")]


def test_clk_03_zone_label_is_localized_text() -> None:
    c = ClockConfig.model_validate(
        {"zones": [{"timezone": "Asia/Tokyo", "label": {"en": "Tokyo", "it": "Tokyo"}}]}
    )
    assert c.zones[0].label is not None
    assert codes({"zones": [{"timezone": "Nowhere/City"}]}) == [("CORE_CFG_TIMEZONE", "timezone")]


@pytest.mark.parametrize("place", [{"lat": 91, "lon": 0}, {"lat": 0, "lon": -181}])
def test_clk_06_location_range(place: dict[str, float]) -> None:
    assert codes({"location": place})[0][0] == "CORE_CFG_RANGE"


def test_clk_06_location_accepts_a_place() -> None:
    place = ClockConfig.model_validate({"location": {"lat": 45.0, "lon": 9.0}}).location
    assert place is not None
    assert (place.lat, place.lon) == (45.0, 9.0)
