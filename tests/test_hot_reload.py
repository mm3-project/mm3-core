"""Milestone 2, part B: hot reload."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from conftest import EngineFactory, Listener, Recorder
from mm3.config import ModuleConfig
from mm3.context import Location
from mm3.core.config import load_config
from mm3.core.config.diff import changed_sections, plan_reload
from mm3.core.config.manager import ConfigManager
from mm3.core.engine import Engine
from mm3.core.supervisor import State, has_backend
from mm3.core.systemd import ServiceNotifier
from mm3.errors import ConfigurationError
from mm3.module import MirrorModule

EVENTS: list[tuple[str, str]] = []


class ProbeConfig(ModuleConfig):
    n: int = Field(default=0, title="N")
    live: bool = Field(default=False, title="LIVE")
    token: str = Field(default="", title="TOKEN")


class Probe(MirrorModule):
    name = "probe"
    config_model = ProbeConfig
    element = "mm3-probe"

    async def start(self) -> None:
        cfg = self.config
        assert isinstance(cfg, ProbeConfig)
        if cfg.n < 0:
            raise ConfigurationError("n", "N_NEGATIVE")
        EVENTS.append(("start", self.ctx.instance_id))

    async def stop(self) -> None:
        EVENTS.append(("stop", self.ctx.instance_id))

    async def reconfigure(self, new: ModuleConfig) -> bool:
        assert isinstance(new, ProbeConfig)
        EVENTS.append(("reconfigure", self.ctx.instance_id))
        return new.live


class Other(Probe):
    name = "other"


class Face(MirrorModule):
    name = "face"
    element = "mm3-face"


@pytest.fixture(autouse=True)
def _events() -> None:
    EVENTS.clear()


Harness = Callable[..., Any]


class Mirror:
    def __init__(self, engine: Engine, manager: ConfigManager, path: Path) -> None:
        self.engine = engine
        self.manager = manager
        self.path = path

    def write(self, text: str) -> None:
        self.path.write_text(text)

    async def reload(self, text: str | None = None) -> bool:
        if text is not None:
            self.write(text)
        EVENTS.clear()
        return await self.manager.reload("file")

    def state(self, iid: str) -> State:
        return self.engine.supervisor.get(iid).state

    def ids(self) -> list[str]:
        return [i.instance_id for i in self.engine.supervisor.instances()]


@pytest.fixture
async def mirror(make_engine: EngineFactory, tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    managers: list[ConfigManager] = []

    async def build(text: str, *, env: str = "", watch: bool = False, **kw: Any) -> Mirror:
        etc = tmp_path / "etc"
        etc.mkdir(exist_ok=True)
        path = etc / "config.yaml"
        path.write_text(text)
        env_file = etc / "mm3.env"
        env_file.write_text(env)
        env_file.chmod(0o600)
        engine = make_engine(Probe, Other, Face, **kw)
        result = load_config(path, registry=engine.registry, env_file=env_file, environ={})
        assert result.ok, result.records
        await engine.start(result.instance_specs())
        manager = ConfigManager(
            engine,
            result,
            registry=engine.registry,
            env_file=env_file,
            debounce=0.05,
            environ={},
        )
        await manager.start(watch=watch)
        managers.append(manager)
        return Mirror(engine, manager, path)

    yield build
    for m in managers:
        await m.close()


def names(recorder: Recorder, prefix: str = "CORE_") -> list[str]:
    return [n.name for n, _ in recorder.notifications if n.name.startswith(prefix)]


def last(recorder: Recorder, name: str) -> Any:
    return [n for n, _ in recorder.notifications if n.name == name][-1]


BASE = """
modules:
  - module: probe
    id: a
    position: top_left
    config: {n: 1}
  - module: probe
    id: b
    position: top_right
    config: {n: 1}
  - module: face
    position: top_bar
