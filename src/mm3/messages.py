"""Message types: the ``Notification`` envelope and client messages.

One envelope is used in Python, on the WebSocket and on MQTT, so a
notification can cross any boundary unchanged.

Implements:
    CORE-20: Core spec 0.4, §5 Messaging protocol.
    CORE-111: Core spec 0.4, §5.1 Envelope.
    CORE-26: Core spec 0.4, §5.3 Routing rules.
    MOD-40, MOD-42: Module Contract 1.0 rev 4, §6.1 Messaging.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "NAME_PATTERN",
    "ClientMessage",
    "Notification",
    "check_payload",
]

MAX_PAYLOAD_BYTES = 256 * 1024
"""Largest allowed payload, as compact UTF-8 JSON (CORE-26)."""

NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
"""``UPPER_SNAKE`` notification names (MOD-40)."""


def check_payload(payload: Any) -> None:
    """Check that a payload can travel on the bus.

    Args:
        payload: The payload to check.

    Raises:
        ValueError: The payload is not a JSON object, is not
            JSON-serialisable, or is larger than 256 KiB.

    Implements:
        CORE-26: Core spec 0.4, §5.3 Routing rules.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object (dict)")
    try:
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload is not JSON-serialisable: {exc}") from exc
    size = len(encoded.encode())
    if size > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is {size} bytes, limit is {MAX_PAYLOAD_BYTES}")


class Notification(BaseModel):
    """A message on the bus.

    Attributes:
        v: Protocol version.
        id: Unique id, for deduplication.
        trace: Trace id; equals ``id`` when this notification starts a trace.
        ts: Time the sender created it (UTC).
        name: ``UPPER_SNAKE`` name, e.g. ``CALENDAR_EVENTS``.
        source: ``"<module>/<instance>"``, ``"core"``, ``"remote"`` or ``"mqtt"``.
        target: ``None`` for a broadcast, otherwise an instance id.
        payload: JSON object, at most 256 KiB.

    Implements:
        CORE-20: Core spec 0.4, §5.1 Envelope.
        CORE-111: Core spec 0.4, §10.5 Diagnostics and debugging.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = 1
    id: UUID
    trace: UUID
    ts: datetime
    name: str
    source: str
    target: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NAME_PATTERN.match(value):
            raise ValueError(f"notification name {value!r} is not UPPER_SNAKE")
        return value

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ts must be timezone-aware (UTC)")
        return value

    @field_validator("payload")
    @classmethod
    def _check_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        check_payload(value)
        return value


class ClientMessage(BaseModel):
    """A message from a module's own front-end to its backend.

    The front-end sends it with ``this.send(type, payload)`` (core spec 5.2).
    The WebSocket frame that carries it is specified with the web gateway
    (implementation plan, milestone 3); until then this type holds the
    fields every frame has.

    Attributes:
        type: Module-private message type, lower snake case (MOD-44).
        payload: JSON object, at most 256 KiB.
        client: Id of the browser client that sent it.
        trace: Trace id under which the message is handled.

    Implements:
        CORE-28: Core spec 0.4, §5.3 Routing rules.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    client: str
    trace: UUID

    @field_validator("payload")
    @classmethod
    def _check_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        check_payload(value)
        return value
