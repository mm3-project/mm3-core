"""The engine's MQTT connection and ``ctx.mqtt`` on top of it.

One client (aiomqtt behind :class:`AiomqttTransport`) is shared by the bridge
and every instance (CORE-339). :class:`MqttConnection` owns the connection:
Last Will, reconnect with backoff, restoring subscriptions and the
connection status. :class:`InstanceMqtt` is one instance's ``ctx.mqtt``:
publish and subscribe need the capability and a topic matching one of the
instance's grant globs, the engine's own topics are never publishable
(CORE-342), and ``ha_entity()`` registers a Home Assistant entity (CORE-344).
Messages for a subscription are queued in the instance inbox as
:class:`MqttDelivery`, so the supervisor handles them like notifications.

Implements:
    CORE-100: Core spec 0.34, §11.1 Engine additions.
    CORE-339: Core spec 0.34, §9.3 MQTT bridge.
    CORE-342: Core spec 0.34, §9.3 MQTT bridge.
    CORE-344: Core spec 0.34, §9.3 MQTT bridge.
    -: Module Contract 1.0 rev 15, §4.4 Mqtt.
    MOD-22: Module Contract 1.0 rev 15, §4.2 Guarantees.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import random
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

import aiomqtt

from mm3.core.bus import MqttDelivery
from mm3.core.clock import Clock
from mm3.core.logging import get_logger
from mm3.core.settings import MqttSettings
from mm3.errors import ConfigurationError, TransientError
from mm3.mqtt import HaEntity, MqttHandler, MqttMessage, Qos, encode_payload

__all__ = [
    "AiomqttTransport",
    "HaEntitySpec",
    "InstanceMqtt",
    "MqttBroker",
    "MqttConnection",
    "MqttTransport",
    "NoBroker",
    "Will",
    "topic_granted",
    "topic_matches",
    "topic_reserved",
]

PUBLISH = "mqtt.publish"
SUBSCRIBE = "mqtt.subscribe"
HA_DISCOVERY = "ha.discovery"
OBJECT_ID = re.compile(r"^[a-z0-9_]{1,64}$")
"""A module entity's ``object_id`` (CORE-344)."""
HA_COMPONENTS = ("sensor", "binary_sensor", "switch", "number", "select", "button")
BACKOFF_FIRST = 1.0
BACKOFF_MAX = 60.0
JITTER = 0.2

_log = get_logger(subsystem="mqtt")


@dataclass(frozen=True)
class HaEntitySpec:
    """What a module asked for in ``ctx.mqtt.ha_entity()`` (CORE-344).

    Attributes:
        object_id: Id inside the instance, ``[a-z0-9_]{1,64}``.
        component: Home Assistant component.
        name: Key of the module's catalog, resolved in ``engine.locale``.
        module: Module name, whose catalog resolves ``name``.
        device_class: Home Assistant device class.
        unit: Unit of measurement.
        icon: Icon.
        options: Options of a ``select``.
        min: Lowest value of a ``number``.
        max: Highest value of a ``number``.
        step: Step of a ``number``.
        commands: Whether the entity takes commands (``on_command`` was given).
    """

    object_id: str
    component: str
    name: str
    module: str = ""
    device_class: str | None = None
    unit: str | None = None
    icon: str | None = None
    options: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    commands: bool = False


