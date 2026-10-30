"""Shared notification registry: names used by many modules and their payloads.

Every registered name has a Pydantic model; its JSON Schema is published
here (:func:`json_schema`) and the engine validates the payload of every
registered name when it is sent (:func:`validate_payload`). Unregistered
names are not validated. Unknown payload fields are accepted, so a name can
gain optional fields without a new name (CORE-226) and handlers tolerate
them (MOD-43).

Implements:
    CORE-224, CORE-225, CORE-226: Core spec 0.19, §5.5 Shared notification registry.
    -: Core spec 0.9, §5.5 Shared notification registry.
    CORE-245: Core spec 0.9, §6.3 Hot reload.
    MOD-40: Module Contract 1.0 rev 6, §6.1 Messaging.
    MOD-24, MOD-29: Module Contract 1.0 rev 8, §6.1 Messaging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from mm3.messages import NAME_PATTERN

__all__ = [
    "REGISTRY",
    "ConfigChanged",
    "ConfigError",
    "ConfigErrorItem",
    "CorePageChanged",
    "HideAlert",
    "LocaleChanged",
    "LocalizedTextValue",
    "NoPayload",
    "PageInfoPayload",
    "PageSet",
    "PresenceEvent",
    "RegisteredName",
    "ShowAlert",
    "WeatherCurrent",
    "WeatherPlace",
    "declaration_problems",
    "json_schema",
    "validate_payload",
]

LocalizedTextValue = str | dict[str, str]
"""``LocalizedText`` in a payload: a string or a map from BCP 47 tags to strings."""


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class PresenceEvent(_Payload):
    """Payload of ``PRESENCE_DETECTED`` and ``PRESENCE_CLEARED``.

    Attributes:
        sensor: Sensor id.
        zone: Optional zone of the sensor.
    """

    sensor: str
    zone: str | None = None


class ShowAlert(_Payload):
    """Payload of ``SHOW_ALERT``.

    Attributes:
        id: Optional id, for ``HIDE_ALERT``.
        title: Optional title.
        message: The message.
        level: Severity.
        timeout_s: Seconds the alert stays; 0 = until hidden.
    """

    id: str | None = None
    title: LocalizedTextValue | None = None
    message: LocalizedTextValue
    level: Literal["info", "warning", "critical"]
    timeout_s: float | None = Field(default=None, ge=0)


class HideAlert(_Payload):
    """Payload of ``HIDE_ALERT``.

    Attributes:
        id: Id given to ``SHOW_ALERT``.
    """

    id: str


class NoPayload(_Payload):
    """Empty payload (``PAGE_NEXT``, ``PAGE_PREV``)."""


class PageSet(_Payload):
    """Payload of ``PAGE_SET``.

    Attributes:
        page: Page id.
    """

    page: str


class PageInfoPayload(_Payload):
    """One page in ``CORE_PAGE_CHANGED``.

    Attributes:
        id: Page id.
        title: Resolved title.
    """

    id: str
    title: str


class CorePageChanged(_Payload):
    """Payload of ``CORE_PAGE_CHANGED`` (engine only).

    Attributes:
        page: Current page id.
        index: Its position, from 0.
        count: Number of pages.
        pages: Ids and titles of all pages.
        reason: Why the page changed.
    """

    page: str
    index: int = Field(ge=0)
    count: int = Field(ge=1)
    pages: list[PageInfoPayload]
    reason: str


@dataclass(frozen=True)
class RegisteredName:
    """One row of the registry.

    Attributes:
        name: Notification name.
        kind: ``event`` or ``command``.
        model: Payload model.
        engine_only: Only the engine emits it.
    """

    name: str
    kind: Literal["event", "command"]
    model: type[BaseModel]
    engine_only: bool = False


def _rows(*rows: RegisteredName) -> Mapping[str, RegisteredName]:
    return MappingProxyType({r.name: r for r in rows})


class WeatherPlace(_Payload):
    """The place of a ``WEATHER_CURRENT`` payload.

    Attributes:
        lat: Latitude in degrees.
        lon: Longitude in degrees.
        name: Optional name.
    """

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    name: LocalizedTextValue | None = None


class WeatherCurrent(_Payload):
    """Payload of ``WEATHER_CURRENT`` (core spec 5.5, WTH-11).

    Attributes:
        location: Where the weather was observed.
        condition: One of the closed condition set (WTH-05).
        is_day: Day or night.
        temperature: In °C or °F according to ``units``.
        feels_like: Optional apparent temperature.
        humidity: Optional relative humidity, 0-100.
        wind_speed: Optional, in km/h or mph according to ``units``.
        units: ``metric`` or ``imperial``.
        observed_at: Aware ISO 8601 time of the observation.
    """

    location: WeatherPlace
    condition: Literal[
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
    is_day: bool
    temperature: float
    feels_like: float | None = None
    humidity: float | None = Field(default=None, ge=0, le=100)
    wind_speed: float | None = Field(default=None, ge=0)
    units: Literal["metric", "imperial"]
    observed_at: AwareDatetime


class ConfigChanged(_Payload):
    """Payload of ``CORE_CONFIG_CHANGED`` (core spec 6.3).

    Attributes:
        version: The configuration version now applied.
        sections: Changed sections (CORE-216).
    """

    version: int
    sections: list[str]


class ConfigErrorItem(_Payload):
    """One error of ``CORE_CONFIG_ERROR``.

    Attributes:
        file: File of the error.
        line: 1-based line, 0 when unknown.
        path: JSON Pointer into the configuration.
        code: ``CORE_CFG_*`` code.
    """

    file: str | None
    line: int
    path: str
    code: str


class ConfigError(_Payload):
    """Payload of ``CORE_CONFIG_ERROR`` (CORE-209).

    Attributes:
        count: Number of errors.
        errors: The first 20 errors.
    """

    count: int
    errors: list[ConfigErrorItem] = Field(max_length=20)


class LocaleChanged(_Payload):
    """Payload of ``CORE_LOCALE_CHANGED`` (CORE-245).

    Attributes:
        locale: The new locale: ``engine.locale`` when broadcast, the
            instance's locale when sent to one instance.
    """

    locale: str


REGISTRY: Mapping[str, RegisteredName] = _rows(
    RegisteredName("PRESENCE_DETECTED", "event", PresenceEvent),
    RegisteredName("PRESENCE_CLEARED", "event", PresenceEvent),
    RegisteredName("SHOW_ALERT", "event", ShowAlert),
    RegisteredName("HIDE_ALERT", "event", HideAlert),
    RegisteredName("PAGE_NEXT", "command", NoPayload),
    RegisteredName("PAGE_PREV", "command", NoPayload),
    RegisteredName("PAGE_SET", "command", PageSet),
    RegisteredName("CORE_PAGE_CHANGED", "event", CorePageChanged, engine_only=True),
    RegisteredName("CORE_CONFIG_CHANGED", "event", ConfigChanged, engine_only=True),
    RegisteredName("CORE_CONFIG_ERROR", "event", ConfigError, engine_only=True),
    RegisteredName("CORE_LOCALE_CHANGED", "event", LocaleChanged, engine_only=True),
    RegisteredName("WEATHER_CURRENT", "event", WeatherCurrent),
)
"""Registered names, as in the core spec §5.5 table (CORE-224)."""


def json_schema(name: str) -> dict[str, Any]:
    """Return the published JSON Schema of a registered name's payload.

    Args:
        name: Registered notification name.

    Returns:
        The JSON Schema.

    Raises:
        KeyError: The name is not registered.
    """
    return REGISTRY[name].model.model_json_schema()


def validate_payload(name: str, payload: Mapping[str, Any]) -> None:
    """Check the payload of a registered name; unregistered names pass.

    Args:
        name: Notification name.
        payload: Its payload.

    Raises:
        ValueError: The name is registered and the payload does not match.

    Implements:
        CORE-225: Core spec 0.7, §5.5 Shared notification registry.
    """
    row = REGISTRY.get(name)
    if row is None:
        return
    try:
        row.model.model_validate(dict(payload))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<payload>'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(
            f"payload of {name} does not match its registered schema: {problems}"
        ) from None


def _is_model(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


def declaration_problems(cls: object) -> list[str]:
    """Check a module class's ``emits``, ``consumes`` and ``answers``.

    A declaration must be a mapping from an ``UPPER_SNAKE`` name to a
    Pydantic model (``answers``: a pair of request and reply models); a
    registered name must use the registry's model (§5.5); a module neither
    emits nor answers an engine-only name. Used by the test kit and by
    ``mm3 module check``; the engine itself validates only registered
    payloads (CORE-225).

    Args:
        cls: The module class.

    Returns:
        One message per problem, empty when the declarations are sound.

    Implements:
        MOD-24, MOD-29: Module Contract 1.0 rev 8, §6.1 Messaging.
    """
    problems: list[str] = []
    for attr in ("emits", "consumes", "answers"):
        table = getattr(cls, attr, {})
        if not isinstance(table, Mapping):
            problems.append(f"{attr} is not a mapping of names to payload models")
            continue
        for name, value in table.items():
            where = f"{attr}[{name!r}]"
            if not isinstance(name, str) or not NAME_PATTERN.match(name):
                problems.append(f"{where}: name is not UPPER_SNAKE")
                continue
            row = REGISTRY.get(name)
            if attr != "consumes" and (name.startswith("CORE_") or (row and row.engine_only)):
                problems.append(f"{where}: {name} is reserved for the engine")
            if attr == "answers":
                pair_ok = (
                    isinstance(value, tuple)
                    and len(value) == 2
                    and all(_is_model(v) for v in value)
                )
                if not pair_ok:
                    problems.append(f"{where}: expected (request model, reply model)")
                    continue
                model: object = value[0]
            else:
                if not _is_model(value):
                    problems.append(f"{where}: expected a Pydantic model")
                    continue
                model = value
            if row is not None and model is not row.model:
                wanted = f"{row.model.__module__}.{row.model.__name__}"
                problems.append(f"{where}: {name} is registered; use {wanted}")
    return problems
