"""Remaining milestone 1 requirements: components, contract rules, health, API rules."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, Field, SecretStr

from conftest import EngineFactory, Recorder
from mm3 import ConfigurationError, MirrorModule, ModuleConfig, Notification
from mm3.context import ModuleContext
from mm3.core.bus import Bus
from mm3.core.logging import recent_records
from mm3.core.publicconfig import public_config
from mm3.core.registry import Registry
from mm3.core.supervisor import InstanceSpec, State, Supervisor
from mm3.testing import VirtualClock


class Probe(MirrorModule):
    name = "probe"

    def __init__(self, ctx: Any, config: ModuleConfig) -> None:
        super().__init__(ctx, config)
        self.seen: list[str] = []
        self.active = 0
        self.max_active = 0

    async def on_notification(self, n: Notification) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.seen.append(n.name)
        self.active -= 1


class BackendOnly(MirrorModule):
    """A backend-only module: no element."""

    name = "presence_like"

    async def start(self) -> None:
        self.ctx.set_state({"present": False})


class FrontendOnly(MirrorModule):
    """A front-end-only module ships the class with no hooks (Contract §2)."""

    name = "clock_like"
    element = "mm3-clock-like"
    scripts = ["clock.js"]  # noqa: RUF012


class Refused(MirrorModule):
    name = "refused"

    async def start(self) -> None:
        raise ConfigurationError("api_key", "API_KEY_REFUSED")


def obj(engine: Any, instance: str) -> Any:
    return engine.supervisor.get(instance).obj


# --- engine components (core §3) ------------------------------------------------


async def test_core_01_engine_wires_subsystems(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = make_engine(Probe)
    assert isinstance(engine.registry, Registry)
    assert isinstance(engine.bus, Bus)
    assert isinstance(engine.supervisor, Supervisor)
    assert engine.supervisor.bus is engine.bus
    assert engine.supervisor.registry is engine.registry
    await engine.start([InstanceSpec("p", "probe")])
    assert engine.running
    await engine.stop()
    assert not engine.running
    assert recorder.names()[0] == "CORE_READY"
    assert recorder.names()[-1] == "CORE_SHUTDOWN"


async def test_core_05_bus_routes_to_backends_browsers_and_bridges(
    make_engine: EngineFactory, recorder: Recorder, clock: VirtualClock
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("a", "probe"), InstanceSpec("b", "probe")])
    await obj(engine, "a").ctx.send_notification("HELLO")
    await clock.settle()
    assert obj(engine, "b").seen[-1] == "HELLO"  # another backend
    assert ("HELLO", "a") in [(n.name, ex) for n, ex in recorder.notifications]  # endpoints


async def test_core_08_builtin_services_on_the_context(make_engine: EngineFactory) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p", "probe")])
    ctx = obj(engine, "p").ctx
    job = ctx.scheduler.every(timedelta(minutes=5), _noop, name="tick")
    assert job is not None
    await ctx.storage.kv_set("k", 1)
    assert await ctx.storage.kv_get("k") == 1
    ctx.log.info("service_check")
    assert any(r.get("event") == "service_check" for r in recent_records())


async def _noop() -> None:
    return None


# --- contract rules (core §4.4) -------------------------------------------------


async def test_core_10_backend_only_and_frontend_only_modules_run(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = make_engine(BackendOnly, FrontendOnly)
    await engine.start([InstanceSpec("pr", "presence_like"), InstanceSpec("cl", "clock_like")])
    assert engine.supervisor.get("pr").state is State.RUNNING
    assert engine.supervisor.get("cl").state is State.RUNNING
    assert BackendOnly.element is None
    assert ("pr", {"present": False}, False) in recorder.state
    assert obj(engine, "cl").snapshot() == {}
    assert await obj(engine, "cl").on_request(None) is None


def test_core_13_public_config_strips_secrets() -> None:
    class Account(BaseModel):
        user: str
        password: SecretStr

    class Cfg(ModuleConfig):
        api_key: SecretStr = Field(json_schema_extra={"secret": True})
        token: str = Field("t0k", json_schema_extra={"secret": True})
        lat: float = 41.9
        every: timedelta = timedelta(minutes=10)
        accounts: list[Account] = []  # noqa: RUF012
        extra: dict[str, str | SecretStr] = {}  # noqa: RUF012

    cfg = Cfg(
        api_key=SecretStr("k"),
        accounts=[Account(user="n", password=SecretStr("p"))],
        extra={"a": "x", "b": SecretStr("y")},
    )
    out = public_config(cfg)
    assert out == {
        "lat": 41.9,
        "every": "PT10M",
        "accounts": [{"user": "n"}],
        "extra": {"a": "x"},
    }
    assert "k" not in str(out)
    assert "p" not in str(out["accounts"])


# --- routing (core §5.3) --------------------------------------------------------


async def test_core_24_at_most_once_per_receiver(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("a", "probe"), InstanceSpec("b", "probe")])
    await obj(engine, "a").ctx.send_notification("ONCE")
    await clock.settle()
    assert obj(engine, "b").seen.count("ONCE") == 1


# --- lifecycle (core §7) --------------------------------------------------------


async def test_core_195_disabled_leaves_only_on_config_change_or_manual_restart(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Refused)
    await engine.start([InstanceSpec("r", "refused")])
    inst = engine.supervisor.get("r")
    assert inst.state is State.DISABLED
    await clock.advance(700)
    assert inst.state is State.DISABLED  # no automatic restart
    restarts = inst.restarts
    await engine.supervisor.restart("r")  # manual restart tries again
    assert inst.restarts == restarts + 1
    assert inst.state is State.DISABLED  # still refused by the module
    await inst.reconfigure({})  # a configuration change also tries again
    assert inst.state is State.DISABLED


async def test_core_197_shared_pool_and_discard_on_cancel(make_engine: EngineFactory) -> None:
    engine = make_engine(Probe, threads=2)
    await engine.start([InstanceSpec("a", "probe"), InstanceSpec("b", "probe")])
    assert engine.executor._max_workers == 2
    names: set[str] = set()

    def who() -> str:
        names.add(threading.current_thread().name)
        return "ok"

    assert await obj(engine, "a").ctx.run_blocking(who) == "ok"
    assert await obj(engine, "b").ctx.run_blocking(who) == "ok"
    assert all(n.startswith("mm3-blocking") for n in names)
    release = threading.Event()
    task = asyncio.create_task(obj(engine, "a").ctx.run_blocking(release.wait, 5))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()


# --- health (core §10.5) ---------------------------------------------------------


async def test_core_113_health_record(make_engine: EngineFactory, clock: VirtualClock) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("a", "probe"), InstanceSpec("b", "probe")])
    await obj(engine, "a").ctx.send_notification("HELLO")
    await clock.settle()
    a, b = engine.health()
    for key in (
        "state",
        "started_at",
        "restarts",
        "last_error",
        "handler_calls",
        "handler_p95_s",
        "handler_timeouts",
        "queue_depth",
        "last_in",
        "last_out",
    ):
        assert key in a
    assert a["state"] == "RUNNING"
    assert a["last_out"] is not None
    assert b["handler_calls"] >= 1
    assert b["last_in"] is not None
    assert b["last_out"] is None


# --- module contract ---------------------------------------------------------------


async def test_mod_18_handlers_one_at_a_time_in_order(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("a", "probe"), InstanceSpec("b", "probe")])
    for i in range(5):
        await obj(engine, "a").ctx.send_notification(f"N{i}")
    await clock.settle()
    assert obj(engine, "b").seen[-5:] == [f"N{i}" for i in range(5)]
    assert obj(engine, "b").max_active == 1


async def test_mod_19_restart_new_object_storage_survives(make_engine: EngineFactory) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("a", "probe")])
    old = obj(engine, "a")
    await old.ctx.storage.kv_set("kept", True)
    await engine.supervisor.restart("a")
    new = obj(engine, "a")
    assert new is not old
    assert new.ctx is not old.ctx
    assert await new.ctx.storage.kv_get("kept") is True


def test_mod_20_context_members_of_api_1_0() -> None:
    members = {name for name, _ in inspect.getmembers(ModuleContext) if not name.startswith("_")}
    assert {
        "instance_id",
        "module_name",
        "locale",
        "timezone",
        "units",
        "state",
        "scheduler",
        "storage",
        "log",
        "send_notification",
        "send_to_client",
        "request",
        "set_state",
        "patch_state",
        "create_task",
        "run_blocking",
    } <= members
    sig = inspect.signature(ModuleContext.request)
    assert list(sig.parameters) == ["self", "name", "payload", "target", "timeout"]
    assert sig.parameters["timeout"].default == 5.0
    assert sig.parameters["target"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_mod_90_api_mismatch_refused_with_reason(make_engine: EngineFactory) -> None:
    class Future(MirrorModule):
        name = "future"
        api_version = "1.9"

    class Major(MirrorModule):
        name = "major"
        api_version = "2.0"

    engine = make_engine(Future, Major)
    await engine.start([InstanceSpec("f", "future"), InstanceSpec("m", "major")])
    for inst, word in (("f", "newer"), ("m", "major")):
        managed = engine.supervisor.get(inst)
        assert managed.state is State.DISABLED
        assert word in (managed.reason or "")


async def test_mod_91_deprecated_member_logged_once_per_instance(
    make_engine: EngineFactory,
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("a", "probe")])
    ctx = obj(engine, "a").ctx
    for _ in range(3):
        ctx.deprecated("ctx.old()", "ctx.new()")
    events = [
        r
        for r in recent_records()
        if r.get("event") == "deprecated_api" and r.get("instance") == "a"
    ]
    assert len(events) == 1
    assert events[0]["replacement"] == "ctx.new()"


def test_mod_94_module_api_exposes_mm3_and_pydantic_types_only() -> None:
    third_party = {
        "httpx",
        "structlog",
        "fastapi",
        "starlette",
        "uvicorn",
        "aiomqtt",
        "babel",
        "ruamel",
        "watchfiles",
        "jinja2",
    }
    public = [
        "mm3",
        "mm3.config",
        "mm3.context",
        "mm3.errors",
        "mm3.messages",
        "mm3.notifications",
        "mm3.scheduler",
    ]
    for module_name in public:
        module = importlib.import_module(module_name)
        for name in module.__all__:
            value = getattr(module, name)
            if not (inspect.isclass(value) or inspect.isfunction(value)):
                continue
            origin = value.__module__.split(".")[0]
            assert origin not in third_party, f"{module_name}.{name} comes from {origin}"
            hints = getattr(value, "__annotations__", {})
            for hint in hints.values():
                assert not any(lib in str(hint) for lib in third_party), (
                    f"{module_name}.{name} exposes {hint}"
                )
