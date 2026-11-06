"""The MQTT bridge and Home Assistant discovery (CORE-338 to CORE-345, CORE-100)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import aiomqtt
import pytest
from pydantic import ValidationError

from conftest import EngineFactory, Recorder, settle
from mm3 import MirrorModule
from mm3.core.engine import Engine
from mm3.core.publicconfig import is_secret_field
from mm3.core.services.grants import GrantSet
from mm3.core.services.hass import ENGINE, HaControls
from mm3.core.services.mqtt import topic_reserved
from mm3.core.settings import MqttSettings
from mm3.core.supervisor import InstanceSpec
from mm3.errors import ConfigurationError, TransientError
from mm3.messages import Notification
from mm3.mqtt import HaEntity, MqttMessage
from mm3.testing import VirtualClock
from mqtt_fake import FakeBroker

BASE = "mm3/test"
NODE = "mm3_test"
CFG = f"homeassistant/%s/{NODE}/%s/config"


def settings(**extra: Any) -> MqttSettings:
    return MqttSettings.model_validate({"url": "mqtt://broker", "base_topic": BASE, **extra})


class Net(MirrorModule):
    name = "net"
    capabilities: ClassVar[set[str]] = {"mqtt.publish", "mqtt.subscribe", "ha.discovery"}

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.got: list[str] = []
        self.commands: list[str] = []
        self.entities: dict[str, HaEntity] = {}

    async def on_msg(self, msg: MqttMessage) -> None:
        self.got.append(msg.text())

    async def on_command(self, text: str) -> None:
        self.commands.append(text)


GRANTS = GrantSet(mqtt_publish=("out/*", "mm3/*"), mqtt_subscribe=("in/#",))


def obj(engine: Engine, iid: str) -> Any:
    return engine.supervisor.get(iid).obj


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


Starter = Callable[..., Awaitable[Engine]]


@pytest.fixture
def start(make_engine: EngineFactory, broker: FakeBroker) -> Starter:
    async def go(
        mqtt: MqttSettings | None = None,
        specs: list[InstanceSpec] | None = None,
        *,
        wait: bool = True,
    ) -> Engine:
        engine = make_engine(Net, mqtt=mqtt if mqtt is not None else settings())
        engine.mqtt.connection._factory = broker.factory
        engine.mqtt.connection._rand = lambda: 0.5  # no jitter
        await engine.start(
            specs if specs is not None else [InstanceSpec("n", "net", grants=GRANTS)]
        )
        if wait:
            await settle()
        return engine

    return go


def notification(name: str, source: str = "core", **payload: Any) -> Notification:
    nid = uuid4()
    return Notification(
        id=nid, trace=nid, ts=datetime.now(UTC), name=name, source=source, payload=payload
    )


# --- CORE-338: configuration -------------------------------------------------------------------


def test_core_338_defaults_and_derived_values() -> None:
    s = MqttSettings.model_validate({"url": "mqtt://broker"})
    assert (s.host, s.port, s.tls) == ("broker", 1883, False)
    assert s.base_topic.startswith("mm3/")
    assert s.client_id.startswith("mm3-")
    assert s.keepalive == 60
    assert s.ha_discovery is False
    assert s.discovery_prefix == "homeassistant"
    assert s.export == []
    assert s.import_ == []
    assert s.node_id == s.base_topic.lower().replace("/", "_").replace("-", "_")[:64]


def test_core_338_mqtts_and_ports() -> None:
    s = MqttSettings.model_validate({"url": "mqtts://secure.example"})
    assert (s.port, s.tls) == (8883, True)
    assert MqttSettings.model_validate({"url": "mqtt://b:1884"}).port == 1884


@pytest.mark.parametrize(
    "url",
    [
        "http://broker",
        "mqtt://",
        "mqtt://broker/path",
        "mqtt://user:pw@broker",
        "mqtt://broker:0",
        "mqtt://broker:70000",
    ],
)
def test_core_338_bad_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        MqttSettings.model_validate({"url": url})


@pytest.mark.parametrize("base", ["a/#", "a/+", "/a", "a/", ""])
def test_core_338_bad_topic_prefixes(base: str) -> None:
    with pytest.raises(ValidationError):
        MqttSettings.model_validate({"url": "mqtt://b", "base_topic": base})
    with pytest.raises(ValidationError):
        MqttSettings.model_validate({"url": "mqtt://b", "discovery_prefix": base})


def test_core_338_node_id_comes_from_the_base_topic() -> None:
    s = MqttSettings.model_validate({"url": "mqtt://b", "base_topic": "mm3/Living-Room"})
    assert s.node_id == "mm3_living_room"
    s = MqttSettings.model_validate({"url": "mqtt://b", "base_topic": "x", "node_id": "mine"})
    assert s.node_id == "mine"


def test_core_338_the_password_is_a_secret_and_not_in_repr() -> None:
    s = settings(username="u", password="hunter2-hunter2")  # noqa: S106
    assert is_secret_field(MqttSettings.model_fields["password"])
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(s)


def test_core_338_unknown_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        MqttSettings.model_validate({"url": "mqtt://b", "nope": 1})


# --- CORE-339: connection ----------------------------------------------------------------------


async def test_core_339_last_will_online_and_subscription(
    start: Starter, broker: FakeBroker
) -> None:
    engine = await start()
    will = broker.wills[0]
    assert (will.topic, will.payload, will.qos, will.retain) == (
        f"{BASE}/state",
        "offline",
        1,
        True,
    )
    online = broker.sent(f"{BASE}/state")
    assert [(m.text(), m.qos, m.retain) for m in online] == [("online", 1, True)]
    assert f"{BASE}/notify/+" in broker.subscribed
    status = engine.mqtt.status()
    assert status is not None
    assert status["connected"] is True
    assert status["error"] is None
    assert status["since"]


async def test_core_339_offline_before_a_clean_stop(start: Starter, broker: FakeBroker) -> None:
    engine = await start()
    await engine.stop()
    assert broker.last(f"{BASE}/state") == "offline"
    assert broker.retained[f"{BASE}/state"] == b"offline"


async def test_core_339_without_the_block_the_bridge_is_off(make_engine: EngineFactory) -> None:
    engine = make_engine(Net)
    await engine.start([])
    assert engine.mqtt.status() is None
    assert engine.mqtt.enabled is False


async def test_core_339_backoff_doubles_and_reports_the_reason(
    start: Starter, broker: FakeBroker, clock: VirtualClock
) -> None:
    broker.failures = [OSError("password=secret-secret"), TimeoutError(), aiomqtt.MqttError("x")]
    engine = await start()
    assert broker.attempts == 1
    status = engine.mqtt.status()
    assert status is not None
    assert status["connected"] is False
    assert status["error"] == "unreachable"
    await clock.advance(0.9)
    await settle()
    assert broker.attempts == 1
    await clock.advance(0.2)  # 1 s
    await settle()
    assert broker.attempts == 2
    assert (engine.mqtt.status() or {})["error"] == "timeout"
    await clock.advance(1.8)  # t = 2.9
    await settle()
    assert broker.attempts == 2
    await clock.advance(0.2)  # t = 3.1: the third attempt was at 3.0
    await settle()
    assert broker.attempts == 3
    assert (engine.mqtt.status() or {})["error"] == "lost"
    await clock.advance(3.8)  # t = 6.9
    await settle()
    assert broker.attempts == 3
    await clock.advance(0.2)  # t = 7.1: the fourth attempt was at 7.0
    await settle()
    assert broker.attempts == 4
    assert engine.mqtt.connected
    assert engine.mqtt.health()["failures"] == 3


async def test_core_339_backoff_is_capped(
    start: Starter, broker: FakeBroker, clock: VirtualClock
) -> None:
    broker.failures = [OSError()] * 10
    await start()
    times: list[int] = [0]
    for second in range(1, 400):
        await clock.advance(1.0)
        await settle()
        if broker.attempts > len(times):
            times.append(second)
    gaps = [b - a for a, b in pairwise(times)]
    assert gaps[:9] == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert broker.connects == 1


async def test_core_339_credentials_never_reach_the_log(
    start: Starter, broker: FakeBroker, tmp_path: Path
) -> None:
    broker.failures = [OSError("bad password hunter2-hunter2 for user bob")]
    await start(settings(username="bob", password="hunter2-hunter2"))  # noqa: S106
    text = (tmp_path / "log.jsonl").read_text()
    assert "hunter2" not in text
    assert "mqtt_connection_failed" in text


async def test_core_339_reconnect_restores_subscriptions_and_says_online(
    start: Starter, broker: FakeBroker, clock: VirtualClock
) -> None:
    engine = await start()
    await obj(engine, "n").ctx.mqtt.subscribe("in/#", obj(engine, "n").on_msg)
    assert "in/#" in broker.subscribed
    broker.subscribed.clear()
    broker.drop()
    await settle()
    assert not engine.mqtt.connected
    await clock.advance(1.1)
    await settle()
    assert engine.mqtt.connected
    assert broker.connects == 2
    assert {"in/#", f"{BASE}/notify/+"} <= set(broker.subscribed)
    assert [m.text() for m in broker.sent(f"{BASE}/state")] == ["online", "online"]


async def test_core_339_reconfigure_reconnects_with_the_new_block(
    start: Starter, broker: FakeBroker
) -> None:
    engine = await start()
    await engine.mqtt.reconfigure(settings(base_topic="mm3/other"), None)
    await settle()
    assert broker.wills[-1].topic == "mm3/other/state"
    assert broker.last("mm3/other/state") == "online"
    assert engine.mqtt.base_topic == "mm3/other"
    await engine.mqtt.reconfigure(None, None)
    await settle()
    assert engine.mqtt.status() is None
    assert broker.last("mm3/other/state") == "offline"


async def test_core_339_the_ca_path_is_relative_to_the_configuration(
    start: Starter, broker: FakeBroker, tmp_path: Path
) -> None:
    engine = await start(settings(ca="ca.pem"), [])
    await engine.mqtt.reconfigure(settings(ca="ca.pem"), tmp_path / "etc")
    await settle()
    assert broker.cas[-1] == tmp_path / "etc" / "ca.pem"


# --- CORE-340: inbound -------------------------------------------------------------------------


async def test_core_340_an_imported_message_reaches_the_bus(
    start: Starter, broker: FakeBroker, recorder: Recorder
) -> None:
    engine = await start(settings(**{"import": ["ALERT_*"]}))
    broker.inject(f"{BASE}/notify/ALERT_DOOR", json.dumps({"open": True}))
    await settle()
    received = [n for n, _ in recorder.notifications if n.name == "ALERT_DOOR"]
    assert len(received) == 1
    assert received[0].source == "mqtt"
    assert received[0].payload == {"open": True}
    assert engine.mqtt.health()["received"] == 1


async def test_core_340_an_empty_payload_is_an_empty_object(
    start: Starter, broker: FakeBroker, recorder: Recorder
) -> None:
    await start(settings(**{"import": ["*"]}))
    broker.inject(f"{BASE}/notify/PING", b"")
    await settle()
    assert [n.payload for n, _ in recorder.notifications if n.name == "PING"] == [{}]


@pytest.mark.parametrize(
    ("name", "payload", "reason"),
    [
        ("OTHER_THING", "{}", "not imported"),
        ("CORE_PAGE_CHANGED", "{}", "reserved"),
        ("lower_case", "{}", "name"),
        ("ALERT_X", "not json", "not JSON"),
        ("ALERT_X", "[1, 2]", "not an object"),
        ("ALERT_X", "{" + '"a":"' + "x" * 300_000 + '"}', "too large"),
    ],
)
async def test_core_340_refused_messages_are_dropped_and_counted(
    start: Starter,
    broker: FakeBroker,
    recorder: Recorder,
    name: str,
    payload: str,
    reason: str,
    tmp_path: Path,
) -> None:
    engine = await start(settings(**{"import": ["ALERT_*", "CORE_*", "lower_*"]}))
    broker.inject(f"{BASE}/notify/{name}", payload)
    await settle()
    assert name not in recorder.names()
    health = engine.mqtt.health()
    assert health["dropped"] == 1
    assert f'"reason": "{reason}"' in (tmp_path / "log.jsonl").read_text().replace('":"', '": "')


async def test_core_340_drop_warnings_are_rate_limited(
    start: Starter, broker: FakeBroker, clock: VirtualClock, tmp_path: Path
) -> None:
    engine = await start(settings(**{"import": ["ALERT_*"]}))
    for _ in range(50):
        broker.inject(f"{BASE}/notify/ALERT_X", "not json")
    await settle()
    assert engine.mqtt.health()["dropped"] == 50
    log = (tmp_path / "log.jsonl").read_text()
    assert log.count("mqtt_notify_dropped") == 1
    await clock.advance(61)
    broker.inject(f"{BASE}/notify/ALERT_X", "not json")
    await settle()
    assert (tmp_path / "log.jsonl").read_text().count("mqtt_notify_dropped") == 2


async def test_core_340_nothing_is_imported_by_default(
    start: Starter, broker: FakeBroker, recorder: Recorder
) -> None:
    engine = await start()
    broker.inject(f"{BASE}/notify/ALERT_X", "{}")
    await settle()
    assert "ALERT_X" not in recorder.names()
    assert engine.mqtt.health()["dropped"] == 1


# --- CORE-341: outbound ------------------------------------------------------------------------


async def test_core_341_exported_names_are_published(start: Starter, broker: FakeBroker) -> None:
    engine = await start(settings(export=["ALERT_*"]))
    engine.bus.publish(notification("ALERT_DOOR", open=True))
    engine.bus.publish(notification("OTHER", x=1))
    await settle()
    sent = broker.sent(f"{BASE}/event/ALERT_DOOR")
    assert len(sent) == 1
    assert (sent[0].qos, sent[0].retain) == (0, False)
    envelope = json.loads(sent[0].payload)
    assert envelope["name"] == "ALERT_DOOR"
    assert envelope["payload"] == {"open": True}
    assert not broker.sent(f"{BASE}/event/OTHER")
    assert engine.mqtt.health()["exported"] == 1


async def test_core_341_messages_from_mqtt_are_not_echoed(
    start: Starter, broker: FakeBroker
) -> None:
    await start(settings(export=["*"], **{"import": ["ALERT_*"]}))
    broker.inject(f"{BASE}/notify/ALERT_X", "{}")
    await settle()
    assert not broker.sent(f"{BASE}/event/ALERT_X")


async def test_core_51_a_message_from_mqtt_is_never_sent_back_but_others_are(
    start: Starter, broker: FakeBroker
) -> None:
    engine = await start(settings(export=["*"], **{"import": ["*"]}))
    broker.inject(f"{BASE}/notify/ALERT_IN", "{}")
    engine.bus.publish(notification("ALERT_OUT"))
    await settle()
    assert not broker.sent(f"{BASE}/event/ALERT_IN")
    assert len(broker.sent(f"{BASE}/event/ALERT_OUT")) == 1


async def test_core_341_targeted_messages_and_replies_stay_inside(
    start: Starter, broker: FakeBroker
) -> None:
    engine = await start(settings(export=["*"]))
    nid = uuid4()
    engine.bus.publish(
        Notification(
            id=nid, trace=nid, ts=datetime.now(UTC), name="ALERT_X", source="core", target="n"
        )
    )
    await settle()
    assert not broker.sent(f"{BASE}/event/ALERT_X")


async def test_core_341_exports_are_dropped_while_disconnected(
    start: Starter, broker: FakeBroker
) -> None:
    broker.failures = [OSError()]
    engine = await start(settings(export=["ALERT_*"]))
    engine.bus.publish(notification("ALERT_X"))
    await settle()
    assert engine.mqtt.health()["export_dropped"] == 1
    assert engine.mqtt.health()["exported"] == 0
    assert not broker.log


# --- CORE-342: ctx.mqtt ------------------------------------------------------------------------


async def test_core_342_without_a_bridge_publish_and_subscribe_are_configuration_errors(
    make_engine: EngineFactory,
) -> None:
    engine = make_engine(Net)
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    ctx = obj(engine, "n").ctx
    with pytest.raises(ConfigurationError) as info:
        await ctx.mqtt.publish("out/x", "1")
    assert info.value.message_key == "CORE_MQTT_NOT_CONFIGURED"
    with pytest.raises(ConfigurationError):
        await ctx.mqtt.subscribe("in/#", obj(engine, "n").on_msg)
    with pytest.raises(ConfigurationError):
        await ctx.mqtt.ha_entity("t", "sensor", name="T")


async def test_core_342_publish_goes_to_the_broker(start: Starter, broker: FakeBroker) -> None:
    engine = await start()
    await obj(engine, "n").ctx.mqtt.publish("out/x", {"a": 1}, qos=1, retain=True)
    sent = broker.sent("out/x")
    assert [(m.text(), m.qos, m.retain) for m in sent] == [('{"a":1}', 1, True)]


async def test_core_342_publish_while_disconnected_is_transient(
    start: Starter, broker: FakeBroker
) -> None:
    broker.failures = [OSError()]
    engine = await start()
    with pytest.raises(TransientError) as info:
        await obj(engine, "n").ctx.mqtt.publish("out/x", "1")
    assert info.value.message_key == "CORE_MQTT_DISCONNECTED"
    assert not broker.log


async def test_core_342_a_failed_publish_is_transient(start: Starter, broker: FakeBroker) -> None:
    engine = await start()
    broker.fail_publish = True
    with pytest.raises(TransientError) as info:
        await obj(engine, "n").ctx.mqtt.publish("out/x", "1")
    assert info.value.message_key == "CORE_MQTT_PUBLISH_FAILED"


@pytest.mark.parametrize(
    "topic",
    [f"{BASE}/notify/ALERT_X", f"{BASE}/event/ALERT_X", f"{BASE}/state", f"{BASE}/ha/n/x/state"],
)
async def test_core_342_engine_topics_cannot_be_published(
    start: Starter, broker: FakeBroker, topic: str
) -> None:
    engine = await start()
    before = len(broker.log)
    with pytest.raises(PermissionError):
        await obj(engine, "n").ctx.mqtt.publish(topic, "x")
    assert len(broker.log) == before


def test_core_342_reserved_topics() -> None:
    assert topic_reserved(BASE, f"{BASE}/state")
    assert topic_reserved(BASE, f"{BASE}/notify/A")
    assert not topic_reserved(BASE, f"{BASE}/other")
    assert not topic_reserved(None, f"{BASE}/state")


async def test_core_342_subscribe_while_disconnected_follows_the_connection(
    start: Starter, broker: FakeBroker, clock: VirtualClock
) -> None:
    broker.failures = [OSError()]
    engine = await start()
    mod = obj(engine, "n")
    await mod.ctx.mqtt.subscribe("in/#", mod.on_msg)
    await clock.advance(1.1)
    await settle()
    assert "in/#" in broker.subscribed
    broker.inject("in/temp", "21")
    await settle()
    assert mod.got == ["21"]


# --- CORE-343: Home Assistant, engine entities -------------------------------------------------


class Pages:
    """A stand-in for the page service."""

    def __init__(self) -> None:
        self.ids = ["home", "news"]
        self.page = "home"
        self.listeners: list[Callable[[], None]] = []
        self.calls: list[tuple[str, str]] = []
        self.hidden: dict[str, bool] = {}

    def set_page(self, page: str, reason: str) -> None:
        self.calls.append((page, reason))
        self.page = page
        for cb in self.listeners:
            cb()

    def visible(self, iid: str) -> bool:
        return not self.hidden.get(iid, False)

    def set_manual(self, iid: str, hidden: bool) -> None:
        self.hidden[iid] = hidden
        for cb in self.listeners:
            cb()


@pytest.fixture
def ha_start(make_engine: EngineFactory, broker: FakeBroker) -> Callable[..., Awaitable[Any]]:
    async def go(*, controls: bool = True, **extra: Any) -> tuple[Engine, Pages, list[str]]:
        pages = Pages()
        reloaded: list[str] = []
        engine = make_engine(Net, mqtt=settings(ha_discovery=True, **extra))
        engine.mqtt.connection._factory = broker.factory
        engine.mqtt.connection._rand = lambda: 0.5
        if controls:
            engine.mqtt.ha.attach_controls(
                HaControls(
                    pages=pages,  # type: ignore[arg-type]
                    reload_shells=lambda: reloaded.append("reload"),
                )
            )
        await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
        await settle()
        return engine, pages, reloaded

    return go


def config(broker: FakeBroker, component: str, object_id: str) -> dict[str, Any]:
    raw = broker.retained[CFG % (component, object_id)]
    assert isinstance(raw, bytes)
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


async def test_core_343_engine_entities_are_discovered(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    await ha_start()
    reload = config(broker, "button", f"{ENGINE}-reload")
    assert reload["name"] == "Reload browsers"
    assert reload["unique_id"] == f"{NODE}-{ENGINE}-reload"
    assert reload["command_topic"] == f"{BASE}/ha/{ENGINE}/reload/set"
    assert reload["availability_topic"] == f"{BASE}/state"
    assert reload["device"]["identifiers"] == [NODE]
    page = config(broker, "select", f"{ENGINE}-page")
    assert page["options"] == ["home", "news"]
    assert page["state_topic"] == f"{BASE}/ha/{ENGINE}/page/state"
    config(broker, "switch", f"{ENGINE}-n_enabled")
    config(broker, "switch", f"{ENGINE}-n_visible")
    assert broker.last(f"{BASE}/ha/{ENGINE}/page/state") == "home"
    assert broker.last(f"{BASE}/ha/{ENGINE}/n_enabled/state") == "ON"
    # Discovery configs are retained at QoS 1 (CORE-343).
    sent = broker.sent(CFG % ("button", f"{ENGINE}-reload"))
    assert (sent[0].qos, sent[0].retain) == (1, True)


async def test_core_343_discovery_is_off_by_default(start: Starter, broker: FakeBroker) -> None:
    await start()
    assert not broker.configs()
    assert "homeassistant/status" not in broker.subscribed


async def test_core_343_commands_steer_the_engine(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, pages, reloaded = await ha_start()
    broker.inject(f"{BASE}/ha/{ENGINE}/page/set", "news")
    broker.inject(f"{BASE}/ha/{ENGINE}/reload/set", "PRESS")
    await settle()
    assert pages.calls == [("news", "ha")]
    assert reloaded == ["reload"]
    assert broker.last(f"{BASE}/ha/{ENGINE}/page/state") == "news"
    broker.inject(f"{BASE}/ha/{ENGINE}/page/set", "nope")
    await settle()
    assert pages.calls == [("news", "ha")]
    assert engine.mqtt.connected


async def test_core_343_an_instance_can_be_stopped_and_started(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    broker.inject(f"{BASE}/ha/{ENGINE}/n_enabled/set", "OFF")
    await settle()
    assert engine.supervisor.get("n").runtime_stopped
    assert broker.last(f"{BASE}/ha/{ENGINE}/n_enabled/state") == "OFF"
    broker.inject(f"{BASE}/ha/{ENGINE}/n_enabled/set", "ON")
    await settle()
    assert not engine.supervisor.get("n").runtime_stopped
    assert broker.last(f"{BASE}/ha/{ENGINE}/n_enabled/state") == "ON"


async def test_core_343_state_follows_page_changes(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    _, pages, _ = await ha_start()
    pages.page = "news"
    for cb in pages.listeners:
        cb()
    await settle()
    assert broker.last(f"{BASE}/ha/{ENGINE}/page/state") == "news"


async def test_core_343_everything_is_published_again_after_a_reconnect(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker, clock: VirtualClock
) -> None:
    await ha_start()
    first = len(broker.sent(CFG % ("button", f"{ENGINE}-reload")))
    broker.drop()
    await settle()
    await clock.advance(1.1)
    await settle()
    assert len(broker.sent(CFG % ("button", f"{ENGINE}-reload"))) == first + 1


async def test_core_343_everything_is_published_again_when_home_assistant_restarts(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    await ha_start()
    first = len(broker.sent(CFG % ("button", f"{ENGINE}-reload")))
    broker.inject("homeassistant/status", "offline")
    await settle()
    assert len(broker.sent(CFG % ("button", f"{ENGINE}-reload"))) == first
    broker.inject("homeassistant/status", "online")
    await settle()
    assert len(broker.sent(CFG % ("button", f"{ENGINE}-reload"))) == first + 1


async def test_core_343_a_locale_change_renames_the_entities(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    engine.supervisor.settings = engine.supervisor.settings.model_copy(update={"locale": "it"})
    engine.bus.publish(notification("CORE_LOCALE_CHANGED"))
    await settle()
    assert config(broker, "button", f"{ENGINE}-reload")["name"] == "Ricarica i browser"


async def test_core_343_a_custom_discovery_prefix(
    make_engine: EngineFactory, broker: FakeBroker
) -> None:
    engine = make_engine(Net, mqtt=settings(ha_discovery=True, discovery_prefix="ha"))
    engine.mqtt.connection._factory = broker.factory
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    await settle()
    assert any(t.startswith(f"ha/switch/{NODE}/") for t in broker.configs("ha"))
    assert "ha/status" in broker.subscribed


# --- CORE-344: entities of modules -------------------------------------------------------------


async def add_entity(
    engine: Engine, object_id: str = "temp", component: str = "sensor", **kw: Any
) -> HaEntity:
    mod = obj(engine, "n")
    entity: HaEntity = await mod.ctx.mqtt.ha_entity(
        object_id, component, name=kw.pop("name", "Temp"), **kw
    )
    mod.entities[object_id] = entity
    return entity


async def test_core_344_a_sensor_is_discovered_and_gets_a_state(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    entity = await add_entity(engine, device_class="temperature", unit="°C", icon="mdi:thermometer")
    cfg = config(broker, "sensor", "n-temp")
    assert cfg["name"] == "Temp"
    assert cfg["device_class"] == "temperature"
    assert cfg["unit_of_measurement"] == "°C"
    assert cfg["icon"] == "mdi:thermometer"
    assert cfg["state_topic"] == f"{BASE}/ha/n/temp/state"
    assert "command_topic" not in cfg
    await entity.set_state(21.5)
    assert broker.last(f"{BASE}/ha/n/temp/state") == "21.5"
    await entity.set_state(True)
    assert broker.last(f"{BASE}/ha/n/temp/state") == "ON"
    await entity.set_state("warm")
    assert broker.last(f"{BASE}/ha/n/temp/state") == "warm"
    state = broker.sent(f"{BASE}/ha/n/temp/state")[-1]
    assert (state.qos, state.retain) == (1, True)


async def test_core_344_a_switch_command_reaches_the_module(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    await add_entity(engine, "lamp", "switch", name="Lamp", on_command=obj(engine, "n").on_command)
    cfg = config(broker, "switch", "n-lamp")
    assert cfg["command_topic"] == f"{BASE}/ha/n/lamp/set"
    broker.inject(f"{BASE}/ha/n/lamp/set", "ON")
    broker.inject(f"{BASE}/ha/n/lamp/set", "OFF")
    await settle()
    assert obj(engine, "n").commands == ["ON", "OFF"]


async def test_core_344_number_and_select_extras(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    on = obj(engine, "n").on_command
    await add_entity(
        engine, "level", "number", name="Level", min=0, max=10, step=0.5, on_command=on
    )
    await add_entity(engine, "mode", "select", name="Mode", options=["a", "b"], on_command=on)
    number = config(broker, "number", "n-level")
    assert (number["min"], number["max"], number["step"]) == (0, 10, 0.5)
    assert config(broker, "select", "n-mode")["options"] == ["a", "b"]


async def test_core_344_arguments_are_checked(
    ha_start: Callable[..., Awaitable[Any]],
) -> None:
    engine, _, _ = await ha_start()
    on = obj(engine, "n").on_command
    await add_entity(engine, "ok", "sensor")
    with pytest.raises(ValueError, match="object_id"):
        await add_entity(engine, "Bad-Id", "sensor")
    with pytest.raises(ValueError, match="already used"):
        await add_entity(engine, "ok", "sensor")
    with pytest.raises(ValueError, match="component"):
        await add_entity(engine, "x", "light")
    with pytest.raises(ValueError, match="options"):
        await add_entity(engine, "s", "select", on_command=on)
    with pytest.raises(ValueError, match="commands"):
        await add_entity(engine, "t", "sensor", on_command=on)


async def test_core_344_the_capability_is_required(
    make_engine: EngineFactory, broker: FakeBroker
) -> None:
    class Plain(MirrorModule):
        name = "plain"
        capabilities: ClassVar[set[str]] = set()

    engine = make_engine(Plain, mqtt=settings(ha_discovery=True))
    engine.mqtt.connection._factory = broker.factory
    await engine.start([InstanceSpec("p", "plain")])
    await settle()
    with pytest.raises(PermissionError, match=r"ha\.discovery"):
        await obj(engine, "p").ctx.mqtt.ha_entity("t", "sensor", name="T")


async def test_core_344_with_discovery_off_an_entity_publishes_nothing(
    start: Starter, broker: FakeBroker
) -> None:
    engine = await start()
    entity = await add_entity(engine)
    await entity.set_state(1)
    assert not broker.configs()
    assert not broker.sent(f"{BASE}/ha/n/temp/state")


async def test_core_344_commands_stop_when_the_instance_stops(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    await add_entity(engine, "lamp", "switch", name="Lamp", on_command=obj(engine, "n").on_command)
    mod = obj(engine, "n")
    await engine.supervisor.get("n").runtime_stop()
    await settle()
    broker.inject(f"{BASE}/ha/n/lamp/set", "ON")
    await settle()
    assert mod.commands == []


# --- CORE-345: stale entities ------------------------------------------------------------------


def record_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "mqtt" / "discovery.json"


async def test_core_345_remove_clears_the_entity_at_once(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    entity = await add_entity(engine)
    topic = CFG % ("sensor", "n-temp")
    assert topic in broker.retained
    await entity.remove()
    assert topic not in broker.retained
    assert broker.sent(topic)[-1].payload == b""
    await entity.remove()  # idempotent
    await add_entity(engine)  # the object_id is free again
    assert topic in broker.retained


async def test_core_345_the_published_entities_are_recorded(
    ha_start: Callable[..., Awaitable[Any]], tmp_path: Path
) -> None:
    engine, _, _ = await ha_start()
    await add_entity(engine)
    data = json.loads(record_file(tmp_path).read_text())
    assert data["entities"][CFG % ("sensor", "n-temp")] == {"instance": "n", "object": "temp"}
    assert data["entities"][CFG % ("button", f"{ENGINE}-reload")] == {
        "instance": ENGINE,
        "object": "reload",
    }


async def test_core_345_entities_of_removed_instances_and_engine_entities_that_are_gone(
    make_engine: EngineFactory, broker: FakeBroker, tmp_path: Path
) -> None:
    gone = CFG % ("sensor", "old-temp")
    engine_gone = CFG % ("button", f"{ENGINE}-vanished")
    record_file(tmp_path).parent.mkdir(parents=True)
    record_file(tmp_path).write_text(
        json.dumps(
            {
                "entities": {
                    gone: {"instance": "old", "object": "temp"},
                    engine_gone: {"instance": ENGINE, "object": "vanished"},
                }
            }
        )
    )
    broker.retained[gone] = b"{}"
    broker.retained[engine_gone] = b"{}"
    engine = make_engine(Net, mqtt=settings(ha_discovery=True))
    engine.mqtt.connection._factory = broker.factory
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    await settle()
    assert gone not in broker.retained
    assert engine_gone not in broker.retained
    assert gone not in json.loads(record_file(tmp_path).read_text())["entities"]


async def test_core_345_a_running_instance_that_does_not_register_again_loses_the_entity(
    make_engine: EngineFactory, broker: FakeBroker, tmp_path: Path
) -> None:
    stale = CFG % ("sensor", "n-before")
    record_file(tmp_path).parent.mkdir(parents=True)
    record_file(tmp_path).write_text(
        json.dumps({"entities": {stale: {"instance": "n", "object": "before"}}})
    )
    broker.retained[stale] = b"{}"
    engine = make_engine(Net, mqtt=settings(ha_discovery=True))
    engine.mqtt.connection._factory = broker.factory
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    await settle()
    assert stale not in broker.retained


async def test_core_345_entities_of_a_stopped_instance_stay(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    await add_entity(engine)
    await engine.supervisor.get("n").runtime_stop()
    await engine.mqtt.ha.sync()
    assert CFG % ("sensor", "n-temp") in broker.retained


async def test_core_345_switching_discovery_off_clears_what_was_published(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    await add_entity(engine)
    assert broker.configs()
    await engine.mqtt.reconfigure(settings(ha_discovery=False), None)
    await settle()
    await engine.mqtt.ha.sync()
    assert not broker.configs()


async def test_core_345_switching_the_bridge_off_clears_what_was_published(
    ha_start: Callable[..., Awaitable[Any]], broker: FakeBroker
) -> None:
    engine, _, _ = await ha_start()
    assert broker.configs()
    await engine.mqtt.reconfigure(None, None)
    await settle()
    assert not broker.configs()


async def test_core_345_an_unreadable_record_is_ignored(
    make_engine: EngineFactory, broker: FakeBroker, tmp_path: Path
) -> None:
    record_file(tmp_path).parent.mkdir(parents=True)
    record_file(tmp_path).write_text("{not json")
    engine = make_engine(Net, mqtt=settings(ha_discovery=True))
    engine.mqtt.connection._factory = broker.factory
    await engine.start([InstanceSpec("n", "net", grants=GRANTS)])
    await settle()
    assert broker.configs()


async def test_core_100_the_bridge_survives_a_slow_start(
    start: Starter, broker: FakeBroker
) -> None:
    engine = await start(wait=False)
    await asyncio.sleep(0)
    await settle()
    assert engine.mqtt.connected
