"""Request and reply (CORE-220..222) and the shared notification registry (CORE-224..226)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from conftest import EngineFactory, Recorder
from mm3 import ConfigurationError, MirrorModule, ModuleConfig, Notification
from mm3.core.engine import Engine
from mm3.core.supervisor import InstanceSpec, State
from mm3.errors import RemoteError, RequestTimeoutError, ReservedNameError, TransientError
from mm3.notifications import REGISTRY, json_schema, validate_payload
from mm3.testing import VirtualClock


class Asker(MirrorModule):
    """Sends requests; records notifications and requests it receives."""

    name = "asker"

    def __init__(self, ctx: Any, config: ModuleConfig) -> None:
        super().__init__(ctx, config)
        self.seen: list[Notification] = []
        self.requests: list[Notification] = []
        self.results: list[Any] = []

    async def on_notification(self, n: Notification) -> None:
        self.seen.append(n)
        if n.name == "ASK_INSIDE":
            # CORE-222: awaiting a request inside a handler resolves.
            self.results.append(await self.ctx.request("TIME_NOW", target="clock_1"))

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        self.requests.append(req)
        return None


class Clock(MirrorModule):
    """Answers TIME_NOW; FAIL_* requests raise."""

    name = "clock"

    def __init__(self, ctx: Any, config: ModuleConfig) -> None:
        super().__init__(ctx, config)
        self.seen: list[Notification] = []
        self.requests: list[Notification] = []

    async def on_notification(self, n: Notification) -> None:
        self.seen.append(n)

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        self.requests.append(req)
        if req.name == "TIME_NOW":
            return {"by": self.ctx.instance_id}
        if req.name == "FAIL_BUG":
            raise KeyError("bug")
        if req.name == "FAIL_TRANSIENT":
            raise TransientError("CLOCK_OFFLINE")
        if req.name == "FAIL_CONFIG":
            raise ConfigurationError("source", "CLOCK_SOURCE_REFUSED")
        if req.name == "SLOW":
            await asyncio.sleep(10)
        if req.name == "BAD_REPLY":
            return {"x": object()}
        return None


class Picky(Clock):
    """Subscribes to nothing it is asked about."""

    name = "picky"
    subscribes = {"UNRELATED"}  # noqa: RUF012


def obj(engine: Engine, instance: str) -> Any:
    return engine.supervisor.get(instance).obj


async def two(make_engine: EngineFactory, *specs: InstanceSpec) -> Engine:
    engine = make_engine(Asker, Clock, Picky)
    await engine.start([InstanceSpec("asker_1", "asker"), InstanceSpec("clock_1", "clock"), *specs])
    return engine


# --- envelope -------------------------------------------------------------------


async def test_core_20_request_and_reply_fields(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = await two(make_engine)
    reply = await obj(engine, "asker_1").ctx.request("TIME_NOW", {"tz": "UTC"}, target="clock_1")
    assert reply == {"by": "clock_1"}
    req = obj(engine, "clock_1").requests[-1]
    assert req.expects_reply
    assert req.reply_to is None
    assert req.is_request
    replies = [n for n, _ in recorder.notifications if n.is_reply]
    assert len(replies) == 1
    assert replies[0].reply_to == req.id
    assert not replies[0].expects_reply


# --- request and reply ----------------------------------------------------------


async def test_core_220_targeted_request_asks_only_target(make_engine: EngineFactory) -> None:
    engine = await two(make_engine, InstanceSpec("clock_2", "clock"))
    reply = await obj(engine, "asker_1").ctx.request("TIME_NOW", target="clock_2")
    assert reply == {"by": "clock_2"}
    assert obj(engine, "clock_1").requests == []


async def test_core_220_broadcast_first_reply_wins_later_dropped(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine, InstanceSpec("clock_2", "clock"))
    asker = obj(engine, "asker_1")
    reply = await asker.ctx.request("TIME_NOW")
    assert reply["by"] in {"clock_1", "clock_2"}
    await clock.settle()
    assert len(obj(engine, "clock_1").requests) == len(obj(engine, "clock_2").requests) == 1
    assert asker.requests == []  # the sender's own backend is excluded (CORE-21)


async def test_core_220_broadcast_request_follows_subscribes(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine, InstanceSpec("picky_1", "picky"))
    await obj(engine, "asker_1").ctx.request("TIME_NOW")
    await clock.settle()
    assert obj(engine, "picky_1").requests == []


async def test_core_220_timeout_and_argument_checks(make_engine: EngineFactory) -> None:
    engine = await two(make_engine)
    ctx = obj(engine, "asker_1").ctx
    with pytest.raises(RequestTimeoutError) as info:
        await ctx.request("NOBODY_ANSWERS", timeout=0.1)
    assert info.value.name == "NOBODY_ANSWERS"
    for bad in (0.05, 61):
        with pytest.raises(ValueError, match="timeout"):
            await ctx.request("TIME_NOW", timeout=bad)
    with pytest.raises(ValueError, match="UPPER_SNAKE"):
        await ctx.request("time_now")
    with pytest.raises(ValueError, match="JSON"):
        await ctx.request("TIME_NOW", {"x": object()})
    assert engine.bus._pending == {}


async def test_core_22_request_with_reserved_name(make_engine: EngineFactory) -> None:
    engine = await two(make_engine)
    with pytest.raises(ReservedNameError):
        await obj(engine, "asker_1").ctx.request("CORE_ANYTHING")


async def test_core_221_none_means_not_mine_and_reply_envelope(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = await two(make_engine, InstanceSpec("asker_2", "asker"))
    asker = obj(engine, "asker_1")
    await asker.ctx.send_notification("WARMUP")
    reply = await asker.ctx.request("TIME_NOW")
    assert reply == {"by": "clock_1"}
    assert len(obj(engine, "asker_2").requests) == 1  # answered None: no reply sent
    req = obj(engine, "clock_1").requests[-1]
    (rep,) = [n for n, _ in recorder.notifications if n.is_reply]
    assert (rep.name, rep.source, rep.target, rep.trace) == (
        "TIME_NOW",
        "clock/clock_1",
        "asker_1",
        req.trace,
    )


async def test_core_221_requests_never_reach_on_notification(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine)
    await obj(engine, "asker_1").ctx.request("TIME_NOW", target="clock_1")
    await clock.settle()
    assert [n.name for n in obj(engine, "clock_1").seen] == ["CORE_READY"]
    assert [n for n in obj(engine, "asker_1").seen if n.is_reply] == []


async def test_core_221_error_reply_internal_and_failure(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine)
    with pytest.raises(RemoteError) as info:
        await obj(engine, "asker_1").ctx.request("FAIL_BUG", target="clock_1")
    assert (info.value.code, info.value.message_key, info.value.source) == (
        "internal",
        "REQUEST_FAILED",
        "clock/clock_1",
    )
    await clock.settle()
    assert engine.supervisor.get("clock_1").state is State.FAILED
    assert engine.supervisor.get("asker_1").state is State.RUNNING


async def test_core_221_error_reply_for_bad_reply_payload(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine)
    with pytest.raises(RemoteError, match="internal"):
        await obj(engine, "asker_1").ctx.request("BAD_REPLY", target="clock_1")
    await clock.settle()
    assert engine.supervisor.get("clock_1").state is State.FAILED


async def test_core_221_transient_error_reply_keeps_running(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine)
    with pytest.raises(RemoteError) as info:
        await obj(engine, "asker_1").ctx.request("FAIL_TRANSIENT", target="clock_1")
    assert (info.value.code, info.value.message_key) == ("transient", "CLOCK_OFFLINE")
    await clock.settle()
    inst = engine.supervisor.get("clock_1")
    assert inst.state is State.RUNNING
    assert inst.counters.transient_errors == 1


async def test_core_221_configuration_error_reply_disables(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine)
    with pytest.raises(RemoteError) as info:
        await obj(engine, "asker_1").ctx.request("FAIL_CONFIG", target="clock_1")
    assert (info.value.code, info.value.message_key) == ("configuration", "CLOCK_SOURCE_REFUSED")
    await clock.settle()
    assert engine.supervisor.get("clock_1").state is State.DISABLED


async def test_core_221_on_request_over_budget_sends_no_reply(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = await two(make_engine)  # handler budget 0.2 s
    with pytest.raises(RequestTimeoutError):
        await obj(engine, "asker_1").ctx.request("SLOW", target="clock_1", timeout=0.5)
    inst = engine.supervisor.get("clock_1")
    assert inst.counters.handler_timeouts == 1
    assert inst.state is State.RUNNING
    assert [n for n, _ in recorder.notifications if n.is_reply] == []


async def test_core_222_request_awaited_inside_handler(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = await two(make_engine)
    await obj(engine, "clock_1").ctx.send_notification("ASK_INSIDE")
    for _ in range(20):
        await clock.settle()
        if obj(engine, "asker_1").results:
            break
        await asyncio.sleep(0.01)
    assert obj(engine, "asker_1").results == [{"by": "clock_1"}]


async def test_core_222_reply_bypasses_inbox_and_subscribes(make_engine: EngineFactory) -> None:
    class Subscribed(Asker):
        name = "subscribed"
        subscribes = {"NOTHING"}  # noqa: RUF012

    engine = make_engine(Subscribed, Clock)
    await engine.start([InstanceSpec("s", "subscribed"), InstanceSpec("clock_1", "clock")])
    s = engine.supervisor.get("s")
    reply = await s.obj.ctx.request("TIME_NOW", target="clock_1")
    assert reply == {"by": "clock_1"}
    assert len(s.inbox) == 0
    assert s.obj.seen == []


async def test_core_222_late_reply_dropped(make_engine: EngineFactory, clock: VirtualClock) -> None:
    engine = await two(make_engine)
    req = Notification(
        id=uuid4(),
        trace=uuid4(),
        ts=clock.wall(),
        name="TIME_NOW",
        source="asker/asker_1",
        expects_reply=True,
    )
    obj(engine, "clock_1").ctx.send_reply(req, {"late": True})  # nobody waits: dropped
    assert engine.bus._pending == {}


# --- shared notification registry -----------------------------------------------


def _spec_registry() -> dict[str, str]:
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docs" / "spec" / "core.md").read_text()
    section = text[text.index("### 5.5") : text.index("## 6. Configuration")]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.replace("\\|", "/").strip("|").split("|")]
        if len(cells) == 4 and cells[0].startswith("`"):
            for name in re.findall(r"`([A-Z0-9_]+)`", cells[0]):
                rows[name] = cells[1]
    return rows


def test_core_224_registry_matches_spec_table() -> None:
    table = _spec_registry()
    assert set(REGISTRY) == set(table)
    for name, kind in table.items():
        assert REGISTRY[name].kind == kind.split(",")[0]
        assert REGISTRY[name].engine_only == ("engine only" in kind)
    assert {r.kind for r in REGISTRY.values()} == {"event", "command"}
    schema = json_schema("SHOW_ALERT")
    assert set(schema["required"]) == {"message", "level"}
    assert schema["properties"]["level"]["enum"] == ["info", "warning", "critical"]
    with pytest.raises(KeyError):
        json_schema("WEATHER_UPDATED")


@pytest.mark.parametrize(
    ("name", "payload", "ok"),
    [
        ("PRESENCE_DETECTED", {"sensor": "pir"}, True),
        ("PRESENCE_CLEARED", {"sensor": "pir", "zone": "hall"}, True),
        ("PRESENCE_DETECTED", {"zone": "hall"}, False),
        ("SHOW_ALERT", {"message": {"en": "Hi", "it": "Ciao"}, "level": "info"}, True),
        ("SHOW_ALERT", {"message": "Hi", "level": "loud"}, False),
        ("SHOW_ALERT", {"message": "Hi", "level": "info", "timeout_s": -1}, False),
        ("HIDE_ALERT", {"id": "a1"}, True),
        ("HIDE_ALERT", {}, False),
        ("PAGE_NEXT", {}, True),
        ("PAGE_SET", {"page": "home"}, True),
        ("PAGE_SET", {"page": 3}, False),
        ("WEATHER_UPDATED", {"anything": object()}, True),  # unregistered: not validated
    ],
)
def test_core_225_payload_validation(name: str, payload: dict[str, Any], ok: bool) -> None:
    if ok:
        validate_payload(name, payload)
    else:
        with pytest.raises(ValueError, match=f"payload of {name}"):
            validate_payload(name, payload)


async def test_core_225_module_sender_gets_value_error(make_engine: EngineFactory) -> None:
    engine = await two(make_engine)
    ctx = obj(engine, "asker_1").ctx
    with pytest.raises(ValueError, match="registered schema"):
        await ctx.send_notification("SHOW_ALERT", {"message": "Hi"})
    with pytest.raises(ValueError, match="registered schema"):
        await ctx.request("PAGE_SET", {})
    await ctx.send_notification("SHOW_ALERT", {"message": "Hi", "level": "warning"})


async def test_core_225_engine_emit_validated(make_engine: EngineFactory) -> None:
    engine = await two(make_engine)
    with pytest.raises(ValueError, match="CORE_PAGE_CHANGED"):
        engine.supervisor.emit("CORE_PAGE_CHANGED", {"page": "home"})
    engine.supervisor.emit(
        "CORE_PAGE_CHANGED",
        {
            "page": "home",
            "index": 0,
            "count": 1,
            "pages": [{"id": "home", "title": "Home"}],
            "reason": "rotation",
        },
    )


def test_core_226_optional_and_unknown_fields_keep_a_name() -> None:
    validate_payload("PRESENCE_DETECTED", {"sensor": "pir", "confidence": 0.9})
    validate_payload("SHOW_ALERT", {"message": "Hi", "level": "critical", "icon": "mdi-bell"})
