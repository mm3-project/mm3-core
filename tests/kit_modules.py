"""Modules for the test-kit tests: one that follows the contract, and its broken twins."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr

from mm3 import MirrorModule, ModuleConfig, Notification, TransientError
from mm3.errors import HttpError, RemoteError, RequestTimeoutError
from mm3.mqtt import MqttMessage
from mm3.notifications import PresenceEvent


class WeatherConfig(ModuleConfig):
    api_key: SecretStr = Field(json_schema_extra={"secret": True})
    lat: float = Field(42.1, ge=-90, le=90)


class Forecast(BaseModel):
    temp_c: float
    at: str


class Ask(BaseModel):
    city: str | None = None


class Answer(BaseModel):
    temp_c: float


URL = "https://api.example.com/forecast"


class Weather(MirrorModule):
    """Follows the contract: declares, degrades, logs events, keeps no globals."""

    name = "kitweather"
    config_model = WeatherConfig
    capabilities: ClassVar[set[str]] = {"http.outbound", "mqtt.publish", "mqtt.subscribe"}
    emits: ClassVar[Any] = {"WEATHER_UPDATED": Forecast}
    consumes: ClassVar[Any] = {"PRESENCE_DETECTED": PresenceEvent}
    answers: ClassVar[Any] = {"WEATHER_NOW": (Ask, Answer)}

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.temp: float | None = None
        self.seen: list[str] = []
        self.sub: Any = None

    async def start(self) -> None:
        self.ctx.scheduler.every(600, self.refresh, name="refresh")
        try:
            self.sub = await self.ctx.mqtt.subscribe("home/+/temp", self.on_temp)
        except PermissionError:
            self.ctx.log.warning("mqtt_not_granted")

    async def stop(self) -> None:
        if self.sub is not None:
            self.sub.cancel()

    async def refresh(self) -> None:
        try:
            resp = await self.ctx.http.get(URL, params={"lat": str(self.config.lat)})
            resp.raise_for_status()
        except PermissionError:
            self.ctx.log.warning("http_not_granted")
            return
        except HttpError as exc:
            raise TransientError("WEATHER_OFFLINE") from exc
        self.temp = float(resp.json()["temp"])
        at = self.ctx.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        self.ctx.set_state({"temp": self.temp})
        self.ctx.health.set("last_refresh", at)
        await self.ctx.send_notification("WEATHER_UPDATED", {"temp_c": self.temp, "at": at})
        try:
            await self.ctx.mqtt.publish("mirror/weather", {"temp_c": self.temp})
        except PermissionError:
            self.ctx.log.warning("mqtt_not_granted")
        self.ctx.log.info("forecast_fetched", temp=self.temp)

    async def on_temp(self, msg: MqttMessage) -> None:
        self.seen.append(msg.topic)
        self.ctx.patch_state({"room": msg.json().get("t")})

    async def on_notification(self, n: Notification) -> None:
        if n.name == "PRESENCE_DETECTED":
            self.ctx.patch_state({"sensor": n.payload.get("sensor")})

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        if req.name == "WEATHER_NOW":
            return {"temp_c": self.temp if self.temp is not None else 0.0}
        return None

    def snapshot(self) -> dict[str, Any]:
        return {"temp": self.temp}


class Asker(MirrorModule):
    """Asks another module and copes with timeouts and errors (MOD-26)."""

    name = "kitasker"

    async def start(self) -> None:
        self.ctx.scheduler.every(60, self.ask, name="ask")

    async def ask(self) -> None:
        try:
            reply = await self.ctx.request("CLOCK_NOW", {}, timeout=2)
        except (RequestTimeoutError, RemoteError) as exc:
            self.ctx.log.warning("clock_unavailable", error=type(exc).__name__)
            return
        self.ctx.set_state({"time": reply.get("time")})


# --- broken twins ----------------------------------------------------------------

_SHARED: list[str] = []


class WritesOutside(Weather):
    name = "kitwrites"

    async def start(self) -> None:
        await super().start()
        path = "/tmp/mm3-kit-outside.txt"  # noqa: S108 - the violation under test
        await self.ctx.run_blocking(_write, path)


def _write(path: str) -> None:
    with open(path, "w") as f:
        f.write("x")


class IoInInit(Weather):
    name = "kitinit"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        with open(__file__) as f:  # the violation under test
            self.source = f.read(10)


class Blocks(Weather):
    name = "kitblocks"

    async def on_notification(self, n: Notification) -> None:
        import time

        time.sleep(0.15)  # noqa: ASYNC251 - the violation under test


class StopTwiceFails(Weather):
    name = "kitstop"

    async def stop(self) -> None:
        if getattr(self, "_stopped", False):
            raise RuntimeError("already stopped")
        self._stopped = True


class SwallowsCancel(Weather):
    name = "kitswallow"

    async def start(self) -> None:
        await super().start()
        self.ctx.create_task(self.loop(), name="loop")

    async def loop(self) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return  # the violation under test


class GlobalState(Weather):
    name = "kitglobal"

    async def start(self) -> None:
        await super().start()
        _SHARED.append(self.ctx.instance_id)  # the violation under test


class LeaksSecret(Weather):
    name = "kitleak"

    def snapshot(self) -> dict[str, Any]:
        return {"key": self.config.api_key.get_secret_value()}

    async def refresh(self) -> None:
        self.ctx.log.info("using_key", key=self.config.api_key.get_secret_value())
        await self.ctx.send_notification(
            "WEATHER_UPDATED",
            {"temp_c": 1.0, "at": "2026-01-01 13:00", "k": self.config.api_key.get_secret_value()},
        )


class AnswersEverything(Weather):
    name = "kitanswers"

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        return {"temp_c": 1.0}


class AsksInRequest(Weather):
    name = "kitasks"

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        if req.name == "WEATHER_NOW":
            with contextlib.suppress(RequestTimeoutError, RemoteError):
                await self.ctx.request("CLOCK_NOW", {}, timeout=1)
            return {"temp_c": 0.0}
        return None


class CrashesOnRemoteError(Asker):
    name = "kitcrash"

    async def ask(self) -> None:
        reply = await self.ctx.request("CLOCK_NOW", {}, timeout=2)
        self.ctx.set_state({"time": reply.get("time")})


class FragileSuspend(Weather):
    name = "kitfragile"

    async def resume(self) -> None:
        self.resumes = getattr(self, "resumes", 0) + 1
        if self.resumes > 3:
            raise RuntimeError("resumed too often")


class Undeclared(Weather):
    name = "kitundeclared"
    emits: ClassVar[Any] = {}


class StrictHandler(Weather):
    name = "kitstrict"

    async def on_notification(self, n: Notification) -> None:
        if n.name == "PRESENCE_DETECTED":
            if set(n.payload) - {"sensor", "zone"}:
                raise KeyError("unknown field")
            self.ctx.patch_state({"zone": n.payload["zone"]})


class UndeclaredCapability(Weather):
    name = "kitcaps"
    capabilities: ClassVar[set[str]] = {"mqtt.publish", "mqtt.subscribe"}


class CrashesWithoutGrants(Weather):
    name = "kitnogrants"

    async def refresh(self) -> None:
        resp = await self.ctx.http.get(URL)
        self.temp = float(resp.json()["temp"])


def forecast_json(temp: float = 18.4) -> dict[str, Any]:
    return {"temp": temp}


GRANTS = {
    "http": ["https://api.example.com/"],
    "mqtt": {"subscribe": ["home/*"], "publish": ["mirror/*"]},
}

CONFIG = {"api_key": "dummy", "lat": 42.1}


def iso(dt: datetime) -> str:
    return json.dumps(dt.isoformat())
