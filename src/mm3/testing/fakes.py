"""Fakes behind ``ctx.http`` and ``ctx.mqtt`` in the module harness.

They replace only the network: the capability and grant checks of the
engine run in front of them, so a URL or topic outside the grants raises
``PermissionError`` as on a mirror (Contract section 8).

Implements:
    -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import json as _json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mm3.core.services.hass import state_text
from mm3.core.services.mqtt import HaEntitySpec, topic_matches, topic_reserved
from mm3.errors import HttpConnectionError, TransientError
from mm3.http import Response
from mm3.mqtt import MqttMessage, Qos, encode_payload

__all__ = ["FakeHaEntity", "FakeHttp", "FakeMqtt", "HttpCall", "Published", "fake_mqtt"]


@dataclass(frozen=True)
class HttpCall:
    """One request the module sent through ``ctx.http``.

    Attributes:
        instance_id: Sending instance.
        method: Upper-case method.
        url: Request URL.
        params: Query parameters.
        headers: Request headers.
        json: JSON body, if any.
        data: Raw body, if any.
        timeout: Requested timeout in seconds.
    """

    instance_id: str
    method: str
    url: str
    params: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None
    json: Any = None
    data: bytes | None = None
    timeout: float = 10.0


@dataclass(frozen=True)
class _Mock:
    method: str
    url: str
    status: int
    body: bytes
    headers: Mapping[str, str]
    raises: BaseException | type[BaseException] | None


class FakeHttp:
    """``h.http``: canned responses for the module's requests.

    Attributes:
        calls: Every request that passed the grant check, in order.
        unmocked: Requests no mock answered; listed by ``check_contract()``.
    """

    def __init__(self) -> None:
        self.calls: list[HttpCall] = []
        self.unmocked: list[HttpCall] = []
        self._mocks: list[_Mock] = []

    def mock(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        json: Any = None,
        text: str | None = None,
        headers: Mapping[str, str] | None = None,
        raises: BaseException | type[BaseException] | None = None,
    ) -> None:
        """Answer requests whose URL starts with ``url``; the latest mock wins.

        Args:
            method: HTTP method.
            url: URL prefix.
            status: Status code.
            json: JSON body.
            text: Text body (when ``json`` is ``None``).
            headers: Response headers.
            raises: Raise this instead of answering, e.g. ``HttpTimeoutError``.
        """
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            hdrs.setdefault("content-type", "application/json")
        else:
            body = (text or "").encode("utf-8")
            hdrs.setdefault("content-type", "text/plain; charset=utf-8")
        self._mocks.append(_Mock(method.upper(), url, status, body, hdrs, raises))

    def copy_mocks(self, other: FakeHttp) -> None:
        """Answer with the mocks of another fake too (used by ``check_contract``)."""
        self._mocks = list(other._mocks) + self._mocks

    def reset(self) -> None:
        """Forget mocks and recorded calls."""
        self._mocks.clear()
        self.calls.clear()
        self.unmocked.clear()

    async def send(
        self,
        instance_id: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        json: Any,
        data: bytes | None,
        timeout: float,  # noqa: ASYNC109 - the transport signature
        grants: Sequence[str],
        ca_files: Mapping[str, str] | None = None,
        user_agent: str = "",
    ) -> Response:
        """Answer a checked request from the mocks.

        Raises:
            HttpConnectionError: No mock matches.
        """
        call = HttpCall(instance_id, method, url, params, headers, json, data, timeout)
        self.calls.append(call)
        for mock in reversed(self._mocks):
            if mock.method == method and url.startswith(mock.url):
                if mock.raises is not None:
                    raise mock.raises
                return Response(mock.status, mock.body, url, dict(mock.headers))
        self.unmocked.append(call)
        raise HttpConnectionError(f"no mock for {method} {url}", url)


@dataclass(frozen=True)
class Published:
    """A message the module published.

    Attributes:
        instance_id: Publishing instance.
        topic: Topic.
        payload: Encoded payload.
        qos: Quality of service.
        retain: Retain flag.
    """

    instance_id: str
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False

    def json(self) -> Any:
        """The payload decoded as JSON."""
        return _json.loads(self.payload)

    def text(self) -> str:
        """The payload decoded as UTF-8."""
        return self.payload.decode("utf-8", errors="replace")


@dataclass
class _Sub:
    instance_id: str
    filter: str
    deliver: Callable[[MqttMessage], None]
    active: bool = True


class FakeHaEntity:
    """A Home Assistant entity a module registered with ``ctx.mqtt.ha_entity()``.

    Attributes:
        instance_id: Owner instance.
        spec: What the module asked for (component, name key, options, ...).
        object_id: The entity's id inside the instance.
        state: The last state as Home Assistant would receive it (``ON``,
            ``OFF``, text, or ``None`` for unknown); ``None`` before any.
        states: Every state set, in order.
        removed: Whether ``remove()`` was called.
    """

    def __init__(
        self,
        fake: FakeMqtt,
        instance_id: str,
        spec: HaEntitySpec,
        deliver: Callable[[MqttMessage], None],
    ) -> None:
        self._fake = fake
        self.instance_id = instance_id
        self.spec = spec
        self.object_id = spec.object_id
        self._deliver = deliver
        self.state: str | None = None
        self.states: list[str] = []
        self.removed = False

    async def set_state(self, value: str | float | bool | None) -> None:
        """Record the state as the bridge would publish it (CORE-344)."""
        text = state_text(value)
        self.state = text
        self.states.append(text)

    async def remove(self) -> None:
        """Record the removal; idempotent (CORE-345)."""
        self.removed = True
        self._fake.entities.pop((self.instance_id, self.object_id), None)

    def detach(self) -> None:
        """The instance stopped; the entity stays in Home Assistant."""

    def command(self, payload: str) -> None:
        """Deliver a Home Assistant command as the bridge would (CORE-344)."""
        topic = f"{self._fake.base_topic}/ha/{self.instance_id}/{self.object_id}/set"
        self._deliver(MqttMessage(topic, payload.encode("utf-8"), 1, False))


@dataclass
class FakeMqtt:
    """``h.mqtt``: an in-memory broker.

    Publishes are recorded and, like on a broker, delivered to every
    matching subscription; :meth:`inject` delivers a message as if another
    client had published it. Deliveries go through the instance inbox
    (CORE-100). ``enabled`` and ``connected`` stand in for the bridge's
    state (CORE-342) and ``entities`` holds the module's Home Assistant
    entities (CORE-344).

    Attributes:
        publishes: Messages the module published, in order.
        enabled: Whether an MQTT bridge is configured; False makes every
            ``ctx.mqtt`` call raise ``ConfigurationError``.
        connected: Whether the broker is connected; False makes ``publish``
            raise ``TransientError``.
        base_topic: The bridge's ``base_topic``; topics under it that belong
            to the engine are refused.
        entities: Entities by ``(instance id, object id)``.
    """

    publishes: list[Published] = field(default_factory=list)
    _subs: list[_Sub] = field(default_factory=list)
    settle: Callable[[], Awaitable[None]] | None = None
    enabled: bool = True
    connected: bool = True
    base_topic: str = "mm3/test"
    entities: dict[tuple[str, str], FakeHaEntity] = field(default_factory=dict)

    def reserved(self, topic: str) -> bool:
        """Whether a topic belongs to the engine (CORE-342)."""
        return topic_reserved(self.base_topic, topic)

    async def ha_entity(
        self, instance_id: str, spec: HaEntitySpec, deliver: Callable[[MqttMessage], None]
    ) -> FakeHaEntity:
        """Register an entity (CORE-344)."""
        entity = FakeHaEntity(self, instance_id, spec, deliver)
        self.entities[(instance_id, spec.object_id)] = entity
        return entity

    async def command(self, instance_id: str, object_id: str, payload: str) -> None:
        """Send a Home Assistant command to an entity and let the module handle it."""
        self.entities[(instance_id, object_id)].command(payload)
        if self.settle is not None:
            await self.settle()

    @property
    def subscriptions(self) -> list[str]:
        """Filters currently subscribed."""
        return [s.filter for s in self._subs if s.active]

    def _deliver(self, message: MqttMessage) -> int:
        count = 0
        for sub in list(self._subs):
            if sub.active and topic_matches(sub.filter, message.topic):
                sub.deliver(message)
                count += 1
        return count

    async def publish(
        self, instance_id: str, topic: str, payload: bytes, qos: Qos, retain: bool
    ) -> None:
        """Record a checked publish and deliver it to matching subscriptions.

        Raises:
            TransientError: ``connected`` is False; nothing is recorded or queued.
        """
        if not self.connected:
            raise TransientError("CORE_MQTT_DISCONNECTED")
        self.publishes.append(Published(instance_id, topic, payload, qos, retain))
        self._deliver(MqttMessage(topic, payload, qos, retain))

    async def subscribe(
        self, instance_id: str, filter: str, qos: Qos, deliver: Callable[[MqttMessage], None]
    ) -> Callable[[], None]:
        """Register a checked subscription."""
        sub = _Sub(instance_id, filter, deliver)
        self._subs.append(sub)

        def unsubscribe() -> None:
            sub.active = False
            if sub in self._subs:
                self._subs.remove(sub)

        return unsubscribe

    async def inject(
        self,
        topic: str,
        payload: Mapping[str, Any] | str | bytes,
        *,
        qos: Qos = 0,
        retain: bool = False,
    ) -> int:
        """Deliver a message to matching subscriptions and let them handle it.

        Args:
            topic: Topic.
            payload: Mapping (sent as JSON), string or bytes.
            qos: Quality of service.
            retain: Retain flag.

        Returns:
            How many subscriptions received it.
        """
        count = self._deliver(MqttMessage(topic, encode_payload(payload), qos, retain))
        if self.settle is not None:
            await self.settle()
        return count

    def last(self, topic: str | None = None) -> Published | None:
        """The latest publish, optionally on a topic."""
        for p in reversed(self.publishes):
            if topic is None or p.topic == topic:
                return p
        return None


fake_mqtt = FakeMqtt
"""The type of ``h.mqtt`` (Contract 12.2)."""
