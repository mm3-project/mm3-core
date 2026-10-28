"""Shared notification registry: names used by many modules and their payloads.

Every registered name has a Pydantic model; its JSON Schema is published
here (:func:`json_schema`) and the engine validates the payload of every
registered name when it is sent (:func:`validate_payload`). Unregistered
names are not validated. Unknown payload fields are accepted, so a name can
gain optional fields without a new name (CORE-226) and handlers tolerate
them (MOD-43).

Implements:
    CORE-224, CORE-225, CORE-226: Core spec 0.7, §5.5 Shared notification registry.
    MOD-40: Module Contract 1.0 rev 6, §6.1 Messaging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "REGISTRY",
    "CorePageChanged",
    "HideAlert",
    "LocalizedTextValue",
    "NoPayload",
    "PageInfoPayload",
    "PageSet",
    "PresenceEvent",
    "RegisteredName",
    "ShowAlert",
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


REGISTRY: Mapping[str, RegisteredName] = _rows(
    RegisteredName("PRESENCE_DETECTED", "event", PresenceEvent),
    RegisteredName("PRESENCE_CLEARED", "event", PresenceEvent),
    RegisteredName("SHOW_ALERT", "event", ShowAlert),
    RegisteredName("HIDE_ALERT", "event", HideAlert),
    RegisteredName("PAGE_NEXT", "command", NoPayload),
    RegisteredName("PAGE_PREV", "command", NoPayload),
    RegisteredName("PAGE_SET", "command", PageSet),
    RegisteredName("CORE_PAGE_CHANGED", "event", CorePageChanged, engine_only=True),
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
