"""ctx.http, ctx.mqtt and ctx.health on the engine (CORE-100, MOD-85, MOD-22)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from conftest import EngineFactory
from mm3 import MirrorModule
from mm3.core.services.grants import GrantSet
from mm3.core.services.http import HttpxTransport, url_granted
from mm3.core.services.mqtt import NoBroker, topic_granted, topic_matches
from mm3.core.supervisor import InstanceSpec, State
from mm3.errors import ContextClosedError, HttpStatusError
from mm3.http import Response
from mm3.mqtt import MqttMessage, encode_payload
from mm3.testing import VirtualClock
from mm3.testing.fakes import FakeMqtt


def obj(engine: Any, iid: str) -> Any:
    return engine.supervisor.get(iid).obj


class Health(MirrorModule):
    name = "health"


async def test_mod_85_health_fields_in_the_record(make_engine: EngineFactory) -> None:
    engine = make_engine(Health)
    await engine.start([InstanceSpec("h", "health")])
    ctx = obj(engine, "h").ctx
    ctx.health.set("items", 12)
    ctx.health.set("last_refresh", "2026-10-30T00:00:00Z")
    assert engine.supervisor.get("h").health()["fields"] == {
        "items": 12,
        "last_refresh": "2026-10-30T00:00:00Z",
    }
    ctx.health.set("items", None)
    assert "items" not in engine.supervisor.get("h").health()["fields"]
    with pytest.raises(ValueError, match="snake"):
        ctx.health.set("Items", 1)
    with pytest.raises(ValueError, match="JSON"):
        ctx.health.set("obj", object())


class Net(MirrorModule):
    name = "net"
    capabilities: ClassVar[set[str]] = {"http.outbound", "mqtt.publish", "mqtt.subscribe"}

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.got: list[str] = []

    async def on_temp(self, msg: MqttMessage) -> None:
        self.got.append(msg.text())
        if msg.text() == "slow":
            await asyncio.sleep(10)


GRANTS = GrantSet(
    http=("https://ok.example/",),
    mqtt_publish=("out/*",),
    mqtt_subscribe=("in/#",),
)


async def test_core_100_engine_without_broker_and_with_the_httpx_transport(
    make_engine: EngineFactory,
) -> None:
    engine = make_engine(Net)
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    ctx = obj(engine, "n").ctx
    assert isinstance(engine.supervisor.http_transport, HttpxTransport)
    assert engine.supervisor.http_transport is engine.http
    with pytest.raises(PermissionError, match="not granted"):
        await ctx.http.post("https://bad.example/")
    assert isinstance(engine.supervisor.mqtt_broker, NoBroker)
    await ctx.mqtt.publish("out/x", {"a": 1})
    sub = await ctx.mqtt.subscribe("in/#", obj(engine, "n").on_temp)
    assert sub.filter == "in/#"
    sub.cancel()
    sub.cancel()


async def test_core_100_messages_go_through_the_inbox_with_budget(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Net)
    broker = FakeMqtt()
    engine.supervisor.mqtt_broker = broker
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    net = obj(engine, "n")
    await net.ctx.mqtt.subscribe("in/#", net.on_temp)
    await broker.inject("in/a", "one")
    await clock.settle()
    assert net.got == ["one"]
    await broker.inject("in/b", "slow")
    await broker.inject("in/c", "two")
    await clock.settle()
    assert net.got == ["one", "slow"]  # one at a time, in arrival order
    await clock.advance(0.2)  # handler budget in the test engine
    assert net.got == ["one", "slow", "two"]
    assert engine.supervisor.get("n").health()["handler_timeouts"] == 1
    await broker.inject("out/x", "nobody")
    await clock.settle()
    assert len(net.got) == 3


async def test_mod_22_subscriptions_cancelled_when_the_instance_stops(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Net)
    broker = FakeMqtt()
    engine.supervisor.mqtt_broker = broker
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    net = obj(engine, "n")
    await net.ctx.mqtt.subscribe("in/#", net.on_temp)
    assert broker.subscriptions == ["in/#"]
    await engine.supervisor.restart("n")
    assert broker.subscriptions == []
    with pytest.raises(ContextClosedError):
        _ = net.ctx.mqtt
    assert engine.supervisor.get("n").state is State.RUNNING


async def test_core_100_cancelled_subscription_skips_queued_messages(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Net)
    broker = FakeMqtt()
    engine.supervisor.mqtt_broker = broker
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    net = obj(engine, "n")
    sub = await net.ctx.mqtt.subscribe("in/#", net.on_temp)
    await broker.inject("in/a", "slow")
    await clock.settle()
    await broker.inject("in/b", "queued")
    sub.cancel()
    await clock.advance(0.3)
    assert net.got == ["slow"]


def test_core_100_topic_and_grant_matching() -> None:
    assert topic_matches("home/+/temp", "home/kitchen/temp")
    assert not topic_matches("home/+/temp", "home/kitchen/hum")
    assert topic_matches("home/#", "home/a/b/c")
    assert not topic_matches("home/+", "home/a/b")
    assert not topic_matches("a/b", "a")
    assert topic_granted("zigbee2mqtt/living_lamp", ["zigbee2mqtt/living_*"])
    assert not topic_granted("zigbee2mqtt/kitchen", ["zigbee2mqtt/living_*"])
    assert url_granted("http://inverter.lan/api/x", ["http://inverter.lan/api/"])
    assert not url_granted("http://inverter.lan/other", ["http://inverter.lan/api/", ""])


def test_core_100_payload_encoding() -> None:
    assert encode_payload({"a": 1}) == b'{"a":1}'
    assert encode_payload("é") == "é".encode()
    assert encode_payload(b"\x00") == b"\x00"
    with pytest.raises(ValueError, match="JSON"):
        encode_payload({"a": object()})
    with pytest.raises(TypeError):
        encode_payload(5)  # type: ignore[arg-type]
    msg = MqttMessage("t", b'{"x": 2}')
    assert msg.json() == {"x": 2}
    assert msg.text() == '{"x": 2}'


def test_core_100_grant_set_from_config() -> None:
    from mm3.core.config.model import Grants

    model = Grants.model_validate({"http": ["https://a/"], "mqtt": {"publish": ["p/*"]}})
    assert GrantSet.from_config(model) == GrantSet(http=("https://a/",), mqtt_publish=("p/*",))
    assert GrantSet.from_config(None) == GrantSet()
    assert GrantSet.from_config({"mqtt": {"subscribe": ["s"]}}).mqtt_subscribe == ("s",)


def test_core_100_http_response() -> None:
    r = Response(
        404,
        "héllo".encode("latin-1"),
        "https://x/",
        {"content-type": "text/plain; charset=latin-1"},
    )
    assert r.text() == "héllo"
    assert r.bytes() == "héllo".encode("latin-1")
    with pytest.raises(HttpStatusError) as info:
        r.raise_for_status()
    assert info.value.status == 404
    Response(204).raise_for_status()
    assert Response(200, b"[1]").json() == [1]


async def test_core_100_grants_reach_the_context_from_the_config(tmp_path: Any) -> None:
    from mm3.core.config.validate import load_config
    from mm3.core.registry import Registry

    (tmp_path / "mm3.yaml").write_text(
        "modules:\n"
        "  - module: net\n"
        "    id: net_1\n"
        "    grants:\n"
        "      http: ['https://ok.example/']\n"
        "      mqtt: {subscribe: ['in/#']}\n"
    )
    registry = Registry()
    registry.add(Net)
    result = load_config(tmp_path / "mm3.yaml", registry=registry, env_file=tmp_path / "env")
    assert result.ok, result.records
    [spec] = result.instance_specs()
    assert spec.grants.http == ("https://ok.example/",)
    assert spec.grants.mqtt_subscribe == ("in/#",)
