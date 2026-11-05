"""Milestone 6, part D3: module routes on ``ctx.router`` (CORE-332, MOD-74)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException, Request

from mm3.core.config import load_config
from mm3.core.engine import Engine
from mm3.core.logging import configure_logging, recent_records
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, Timeouts
from mm3.core.supervisor import State
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect
from mm3.core.web.auth import WebAuth
from mm3.module import MirrorModule
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"
SLOW_ENTERED: list[asyncio.Event] = []


class Routed(MirrorModule):
    """Registers routes in start()."""

    name = "routed"
    capabilities = frozenset({"api.routes"})

    async def start(self) -> None:
        r = self.ctx.router
        iid = self.ctx.instance_id

        @r.get("/ping")
        async def ping() -> dict[str, Any]:
            return {"pong": iid}

        @r.post("/echo")
        async def echo(request: Request) -> dict[str, Any]:
            return {"got": await request.json()}

        @r.get("/teapot")
        async def teapot() -> None:
            raise HTTPException(status_code=418, detail="short and stout")

        @r.get("/slow")
        async def slow() -> None:
            SLOW_ENTERED[-1].set()
            await asyncio.Event().wait()

        @r.get("/boom")
        async def boom() -> None:
            raise RuntimeError("module bug")

        @r.post("/health")
        async def not_mine() -> dict[str, str]:
            return {"hijacked": "yes"}


class Plain(MirrorModule):
    """No api.routes: ctx.router is refused."""

    name = "plain"

    async def start(self) -> None:
        try:
            _ = self.ctx.router
        except PermissionError:
            self.ctx.set_state({"refused": True})


CONFIG = """
modules:
  - module: routed
    id: r
  - module: plain
    id: p