class MqttBroker(Protocol):
    """The engine's broker connection as ``ctx.mqtt`` sees it."""

    @property
    def enabled(self) -> bool:
        """Whether ``engine.mqtt`` is configured (the bridge is on)."""
        ...

    @property
    def connected(self) -> bool:
        """Whether the broker is connected right now."""
        ...

    def reserved(self, topic: str) -> bool:
        """Whether a topic belongs to the engine (CORE-342)."""
        ...

    async def publish(
        self, instance_id: str, topic: str, payload: bytes, qos: Qos, retain: bool
    ) -> None:
        """Publish a checked message.

        Raises:
            TransientError: The broker is not connected.
        """
        ...

    async def subscribe(
        self, instance_id: str, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Subscribe; ``deliver`` is called per message; returns the unsubscribe."""
        ...

    async def ha_entity(
        self, instance_id: str, spec: HaEntitySpec, deliver: Callable[[MqttMessage], None]
    ) -> HaEntityHandle:
        """Register a module's Home Assistant entity (CORE-344)."""
        ...


class HaEntityHandle(HaEntity, Protocol):
    """What the engine returns for a module's entity: a ``HaEntity`` that can detach."""

    def detach(self) -> None:
        """Forget the live entity without clearing it in Home Assistant (instance stop)."""
        ...


class NoBroker:
    """No bridge configured: every call that needs the broker raises ``ConfigurationError``."""

    @property
    def enabled(self) -> bool:
        """Always False."""
        return False

    @property
    def connected(self) -> bool:
        """Always False."""
        return False

    def reserved(self, topic: str) -> bool:
        """Nothing is reserved without a bridge."""
        return False

    async def publish(
        self, instance_id: str, topic: str, payload: bytes, qos: Qos, retain: bool
    ) -> None:
        """Refuse: there is no bridge."""
        raise ConfigurationError("engine.mqtt", "CORE_MQTT_NOT_CONFIGURED")

    async def subscribe(
        self, instance_id: str, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Refuse: there is no bridge."""
        raise ConfigurationError("engine.mqtt", "CORE_MQTT_NOT_CONFIGURED")

    async def ha_entity(
        self, instance_id: str, spec: HaEntitySpec, deliver: Callable[[MqttMessage], None]
    ) -> HaEntityHandle:
        """Refuse: there is no bridge."""
        raise ConfigurationError("engine.mqtt", "CORE_MQTT_NOT_CONFIGURED")


# --- the connection ------------------------------------------------------------------


@dataclass(frozen=True)
class Will:
    """The Last Will of the connection (CORE-339)."""

    topic: str
    payload: str
    qos: Qos = 1
    retain: bool = True


class MqttTransport(Protocol):
    """One MQTT client session; the only place that knows the library."""

    async def __aenter__(self) -> Self:
        """Connect."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Disconnect."""
        ...

    async def publish(self, topic: str, payload: bytes, qos: Qos, retain: bool) -> None:
        """Publish."""
        ...

    async def subscribe(self, filter: str, qos: Qos) -> None:
        """Subscribe on the broker."""
        ...

    async def unsubscribe(self, filter: str) -> None:
        """Unsubscribe on the broker."""
        ...

    def messages(self) -> AsyncIterator[MqttMessage]:
        """Incoming messages; ends or raises when the connection is lost."""
        ...


TransportFactory = Callable[[MqttSettings, Path | None, Will], MqttTransport]
"""Builds the transport of one connection attempt (replaced in tests)."""


class AiomqttTransport:
    """aiomqtt behind :class:`MqttTransport`.

    Args:
        settings: ``engine.mqtt``.
        ca: Absolute path of the CA file, or ``None`` for the system store.
        will: Last Will.

    Implements:
        CORE-338: Core spec 0.34, §9.3 MQTT bridge.
        CORE-339: Core spec 0.34, §9.3 MQTT bridge.
    """

    def __init__(self, settings: MqttSettings, ca: Path | None, will: Will) -> None:
        context: ssl.SSLContext | None = None
        if settings.tls:
            # TLS 1.2 or later; the certificate and host name are always verified,
            # and there are no client certificates (CORE-338).
            context = ssl.create_default_context(cafile=str(ca) if ca is not None else None)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        self._client = aiomqtt.Client(
            hostname=settings.host,
            port=settings.port,
            username=settings.username,
            password=settings.password,
            identifier=settings.client_id,
            keepalive=settings.keepalive,
            will=aiomqtt.Will(
                topic=will.topic, payload=will.payload, qos=will.qos, retain=will.retain
            ),
            tls_context=context,
        )

    async def __aenter__(self) -> Self:
        """Connect."""
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Disconnect."""
        await self._client.__aexit__(exc_type, exc, tb)

    async def publish(self, topic: str, payload: bytes, qos: Qos, retain: bool) -> None:
        """Publish."""
        await self._client.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, filter: str, qos: Qos) -> None:
        """Subscribe on the broker."""
        await self._client.subscribe(filter, qos=qos)

    async def unsubscribe(self, filter: str) -> None:
        """Unsubscribe on the broker."""
        await self._client.unsubscribe(filter)

    async def messages(self) -> AsyncIterator[MqttMessage]:
        """Incoming messages."""
        async for m in self._client.messages:
            raw = m.payload
            if isinstance(raw, bytes | bytearray):
                payload = bytes(raw)
            elif raw is None:
                payload = b""
            else:
                payload = str(raw).encode("utf-8")
            yield MqttMessage(str(m.topic), payload, int(m.qos), bool(m.retain))


def failure_reason(exc: BaseException) -> str:
    """Return a short reason for a connection failure, with no credentials.

    Args:
        exc: What the transport raised.

    Returns:
        ``tls``, ``timeout``, ``refused``, ``unreachable``, ``lost`` or ``error``.
    """
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, aiomqtt.MqttCodeError):
        return "refused"
    if isinstance(exc, OSError):
        return "unreachable"
    if isinstance(exc, aiomqtt.MqttError):
        return "lost"
    return "error"


@dataclass
class _Sub:
    filter: str
    qos: Qos
    deliver: Callable[[MqttMessage], None]


@dataclass
class _Status:
    connected: bool = False
    since: datetime | None = None
    error: str | None = None
    connects: int = 0
    failures: int = 0
    last_reason: str | None = field(default=None, repr=False)


class MqttConnection:
    """The one MQTT client of the engine (CORE-339).

    Args:
        clock: Engine clock (wall time, backoff sleeps).
        factory: Builds the transport; aiomqtt by default.
        rand: Returns a number in 0..1 for the backoff jitter.

    Implements:
        CORE-339: Core spec 0.34, §9.3 MQTT bridge.
    """

    def __init__(
        self,
        clock: Clock,
        factory: TransportFactory | None = None,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._clock = clock
        self._factory: TransportFactory = factory or AiomqttTransport
        self._rand = rand
        self._status = _Status()
        self._settings: MqttSettings | None = None
        self._ca: Path | None = None
        self._will: Will | None = None
        self._task: asyncio.Task[None] | None = None
        self._transport: MqttTransport | None = None
        self._subs: dict[int, _Sub] = {}
        self._next = 0
        self._on_connect: list[Callable[[], Awaitable[None]]] = []
        self._on_change: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Future[None]] = set()

    @property
    def configured(self) -> bool:
        """Whether a configuration is applied."""
        return self._settings is not None

    @property
    def connected(self) -> bool:
        """Whether the broker is connected."""
        return self._status.connected

    @property
    def since(self) -> datetime | None:
        """When the current connection was made."""
        return self._status.since

    @property
    def error(self) -> str | None:
        """Short reason of the last failure, ``None`` while connected."""
        return self._status.error

    @property
    def failures(self) -> int:
        """Connection attempts that failed since the start."""
        return self._status.failures

    def on_connect(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run ``callback`` after every connect, once the subscriptions are restored."""
        self._on_connect.append(callback)

    def on_change(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` when the connection state changes."""
        self._on_change.append(callback)

    # --- life cycle ---------------------------------------------------------------

    async def start(self, settings: MqttSettings, ca: Path | None) -> None:
        """Connect with ``settings`` and keep the connection.

        Args:
            settings: ``engine.mqtt``.
            ca: Absolute CA file, or ``None``.
        """
        await self.stop()
        self._settings = settings
        self._ca = ca
        self._will = Will(f"{settings.base_topic}/state", "offline")
        self._status = _Status()
        self._task = asyncio.create_task(self._run(settings, ca, self._will), name="mm3-mqtt")

    async def stop(self) -> None:
        """Say ``offline``, disconnect and forget the configuration."""
        task, self._task = self._task, None
        transport = self._transport
        will = self._will
        if transport is not None and will is not None and self._status.connected:
            with contextlib.suppress(Exception):
                await transport.publish(will.topic, will.payload.encode(), will.qos, will.retain)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._settings = None
        self._will = None
        self._transport = None
        self._set(connected=False, error=None)

    async def reconfigure(self, settings: MqttSettings | None, ca: Path | None) -> None:
        """Apply a changed ``engine.mqtt``: reconnect, or stop when it is gone (CORE-339)."""
        if settings is None:
            await self.stop()
        else:
            await self.start(settings, ca)

    # --- the loop -----------------------------------------------------------------

    def _set(self, *, connected: bool, error: str | None) -> None:
        changed = connected != self._status.connected or error != self._status.error
        if connected and not self._status.connected:
            self._status.since = self._clock.wall()
        elif not connected:
            self._status.since = None
        self._status.connected = connected
        self._status.error = error
        if changed:
            for cb in list(self._on_change):
                cb()

    async def _run(self, settings: MqttSettings, ca: Path | None, will: Will) -> None:
        delay = BACKOFF_FIRST
        while True:
            try:
                async with self._factory(settings, ca, will) as transport:
                    self._transport = transport
                    for filt in {s.filter: s.qos for s in self._subs.values()}.items():
                        await transport.subscribe(filt[0], filt[1])
                    self._status.connects += 1
                    self._status.last_reason = None
                    self._set(connected=True, error=None)
                    _log.info("mqtt_connected", host=settings.host, port=settings.port)
                    delay = BACKOFF_FIRST
                    for cb in list(self._on_connect):
                        try:
                            await cb()
                        except asyncio.CancelledError:
                            raise
                        except (TransientError, aiomqtt.MqttError, OSError):
                            raise
                        except Exception as exc:  # a bug in a callback must not drop the broker
                            _log.error("mqtt_on_connect_failed", error=type(exc).__name__)
                    async for message in transport.messages():
                        self._dispatch(message)
                    raise aiomqtt.MqttError("connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = failure_reason(exc)
                self._status.failures += 1
                if reason != self._status.last_reason:
                    # The reason only: never the exception text, which may name the user.
                    _log.warning("mqtt_connection_failed", reason=reason)
                    self._status.last_reason = reason
                self._transport = None
                self._set(connected=False, error=reason)
            wait = delay * (1 + JITTER * (2 * self._rand() - 1))
            delay = min(delay * 2, BACKOFF_MAX)
            await self._clock.sleep(wait)

    def _dispatch(self, message: MqttMessage) -> None:
        for sub in list(self._subs.values()):
            if topic_matches(sub.filter, message.topic):
                try:
                    sub.deliver(message)
                except Exception as exc:  # one bad receiver never stops the others
                    _log.warning(
                        "mqtt_deliver_failed", topic=message.topic, error=type(exc).__name__
                    )

    # --- publish and subscribe ----------------------------------------------------

    async def publish(self, topic: str, payload: bytes, qos: Qos = 0, retain: bool = False) -> None:
        """Publish now.

        Raises:
            TransientError: The broker is not connected, or the publish failed.
        """
        transport = self._transport
        if transport is None or not self._status.connected:
            raise TransientError("CORE_MQTT_DISCONNECTED")
        try:
            await transport.publish(topic, payload, qos, retain)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransientError("CORE_MQTT_PUBLISH_FAILED") from exc

    def _register(
        self, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> tuple[bool, Callable[[], None]]:
        """Remember a subscription; returns whether the broker has to be told, and the cancel."""
        key = self._next
        self._next += 1
        first = not any(s.filter == filter for s in self._subs.values())
        self._subs[key] = _Sub(filter, qos, deliver)

        def cancel() -> None:
            sub = self._subs.pop(key, None)
            if sub is None or any(s.filter == filter for s in self._subs.values()):
                return
            current = self._transport
            if current is not None and self._status.connected:
                self._spawn(self._unsubscribe(current, filter))

        return first, cancel

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def subscribe(
        self, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Subscribe; it takes effect on connect when the broker is away (CORE-342).

        Returns:
            The function that ends this subscription.
        """
        first, cancel = self._register(filter, qos, deliver)
        transport = self._transport
        if first and transport is not None and self._status.connected:
            try:
                await transport.subscribe(filter, qos)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.debug("mqtt_subscribe_deferred", error=type(exc).__name__)
        return cancel

    def add_subscription(
        self, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Subscribe without waiting for the broker (the engine's own subscriptions).

        Returns:
            The function that ends this subscription.
        """
        first, cancel = self._register(filter, qos, deliver)
        transport = self._transport
        if first and transport is not None and self._status.connected:
            self._spawn(self._subscribe_broker(transport, filter, qos))
        return cancel

    @staticmethod
    async def _subscribe_broker(transport: MqttTransport, filter: str, qos: Qos) -> None:
        with contextlib.suppress(Exception):
            await transport.subscribe(filter, qos)

    @staticmethod
    async def _unsubscribe(transport: MqttTransport, filter: str) -> None:
        with contextlib.suppress(Exception):
            await transport.unsubscribe(filter)


def topic_reserved(base: str | None, topic: str) -> bool:
    """Tell whether a topic belongs to the engine (CORE-342).

    Args:
        base: The configured ``base_topic``, ``None`` without a bridge.
        topic: A topic a module wants to publish on.

    Returns:
        True for ``<base>/notify/…``, ``<base>/event/…``, ``<base>/state`` and
        ``<base>/ha/…``.
    """
    if base is None:
        return False
    return (
        topic.startswith((f"{base}/notify/", f"{base}/event/", f"{base}/ha/"))
        or topic == f"{base}/state"
        or topic.startswith(f"{base}/state/")
    )


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

    Every call checks, in this order: the capability (``PermissionError``,
    MOD-70), that the bridge is on (``ConfigurationError``), the grant and
    the engine's own topics (``PermissionError``), and for a publish that the
    broker is connected (``TransientError``, nothing is queued) (CORE-342).

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
        module: Module name, whose catalog names the instance's entities.

    Implements:
        CORE-100: Core spec 0.34, §11.1 Engine additions.
        CORE-342: Core spec 0.34, §9.3 MQTT bridge.
        CORE-344: Core spec 0.34, §9.3 MQTT bridge.
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
        module: str = "",
    ) -> None:
        self._instance_id = instance_id
        self._broker = broker
        self._capabilities = capabilities
        self._publish = tuple(publish_grants)
        self._subscribe = tuple(subscribe_grants)
        self._enqueue = enqueue
        self._guard = guard
        self._on_denied = on_denied
        self._module = module
        self._subs: list[_Subscription] = []
        self._entities: dict[str, HaEntityHandle] = {}

    def _need(self, capability: str) -> None:
        self._guard()
        if capability not in self._capabilities:
            if self._on_denied is not None:
                self._on_denied(capability)
            raise PermissionError(f"capability {capability} is not declared")

    def _bridge(self) -> None:
        if not self._broker.enabled:
            raise ConfigurationError("engine.mqtt", "CORE_MQTT_NOT_CONFIGURED")

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
            PermissionError: ``mqtt.publish`` not declared, topic not granted,
                or a topic of the engine (``<base>/notify/``, ``event/``,
                ``state`` and ``ha/``), even when a grant matches.
            ConfigurationError: There is no MQTT bridge.
            TransientError: The broker is not connected; nothing is queued.
            ValueError: A mapping payload is not JSON-serialisable.
        """
        self._need(PUBLISH)
        self._bridge()
        if not topic_granted(topic, self._publish):
            raise PermissionError(f"{topic} is not granted to {self._instance_id} for publish")
        if self._broker.reserved(topic):
            raise PermissionError(f"{topic} belongs to the engine")
        await self._broker.publish(self._instance_id, topic, encode_payload(payload), qos, retain)

    async def subscribe(self, filter: str, handler: MqttHandler, *, qos: Qos = 0) -> _Subscription:
        """Subscribe on a granted filter; messages go through the inbox.

        While the broker is disconnected the call succeeds and takes effect on
        connect.

        Raises:
            PermissionError: ``mqtt.subscribe`` not declared or filter not granted.
            ConfigurationError: There is no MQTT bridge.
        """
        self._need(SUBSCRIBE)
        self._bridge()
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

    async def ha_entity(
        self,
        object_id: str,
        component: str,
        *,
        name: str,
        device_class: str | None = None,
        unit: str | None = None,
        icon: str | None = None,
        options: Sequence[str] | None = None,
        min: float | None = None,
        max: float | None = None,
        step: float | None = None,
        on_command: Callable[[str], Awaitable[None]] | None = None,
    ) -> HaEntity:
        """Register a Home Assistant entity on the mirror's device (CORE-344).

        A command reaches ``on_command`` with the payload as text through the
        instance inbox, one at a time under the handler budget. With
        ``ha_discovery`` off the entity publishes nothing.

        Raises:
            PermissionError: ``ha.discovery`` not declared.
            ConfigurationError: There is no MQTT bridge.
            ValueError: ``object_id`` is not ``[a-z0-9_]{1,64}`` or is used by
                another entity of the instance, ``component`` is unknown, a
                ``select`` has no ``options``, or ``on_command`` is given to a
                ``sensor`` or ``binary_sensor``.
        """
        self._need(HA_DISCOVERY)
        self._bridge()
        if not OBJECT_ID.match(object_id):
            raise ValueError(f"object_id {object_id!r} is not [a-z0-9_]{{1,64}}")
        if object_id in self._entities:
            raise ValueError(f"object_id {object_id!r} is already used in {self._instance_id}")
        if component not in HA_COMPONENTS:
            raise ValueError(f"component {component!r} is not one of {', '.join(HA_COMPONENTS)}")
        if component == "select" and not options:
            raise ValueError("a select needs options")
        if on_command is not None and component in ("sensor", "binary_sensor"):
            raise ValueError(f"a {component} takes no commands")
        spec = HaEntitySpec(
            object_id=object_id,
            component=component,
            name=name,
            module=self._module,
            device_class=device_class,
            unit=unit,
            icon=icon,
            options=tuple(options) if options else None,
            min=min,
            max=max,
            step=step,
            commands=on_command is not None,
        )
        active = [True]

        def deliver(message: MqttMessage) -> None:
            if on_command is None or not active[0]:
                return

            async def handler(m: MqttMessage) -> None:
                await on_command(m.text())

            self._enqueue(MqttDelivery(handler, message, lambda: active[0]))

        entity = await self._broker.ha_entity(self._instance_id, spec, deliver)
        if self._guard_closed():
            active[0] = False
            entity.detach()
            self._guard()  # raises ContextClosedError
        self._entities[object_id] = entity
        return _Entity(entity, self, active)

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

    def forget_entity(self, object_id: str) -> None:
        """Free an ``object_id`` after ``remove()``."""
        self._entities.pop(object_id, None)

    def close(self) -> None:
        """Cancel every subscription and detach every entity (MOD-22)."""
        for sub in list(self._subs):
            sub.cancel()
        for entity in list(self._entities.values()):
            entity.detach()
        self._entities.clear()


class _Entity:
    """The ``HaEntity`` a module holds; ``remove()`` frees its ``object_id``."""

    def __init__(self, inner: HaEntityHandle, owner: InstanceMqtt, active: list[bool]) -> None:
        self._inner = inner
        self._owner = owner
        self._active = active
        self.object_id = inner.object_id

    async def set_state(self, value: str | float | bool | None) -> None:
        """Publish the state (CORE-344)."""
        await self._inner.set_state(value)

    async def remove(self) -> None:
        """Clear the entity in Home Assistant now; idempotent (CORE-345)."""
        self._active[0] = False
        self._owner.forget_entity(self.object_id)
        await self._inner.remove()
