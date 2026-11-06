"""An in-memory MQTT broker and transport for the bridge tests (CORE-338 to CORE-345)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

import aiomqtt

from mm3.core.services.mqtt import Will, topic_matches
from mm3.core.settings import MqttSettings
from mm3.mqtt import MqttMessage, Qos

_END = object()


@dataclass
class Sent:
    """One message the engine published."""

    topic: str
    payload: bytes
    qos: int
    retain: bool

    def text(self) -> str:
        return self.payload.decode()


@dataclass
class FakeBroker:
    """What a broker keeps: the log, the retained messages and the subscriptions.

    ``failures`` holds exceptions the next connection attempts raise, one each.
    """

    failures: list[BaseException] = field(default_factory=list)
    log: list[Sent] = field(default_factory=list)
    retained: dict[str, bytes] = field(default_factory=dict)
    subscribed: list[str] = field(default_factory=list)
    attempts: int = 0
    connects: int = 0
    wills: list[Will] = field(default_factory=list)
    settings: list[MqttSettings] = field(default_factory=list)
    cas: list[Path | None] = field(default_factory=list)
    transport: FakeTransport | None = None
    fail_publish: bool = False

    def factory(self, settings: MqttSettings, ca: Path | None, will: Will) -> FakeTransport:
        self.attempts += 1
        self.settings.append(settings)
        self.cas.append(ca)
        self.wills.append(will)
        return FakeTransport(self, will)

    def sent(self, topic: str) -> list[Sent]:
        return [m for m in self.log if m.topic == topic]

    def last(self, topic: str) -> str | None:
        found = self.sent(topic)
        return found[-1].text() if found else None

    def inject(self, topic: str, payload: bytes | str, *, retain: bool = False) -> None:
        """A message from the broker to the engine."""
        data = payload.encode() if isinstance(payload, str) else payload
        if self.transport is not None:
            self.transport.queue.put_nowait(MqttMessage(topic, data, 1, retain))

    def drop(self) -> None:
        """The broker closes the connection."""
        if self.transport is not None:
            self.transport.queue.put_nowait(_END)

    def configs(self, prefix: str = "homeassistant") -> dict[str, bytes]:
        """The retained discovery configs that are not empty."""
        return {t: p for t, p in self.retained.items() if t.startswith(prefix + "/") and p}


class FakeTransport:
    """One connection of the :class:`FakeBroker`."""

    def __init__(self, broker: FakeBroker, will: Will) -> None:
        self.broker = broker
        self.will = will
        self.queue: asyncio.Queue[object] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        if self.broker.failures:
            raise self.broker.failures.pop(0)
        self.broker.connects += 1
        self.broker.transport = self
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.broker.transport is self:
            self.broker.transport = None

    async def publish(self, topic: str, payload: bytes, qos: Qos, retain: bool) -> None:
        if self.broker.fail_publish:
            raise aiomqtt.MqttError("publish failed")
        self.broker.log.append(Sent(topic, payload, qos, retain))
        if retain:
            if payload:
                self.broker.retained[topic] = payload
            else:
                self.broker.retained.pop(topic, None)

    async def subscribe(self, filter: str, qos: Qos) -> None:
        self.broker.subscribed.append(filter)

    async def unsubscribe(self, filter: str) -> None:
        if filter in self.broker.subscribed:
            self.broker.subscribed.remove(filter)

    async def messages(self) -> AsyncIterator[MqttMessage]:
        while True:
            item = await self.queue.get()
            if item is _END:
                return
            assert isinstance(item, MqttMessage)
            if any(topic_matches(f, item.topic) for f in self.broker.subscribed):
                yield item