"""


@dataclass
class World:
    client: httpx.AsyncClient
    engine: Engine
    clock: VirtualClock
    token: str

    async def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {"authorization": f"Bearer {self.token}"}
        return await self.client.request(method, path, headers=headers, **kw)


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    configure_logging("info", stream=io.StringIO(), json=True)
    reg = Registry()
    reg.add(Routed)
    reg.add(Plain)
    (tmp_path / "config.yaml").write_text(CONFIG)
    result = load_config(tmp_path / "config.yaml", registry=reg, env_file=tmp_path / "x.env")
    assert result.ok, result.records
    clock = VirtualClock()
    settings = EngineSettings(
        timeouts=Timeouts(start=1, stop=1, suspend=1, reconfigure=1, handler=2, job=1)
    )
    engine = Engine(settings, registry=reg, clock=clock, state_dir=tmp_path / "state")
    auth = WebAuth.open(tmp_path / "web")
    app = protect(admin_app(auth, api=ApiServices(engine)), lambda: [], "admin")
    await engine.start(result.instance_specs())
    token = auth.tokens.new("t")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5))
    async with httpx.AsyncClient(transport=transport, base_url=ADMIN) as client:
        try:
            yield World(client, engine, clock, token)
        finally:
            await engine.stop()


async def test_core_332_routes_answer_in_their_own_format(world: World) -> None:
    r = await world.call("GET", "/api/v1/modules/r/ping")
    assert (r.status_code, r.json()) == (200, {"pong": "r"})
    r = await world.call("POST", "/api/v1/modules/r/echo", json={"a": 1})
    assert r.json() == {"got": {"a": 1}}
    r = await world.call("GET", "/api/v1/modules/r/teapot")
    assert (r.status_code, r.json()) == (418, {"detail": "short and stout"})
    r = await world.call("GET", "/api/v1/modules/r/nothing")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}  # the module's own 404


async def test_core_332_routes_need_authentication(world: World) -> None:
    r = await world.client.get("/api/v1/modules/r/ping")
    assert r.status_code == 401
    assert r.json()["code"] == "CORE_API_UNAUTHORIZED"


async def test_core_332_without_the_capability_there_are_no_routes(world: World) -> None:
    obj = world.engine.supervisor.get("p").obj
    assert obj is not None
    assert obj.ctx._state == {"refused": True}  # type: ignore[attr-defined]
    r = await world.call("GET", "/api/v1/modules/p/ping")
    assert r.json()["code"] == "CORE_API_NOT_FOUND"
    r = await world.call("GET", "/api/v1/modules/nobody/ping")
    assert (r.status_code, r.json()["code"]) == (404, "CORE_API_NOT_FOUND")


async def test_core_332_engine_sub_paths_win(world: World) -> None:
    r = await world.call("GET", "/api/v1/modules/r/health")
    assert r.status_code == 200
    assert "state" in r.json()  # the engine's health record
    r = await world.call("POST", "/api/v1/modules/r/health")
    assert r.json()["code"] == "CORE_API_NOT_FOUND"
    await world.call("GET", "/api/v1/modules/r/ping")
    assert any(rec.get("event") == "route_reserved_path" for rec in recent_records())


async def test_core_332_stopped_instance_is_503(world: World) -> None:
    r = await world.call("POST", "/api/v1/modules/r/stop")
    assert r.status_code == 200
    assert world.engine.supervisor.get("r").state is State.STOPPED
    r = await world.call("GET", "/api/v1/modules/r/ping")
    assert (r.status_code, r.json()["code"]) == (503, "CORE_API_UNAVAILABLE")
    await world.call("POST", "/api/v1/modules/r/start")
    r = await world.call("GET", "/api/v1/modules/r/ping")
    assert r.json() == {"pong": "r"}  # a new incarnation registered its routes again


async def slow_call(world: World) -> httpx.Response:
    """Call /slow and move the engine clock past the handler budget once it waits."""
    SLOW_ENTERED.append(asyncio.Event())
    task = asyncio.create_task(world.call("GET", "/api/v1/modules/r/slow"))
    await asyncio.wait_for(SLOW_ENTERED[-1].wait(), 5)
    await world.clock.advance(3)  # handler budget 2 s on the engine clock
    return await asyncio.wait_for(task, 5)


async def test_core_332_handler_over_budget_is_504_and_counted(world: World) -> None:
    r = await slow_call(world)
    assert (r.status_code, r.json()["code"]) == (504, "CORE_API_REQUEST_TIMEOUT")
    inst = world.engine.supervisor.get("r")
    assert inst.counters.handler_timeouts == 1
    assert inst.state is State.RUNNING


async def test_core_332_three_route_timeouts_fail_the_instance(world: World) -> None:
    for _ in range(3):
        assert (await slow_call(world)).status_code == 504
    await world.clock.settle()
    # It fails like three handler timeouts (CORE-193) and is restarted (CORE-194).
    inst = world.engine.supervisor.get("r")
    assert inst.last_error is not None
    assert inst.last_error["reason"] == "task_failed"
    assert "3 handler timeouts in a row" in str(inst.last_error)
    assert inst.restarts == 1


async def test_core_332_exception_is_500_and_the_instance_keeps_running(world: World) -> None:
    r = await world.call("GET", "/api/v1/modules/r/boom")
    assert (r.status_code, r.content) == (500, b"")
    assert world.engine.supervisor.get("r").state is State.RUNNING
    assert any(rec.get("event") == "route_error" for rec in recent_records())
    assert (await world.call("GET", "/api/v1/modules/r/ping")).status_code == 200


# --- MOD-74 in the test kit ------------------------------------------------------------


class GoodRoutes(MirrorModule):
    name = "goodroutes"
    capabilities = frozenset({"api.routes"})

    async def start(self) -> None:
        @self.ctx.router.get("/count")
        async def count() -> dict[str, int]:
            return {"n": 3}


class BadRoutes(MirrorModule):
    name = "badroutes"
    capabilities = frozenset({"api.routes"})

    async def start(self) -> None:
        @self.ctx.router.get("/restart")
        async def mine() -> dict[str, str]:
            return {"no": "engine path"}

        @self.ctx.router.get("/slow")
        async def slow() -> None:
            await asyncio.Event().wait()


async def test_mod_74_harness_calls_routes() -> None:
    from mm3.testing import ModuleHarness

    async with ModuleHarness(GoodRoutes) as h:
        assert await h.route("GET", "/count") == (200, {"n": 3})
        status, _body = await h.route("GET", "/missing")
        assert status == 404
        await h.check_contract()


async def test_mod_74_kit_flags_engine_paths_and_slow_routes() -> None:
    from mm3.testing import ContractError, ModuleHarness

    async with ModuleHarness(BadRoutes) as h:
        assert (await h.route("GET", "/slow"))[0] == 504
        with pytest.raises(ContractError) as err:
            await h.check_contract()
    text = str(err.value)
    assert "MOD-74" in text
    assert "/restart" in text
    assert "/slow" in text
