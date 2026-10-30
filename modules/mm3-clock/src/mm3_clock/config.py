"""Configuration of the clock module.

Implements:
    CLK-02, CLK-03, CLK-04, CLK-06: Core spec 0.19, §9.4 Reference modules.
    MOD-45, MOD-46, MOD-48, MOD-49: Module Contract 1.0 rev 12, §6.2 Configuration.
"""

from __future__ import annotations

from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from mm3 import LocalizedText, ModuleConfig


def _check_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PydanticCustomError("CORE_CFG_TIMEZONE", "{value}", {"value": value}) from exc
    return value


class Zone(BaseModel):
    """One extra time zone shown under the clock (CLK-03)."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(title="CFG_ZONE_TIMEZONE", description="CFG_ZONE_TIMEZONE_HELP")
    label: LocalizedText | None = Field(
        None, title="CFG_ZONE_LABEL", description="CFG_ZONE_LABEL_HELP"
    )

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        return _check_zone(value)


class Place(BaseModel):
    """A place for the sun and moon times (CLK-06)."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90, title="CFG_LAT", description="CFG_LAT_HELP")
    lon: float = Field(ge=-180, le=180, title="CFG_LON", description="CFG_LON_HELP")


class ClockConfig(ModuleConfig):
    """Configuration of :class:`mm3_clock.ClockModule`."""

    display: Literal["digital", "analog", "both"] = Field(
        "digital", title="CFG_DISPLAY", description="CFG_DISPLAY_HELP"
    )
    show_seconds: bool = Field(True, title="CFG_SHOW_SECONDS", description="CFG_SHOW_SECONDS_HELP")
    time_format: Literal["locale", "12", "24"] = Field(
        "locale", title="CFG_TIME_FORMAT", description="CFG_TIME_FORMAT_HELP"
    )
    show_date: bool = Field(True, title="CFG_SHOW_DATE", description="CFG_SHOW_DATE_HELP")
    date_style: Literal["short", "medium", "long", "full"] = Field(
        "long", title="CFG_DATE_STYLE", description="CFG_DATE_STYLE_HELP"
    )
    show_week: bool = Field(False, title="CFG_SHOW_WEEK", description="CFG_SHOW_WEEK_HELP")
    timezone: str | None = Field(None, title="CFG_TIMEZONE", description="CFG_TIMEZONE_HELP")
    zones: list[Zone] = Field(
        default_factory=list, max_length=4, title="CFG_ZONES", description="CFG_ZONES_HELP"
    )
    face: Literal["numbers", "ticks", "none"] = Field(
        "ticks", title="CFG_FACE", description="CFG_FACE_HELP"
    )
    sun: bool = Field(False, title="CFG_SUN", description="CFG_SUN_HELP")
    moon: bool = Field(False, title="CFG_MOON", description="CFG_MOON_HELP")
    location: Place | None = Field(None, title="CFG_LOCATION", description="CFG_LOCATION_HELP")

    @field_validator("time_format", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Any:
        """YAML reads ``12`` and ``24`` as integers."""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        return None if value is None else _check_zone(value)
