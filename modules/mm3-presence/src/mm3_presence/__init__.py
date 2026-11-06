"""Presence and power module: sensors, idle dimming, schedule and display drivers.

The module keeps one state, ``on``, ``idle`` or ``off``, and tells the engine
and other modules about it. Presence comes from MQTT topics, from
``PRESENCE_DETECTED`` and ``PRESENCE_CLEARED`` sent by other modules and from
touches on the kiosk (``CORE_INTERACTION``). While presence is held the state
is ``on``; from the moment it is no longer held, or from a moment of presence,
the module counts: after ``idle_after`` the display dims, after ``off_after``
it is turned off through a driver and the engine is asked to suspend. Schedule
windows switch the display off by the clock.

Implements:
    PRS-01, PRS-02, PRS-03, PRS-04, PRS-05, PRS-06, PRS-07, PRS-08: Core spec 0.34,
        §9.2 Presence and power: module boundary.
    CORE-52, CORE-53, CORE-346, CORE-347: Core spec 0.34, §9.2 Presence and power:
        module boundary.
    CORE-289: Core spec 0.34, §9.4 Reference modules.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from mm3 import ConfigurationError, MirrorModule, ModuleConfig
from mm3.errors import TransientError
from mm3.mqtt import HaEntity, MqttMessage
from mm3.notifications import CoreInteraction, PresenceEvent, PresenceState

from .config import MqttSource, PresenceConfig, Window
from .drivers import DisplayDriver, load_driver
from .schedule import in_window, next_boundary

__all__ = ["MqttSource", "PresenceConfig", "PresenceModule", "Window"]

State = Literal["on", "idle", "off"]
Reason = Literal["start", "presence", "timeout", "schedule", "command"]

_RUNNING: dict[Path, str] = {}
"""The running instance by engine, keyed by the engine's ``<state>/modules`` directory.

