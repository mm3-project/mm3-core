"""``ctx.mqtt``: publish and subscribe on the engine's MQTT connection.

Every call needs the capability (``mqtt.publish`` or ``mqtt.subscribe``) and a
topic matching the instance's grant globs; otherwise it raises
``PermissionError`` (Contract section 8). Messages for a subscription are
queued in the instance inbox and handled like notifications: one at a time,
in arrival order, under the handler budget (core CORE-100, CORE-28).
``ha_entity()`` is specified with the MQTT bridge in a later revision.

Implements:
    CORE-100: Core spec 0.18, §11.1 Engine additions.
    -: Module Contract 1.0 rev 9, §4.4 Mqtt.
"""

from __future__ import annotations

import json as _json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

__all__ = ["Mqtt", "MqttHandler", "MqttMessage", "Qos", "Subscription", "encode_payload"]

Qos = Literal[0, 1, 2]
"""MQTT quality of service."""


@dataclass(frozen=True)
class MqttMessage:
    """A message received on a subscription.

    Attributes:
        topic: Topic it was published on.
        payload: Raw payload.
        qos: Quality of service it was delivered with.
        retain: Whether it is a retained message.
    """

    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False

    def text(self) -> str:
        """The payload decoded as UTF-8."""
        return self.payload.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The payload decoded as JSON."""
        return _json.loads(self.payload)


MqttHandler = Callable[[MqttMessage], Awaitable[None]]
"""An async callable handling one message."""


class Subscription(Protocol):
    """An active subscription."""

    @property
    def filter(self) -> str:
        """The subscribed topic filter."""
        ...

    def cancel(self) -> None:
        """Stop receiving; idempotent, and done when the instance stops (MOD-22)."""
        ...


class Mqtt(Protocol):
    """``ctx.mqtt`` (Contract 4.4)."""

    async def publish(
        self,
        topic: str,
        payload: Mapping[str, Any] | str | bytes,
        *,
        qos: Qos = 0,
        retain: bool = False,
    ) -> None:
        """Publish a message; a mapping is sent as JSON, a string as UTF-8.

        Raises:
            PermissionError: ``mqtt.publish`` not declared or topic not granted.
        """
        ...

    async def subscribe(self, filter: str, handler: MqttHandler, *, qos: Qos = 0) -> Subscription:
        """Subscribe to a topic filter; messages go through the instance inbox.

        Raises:
            PermissionError: ``mqtt.subscribe`` not declared or filter not granted.
        """
        ...


def encode_payload(payload: Mapping[str, Any] | str | bytes) -> bytes:
    """Encode a publish payload: a mapping as JSON, a string as UTF-8.

    Args:
        payload: The payload.

    Returns:
        The bytes to send.

    Raises:
        TypeError: The payload has another type.
        ValueError: A mapping is not JSON-serialisable.
    """
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, Mapping):
        try:
            return _json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"MQTT payload is not JSON-serialisable: {exc}") from None
    raise TypeError(f"MQTT payload must be a mapping, str or bytes, not {type(payload).__name__}")
