"""Configuration of the presence module.

Implements:
    PRS-02: Core spec 0.34, §9.2 Presence and power: module boundary.
    PRS-05: Core spec 0.34, §9.2 Presence and power: module boundary.
    MOD-45, MOD-46, MOD-48, MOD-49: Module Contract 1.0 rev 15, §6.2 Configuration.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mm3 import ModuleConfig

__all__ = ["DAYS", "MqttSource", "PresenceConfig", "Window"]

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
"""Day names of ``days``, in ``datetime.weekday()`` order (PRS-05)."""

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_CLOCK = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class MqttSource(BaseModel):
    """One MQTT topic that carries presence (PRS-03).

    Attributes:
        topic: Topic filter; needs a ``mqtt.subscribe`` grant.
        field: Dotted path into the JSON payload; without it the payload text is compared.
        value: What a matching message carries: default ``true`` with ``field``,
            ``ON`` (any case) without.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, title="CFG_MQTT_TOPIC", description="CFG_MQTT_TOPIC_HELP")
    field: str | None = Field(None, title="CFG_MQTT_FIELD", description="CFG_MQTT_FIELD_HELP")
    value: str | bool | int | float | None = Field(
        None, title="CFG_MQTT_VALUE", description="CFG_MQTT_VALUE_HELP"
    )


class Window(BaseModel):
    """A schedule window: the display is off from ``off`` to ``on`` (PRS-05).

    Attributes:
        off: Local time the window starts, ``HH:MM``.
        on: Local time it ends; not after ``off`` means the next day.
        days: The days a window starts; default every day.
    """

    model_config = ConfigDict(extra="forbid")

    off: str = Field(pattern=_CLOCK, title="CFG_WINDOW_OFF", description="CFG_WINDOW_OFF_HELP")
    on: str = Field(pattern=_CLOCK, title="CFG_WINDOW_ON", description="CFG_WINDOW_ON_HELP")
    days: list[Day] | None = Field(
        None, min_length=1, title="CFG_WINDOW_DAYS", description="CFG_WINDOW_DAYS_HELP"
    )

    @field_validator("days")
    @classmethod
    def _unique(cls, value: list[Day] | None) -> list[Day] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("CFG_DAY_REPEATED")
        return value


class PresenceConfig(ModuleConfig):
    """Configuration of :class:`mm3_presence.PresenceModule`."""

    mqtt: list[MqttSource] = Field(
        default_factory=list, title="CFG_MQTT", description="CFG_MQTT_HELP"
    )
    touch: bool = Field(True, title="CFG_TOUCH", description="CFG_TOUCH_HELP")
    idle_after: timedelta = Field(
        timedelta(0),
        ge=timedelta(0),
        title="CFG_IDLE_AFTER",
        description="CFG_IDLE_AFTER_HELP",
    )
    idle_brightness: int = Field(
        30, ge=0, le=100, title="CFG_IDLE_BRIGHTNESS", description="CFG_IDLE_BRIGHTNESS_HELP"
    )
    brightness: int | None = Field(
        None, ge=0, le=100, title="CFG_BRIGHTNESS", description="CFG_BRIGHTNESS_HELP"
    )
    off_after: timedelta = Field(
        timedelta(minutes=10),
        gt=timedelta(0),
        title="CFG_OFF_AFTER",
        description="CFG_OFF_AFTER_HELP",
    )
    schedule: list[Window] = Field(
        default_factory=list, title="CFG_SCHEDULE", description="CFG_SCHEDULE_HELP"
    )
    wake_in_schedule: bool = Field(
        False, title="CFG_WAKE_IN_SCHEDULE", description="CFG_WAKE_IN_SCHEDULE_HELP"
    )
    display: str = Field("kiosk", min_length=1, title="CFG_DISPLAY", description="CFG_DISPLAY_HELP")
    display_options: dict[str, Any] = Field(
        default_factory=dict, title="CFG_DISPLAY_OPTIONS", description="CFG_DISPLAY_OPTIONS_HELP"
    )

    @model_validator(mode="after")
    def _idle_before_off(self) -> PresenceConfig:
        if self.idle_after > timedelta(0) and self.idle_after >= self.off_after:
            raise ValueError("CFG_IDLE_BELOW_OFF")
        return self