"""


# --- CORE-208: triggers ----------------------------------------------------------------


async def test_core_208_debounce_and_exactly_one_more(mirror: Harness) -> None:
    m = await mirror(BASE)
    calls: list[str] = []
    gate = asyncio.Event()

    async def fake(origin: str = "file") -> bool:
        calls.append(origin)
        await gate.wait()
        return True

    m.manager.reload = fake  # type: ignore[method-assign]
    for _ in range(5):
        m.manager.notify_change()
        await asyncio.sleep(0.01)
    assert calls == []
    await asyncio.sleep(0.1)
    assert calls == ["file"]
    for _ in range(3):
        m.manager.request_reload("sighup")
    gate.set()
    await m.manager.idle()
    assert calls == ["file", "sighup"]


async def test_core_208_watcher_reloads_on_file_include_and_env_changes(
    mirror: Harness, recorder: Recorder
) -> None:
    m = await mirror(BASE, watch=True)
    await asyncio.sleep(0.3)  # let the watcher start
    inc = m.path.parent / "probe.yaml"
    inc.write_text("{n: 2}\n")

    async def wait_version(v: int) -> None:
        for _ in range(200):
            if m.manager.version >= v:
                await m.manager.idle()
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"version {v} not reached: {m.manager.version}")

    m.write(
        BASE.replace(
            "config: {n: 1}\n  - module: probe", "config: !include probe.yaml\n  - module: probe", 1
        )
    )
    await wait_version(2)
    inc.write_text("{n: 3}\n")
    await wait_version(3)
    m.write(BASE.replace("{n: 1}", "{n: 1, token: !env [TOK, none]}", 1))
    await wait_version(4)
    (m.path.parent / "mm3.env").write_text("TOK=x\n")
    await wait_version(5)
    templates = m.path.parent / "widget-templates"
    assert templates in m.manager.watched_directories()
    assert names(recorder).count("CORE_CONFIG_CHANGED") == 4


async def test_core_208_sighup_reloads_now(mirror: Harness) -> None:
    m = await mirror(BASE)
    m.write(BASE.replace("{n: 1}", "{n: 5}", 1))
    m.manager.request_reload("sighup")
    await m.manager.idle()
    assert m.manager.version == 2


# --- CORE-209: invalid configuration ---------------------------------------------------


async def test_core_209_invalid_config_never_replaces_running(
    mirror: Harness, recorder: Recorder
) -> None:
    m = await mirror(BASE)
    bad = BASE.replace("{n: 1}", "{n: x}", 1) + "  - module: missing\n"
    assert await m.reload(bad) is False
    assert EVENTS == []
    assert m.manager.status() == {"version": 1, "disk_valid": False}
    assert m.state("a") is State.RUNNING
    event = last(recorder, "CORE_CONFIG_ERROR")
    assert event.payload["count"] == 2
    assert {e["code"] for e in event.payload["errors"]} == {
        "CORE_CFG_TYPE",
        "CORE_CFG_MODULE_MISSING",
    }
    assert set(event.payload["errors"][0]) == {"file", "line", "path", "code"}
    assert await m.reload(BASE.replace("{n: 1}", "{n: 2}", 1)) is True
    assert m.manager.status() == {"version": 2, "disk_valid": True}


async def test_core_290_reload_tells_systemd_applied_or_rejected(
    mirror: Harness, listener: Listener
) -> None:
    m = await mirror(BASE)
    m.engine.notifier = ServiceNotifier(str(listener.path))
    m.engine.notify_ready()
    listener.messages()
    assert await m.reload(BASE.replace("{n: 1}", "{n: 2}", 1)) is True
    [reloading, ready] = listener.messages()
    assert reloading[0] == "RELOADING=1"
    assert ready[0] == "READY=1"
    assert ready[1] == f"STATUS={m.engine.status_line()}"
    assert await m.reload(BASE + "  - module: missing\n") is False  # rejected
    assert [x[0] for x in listener.messages()] == ["RELOADING=1", "READY=1"]
    assert await m.reload(BASE.replace("{n: 1}", "{n: 2}", 1)) is False  # nothing to apply
    assert [x[0] for x in listener.messages()] == ["RELOADING=1", "READY=1"]


async def test_core_209_error_event_lists_at_most_20(mirror: Harness, recorder: Recorder) -> None:
    m = await mirror(BASE)
    bad = "modules:\n" + "".join(f"  - module: missing{i}\n" for i in range(25))
    await m.reload(bad)
    event = last(recorder, "CORE_CONFIG_ERROR")
    assert event.payload["count"] == 25
    assert len(event.payload["errors"]) == 20


# --- CORE-210: apply order -------------------------------------------------------------


async def test_core_210_only_affected_instances_change(mirror: Harness, recorder: Recorder) -> None:
    m = await mirror(BASE)
    new = """
modules:
  - module: other
    id: a
    position: top_left
    config: {n: 1}
  - module: probe
    id: b
    position: {col: 1, row: 5, width: 3, height: 2}
    config: {n: 1}
  - module: face
    position: top_bar
  - module: probe
    id: c
    position: bottom_bar
"""
    assert await m.reload(new) is True
    assert EVENTS == [("stop", "a"), ("start", "a"), ("start", "c")]
    assert m.ids() == ["b", "face", "a", "c"]
    changed = last(recorder, "CORE_CONFIG_CHANGED")
    assert changed.payload == {"version": 2, "sections": ["modules/a", "modules/b", "modules/c"]}
    assert names(recorder)[-1] == "CORE_CONFIG_CHANGED"


async def test_core_210_reconfigure_then_restart_when_false(mirror: Harness) -> None:
    m = await mirror(BASE)
    text = BASE.replace("{n: 1}", "{n: 2, live: true}", 1).replace("{n: 1}", "{n: 2}", 1)
    assert await m.reload(text)
    assert EVENTS == [
        ("reconfigure", "a"),
        ("reconfigure", "b"),
        ("stop", "b"),
        ("start", "b"),
    ]


async def test_core_210_removed_disabled_and_grants(mirror: Harness) -> None:
    m = await mirror(BASE)
    text = """
