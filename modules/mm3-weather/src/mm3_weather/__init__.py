"""Weather module: current conditions, daily and hourly forecasts.

The backend fetches from a provider (Open-Meteo is built in) every ``refresh``,
publishes the data of the instance's ``type`` as state, keeps the last good data
in storage and sends ``WEATHER_CURRENT`` for other modules.

Implements:
    WTH-01, WTH-02, WTH-03, WTH-07, WTH-08, WTH-09, WTH-11: Core spec 0.19,
        §9.4 Reference modules.
    CORE-289: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from mm3 import ConfigurationError, MirrorModule, ModuleConfig, TransientError
from mm3.errors import HttpError, HttpStatusError
from mm3.notifications import WeatherCurrent

from .config import Place, WeatherConfig
from .models import WeatherData, WeatherRequest
from .providers import Provider, load_provider

__all__ = ["Place", "WeatherConfig", "WeatherModule"]

STORED_MAX_AGE = timedelta(hours=24)
"""Stored data older than this is dropped after a restart (WTH-08)."""


class WeatherModule(MirrorModule):
    """Fetches weather and publishes it to its element (WTH-01)."""

    name: ClassVar[str] = "weather"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = WeatherConfig
    element: ClassVar[str | None] = "mm3-weather"
    scripts: ClassVar[list[str]] = ["weather.js"]
    styles: ClassVar[list[str]] = ["weather.css"]
    capabilities: ClassVar[set[str]] = {"http.outbound"}
    emits: ClassVar[dict[str, type[BaseModel]]] = {"WEATHER_CURRENT": WeatherCurrent}  # type: ignore[assignment]
    min_size_px: ClassVar[tuple[int, int] | None] = (200, 100)

    config: WeatherConfig  # type: ignore[assignment]

    async def start(self) -> None:
        """Resolve location and provider, show stored data, then refresh on a schedule."""
        cfg = self.config
        place = cfg.location or self.ctx.location
        if place is None:
            raise ConfigurationError("location", "CFG_LOCATION_REQUIRED")
        self._place = {"lat": place.lat, "lon": place.lon}
        name = getattr(place, "name", None)
        if name is not None:
            # a LocalizedText from the config, a plain value from engine.location
            self._place["name"] = getattr(name, "value", name)
        provider_cls = load_provider(cfg.provider, self.ctx)
        self._options: BaseModel | None = None
        if provider_cls.options_model is not None:
            try:
                self._options = provider_cls.options_model.model_validate(cfg.provider_options)
            except ValidationError as exc:
                raise ConfigurationError(
                    "provider_options", "CFG_PROVIDER_OPTIONS_INVALID"
                ) from exc
        self._provider: Provider = provider_cls()
        self._provider.log = self.ctx.log  # type: ignore[attr-defined]
        self._units = cfg.units or self.ctx.units
        await self._restore()
        self.ctx.scheduler.every(cfg.refresh, self._refresh, start_now=True, name="refresh")

    # --- stored data (WTH-08) ------------------------------------------------------

    async def _restore(self) -> None:
        stored = await self.ctx.storage.kv_get("last")
        if not isinstance(stored, dict):
            return
        try:
            age = self.ctx.now() - datetime.fromisoformat(stored["fetched_at"])
        except (KeyError, ValueError, TypeError):
            await self.ctx.storage.kv_delete("last")
            return
        if age > STORED_MAX_AGE:
            await self.ctx.storage.kv_delete("last")
            return
        if stored.get("type") == self.config.type and isinstance(stored.get("state"), dict):
            self.ctx.set_state(stored["state"])  # shown as stale until the first success

    # --- refresh (WTH-07) ----------------------------------------------------------

    async def _refresh(self) -> None:
        cfg = self.config
        request = WeatherRequest(
            lat=self._place["lat"],
            lon=self._place["lon"],
            units=self._units,
            timezone=self.ctx.timezone.key,
            days=cfg.days,
            hours=cfg.hours * cfg.hour_step,
            options=self._options,
        )
        try:
            data = await self._provider.fetch(self.ctx.http, request)
        except PermissionError:
            self.ctx.log.warning("grant_missing", prefix=self._provider.url_prefix)
            self.ctx.set_state({"error": "grant", "grant": self._provider.url_prefix})
            return
        except HttpStatusError as exc:
            self._failed(exc.status)
            raise self._error_for(exc.status) from exc
        except HttpError as exc:
            self._failed(None)
            raise TransientError("WEATHER_UNAVAILABLE") from exc
        except TransientError:
            self._failed(None)
            raise
        await self._publish(data)

    def _failed(self, status: int | None) -> None:
        self.ctx.log.warning("weather_refresh_failed", status=status)
        if not self.ctx.state:
            self.ctx.set_state({"error": "unavailable"})

    @staticmethod
    def _error_for(status: int) -> Exception:
        if status == 429 or status >= 500:
            return TransientError("WEATHER_UNAVAILABLE")
        # 400 and 422 mean the coordinates were refused; the rest the provider or its key
        if status in (400, 422):
            return ConfigurationError("location", "CFG_LOCATION_REFUSED")
        return ConfigurationError("provider", "CFG_PROVIDER_REFUSED")

    async def _publish(self, data: WeatherData) -> None:
        cfg = self.config
        now = self.ctx.now()
        if cfg.type == "current":
            block: Any = data.current.model_dump(mode="json")
            items = 1
        elif cfg.type == "daily":
            block = [d.model_dump(mode="json") for d in data.daily[: cfg.days]]
            items = len(block)
        else:
            block = [h.model_dump(mode="json") for h in data.hourly[:: cfg.hour_step][: cfg.hours]]
            items = len(block)
        state = {
            "type": cfg.type,
            "units": self._units,
            "location": self._place,
            "fetched_at": now.isoformat(),
            "data": block,
        }
        self.ctx.set_state(state)
        self.ctx.health.set("last_refresh", now.isoformat())
        self.ctx.health.set("items", items)
        self.ctx.log.info("weather_refreshed", provider=cfg.provider, type=cfg.type, items=items)
        await self.ctx.storage.kv_set(
            "last", {"fetched_at": now.isoformat(), "type": cfg.type, "state": state}
        )
        if cfg.emit:
            await self.ctx.send_notification("WEATHER_CURRENT", self._payload(data))

    def _payload(self, data: WeatherData) -> dict[str, Any]:
        c = data.current
        payload: dict[str, Any] = {
            "location": self._place,
            "condition": c.condition,
            "is_day": c.is_day,
            "temperature": c.temperature,
            "units": self._units,
            "observed_at": c.observed_at.astimezone(UTC).isoformat(),  # MOD-42: UTC
        }
        for key, value in (
            ("feels_like", c.feels_like),
            ("humidity", c.humidity),
            ("wind_speed", c.wind_speed),
        ):
            if value is not None:
                payload[key] = value
        return payload

    def snapshot(self) -> dict[str, Any]:
        """Latest state for newly connected clients."""
        return dict(self.ctx.state)
