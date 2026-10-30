"""Normalised weather data shared by the providers and the module.

Implements:
    WTH-04, WTH-05: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Condition = Literal[
    "clear",
    "partly_cloudy",
    "cloudy",
    "fog",
    "drizzle",
    "rain",
    "heavy_rain",
    "freezing_rain",
    "snow",
    "sleet",
    "thunderstorm",
    "hail",
]
CONDITIONS: tuple[str, ...] = (
    "clear",
    "partly_cloudy",
    "cloudy",
    "fog",
    "drizzle",
    "rain",
    "heavy_rain",
    "freezing_rain",
    "snow",
    "sleet",
    "thunderstorm",
    "hail",
)
Units = Literal["metric", "imperial"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Current(_Model):
    """The weather now (WTH-04).

    Attributes:
        observed_at: When it was observed (aware).
        condition: One of :data:`CONDITIONS`.
        is_day: Day or night variant.
        temperature: °C or °F.
        feels_like: Apparent temperature.
        humidity: 0-100.
        wind_speed: km/h or mph.
        wind_direction: Degrees the wind comes from.
        precipitation: mm or inch in the last hour.
    """

    observed_at: AwareDatetime
    condition: Condition
    is_day: bool
    temperature: float
    feels_like: float | None = None
    humidity: float | None = Field(default=None, ge=0, le=100)
    wind_speed: float | None = Field(default=None, ge=0)
    wind_direction: float | None = Field(default=None, ge=0, le=360)
    precipitation: float | None = Field(default=None, ge=0)


class Daily(_Model):
    """One day of a forecast (WTH-04)."""

    date: str
    condition: Condition
    temp_min: float
    temp_max: float
    precipitation_probability: float | None = Field(default=None, ge=0, le=100)
    precipitation_sum: float | None = Field(default=None, ge=0)
    sunrise: AwareDatetime | None = None
    sunset: AwareDatetime | None = None


class Hourly(_Model):
    """One hour of a forecast (WTH-04)."""

    time: AwareDatetime
    condition: Condition
    is_day: bool
    temperature: float
    precipitation_probability: float | None = Field(default=None, ge=0, le=100)


class WeatherData(_Model):
    """Everything one refresh returns (WTH-04): the current block is always present."""

    current: Current
    daily: list[Daily] = Field(default_factory=list)
    hourly: list[Hourly] = Field(default_factory=list)


@dataclass(frozen=True)
class WeatherRequest:
    """What a provider is asked for (WTH-03).

    Attributes:
        lat: Latitude.
        lon: Longitude.
        units: ``metric`` or ``imperial``; the provider converts (WTH-04).
        timezone: IANA name; the times of the answer are in it.
        days: Daily entries wanted (1-16).
        hours: Hours of hourly data wanted, from now.
        options: The module's ``provider_options``, validated by the provider's model.
    """

    lat: float
    lon: float
    units: Units
    timezone: str
    days: int
    hours: int
    options: BaseModel | None = None
