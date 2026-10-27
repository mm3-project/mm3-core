"""Lifecycle, isolation, restarts and the context (CORE-04, CORE-190..198, MOD-10..22)."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from conftest import EngineFactory, Recorder
from mm3 import ClientMessage, ConfigurationError, MirrorModule, ModuleConfig, Notification
from mm3.core.engine import Engine
from mm3.core.logging import recent_records
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.core.supervisor import InstanceSpec, State
from mm3.errors import ContextClosedError, ReservedNameError, TransientError
from mm3.testing import VirtualClock


class Probe(MirrorModule):
    """Records notifications and client messages."""

    name = "probe"

    def __init__(self, ctx: Any, config: ModuleConfig) -> None:
        super().__init__(ctx, config)
        self.seen: list[Notification] = []
        self.messages: list[ClientMessage] = []
        self.traces: list[UUID] = []

    async def on_notification(self, n: Notification) -> None:
        self.seen.append(n)

    async def on_client_message(self, msg: ClientMessage) -> None:
        self.messages.append(msg)

    def names(self) -> list[str]:
        return [n.name for n in self.seen]


class Hooks(MirrorModule):
    """Records lifecycle hooks."""

    name = "hooks"
    calls: list[str] = []  # noqa: RUF012 - shared on purpose across incarnations

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def suspend(self) -> None:
        self.calls.append("suspend")

    async def resume(self) -> None:
        self.calls.append("resume")


def obj(engine: Engine, instance: str) -> Any:
    return engine.supervisor.get(instance).obj


def state(engine: Engine, instance: str) -> State:
    return engine.supervisor.get(instance).state


async def test_core_190_lifecycle_start_stop(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    Hooks.calls = []
    engine = make_engine(Hooks)
    await engine.start([InstanceSpec("hooks_1", "hooks")])
    assert state(engine, "hooks_1") is State.RUNNING
    await engine.supervisor.get("hooks_1").stop()
    assert state(engine, "hooks_1") is State.STOPPED
    await engine.supervisor.get("hooks_1").stop()  # idempotent from the engine side
    assert Hooks.calls == ["start", "stop"]


async def test_core_22_core_ready_and_shutdown_emitted(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p", "probe")])
    await asyncio.sleep(0)
    assert "CORE_READY" in recorder.names()
    await engine.stop()
    assert recorder.names()[-1] == "CORE_SHUTDOWN"


async def test_core_21_instances_exchange_and_sender_excluded(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p1", "probe"), InstanceSpec("p2", "probe")])
    await obj(engine, "p1").ctx.send_notification("HELLO", {"n": 1})
    await clock.settle()
    assert "HELLO" not in obj(engine, "p1").names()
    assert obj(engine, "p2").names()[-1] == "HELLO"
    assert obj(engine, "p2").seen[-1].source == "probe/p1"


async def test_core_22_reserved_and_bad_names(make_engine: EngineFactory) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p1", "probe")])
    ctx = obj(engine, "p1").ctx
    with pytest.raises(ReservedNameError):
        await ctx.send_notification("CORE_READY")
    with pytest.raises(ValueError, match="UPPER_SNAKE"):
        await ctx.send_notification("hello")
    with pytest.raises(ValueError, match="JSON"):
        await ctx.send_notification("HELLO", {"x": object()})


async def test_core_111_handler_inherits_trace_new_send_starts_one(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    class Relay(MirrorModule):
        name = "relay"

        async def on_notification(self, n: Notification) -> None:
            if n.name == "START":
                await self.ctx.send_notification("RELAYED")
                self.ctx.log.info("relayed")

    engine = make_engine(Probe, Relay)
    await engine.start([InstanceSpec("p", "probe"), InstanceSpec("r", "relay")])
    first = await obj(engine, "p").ctx.send_notification("START")
    await clock.settle()
    relayed = next(n for n in obj(engine, "p").seen if n.name == "RELAYED")
    assert relayed.trace == first
    assert relayed.id != first
    record = next(r for r in recent_records() if r["event"] == "relayed")
    assert record["trace_id"] == str(first)
    assert record["module"] == "relay"
    assert record["instance"] == "r"


async def test_core_29_targeted_and_client_messages(
    make_engine: EngineFactory, clock: VirtualClock, recorder: Recorder
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p1", "probe"), InstanceSpec("p2", "probe")])
    await obj(engine, "p1").ctx.send_notification("PRIVATE", target="p2")
    await obj(engine, "p1").ctx.send_notification("PRIVATE", target="nobody")
    msg = ClientMessage(type="tap", client="kiosk", trace=UUID(int=7))
    assert engine.supervisor.deliver_client_message("p1", msg)
    await clock.settle()
    assert [x for x in obj(engine, "p2").names() if x != "CORE_READY"] == ["PRIVATE"]
    assert obj(engine, "p1").messages == [msg]


async def test_mod_21_state_and_client_channel(
    make_engine: EngineFactory, recorder: Recorder
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p1", "probe")])
    ctx = obj(engine, "p1").ctx
    ctx.set_state({"a": 1, "b": 2})
    ctx.patch_state({"b": 3})
    await ctx.send_to_client("tick", {"n": 1}, client="k")
    assert dict(ctx.state) == {"a": 1, "b": 3}
    assert recorder.state == [("p1", {"a": 1, "b": 2}, False), ("p1", {"b": 3}, True)]
    assert recorder.client == [("p1", "tick", {"n": 1}, "k")]
    with pytest.raises(TypeError):
        ctx.state["a"] = 2
    with pytest.raises(ValueError, match="limit"):
        ctx.set_state({"big": "x" * 300_000})


class Crashy(MirrorModule):
    name = "crashy"

    async def on_notification(self, n: Notification) -> None:
        if n.name == "CRASH":
            raise RuntimeError("boom")


async def test_core_04_crash_isolated_and_restarted_with_backoff(
    make_engine: EngineFactory, clock: VirtualClock, recorder: Recorder
) -> None:
    engine = make_engine(Probe, Crashy)
    await engine.start([InstanceSpec("p", "probe"), InstanceSpec("c", "crashy")])
    first = obj(engine, "c")
    await obj(engine, "p").ctx.send_notification("CRASH")
    await clock.settle()
    assert state(engine, "c") is State.FAILED
    assert state(engine, "p") is State.RUNNING
    failed = [n for n, _ in recorder.notifications if n.name == "CORE_MODULE_FAILED"]
    assert failed[-1].payload == {
        "instance": "c",
        "module": "crashy",
        "state": "FAILED",
        "reason": "task_failed",
    }
    health = engine.supervisor.get("c").health()
    assert "RuntimeError: boom" in health["last_error"]["message"]
    await clock.advance(0.9)
    assert state(engine, "c") is State.FAILED
    await clock.advance(0.2)
    assert state(engine, "c") is State.RUNNING
    assert obj(engine, "c") is not first  # CORE-191: new object
    assert engine.supervisor.get("c").restarts == 1


async def test_core_194_backoff_sequence_and_disable_on_fifth(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe, Crashy)
    await engine.start([InstanceSpec("p", "probe"), InstanceSpec("c", "crashy")])
    for delay in (1, 2, 4, 8):
        await obj(engine, "p").ctx.send_notification("CRASH")
        await clock.settle()
        assert state(engine, "c") is State.FAILED
        await clock.advance(delay - 0.1)
        assert state(engine, "c") is State.FAILED
        await clock.advance(0.2)
        assert state(engine, "c") is State.RUNNING
    await obj(engine, "p").ctx.send_notification("CRASH")
    await clock.settle()
    assert state(engine, "c") is State.DISABLED
    await clock.advance(60)
    assert state(engine, "c") is State.DISABLED  # CORE-195: stays until config change / restart
    await engine.supervisor.restart("c")
    assert state(engine, "c") is State.RUNNING


async def test_core_194_window_slides(make_engine: EngineFactory, clock: VirtualClock) -> None:
    engine = make_engine(Probe, Crashy)
    await engine.start([InstanceSpec("p", "probe"), InstanceSpec("c", "crashy")])
    await obj(engine, "p").ctx.send_notification("CRASH")
    await clock.settle()
    await clock.advance(1.1)
    await clock.advance(11 * 60)  # the window empties
    await obj(engine, "p").ctx.send_notification("CRASH")
    await clock.settle()
    await clock.advance(1.1)  # first delay again, not 2 s
    assert state(engine, "c") is State.RUNNING
    assert engine.supervisor.get("c").health()["failures_in_window"] == 1


async def test_core_192_init_failure_and_config_errors_disable(make_engine: EngineFactory) -> None:
    class BadInit(MirrorModule):
        name = "badinit"

        def __init__(self, ctx: Any, config: ModuleConfig) -> None:
            raise RuntimeError("no")

    class BadConfig(MirrorModule):
        name = "badconfig"

        async def start(self) -> None:
            raise ConfigurationError("api_key", "API_KEY_REFUSED")

    class Cfg(ModuleConfig):
        n: int = 1

    class Typed(MirrorModule):
        name = "typed"
        config_model = Cfg

    engine = make_engine(BadInit, BadConfig, Typed)
    await engine.start(
        [
            InstanceSpec("i", "badinit"),
            InstanceSpec("c", "badconfig"),
            InstanceSpec("t", "typed", {"n": "x"}),
            InstanceSpec("u", "unknown"),
        ]
    )
    assert [state(engine, x) for x in "ictu"] == [State.DISABLED] * 4
    assert engine.supervisor.get("c").reason == "configuration_error"
    assert engine.supervisor.get("t").reason == "config_invalid"
    await engine.supervisor.get("t").reconfigure({"n": 2})  # CORE-195: config change
    assert state(engine, "t") is State.RUNNING
    assert obj(engine, "t").config.n == 2


async def test_core_192_transient_error_keeps_running(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    class Flaky(MirrorModule):
        name = "flaky"

        async def start(self) -> None:
            raise TransientError("NETWORK_DOWN")

        async def on_notification(self, n: Notification) -> None:
            if n.name == "FLAKE":
                raise TransientError("NETWORK_DOWN")

    engine = make_engine(Probe, Flaky)
    await engine.start([InstanceSpec("p", "probe"), InstanceSpec("f", "flaky")])
    assert state(engine, "f") is State.RUNNING
    await obj(engine, "p").ctx.send_notification("FLAKE")
    await clock.settle()
    assert state(engine, "f") is State.RUNNING
    assert engine.supervisor.get("f").health()["transient_errors"] == 2


async def test_core_193_handler_timeouts_three_in_a_row_fail(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    class Slow(MirrorModule):
        name = "slow"

        async def on_notification(self, n: Notification) -> None:
            if n.name == "SLOW":
                await asyncio.sleep(1)

    engine = make_engine(Probe, Slow)
    await engine.start([InstanceSpec("p", "probe"), InstanceSpec("s", "slow")])
    ctx = obj(engine, "p").ctx
    for _ in range(2):
        await ctx.send_notification("SLOW")
    await ctx.send_notification("FAST")
    await ctx.send_notification("SLOW")
    await asyncio.sleep(1.0)
    assert state(engine, "s") is State.RUNNING  # the fast one reset the count
    assert engine.supervisor.get("s").health()["handler_timeouts"] == 3
    await ctx.send_notification("SLOW")
    await ctx.send_notification("SLOW")
    await asyncio.sleep(0.6)
    await clock.settle()
    assert state(engine, "s") is State.FAILED
    assert engine.supervisor.get("s").reason == "task_failed"


async def test_core_192_start_timeout_fails(make_engine: EngineFactory) -> None:
    class Hang(MirrorModule):
        name = "hang"

        async def start(self) -> None:
            await asyncio.sleep(10)

    engine = make_engine(Hang)
    await engine.start([InstanceSpec("h", "hang")])
    assert state(engine, "h") is State.FAILED
    assert engine.supervisor.get("h").reason == "start_timeout"


async def test_core_185_job_errors_and_timeouts(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    class Jobs(MirrorModule):
        name = "jobs"
        runs: list[str] = []  # noqa: RUF012

        async def start(self) -> None:
            self.ctx.scheduler.every(10, self.slow, name="slow")
            self.ctx.scheduler.every(25, self.bad, name="bad")

        async def slow(self) -> None:
            self.runs.append("slow")
            await asyncio.sleep(5)

        async def bad(self) -> None:
            raise RuntimeError("job broke")

    engine = make_engine(Jobs)
    await engine.start([InstanceSpec("j", "jobs")])
    await clock.advance(10)
    await asyncio.sleep(0.4)  # job budget 0.3 s
    await clock.settle()
    assert state(engine, "j") is State.RUNNING  # CORE-193: job timeout is a warning
    assert engine.supervisor.get("j").health()["job_timeouts"] == 1
    await clock.advance(15)
    assert state(engine, "j") is State.FAILED  # CORE-185: job exception fails


async def test_core_191_old_context_closed_after_restart(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p", "probe")])
    old = obj(engine, "p")
    await engine.supervisor.restart("p")
    assert obj(engine, "p") is not old
    with pytest.raises(ContextClosedError):
        await old.ctx.send_notification("LATE")
    with pytest.raises(ContextClosedError):
        _ = old.ctx.instance_id

    async def work() -> None:
        await asyncio.sleep(0)

    coro = work()
    with pytest.raises(ContextClosedError):
        old.ctx.create_task(coro, name="late")


async def test_mod_22_tasks_and_jobs_cancelled_on_stop(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    class Busy(MirrorModule):
        name = "busy"
        cancelled: list[str] = []  # noqa: RUF012

        async def start(self) -> None:
            self.task = self.ctx.create_task(self.forever(), name="forever")
            self.job = self.ctx.scheduler.every(5, self.tick)

        async def forever(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append("task")
                raise

        async def tick(self) -> None:
            pass

    engine = make_engine(Busy)
    await engine.start([InstanceSpec("b", "busy")])
    busy = obj(engine, "b")
    await engine.supervisor.get("b").stop()
    assert Busy.cancelled == ["task"]
    assert busy.task.cancelled()
    assert busy.job.next_run is None


async def test_core_196_suspend_resume_and_delivery(
    make_engine: EngineFactory, clock: VirtualClock
) -> None:
    Hooks.calls = []
    engine = make_engine(Hooks, Probe)
    await engine.start(
        [InstanceSpec("h", "hooks"), InstanceSpec("p", "probe"), InstanceSpec("q", "probe")]
    )
    await engine.supervisor.set_suspended(True)
    assert state(engine, "h") is State.SUSPENDED
    await obj(engine, "q").ctx.send_notification("WHILE_SUSPENDED")
    await clock.settle()
    assert "WHILE_SUSPENDED" in obj(engine, "p").names()
    assert "CORE_SUSPEND" in obj(engine, "p").names()
    await engine.supervisor.set_suspended(False)
    assert state(engine, "h") is State.RUNNING
    assert Hooks.calls == ["start", "suspend", "resume"]


async def test_mod_50_reconfigure_live_or_restart(make_engine: EngineFactory) -> None:
    class Cfg(ModuleConfig):
        live: bool = True
        value: int = 0

    class Conf(MirrorModule):
        name = "conf"
        config_model = Cfg

        async def reconfigure(self, new: ModuleConfig) -> bool:
            assert isinstance(new, Cfg)
            return new.live

    engine = make_engine(Conf)
    await engine.start([InstanceSpec("c", "conf")])
    first = obj(engine, "c")
    await engine.supervisor.get("c").reconfigure({"value": 1})
    assert obj(engine, "c") is first
    assert first.config.value == 1
    await engine.supervisor.get("c").reconfigure({"live": False, "value": 2})
    assert obj(engine, "c") is not first
    assert obj(engine, "c").config.value == 2
    assert state(engine, "c") is State.RUNNING


async def test_core_198_storage_and_run_blocking(
    make_engine: EngineFactory, tmp_path: Path
) -> None:
    engine = make_engine(Probe)
    await engine.start([InstanceSpec("p", "probe")])
    ctx = obj(engine, "p").ctx
    assert ctx.storage.path == tmp_path / "state" / "modules" / "p"
    assert ctx.storage.path.is_dir()
    assert await ctx.storage.kv_get("missing", 5) == 5
    await ctx.storage.kv_set("count", {"n": 1})
    await ctx.storage.kv_delete("nothing")
    assert await ctx.run_blocking(sum, [1, 2, 3]) == 6
    await engine.supervisor.restart("p")
    assert await obj(engine, "p").ctx.storage.kv_get("count") == {"n": 1}  # MOD-19
    await obj(engine, "p").ctx.storage.kv_delete("count")
    assert await obj(engine, "p").ctx.storage.kv_get("count") is None
    with pytest.raises(ValueError, match="1048576"):
        await obj(engine, "p").ctx.storage.kv_set("big", "x" * (1024 * 1024))
    with pytest.raises(ValueError, match="JSON"):
        await obj(engine, "p").ctx.storage.kv_set("bad", object())
    await engine.supervisor.remove("p")
    assert (tmp_path / "state" / "kv" / "p.json").exists()  # kept after removal


# --- milestone 1 done criterion --------------------------------------------------


@pytest.fixture
def mirror_pkg(tmp_path: Path) -> Iterator[None]:
    """A module distribution installed on sys.path, discovered via entry points."""
    root = tmp_path / "site"
    pkg = root / "mm3_mirrortest"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            """
            from mm3 import MirrorModule, Notification

            class Echo(MirrorModule):
                name = "echo"

                async def on_notification(self, n: Notification) -> None:
                    if n.name == "PING":
                        await self.ctx.send_notification(
                            "PONG", {"by": id(self)}, target=n.source.split("/")[1]
                        )
                    elif n.name == "ECHO_CRASH":
                        raise RuntimeError("echo crashed")

            class Crashy(MirrorModule):
                name = "crashy"

                async def on_notification(self, n: Notification) -> None:
                    if n.name == "CRASH":
                        raise RuntimeError("crashy crashed")
            """
        )
    )
    dist = root / "mm3_mirrortest-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: mm3-mirrortest\nVersion: 0.1.0\n")
    (dist / "entry_points.txt").write_text(
        "[mm3.modules]\necho = mm3_mirrortest:Echo\ncrashy = mm3_mirrortest:Crashy\n"
    )
    sys.path.insert(0, str(root))
    yield
    sys.path.remove(str(root))
    sys.modules.pop("mm3_mirrortest", None)


async def test_milestone_1_done_criterion(
    mirror_pkg: None, clock: VirtualClock, recorder: Recorder, tmp_path: Path
) -> None:
    registry = Registry()
    registry.discover()
    registry.add(Probe)
    engine = Engine(
        EngineSettings(timeouts=Timeouts(start=1, stop=1, handler=0.5)),
        registry=registry,
        clock=clock,
        clients=recorder,
        state_dir=tmp_path / "state",
    )
    try:
        await engine.start(
            [
                InstanceSpec("echo_1", "echo"),
                InstanceSpec("crashy_1", "crashy"),
                InstanceSpec("probe", "probe"),
            ]
        )
        probe = obj(engine, "probe")

        async def ping() -> int:
            before = len(probe.seen)
            await probe.ctx.send_notification("PING")
            await clock.settle()
            pongs = [n for n in probe.seen[before:] if n.name == "PONG"]
            assert len(pongs) == 1
            assert pongs[0].source == "echo/echo_1"
            return int(pongs[0].payload["by"])

        # Starts and exchanges notifications.
        first_echo = await ping()

        # Survives a crash of another module.
        await probe.ctx.send_notification("CRASH")
        await clock.settle()
        assert state(engine, "crashy_1") is State.FAILED
        assert state(engine, "echo_1") is State.RUNNING
        assert await ping() == first_echo
        await clock.advance(1.1)
        assert state(engine, "crashy_1") is State.RUNNING

        # Is restarted after its own crash, as a new instance.
        await probe.ctx.send_notification("ECHO_CRASH")
        await clock.settle()
        assert state(engine, "echo_1") is State.FAILED
        await clock.advance(1.1)
        assert state(engine, "echo_1") is State.RUNNING
        assert await ping() != first_echo
    finally:
        await engine.stop()
