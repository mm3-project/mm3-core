"""Configuration of the weather module.

Implements:
    WTH-02, WTH-03: Core spec 0.19, §9.4 Reference modules.
    MOD-45, MOD-46, MOD-48, MOD-49: Module Contract 1.0 rev 12, §6.2 Configuration.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mm3 import LocalizedText, ModuleConfig


class Place(BaseModel):
    """A place: latitude, longitude and an optional name."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90, title="CFG_LAT", description="CFG_LAT_HELP")
    lon: float = Field(ge=-180, le=180, title="CFG_LON", description="CFG_LON_HELP")
    name: LocalizedText | None = Field(None, title="CFG_NAME", description="CFG_NAME_HELP")


class WeatherConfig(ModuleConfig):
    """Configuration of :class:`mm3_weather.WeatherModule`."""

    type: Literal["current", "daily", "hourly"] = Field(
        "current", title="CFG_TYPE", description="CFG_TYPE_HELP"
    )
    provider: str = Field("open_meteo", title="CFG_PROVIDER", description="CFG_PROVIDER_HELP")
    provider_options: dict[str, Any] = Field(
        default_factory=dict, title="CFG_PROVIDER_OPTIONS", description="CFG_PROVIDER_OPTIONS_HELP"
    )
    location: Place | None = Field(None, title="CFG_LOCATION", description="CFG_LOCATION_HELP")
    units: Literal["metric", "imperial"] | None = Field(
        None, title="CFG_UNITS", description="CFG_UNITS_HELP"
    )
    refresh: timedelta = Field(
        timedelta(minutes=10),
        ge=timedelta(minutes=5),
        title="CFG_REFRESH",
        description="CFG_REFRESH_HELP",
    )
    days: int = Field(5, ge=1, le=16, title="CFG_DAYS", description="CFG_DAYS_HELP")
    hours: int = Field(8, ge=1, le=48, title="CFG_HOURS", description="CFG_HOURS_HELP")
    hour_step: int = Field(1, ge=1, le=6, title="CFG_HOUR_STEP", description="CFG_HOUR_STEP_HELP")
    emit: bool = Field(True, title="CFG_EMIT", description="CFG_EMIT_HELP")
    show_feels_like: bool = Field(
        True, title="CFG_SHOW_FEELS_LIKE", description="CFG_SHOW_FEELS_LIKE_HELP"
    )
    show_humidity: bool = Field(
        False, title="CFG_SHOW_HUMIDITY", description="CFG_SHOW_HUMIDITY_HELP"
    )
    show_wind: bool = Field(False, title="CFG_SHOW_WIND", description="CFG_SHOW_WIND_HELP")
    show_precipitation: bool = Field(
        False, title="CFG_SHOW_PRECIPITATION", description="CFG_SHOW_PRECIPITATION_HELP"
    )
    show_sun: bool = Field(False, title="CFG_SHOW_SUN", description="CFG_SHOW_SUN_HELP")
