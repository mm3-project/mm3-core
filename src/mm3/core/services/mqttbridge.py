"""The MQTT bridge: notifications in and out, the connection's owner for modules.

The bridge owns the engine's one broker connection (:class:`MqttConnection`),
turns ``<base>/notify/<NAME>`` messages into bus broadcasts with source
``mqtt``, publishes the notifications listed in ``mqtt.export`` as envelopes
on ``<base>/event/<NAME>``, and is the broker behind every instance's
``ctx.mqtt`` (CORE-339 to CORE-342). Home Assistant discovery and entities
live in :mod:`mm3.core.services.hass`, which the bridge hosts.

Implements:
    CORE-339: Core spec 0.34, §9.3 MQTT bridge.
    CORE-340: Core spec 0.34, §9.3 MQTT bridge.
    CORE-341: Core spec 0.34, §9.3 MQTT bridge.
    CORE-342: Core spec 0.34, §9.3 MQTT bridge.
    CORE-51: Core spec 0.34, §9.3 MQTT bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mm3.core.bus import Bus
from mm3.core.clock import Clock
from mm3.core.logging import get_logger
from mm3.core.services.hass import HaService
from mm3.core.services.mqtt import (
    HaEntityHandle,
    HaEntitySpec,
    MqttConnection,
    TransportFactory,
    topic_reserved,
)
from mm3.core.settings import MqttSettings
from mm3.errors import TransientError
from mm3.messages import MAX_PAYLOAD_BYTES, NAME_PATTERN, Notification
from mm3.mqtt import MqttMessage, Qos
from mm3.notifications import validate_payload

if TYPE_CHECKING:
    from mm3.core.i18n import I18n
    from mm3.core.supervisor import Supervisor

__all__ = ["MqttBridge"]

WARN_EVERY = 60.0
"""Seconds between two warnings about dropped messages of the same name (CORE-340)."""
WARN_NAMES = 256
"""Names whose last warning is remembered; the oldest go first."""
EXPORT_QUEUE = 1000
"""Exports waiting for the broker; the oldest are dropped when it is slow."""

_log = get_logger(subsystem="mqtt")


class MqttBridge:
    """The MQTT bridge of the engine.

    Args:
        bus: The bus the bridge listens to and publishes on.
        supervisor: Instances, for Home Assistant entities.
        clock: Engine clock.
        i18n: Catalogs that name the Home Assistant entities.
        state_dir: Engine state directory (``mqtt/discovery.json``).
        factory: Builds the broker transport; aiomqtt by default.
        rand: Jitter source of the reconnect backoff.

    Implements:
        CORE-339: Core spec 0.34, §9.3 MQTT bridge.
        CORE-340: Core spec 0.34, §9.3 MQTT bridge.
        CORE-341: Core spec 0.34, §9.3 MQTT bridge.
        CORE-342: Core spec 0.34, §9.3 MQTT bridge.
    """

    def __init__(
        self,
        *,
        bus: Bus,
        supervisor: Supervisor,
        clock: Clock,
        i18n: I18n,
        state_dir: Path,
        factory: TransportFactory | None = None,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self.settings: MqttSettings | None = None
        kwargs: dict[str, Any] = {}
        if rand is not None:
            kwargs["rand"] = rand
        self.connection = MqttConnection(clock, factory, **kwargs)
        self.ha = HaService(self, supervisor, i18n, state_dir)
        self._inbound: Callable[[], None] | None = None
        self._warned: dict[str, float] = {}
        self._exports: deque[Notification] = deque()
        self._export_ready = asyncio.Event()
        self._export_task: asyncio.Task[None] | None = None
        self.received = 0
        self.dropped = 0
        self.exported = 0
        self.export_dropped = 0
        self.connection.on_connect(self._after_connect)

    # --- state ----------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether ``engine.mqtt`` is configured (CORE-338)."""
        return self.settings is not None

    @property
    def connected(self) -> bool:
        """Whether the broker is connected right now."""
        return self.connection.connected

    @property
    def base_topic(self) -> str | None:
        """The configured ``base_topic``, ``None`` when the bridge is off."""
        return self.settings.base_topic if self.settings is not None else None

    def reserved(self, topic: str) -> bool:
        """Tell whether a topic belongs to the engine (CORE-342).

        Args:
            topic: A topic a module wants to publish on.

        Returns:
            True for ``<base>/notify/…``, ``<base>/event/…``, ``<base>/state``
            and ``<base>/ha/…``.
        """
        return topic_reserved(self.base_topic, topic)

    def status(self) -> dict[str, Any] | None:
        """Return the ``mqtt`` object of ``GET /api/v1/status`` (CORE-339).

        Returns:
            ``{connected, since, error}``, or ``None`` when the bridge is off.
            ``error`` is a short reason with no credentials.
        """
        if self.settings is None:
            return None
        since = self.connection.since
        return {
            "connected": self.connection.connected,
            "since": since.isoformat() if since is not None else None,
            "error": self.connection.error,
        }

    def health(self) -> dict[str, Any]:
        """Return the bridge's counters (CORE-340).

        Returns:
            ``received`` and ``dropped`` inbound messages, ``exported`` and
            ``export_dropped`` notifications, and ``failures`` of the connection.
        """
        return {
            "received": self.received,
            "dropped": self.dropped,
            "exported": self.exported,
            "export_dropped": self.export_dropped,
            "failures": self.connection.failures,
        }

    # --- life cycle -----------------------------------------------------------------

    async def start(self, settings: MqttSettings | None, config_dir: Path | None) -> None:
        """Connect when ``engine.mqtt`` is configured; do nothing otherwise.

        Args:
            settings: ``engine.mqtt``, ``None`` for off.
            config_dir: Directory of the main configuration file, for ``ca``.
        """
        await self.reconfigure(settings, config_dir)

    async def reconfigure(self, settings: MqttSettings | None, config_dir: Path | None) -> None:
        """Apply a new ``engine.mqtt``: reconnect with it, or stop (CORE-339).

        Args:
            settings: The new block, ``None`` for off.
            config_dir: Directory of the main configuration file, for ``ca``.
        """
        if self._inbound is not None:
            self._inbound()
            self._inbound = None
        self.settings = settings
        if settings is None:
            await self.ha.apply(None)
            await self.connection.stop()
            await self._stop_exports()
            return
        ca = None
        if settings.ca is not None:
            ca = (config_dir or Path()) / settings.ca
        self._inbound = self.connection.add_subscription(
            f"{settings.base_topic}/notify/+", 1, self._on_notify
        )
        if self._export_task is None:
            self._export_task = asyncio.create_task(self._export_loop(), name="mm3-mqtt-export")
        await self.ha.apply(settings)
        await self.connection.start(settings, ca)

    async def stop(self) -> None:
        """Say ``offline`` and disconnect (before a clean stop, CORE-339)."""
        if self._inbound is not None:
            self._inbound()
            self._inbound = None
        await self.ha.close()
        await self.connection.stop()
        self.settings = None
        await self._stop_exports()

    async def _stop_exports(self) -> None:
        task, self._export_task = self._export_task, None
        self._exports.clear()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _after_connect(self) -> None:
        """Publish ``online`` and let Home Assistant publish its state (CORE-339)."""
        settings = self.settings
        if settings is None:
            return
        await self.connection.publish(f"{settings.base_topic}/state", b"online", 1, True)
        await self.ha.connected()

    # --- the broker behind ctx.mqtt (CORE-342) --------------------------------------

    async def publish(
        self, instance_id: str, topic: str, payload: bytes, qos: Qos, retain: bool
    ) -> None:
        """Publish a message a module sent (already checked).

        Raises:
            TransientError: The broker is not connected; nothing is queued.
        """
        await self.connection.publish(topic, payload, qos, retain)

    async def subscribe(
        self, instance_id: str, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Subscribe for a module; the broker side follows the connection."""
        return await self.connection.subscribe(filter, qos, deliver)

    async def ha_entity(
        self, instance_id: str, spec: HaEntitySpec, deliver: Callable[[MqttMessage], None]
    ) -> HaEntityHandle:
        """Register a module's Home Assistant entity (CORE-344)."""
        return await self.ha.register(instance_id, spec, deliver)

    # --- inbound (CORE-340) ---------------------------------------------------------

    def _on_notify(self, message: MqttMessage) -> None:
        settings = self.settings
        if settings is None:
            return
        prefix = f"{settings.base_topic}/notify/"
        if not message.topic.startswith(prefix):
            return
        name = message.topic[len(prefix) :]
        self.received += 1
        reason = self._refusal(settings, name, message.payload)
        if reason is not None:
            self._drop(name, reason)
            return
        payload = json.loads(message.payload) if message.payload.strip() else {}
        nid = uuid4()
        self._bus.publish(
            Notification(
                id=nid,
                trace=nid,
                ts=self._clock.wall(),
                name=name,
                source="mqtt",
                payload=payload,
            )
        )

    @staticmethod
    def _refusal(settings: MqttSettings, name: str, raw: bytes) -> str | None:
        """Return why a message is dropped, or ``None`` when it goes on the bus."""
        if not NAME_PATTERN.match(name):
            return "name"
        if name.startswith("CORE_"):
            return "reserved"
        if not any(fnmatch.fnmatchcase(name, g) for g in settings.import_):
            return "not imported"
        if len(raw) > MAX_PAYLOAD_BYTES:
            return "too large"
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return "not JSON"
        if not isinstance(payload, dict):
            return "not an object"
        try:
            Notification.model_validate(
                {
                    "id": uuid4(),
                    "trace": uuid4(),
                    "ts": "2000-01-01T00:00:00+00:00",
                    "name": name,
                    "source": "mqtt",
                    "payload": payload,
                }
            )
            validate_payload(name, payload)
        except ValueError:
            return "invalid payload"
        return None

    def _drop(self, name: str, reason: str) -> None:
        """Count a dropped message and warn once a minute per name (CORE-340)."""
        self.dropped += 1
        now = self._clock.boottime()
        last = self._warned.get(name)
        if last is not None and now - last < WARN_EVERY:
            return
        self._warned.pop(name, None)
        self._warned[name] = now
        while len(self._warned) > WARN_NAMES:
            self._warned.pop(next(iter(self._warned)))
        _log.warning("mqtt_notify_dropped", name=name[:80], reason=reason)

    # --- outbound (CORE-341) --------------------------------------------------------

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        """Bus endpoint: queue a broadcast that ``mqtt.export`` names (CORE-341).

        Notifications with source ``mqtt`` are never exported (CORE-51), and
        while the broker is disconnected exports are dropped, not queued.

        Args:
            n: The notification.
            exclude_instance: Unused; the bridge has no elements.
        """
        self.ha.on_notification(n)
        settings = self.settings
        if settings is None or not settings.export:
            return
        if n.target is not None or n.is_reply or n.source == "mqtt":
            return
        if not any(fnmatch.fnmatchcase(n.name, g) for g in settings.export):
            return
        if not self.connection.connected:
            self.export_dropped += 1
            return
        if len(self._exports) >= EXPORT_QUEUE:
            self._exports.popleft()
            self.export_dropped += 1
        self._exports.append(n)
        self._export_ready.set()

    async def _export_loop(self) -> None:
        while True:
            while not self._exports:
                self._export_ready.clear()
                await self._export_ready.wait()
            n = self._exports.popleft()
            settings = self.settings
            if settings is None:
                continue
            try:
                await self.connection.publish(
                    f"{settings.base_topic}/event/{n.name}", n.model_dump_json().encode(), 0, False
                )
                self.exported += 1
            except TransientError:
                self.export_dropped += 1
