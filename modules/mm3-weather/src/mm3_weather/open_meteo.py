"""The built-in provider: Open-Meteo, no API key.

The WMO weather codes map to the closed condition set like this (WTH-05):

==============  ==============================  ===================================
Condition       WMO codes                       Meaning
==============  ==============================  ===================================
clear           0, 1                            clear, mainly clear
partly_cloudy   2                               partly cloudy
cloudy          3                               overcast; also any unmapped code
fog             45, 48                          fog, rime fog
drizzle         51, 53, 55                      drizzle
rain            61, 63, 80, 81                  slight and moderate rain, showers
heavy_rain      65, 82                          heavy rain, violent showers
freezing_rain   56, 57, 66, 67                  freezing drizzle and rain
snow            71, 73, 75, 77, 85, 86          snow, snow grains, snow showers
sleet           (none)                          Open-Meteo has no sleet code
thunderstorm    95                              thunderstorm
hail            96, 99                          thunderstorm with hail
==============  ==============================  ===================================

Implements:
    WTH-03, WTH-04, WTH-05: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from mm3 import TransientError

from .models import Current, Daily, Hourly, WeatherData, WeatherRequest

URL = "https://api.open-meteo.com/v1/forecast"
CODES: dict[int, str] = {
    **dict.fromkeys((0, 1), "clear"),
    2: "partly_cloudy",
    3: "cloudy",
    **dict.fromkeys((45, 48), "fog"),
    **dict.fromkeys((51, 53, 55), "drizzle"),
    **dict.fromkeys((61, 63, 80, 81), "rain"),
    **dict.fromkeys((65, 82), "heavy_rain"),
    **dict.fromkeys((56, 57, 66, 67), "freezing_rain"),
    **dict.fromkeys((71, 73, 75, 77, 85, 86), "snow"),
    95: "thunderstorm",
    **dict.fromkeys((96, 99), "hail"),
}
CURRENT = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,"
    "weather_code,wind_speed_10m,wind_direction_10m"
)
DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
    "precipitation_sum,precipitation_probability_max"
)
HOURLY = "temperature_2m,weather_code,is_day,precipitation_probability"


class OpenMeteo:
    """Open-Meteo as a provider (WTH-03)."""

    name: ClassVar[str] = "open_meteo"
    options_model: ClassVar[type[BaseModel] | None] = None
    url_prefix: ClassVar[str] = "https://api.open-meteo.com/"

    log: Any = None
    """The module's logger, set by the module before the first fetch."""

    async def fetch(self, http: Any, request: WeatherRequest) -> WeatherData:
        """One request for the current, daily and hourly blocks (WTH-07).

        Raises:
            mm3.errors.HttpError: Network failure, timeout or an error status.
            TransientError: ``WEATHER_UNAVAILABLE`` when the answer cannot be read.
        """
        imperial = request.units == "imperial"
        response = await http.get(
            URL,
            params={
                "latitude": f"{request.lat:.4f}",
                "longitude": f"{request.lon:.4f}",
                "current": CURRENT,
                "daily": DAILY,
                "hourly": HOURLY,
                "timezone": request.timezone,
                "forecast_days": str(request.days),
                "forecast_hours": str(request.hours),
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
                "precipitation_unit": "inch" if imperial else "mm",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        unmapped: set[int] = set()
        try:
            data = self._parse(response.json(), request, unmapped)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            if self.log is not None:
                self.log.warning("weather_response_unreadable", error=str(exc)[:200])
            raise TransientError("WEATHER_UNAVAILABLE") from exc
        if unmapped and self.log is not None:
            self.log.debug("condition_unmapped", codes=sorted(unmapped))
        return data

    @staticmethod
    def _condition(code: Any, unmapped: set[int]) -> str:
        if isinstance(code, int) and code in CODES:
            return CODES[code]
        unmapped.add(code if isinstance(code, int) else -1)
        return "cloudy"

    @classmethod
    def _parse(
        cls, body: dict[str, Any], request: WeatherRequest, unmapped: set[int]
    ) -> WeatherData:
        zone = ZoneInfo(request.timezone)

        def when(text: str) -> datetime:
            return datetime.fromisoformat(text).replace(tzinfo=zone)

        def optional(block: dict[str, Any], key: str, index: int | None = None) -> Any:
            value = block.get(key)
            if index is not None and isinstance(value, list):
                value = value[index] if index < len(value) else None
            return value

        c = body["current"]
        current = Current(
            observed_at=when(c["time"]),
            condition=cls._condition(c["weather_code"], unmapped),  # type: ignore[arg-type]
            is_day=bool(c["is_day"]),
            temperature=c["temperature_2m"],
            feels_like=c.get("apparent_temperature"),
            humidity=c.get("relative_humidity_2m"),
            wind_speed=c.get("wind_speed_10m"),
            wind_direction=c.get("wind_direction_10m"),
            precipitation=c.get("precipitation"),
        )
        daily: list[Daily] = []
        d = body.get("daily") or {}
        for i, day in enumerate(d.get("time", [])):
            sunrise, sunset = optional(d, "sunrise", i), optional(d, "sunset", i)
            daily.append(
                Daily(
                    date=day,
                    condition=cls._condition(d["weather_code"][i], unmapped),  # type: ignore[arg-type]
                    temp_min=d["temperature_2m_min"][i],
                    temp_max=d["temperature_2m_max"][i],
                    precipitation_probability=optional(d, "precipitation_probability_max", i),
                    precipitation_sum=optional(d, "precipitation_sum", i),
                    sunrise=when(sunrise) if sunrise else None,
                    sunset=when(sunset) if sunset else None,
                )
            )
        hourly: list[Hourly] = []
        h = body.get("hourly") or {}
        for i, hour in enumerate(h.get("time", [])):
            hourly.append(
                Hourly(
                    time=when(hour),
                    condition=cls._condition(h["weather_code"][i], unmapped),  # type: ignore[arg-type]
                    is_day=bool(h["is_day"][i]),
                    temperature=h["temperature_2m"][i],
                    precipitation_probability=optional(h, "precipitation_probability", i),
                )
            )
        return WeatherData(current=current, daily=daily, hourly=hourly)
