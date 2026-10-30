"""``ctx.mqtt`` on the engine side: capability and grant checks over a broker.

Publish and subscribe need the capability and a topic matching one of the
instance's grant globs (CORE-100, Contract section 8). Messages for a
subscription are queued in the instance inbox as :class:`MqttDelivery`, so
the supervisor handles them like notifications. The broker connection
arrives with the MQTT bridge (milestone 7); until then the engine has no
broker: publishes are dropped and subscriptions receive nothing. The test
kit supplies its fake broker.

Implements:
    CORE-100: Core spec 0.18, §11.1 Engine additions.
    -: Module Contract 1.0 rev 9, §4.4 Mqtt.
    MOD-22: Module Contract 1.0 rev 9, §4.2 Guarantees.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from mm3.core.bus import MqttDelivery
from mm3.core.logging import get_logger
from mm3.mqtt import MqttHandler, MqttMessage, Qos, encode_payload

__all__ = ["InstanceMqtt", "MqttBroker", "NoBroker", "topic_granted", "topic_matches"]

PUBLISH = "mqtt.publish"
SUBSCRIBE = "mqtt.subscribe"


class MqttBroker(Protocol):
    """The engine's broker connection."""

    async def publish(
        self, instance_id: str, topic: str, payload: bytes, qos: Qos, retain: bool
    ) -> None:
        """Publish a checked message."""
        ...

    async def subscribe(
        self, instance_id: str, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Subscribe; ``deliver`` is called per message; returns the unsubscribe."""
        ...


class NoBroker:
    """No broker configured: publishes are dropped, nothing is received."""

    async def publish(
        self, instance_id: str, topic: str, payload: bytes, qos: Qos, retain: bool
    ) -> None:
        """Drop the message."""
        get_logger(subsystem="mqtt").debug("mqtt_no_broker", instance=instance_id, topic=topic)

    async def subscribe(
        self, instance_id: str, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Accept the subscription; it receives nothing."""
        return lambda: None


def topic_granted(topic: str, globs: Sequence[str]) -> bool:
    """Return whether a topic or filter matches one of the grant globs.

    Args:
        topic: Topic, or a subscription filter taken literally.
        globs: ``grants.mqtt.publish`` or ``grants.mqtt.subscribe``.

    Returns:
        True when granted.
    """
    return any(fnmatch.fnmatchcase(topic, g) for g in globs)


def topic_matches(filter: str, topic: str) -> bool:
    """Return whether an MQTT topic matches a filter with ``+`` and ``#``.

    Args:
        filter: Subscription filter.
        topic: Published topic.

    Returns:
        True on a match.
    """
    f_parts, t_parts = filter.split("/"), topic.split("/")
    for i, part in enumerate(f_parts):
        if part == "#":
            return True
        if i >= len(t_parts) or (part != "+" and part != t_parts[i]):
            return False
    return len(f_parts) == len(t_parts)


class _Subscription:
    def __init__(self, filter: str, owner: InstanceMqtt) -> None:
        self._filter = filter
        self._owner = owner
        self.active = True
        self.unsubscribe: Callable[[], None] = lambda: None

    @property
    def filter(self) -> str:
        return self._filter

    def cancel(self) -> None:
        if self.active:
            self.active = False
            self.unsubscribe()
            self._owner.forget(self)


class InstanceMqtt:
    """One instance's ``ctx.mqtt``.

    Args:
        instance_id: Owner instance.
        broker: Broker connection.
        capabilities: The module's declared capabilities.
        publish_grants: ``grants.mqtt.publish`` globs.
        subscribe_grants: ``grants.mqtt.subscribe`` globs.
        enqueue: Puts an item into the instance inbox.
        guard: Raises ``ContextClosedError`` once the context is closed.
        on_denied: Called with the capability when a call is refused
            because it was not declared (the test kit records MOD-70).

    Implements:
        CORE-100: Core spec 0.18, §11.1 Engine additions.
    """

    def __init__(
        self,
        instance_id: str,
        broker: MqttBroker,
        capabilities: frozenset[str],
        publish_grants: Sequence[str],
        subscribe_grants: Sequence[str],
        enqueue: Callable[[MqttDelivery], None],
        guard: Callable[[], None],
        on_denied: Callable[[str], None] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._broker = broker
        self._capabilities = capabilities
        self._publish = tuple(publish_grants)
        self._subscribe = tuple(subscribe_grants)
        self._enqueue = enqueue
        self._guard = guard
        self._on_denied = on_denied
        self._subs: list[_Subscription] = []

    def _need(self, capability: str) -> None:
        self._guard()
        if capability not in self._capabilities:
            if self._on_denied is not None:
                self._on_denied(capability)
            raise PermissionError(f"capability {capability} is not declared")

    async def publish(
        self,
        topic: str,
        payload: Mapping[str, Any] | str | bytes,
        *,
        qos: Qos = 0,
        retain: bool = False,
    ) -> None:
        """Publish on a granted topic.

        Raises:
            PermissionError: ``mqtt.publish`` not declared or topic not granted.
            ValueError: A mapping payload is not JSON-serialisable.
        """
        self._need(PUBLISH)
        if not topic_granted(topic, self._publish):
            raise PermissionError(f"{topic} is not granted to {self._instance_id} for publish")
        await self._broker.publish(self._instance_id, topic, encode_payload(payload), qos, retain)

    async def subscribe(self, filter: str, handler: MqttHandler, *, qos: Qos = 0) -> _Subscription:
        """Subscribe on a granted filter; messages go through the inbox.

        Raises:
            PermissionError: ``mqtt.subscribe`` not declared or filter not granted.
        """
        self._need(SUBSCRIBE)
        if not topic_granted(filter, self._subscribe):
            raise PermissionError(f"{filter} is not granted to {self._instance_id} for subscribe")
        sub = _Subscription(filter, self)

        def deliver(message: MqttMessage) -> None:
            if sub.active:
                self._enqueue(MqttDelivery(handler, message, lambda: sub.active))

        sub.unsubscribe = await self._broker.subscribe(self._instance_id, filter, qos, deliver)
        if self._guard_closed():
            sub.cancel()
        else:
            self._subs.append(sub)
        return sub

    def _guard_closed(self) -> bool:
        try:
            self._guard()
        except Exception:
            return True
        return False

    def forget(self, sub: _Subscription) -> None:
        """Drop a cancelled subscription from the list."""
        if sub in self._subs:
            self._subs.remove(sub)

    def close(self) -> None:
        """Cancel every subscription (MOD-22)."""
        for sub in list(self._subs):
            sub.cancel()
