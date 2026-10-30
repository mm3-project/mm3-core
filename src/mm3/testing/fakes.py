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

from mm3.core.services.mqtt import topic_matches
from mm3.errors import HttpConnectionError
from mm3.http import Response
from mm3.mqtt import MqttMessage, Qos, encode_payload

__all__ = ["FakeHttp", "FakeMqtt", "HttpCall", "Published", "fake_mqtt"]


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


@dataclass
class FakeMqtt:
    """``h.mqtt``: an in-memory broker.

    Publishes are recorded and, like on a broker, delivered to every
    matching subscription; :meth:`inject` delivers a message as if another
    client had published it. Deliveries go through the instance inbox
    (CORE-100).

    Attributes:
        publishes: Messages the module published, in order.
    """

    publishes: list[Published] = field(default_factory=list)
    _subs: list[_Sub] = field(default_factory=list)
    settle: Callable[[], Awaitable[None]] | None = None

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
        """Record a checked publish and deliver it to matching subscriptions."""
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