modules:
  - module: probe
    id: a
    position: top_left
    config: {n: 1}
    grants: {http: ["http://x/"]}
  - module: face
    position: top_bar
    disabled: true
"""
    assert await m.reload(text)
    assert sorted(EVENTS) == [("start", "a"), ("stop", "a"), ("stop", "b")]
    assert m.ids() == ["a"]
    assert await m.reload(text.replace("    disabled: true\n", ""))
    assert m.ids() == ["a", "face"]


async def test_core_210_disabled_instance_restarts_when_config_changes(mirror: Harness) -> None:
    m = await mirror(BASE.replace("{n: 1}", "{n: -1}", 1))
    assert m.state("a") is State.DISABLED
    assert await m.reload(BASE)
    assert m.state("a") is State.RUNNING
    assert ("start", "a") in EVENTS


async def test_core_210_layout_only_and_formatting_only(mirror: Harness) -> None:
    m = await mirror(BASE)
    calls: list[Any] = []

    async def listener(new: Any, plan: Any) -> None:
        calls.append(plan.relayout)

    m.manager.layout_listeners.append(listener)
    assert await m.reload(BASE.replace("position: top_right", "position: bottom_right"))
    assert EVENTS == []
    assert calls == [["b"]]
    assert (
        await m.reload(
            "# a comment\n" + BASE.replace("position: top_right", "position: bottom_right")
        )
        is False
    )
    assert m.manager.version == 2


# --- CORE-211, CORE-245: current values in ctx -----------------------------------------


async def test_core_211_locale_timezone_and_units(mirror: Harness, recorder: Recorder) -> None:
    m = await mirror(BASE)
    sup = m.engine.supervisor
    text = "engine:\n  locale: it\n  timezone: Asia/Tokyo\n" + BASE
    assert await m.reload(text)
    ctx = sup.get("a").context
    assert ctx is not None
    assert (ctx.locale, ctx.timezone.key) == ("it", "Asia/Tokyo")
    assert EVENTS == []
    assert await m.reload(
        "engine:\n  locale: it\n  timezone: Asia/Tokyo\n  units: imperial\n" + BASE
    )
    assert sorted(EVENTS) == [("start", "a"), ("start", "b"), ("stop", "a"), ("stop", "b")]
    assert sup.get("face").context is not None
    ctx = sup.get("a").context
    assert ctx is not None
    assert ctx.units == "imperial"


async def test_core_245_locale_changed_broadcast_and_targeted(
    mirror: Harness, recorder: Recorder
) -> None:
    m = await mirror(BASE)
    assert await m.reload("engine:\n  locale: de\n" + BASE)
    event = last(recorder, "CORE_LOCALE_CHANGED")
    assert (event.target, event.payload) == (None, {"locale": "de"})
    text = "engine:\n  locale: de\n" + BASE.replace("id: b\n", "id: b\n    locale: fr\n")
    assert await m.reload(text)
    event = last(recorder, "CORE_LOCALE_CHANGED")
    assert (event.target, event.payload) == ("b", {"locale": "fr"})
    ctx = m.engine.supervisor.get("b").context
    assert ctx is not None
    assert ctx.locale == "fr"
    assert EVENTS == []


async def test_core_211_restart_keys_and_live_keys(mirror: Harness) -> None:
    m = await mirror(BASE)
    text = "engine:\n  threads: 2\n  log_level: debug\n  timeouts: {handler: 0.3}\n" + BASE
    assert await m.reload(text)
    sup = m.engine.supervisor
    assert sup.settings.timeouts.handler == 0.3
    assert sup.settings.log_level == "debug"
    assert EVENTS == []


def test_core_211_has_backend() -> None:
    assert has_backend(Probe)
    assert not has_backend(Face)


# --- MOD-50 and sections ---------------------------------------------------------------


async def test_mod_50_live_reconfigure_keeps_the_incarnation(mirror: Harness) -> None:
    m = await mirror(BASE)
    inst = m.engine.supervisor.get("a")
    obj = inst.obj
    assert await m.reload(BASE.replace("{n: 1}", "{n: 9, live: true}", 1))
    assert inst.obj is obj
    assert isinstance(obj, Probe)
    assert isinstance(obj.config, ProbeConfig)
    assert obj.config.n == 9


async def test_core_216_env_values_change_no_section(mirror: Harness, tmp_path: Path) -> None:
    m = await mirror(BASE.replace("{n: 1}", "{n: 1, token: !env TOK}", 1), env="TOK=one\n")
    (m.path.parent / "mm3.env").write_text("TOK=two\n")
    old = m.manager.current
    new = load_config(
        m.path, registry=m.engine.registry, env_file=m.path.parent / "mm3.env", environ={}
    )
    assert changed_sections(old, new) == []
    plan = plan_reload(old, new)
    assert list(plan.reconfigure) == ["a"]
    assert await m.manager.reload()
    assert ("reconfigure", "a") in EVENTS


# --- custom.css and server.allow (CORE-208, CORE-216, CORE-201) -----------------------


async def test_core_208_custom_css_is_watched_and_reloads(mirror: Harness) -> None:
    m = await mirror(BASE, watch=True)
    css = m.path.parent / "custom.css"
    assert css in m.manager.watched_files()
    seen: list[Any] = []

    async def listener(new: Any, plan: Any) -> None:
        seen.append(plan)

    m.manager.layout_listeners.append(listener)
    await asyncio.sleep(0.3)  # let the watcher start
    css.write_text("body { color: red; }\n")
    for _ in range(200):
        if m.manager.version >= 2:
            break
        await asyncio.sleep(0.05)
    await m.manager.idle()
    assert m.manager.version == 2
    assert [p.sections for p in seen] == [["custom_css"]]
    assert seen[0].custom_css
    assert m.manager.current.assets.custom_css == b"body { color: red; }\n"


async def test_core_208_invalid_custom_css_keeps_the_running_config(mirror: Harness) -> None:
    m = await mirror(BASE)
    (m.path.parent / "custom.css").write_text("@import 'x';")
    assert not await m.reload()
    assert not m.manager.disk_valid
    assert m.manager.current.assets.custom_css is None


async def test_core_201_server_allow_is_live_host_needs_restart(mirror: Harness) -> None:
    from mm3.core.config.diff import plan_reload

    m = await mirror(BASE)
    old = m.manager.current
    m.write("engine:\n  server: {allow: [192.168.1.0/24]}\n" + BASE)
    new = load_config(m.path, registry=m.engine.registry, env_file=m.manager.env_file, environ={})
    plan = plan_reload(old, new)
    assert plan.engine_live == {"server.allow"}
    assert plan.engine_restart == set()
    m.write("engine:\n  server: {allow: [192.168.1.0/24], port: 9000}\n" + BASE)
    new2 = load_config(m.path, registry=m.engine.registry, env_file=m.manager.env_file, environ={})
    plan2 = plan_reload(old, new2)
    assert plan2.engine_live == {"server.allow"}
    assert plan2.engine_restart == {"server"}


# --- CORE-288, CORE-211: engine.location ---------------------------------------------

PLACE = "  location: {lat: 41.9, lon: 12.5, name: {en: Rome, it: Roma}}\n"


async def test_core_288_location_in_ctx_and_backend_instances_restart(mirror: Harness) -> None:
    m = await mirror(BASE)
    sup = m.engine.supervisor
    ctx = sup.get("a").context
    assert ctx is not None
    assert ctx.location is None  # not set: None, not an empty object
    assert await m.reload("engine:\n" + PLACE + BASE)
    assert sorted(EVENTS) == [("start", "a"), ("start", "b"), ("stop", "a"), ("stop", "b")]
    EVENTS.clear()
    ctx = sup.get("a").context
    assert ctx is not None
    assert ctx.location == Location(41.9, 12.5, "Rome")
    # the name follows the instance's locale chain
    assert await m.reload("engine:\n  locale: it\n" + PLACE + BASE)
    ctx = sup.get("a").context
    assert ctx is not None
    assert ctx.location == Location(41.9, 12.5, "Roma")
    EVENTS.clear()
    # any change of the location restarts; removing it clears the value
    assert await m.reload("engine:\n  locale: it\n" + PLACE.replace("41.9", "42") + BASE)
    assert ("start", "a") in EVENTS
    EVENTS.clear()
    assert await m.reload("engine:\n  locale: it\n" + BASE)
    assert ("start", "a") in EVENTS
    ctx = sup.get("a").context
    assert ctx is not None
    assert ctx.location is None


def test_core_288_location_without_a_name() -> None:
    from mm3.core.settings import EngineSettings

    settings = EngineSettings.model_validate({"location": {"lat": 0, "lon": 0}})
    assert settings.location is not None
    assert settings.location.name is None


async def test_core_288_harness_location() -> None:
    from mm3.testing import ModuleHarness

    seen: list[Location | None] = []

    class Where(MirrorModule):
        name = "where"

        async def start(self) -> None:
            seen.append(self.ctx.location)

    async with ModuleHarness(
        Where, location={"lat": 1.5, "lon": 2.5, "name": {"en": "Home", "it": "Casa"}}
    ):
        pass
    assert seen == [Location(1.5, 2.5, "Home")]