The Module API has no list of instances, so the directory above an instance's own
storage tells engines apart (PRS-01); the dictionary is empty whenever no instance runs.
"""


class PresenceModule(MirrorModule):
    """Presence and display power (PRS-01)."""

    name: ClassVar[str] = "presence"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = PresenceConfig
    capabilities: ClassVar[set[str]] = {
        "system.power",
        "mqtt.subscribe",
        "ha.discovery",
        "api.routes",
    }
    subscribes: ClassVar[set[str] | None] = {
        "PRESENCE_DETECTED",
        "PRESENCE_CLEARED",
        "CORE_INTERACTION",
    }
    emits: ClassVar[dict[str, type[BaseModel]]] = {"PRESENCE_STATE": PresenceState}  # type: ignore[assignment]
    consumes: ClassVar[dict[str, type[BaseModel]]] = {  # type: ignore[assignment]
        "PRESENCE_DETECTED": PresenceEvent,
        "PRESENCE_CLEARED": PresenceEvent,
        "CORE_INTERACTION": CoreInteraction,
    }

    config: PresenceConfig  # type: ignore[assignment]

    # --- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Build the driver, subscribe to the topics, offer the routes and begin as ``on``."""
        iid = self.ctx.instance_id
        engine = self.ctx.storage.path.parent
        if _RUNNING.get(engine, iid) != iid:
            raise ConfigurationError("instance", "CFG_SINGLE_INSTANCE")
        _RUNNING[engine] = iid
        try:
            await self._start()
        except BaseException:
            _RUNNING.pop(engine, None)
            raise

    async def stop(self) -> None:
        """Release the instance slot; the display is left as it is."""
        engine = self.ctx.storage.path.parent
        if _RUNNING.get(engine) == self.ctx.instance_id:
            del _RUNNING[engine]

    async def _start(self) -> None:
        cfg = self.config
        self._driver: DisplayDriver = load_driver(cfg.display, self.ctx, cfg.display_options)
        self._lock = asyncio.Lock()
        self._state: State = "off"  # a placeholder; start() always sets the first state
        self._reason: Reason = "start"
        self._since: datetime = self.ctx.now()
        self._held: set[str] = set()
        self._last_activity: datetime = self._since
        self._level: int | None = None
        self._level_before_idle: int | None = None
        self._override: int | None = None
        self._check_error: str | None = None
        self._call_error: str | None = None
        self._inside = False
        self._entities: dict[str, HaEntity] = {}
        self._count_job: Any = None
        self._window_job: Any = None
        self.ctx.health.set("driver", self._driver.name)
        await self._check_driver()
        await self._subscribe()
        self._routes()
        await self._register_entities()
        async with self._lock:
            now = self.ctx.now()
            self._inside = in_window(cfg.schedule, self.ctx.timezone, now) is not None
            if self._inside:
                await self._to_off("schedule", start=True)
            else:
                await self._enter_on("start", was="start")
            self._arm()

    # --- inputs: topics, notifications, touches (PRS-03) ----------------------------

    async def _subscribe(self) -> None:
        """Subscribe to the configured topics; a topic without a grant is skipped (MOD-72)."""
        missing: list[str] = []
        for source in self.config.mqtt:
            try:
                await self.ctx.mqtt.subscribe(source.topic, self._topic_handler(source))
            except PermissionError:
                self.ctx.log.warning("mqtt_grant_missing", topic=source.topic)
                missing.append(source.topic)
        if missing:
            self.ctx.health.set("mqtt_error", self.ctx.t("CFG_MQTT_GRANT_MISSING"))
            self.ctx.health.set("mqtt_topics_without_grant", missing)

    def _topic_handler(self, source: MqttSource) -> Any:
        async def handle(message: MqttMessage) -> None:
            await self._presence(f"mqtt:{message.topic}", _matches(source, message))

        return handle

    async def on_notification(self, n: Any) -> None:
        """Presence from other modules and touches on the kiosk (PRS-03)."""
        if n.name in ("PRESENCE_DETECTED", "PRESENCE_CLEARED"):
            sensor = n.payload.get("sensor")
            if isinstance(sensor, str) and sensor:
                await self._presence(f"{n.source}:{sensor}", n.name == "PRESENCE_DETECTED")
        elif n.name == "CORE_INTERACTION" and self.config.touch:
            async with self._lock:
                await self._moment("presence")

    async def _presence(self, key: str, present: bool) -> None:
        """Set or clear presence for one source (PRS-03)."""
        async with self._lock:
            before = bool(self._held)
            if present:
                self._held.add(key)
                await self._moment("presence")
            else:
                self._held.discard(key)
                if before and not self._held:
                    self._last_activity = self.ctx.now()
                    self._arm()
            if before != bool(self._held):
                await self._publish_entities()

    # --- state changes (PRS-04, PRS-05); callers hold the lock ----------------------

    async def _moment(self, reason: Reason) -> None:
        """A moment of presence: wake up and start counting again."""
        self._last_activity = self.ctx.now()
        if self._inside and not self.config.wake_in_schedule:
            return  # presence is ignored inside a window (PRS-05)
        if self._state != "on":
            await self._enter_on(reason, was=self._state)
        self._arm()

    async def _enter_on(self, reason: Reason, *, was: State | Literal["start"]) -> None:
        """Turn the display on (from ``off``) and restore the brightness (PRS-04)."""
        self.ctx.system.request_resume("presence")
        if was in ("off", "start"):
            await self._drive("set_power", True)
        await self._restore_brightness()
        await self._set_state("on", reason)

    async def _to_idle(self) -> None:
        cfg = self.config
        if self._driver.supports_brightness:
            if cfg.brightness is None and self._override is None:
                getter = getattr(self._driver, "get_brightness", None)
                if getter is not None:
                    self._level_before_idle = await self._drive("get_brightness")
                else:
                    self._level_before_idle = self._level
            await self._drive("set_brightness", cfg.idle_brightness)
            self._level = cfg.idle_brightness
        await self._set_state("idle", "timeout")

    async def _to_off(self, reason: Reason, *, start: bool = False) -> None:
        """Turn the display off through the driver and ask the engine to suspend (PRS-04)."""
        if self._state != "off" or start:
            await self._drive("set_power", False)
            self.ctx.system.request_suspend("presence")
        await self._set_state("off", reason)

    async def _restore_brightness(self) -> None:
        if not self._driver.supports_brightness:
            return
        target = self._override if self._override is not None else self.config.brightness
        if target is None:
            target = self._level_before_idle
        self._level_before_idle = None
        if target is not None:
            await self._drive("set_brightness", target)
            self._level = target

    async def _drive(self, method: str, *args: Any) -> Any:
        """Call the driver; a failure is logged and shown in the health, never fatal (PRS-06)."""
        try:
            result = await getattr(self._driver, method)(*args)
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            self.ctx.log.warning(
                "driver_failed", driver=self._driver.name, call=method, reason=reason
            )
            self._call_error = reason
            self._show_driver_error()
            return None
        if self._call_error is not None:
            self._call_error = None
            self._show_driver_error()
        return result

    async def _check_driver(self) -> None:
        try:
            reason = await self._driver.check()
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
        if reason:
            self.ctx.log.warning("driver_unavailable", driver=self._driver.name, reason=reason)
            self._check_error = reason
            self._show_driver_error()

    def _show_driver_error(self) -> None:
        """Show why the driver does not work in the health record (CORE-312)."""
        self.ctx.health.set("driver_error", self._call_error or self._check_error)

    async def _set_state(self, state: State, reason: Reason) -> None:
        changed = (state, reason) != (self._state, self._reason)
        self._state, self._reason = state, reason
        if not changed:
            return
        self._since = self.ctx.now()
        self.ctx.log.info("presence_state", state=state, reason=reason)
        await self._announce()
        await self._publish_entities()

    async def _announce(self) -> None:
        """Broadcast ``PRESENCE_STATE`` (PRS-07)."""
        await self.ctx.send_notification(
            "PRESENCE_STATE", {"state": self._state, "reason": self._reason}
        )

    # --- timers --------------------------------------------------------------------

    def _arm(self) -> None:
        """Plan the next count deadline and the next schedule boundary."""
        cfg = self.config
        for job in (self._count_job, self._window_job):
            if job is not None:
                job.cancel()
        self._count_job = self._window_job = None
        if not self._held and self._state in ("on", "idle"):
            deadlines = [self._last_activity + cfg.off_after]
            if self._state == "on" and cfg.idle_after:
                deadlines.append(self._last_activity + cfg.idle_after)
            self._count_job = self.ctx.scheduler.at(min(deadlines), self._count_due, name="count")
        boundary = next_boundary(cfg.schedule, self.ctx.timezone, self.ctx.now())
        if boundary is not None:
            self._window_job = self.ctx.scheduler.at(boundary, self._window_due, name="window")

    async def _count_due(self) -> None:
        cfg = self.config
        async with self._lock:
            if self._held or self._state == "off":
                return
            elapsed = self.ctx.now() - self._last_activity
            if elapsed >= cfg.off_after:
                await self._to_off("timeout")
            elif self._state == "on" and cfg.idle_after and elapsed >= cfg.idle_after:
                await self._to_idle()
            self._arm()

    async def _window_due(self) -> None:
        cfg = self.config
        async with self._lock:
            now = self.ctx.now()
            inside = in_window(cfg.schedule, self.ctx.timezone, now) is not None
            if inside and not self._inside:
                self._inside = True
                if not (cfg.wake_in_schedule and self._held):
                    await self._to_off("schedule")
            elif not inside and self._inside:
                self._inside = False
                self._last_activity = now
                await self._enter_on("schedule", was=self._state)
            self._arm()

    # --- control (PRS-08) -----------------------------------------------------------

    def _view(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "reason": self._reason,
            "since": self._since.isoformat(),
            "held": sorted(self._held),
            "driver": self._driver.name,
        }

    def _routes(self) -> None:
        router = self.ctx.router

        @router.get("/state")
        async def state() -> dict[str, Any]:
            return self._view()

        @router.post("/display/on")
        async def display_on() -> dict[str, Any]:
            await self._command(True)
            return self._view()

        @router.post("/display/off")
        async def display_off() -> dict[str, Any]:
            await self._command(False)
            return self._view()

    async def _command(self, on: bool) -> None:
        """``display/on`` is a moment of presence, ``display/off`` switches off at once."""
        async with self._lock:
            if on:
                await self._moment("command")
            else:
                await self._to_off("command")
                self._arm()

    # --- Home Assistant (PRS-08) ----------------------------------------------------

    async def _register_entities(self) -> None:
        try:
            self._entities["display"] = await self.ctx.mqtt.ha_entity(
                "display",
                "switch",
                name="HA_DISPLAY",
                icon="mdi:monitor",
                on_command=self._ha_display,
            )
            self._entities["presence"] = await self.ctx.mqtt.ha_entity(
                "presence", "binary_sensor", name="HA_PRESENCE", device_class="occupancy"
            )
            self._entities["state"] = await self.ctx.mqtt.ha_entity(
                "state", "sensor", name="HA_STATE", icon="mdi:motion-sensor"
            )
            if self._driver.supports_brightness:
                self._entities["brightness"] = await self.ctx.mqtt.ha_entity(
                    "brightness",
                    "number",
                    name="HA_BRIGHTNESS",
                    min=0,
                    max=100,
                    step=1,
                    unit="%",
                    on_command=self._ha_brightness,
                )
        except ConfigurationError:
            self.ctx.log.info("ha_entities_skipped", reason="no MQTT bridge")
            self._entities.clear()

    async def _ha_display(self, payload: str) -> None:
        await self._command(payload.strip().upper() == "ON")

    async def _ha_brightness(self, payload: str) -> None:
        try:
            value = max(0, min(100, round(float(payload))))
        except ValueError:
            return
        async with self._lock:
            self._override = value  # a run-time value until the next start (PRS-08)
            if self._state == "on" and self._driver.supports_brightness:
                await self._drive("set_brightness", value)
                self._level = value
            await self._publish_entities()

    async def _publish_entities(self) -> None:
        values: dict[str, str | float | bool | None] = {
            "display": self._state != "off",
            "presence": bool(self._held),
            "state": self._state,
            "brightness": self._level,
        }
        for object_id, entity in self._entities.items():
            try:
                await entity.set_state(values[object_id])
            except TransientError:
                self.ctx.log.debug("ha_state_not_sent", entity=object_id)


def _matches(source: MqttSource, message: MqttMessage) -> bool:
    """Tell whether a message on a topic says someone is there (PRS-03)."""
    if source.field is None:
        expected = "ON" if source.value is None else str(source.value)
        return message.text().strip().lower() == expected.strip().lower()
    try:
        found: Any = json.loads(message.payload)
    except ValueError:
        return False
    for part in source.field.split("."):
        if not isinstance(found, dict) or part not in found:
            return False
        found = found[part]
    expected_value: Any = True if source.value is None else source.value
    return _same(found, expected_value)


def _same(actual: Any, expected: Any) -> bool:
    """Equality that does not let ``1`` match ``true``."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    return bool(actual == expected)
