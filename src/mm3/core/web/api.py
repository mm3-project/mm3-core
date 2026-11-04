"""The engine's REST API under ``/api/v1`` on the admin listener (core spec 9.1).

This part serves the REST base of milestone 6: engine status, instances and
their health, instance controls, log levels, kiosk status and run-time
audio, notify and request, pages, shell reload and the OpenAPI schema. The
configuration, history, debug and preview routes follow in later parts.

Every route needs an admin token or session (CORE-62, CORE-253) and answers
errors as ``application/problem+json`` (CORE-298). Before the engine is
running and while it stops, routes answer 503 ``CORE_API_UNAVAILABLE``.

Implements:
    CORE-298: Core spec 0.23, §9.1 Remote-control API.
    CORE-305, CORE-306, CORE-308, CORE-313: Core spec 0.22, §9.1 Remote-control API.
    CORE-314, CORE-315: Core spec 0.23, §9.1 Remote-control API.
    CORE-112: Core spec 0.23, §10.5 Diagnostics and debugging.
    CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
    CORE-223: Core spec 0.13, §5.4 Request and reply.
    CORE-236, CORE-240: Core spec 0.14, §6.9 Pages and visibility.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from mm3.__about__ import __version__
from mm3.core.context import REQUEST_TIMEOUT_RANGE
from mm3.core.logging import get_logger, log_levels
from mm3.core.web.auth import WebAuth
from mm3.core.web.kiosk import KioskStatus
from mm3.core.web.problems import ApiError
from mm3.messages import MAX_PAYLOAD_BYTES, Notification, check_payload
from mm3.notifications import validate_payload

if TYPE_CHECKING:
    from mm3.core.config.manager import ConfigManager
    from mm3.core.engine import Engine
    from mm3.core.pages import PageService
    from mm3.core.supervisor import ManagedInstance
    from mm3.core.web.kiosk import KioskAgents
    from mm3.core.web.ws import ClientHub

__all__ = ["ApiServices", "api_router", "check_api_auth", "openapi_schema"]

_log = get_logger(subsystem="api")

Level = Literal["debug", "info", "warning", "error"]


@dataclass
class ApiServices:
    """What the REST API reads and controls.

    Attributes:
        engine: The engine (supervisor, bus, settings).
        manager: The configuration manager, for the ``config`` status.
        pages: The page service; without it there is one page ``main``.
        hub: Connected shells, for ``clients`` and shell reload.
        agents: The kiosk agent endpoint, if the kiosk key could be written.
        build: The shell's ``engine_build`` (CORE-273).
    """

    engine: Engine
    manager: ConfigManager | None = None
    pages: PageService | None = None
    hub: ClientHub | None = None
    agents: KioskAgents | None = None
    build: str = ""


# --- request bodies ---------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LevelBody(_Body):
    """``PUT …/log-level``: a level, or null for the default (CORE-112)."""

    level: Level | None


class AudioBody(_Body):
    """``PUT /api/v1/kiosk/audio``: run-time volume and mute (CORE-306)."""

    volume: StrictInt | None = Field(default=None, ge=0, le=100)
    mute: StrictBool | None = None


class NotifyBody(_Body):
    """``POST /api/v1/notify`` (CORE-308)."""

    name: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    target: str | None = None


class RequestBody(NotifyBody):
    """``POST /api/v1/request`` (CORE-314)."""

    timeout: float = Field(default=5.0, ge=REQUEST_TIMEOUT_RANGE[0], le=REQUEST_TIMEOUT_RANGE[1])


# --- authentication -----------------------------------------------------------------


async def check_api_auth(auth: WebAuth, request: Request) -> None:
    """Refuse a request without an admin token or session (CORE-298).

    Args:
        auth: Token store and sessions.
        request: The request.

    Raises:
        ApiError: 401 ``CORE_API_UNAUTHORIZED`` or 403 ``CORE_API_FORBIDDEN``.

    Implements:
        CORE-298: Core spec 0.23, §9.1 Remote-control API.
    """
    check = await auth.check_admin(request, request.method)
    if check.outcome == "origin":
        raise ApiError("CORE_API_FORBIDDEN", "Origin does not match the admin listener")
    if check.outcome != "ok":
        raise ApiError(
            "CORE_API_UNAUTHORIZED",
            "An admin token or session is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --- the router ---------------------------------------------------------------------


def api_router(auth: WebAuth, services: ApiServices) -> APIRouter:
    """Build the ``/api/v1`` routes of the REST base.

    Args:
        auth: Token store and sessions.
        services: What the routes read and control.

    Returns:
        The router; every route needs admin authentication.

    Implements:
        CORE-298: Core spec 0.23, §9.1 Remote-control API.
    """

    async def authenticated(request: Request) -> None:
        await check_api_auth(auth, request)
        engine = services.engine
        if not engine.running or engine.stopping:
            raise ApiError("CORE_API_UNAVAILABLE", "The engine is starting or stopping")

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticated)])
    sup = services.engine.supervisor

    def instance(iid: str) -> ManagedInstance:
        try:
            return sup.get(iid)
        except KeyError:
            raise ApiError("CORE_API_NOT_FOUND", f"Unknown instance {iid!r}") from None

    def target_of(name: str | None) -> None:
        if name is not None:
            instance(name)

    # --- status ---------------------------------------------------------------------

    def page_info() -> dict[str, Any]:
        pages = services.pages
        if pages is None:
            return {"id": "main", "index": 0, "count": 1}
        ids = pages.ids
        return {"id": pages.page, "index": ids.index(pages.page), "count": len(ids)}

    def status() -> dict[str, Any]:
        """The status object (CORE-305)."""
        engine = services.engine
        started = engine.started_at
        uptime = (engine.clock.wall() - started).total_seconds() if started else 0.0
        clients = services.hub.clients() if services.hub is not None else []
        agents = services.agents
        return {
            "version": __version__,
            "engine_build": services.build,
            "started_at": started.isoformat() if started else None,
            "uptime_s": round(max(uptime, 0.0), 3),
            "suspended": sup.suspended,
            "config": services.manager.status() if services.manager is not None else None,
            "page": page_info(),
            "clients": [
                {k: c.get(k) for k in ("role", "address", "user_agent", "since")} for c in clients
            ],
            "kiosk": agents.status.api_object() if agents is not None else None,
        }

    @router.get("/status")
    async def get_status() -> dict[str, Any]:
        """Engine, config, page, clients and kiosk status (CORE-305)."""
        return status()

    # --- instances ------------------------------------------------------------------

    def entry(inst: ManagedInstance) -> dict[str, Any]:
        """One element of ``GET /api/v1/modules`` (CORE-315)."""
        iid = inst.instance_id
        pages_cfg: Any = "all"
        position: Any = None
        positions: dict[str, Any] = {}
        if services.manager is not None:
            for cfg in services.manager.current.instances:
                if cfg.id == iid:
                    pages_cfg = cfg.spec.pages
                    position = _json(cfg.spec.position)
                    positions = {k: _json(v) for k, v in cfg.spec.positions.items()}
                    break
        hidden = sorted(services.pages.reasons(iid)) if services.pages is not None else []
        return {
            "id": iid,
            "module": inst.spec.module,
            "version": sup.module_version(inst.spec.module),
            "state": str(inst.state),
            "reason": inst.reason,
            "hidden": hidden,
            "pages": pages_cfg if pages_cfg == "all" else list(pages_cfg),
            "position": position,
            "positions": positions,
            "health": inst.health(),
        }

    async def settled(inst: ManagedInstance) -> dict[str, Any]:
        if services.pages is not None:
            await services.pages.idle()
        return entry(inst)

    @router.get("/modules")
    async def list_modules() -> list[dict[str, Any]]:
        """Instances in configuration order with state, visibility, positions and health."""
        return [entry(i) for i in sup.instances()]

    @router.get("/modules/{iid}/health")
    async def module_health(iid: str) -> dict[str, Any]:
        """The health record of one instance (CORE-113)."""
        return instance(iid).health()

    def has_element(inst: ManagedInstance) -> bool:
        reg = sup.registry.get(inst.spec.module)
        return reg is not None and reg.cls is not None and reg.cls.element is not None

    @router.post("/modules/{iid}/show")
    async def show(iid: str) -> dict[str, Any]:
        """Clear the ``manual`` hide reason; idempotent (CORE-238, CORE-315)."""
        inst = instance(iid)
        if services.pages is not None and has_element(inst):
            services.pages.set_manual(iid, False)
        return await settled(inst)

    @router.post("/modules/{iid}/hide")
    async def hide(iid: str) -> dict[str, Any]:
        """Set the ``manual`` hide reason; idempotent (CORE-238, CORE-315)."""
        inst = instance(iid)
        if services.pages is not None and has_element(inst):
            services.pages.set_manual(iid, True)
        return await settled(inst)

    @router.post("/modules/{iid}/restart")
    async def restart(iid: str) -> dict[str, Any]:
        """Manual restart (CORE-195, CORE-315)."""
        inst = instance(iid)
        await inst.restart()
        _log.info("api_instance_restart", instance=iid)
        return await settled(inst)

    @router.post("/modules/{iid}/stop")
    async def stop(iid: str) -> dict[str, Any]:
        """Runtime stop, not saved (CORE-240, CORE-315)."""
        inst = instance(iid)
        await inst.runtime_stop()
        _log.info("api_instance_stop", instance=iid)
        return await settled(inst)

    @router.post("/modules/{iid}/start")
    async def start(iid: str) -> dict[str, Any]:
        """Runtime start; restarts a FAILED or DISABLED instance (CORE-240, CORE-315)."""
        inst = instance(iid)
        await inst.runtime_start()
        _log.info("api_instance_start", instance=iid)
        return await settled(inst)

    # --- log levels -----------------------------------------------------------------

    def level_out(level: str, expires_at: datetime | None) -> dict[str, Any]:
        return {"level": level, "expires_at": expires_at.isoformat() if expires_at else None}

    @router.put("/modules/{iid}/log-level")
    async def instance_level(iid: str, body: LevelBody) -> dict[str, Any]:
        """Run-time log level of an instance (CORE-112)."""
        instance(iid)
        o = log_levels().set("instance", iid, body.level, sup.settings.debug_ttl)
        _log.info("api_log_level", instance=iid, level=o.level)
        return level_out(o.level, o.expires_at)

    @router.get("/debug/log-level")
    async def logger_levels() -> list[dict[str, Any]]:
        """The engine loggers and their levels (CORE-112)."""
        levels = log_levels()
        out = []
        for name in sorted(levels.subsystems):
            o = levels.get("logger", name)
            out.append(
                {"logger": name, "default": levels.default, **level_out(o.level, o.expires_at)}
            )
        return out

    @router.put("/debug/log-level/{logger}")
    async def logger_level(logger: str, body: LevelBody) -> dict[str, Any]:
        """Run-time log level of an engine logger (CORE-112)."""
        levels = log_levels()
        if logger not in levels.subsystems:
            raise ApiError("CORE_API_NOT_FOUND", f"Unknown logger {logger!r}")
        o = levels.set("logger", logger, body.level, sup.settings.debug_ttl)
        _log.info("api_log_level", logger=logger, level=o.level)
        return level_out(o.level, o.expires_at)

    # --- kiosk ----------------------------------------------------------------------

    def kiosk() -> dict[str, Any]:
        status_ = services.agents.status if services.agents is not None else KioskStatus()
        obj = status_.api_object(always=True)
        assert obj is not None  # noqa: S101 - always=True
        return {**obj, "outputs": status_.outputs, "sinks": status_.sinks}

    @router.get("/kiosk")
    async def get_kiosk() -> dict[str, Any]:
        """The kiosk object with the agent's last outputs and sinks (CORE-306)."""
        return kiosk()

    @router.put("/kiosk/audio")
    async def put_audio(body: AudioBody) -> dict[str, Any]:
        """Run-time volume and mute, never saved (CORE-306)."""
        agents = services.agents
        if agents is None or not agents.status.connected:
            raise ApiError("CORE_API_NO_KIOSK", "No kiosk agent is connected")
        if not await agents.set_runtime_audio(volume=body.volume, mute=body.mute):
            raise ApiError("CORE_API_NO_KIOSK", "The kiosk agent did not take the values")
        _log.info("api_kiosk_audio", volume=body.volume, mute=body.mute)
        return kiosk()

    # --- notify and request ---------------------------------------------------------

    def notification(body: NotifyBody, *, expects_reply: bool) -> Notification:
        if body.name.startswith("CORE_"):
            raise ApiError("CORE_API_RESERVED_NAME", "Names starting with CORE_ are reserved")
        try:
            check_payload(body.payload)
        except ValueError as exc:
            raise ApiError(
                "CORE_API_TOO_LARGE", f"{exc} (limit {MAX_PAYLOAD_BYTES} bytes)"
            ) from None
        try:
            validate_payload(body.name, body.payload)
        except ValueError as exc:
            raise ApiError("CORE_API_PAYLOAD_INVALID", str(exc)) from None
        target_of(body.target)
        nid = uuid4()
        try:
            return Notification(
                id=nid,
                trace=nid,
                ts=datetime.now(UTC),
                name=body.name,
                source="remote",
                target=body.target,
                expects_reply=expects_reply,
                payload=body.payload,
            )
        except (ValidationError, ValueError) as exc:
            raise ApiError("CORE_API_INVALID_REQUEST", str(exc)) from None

    @router.post("/notify")
    async def notify(body: NotifyBody) -> dict[str, Any]:
        """Inject a notification with source ``remote`` (CORE-308)."""
        n = notification(body, expects_reply=False)
        services.engine.bus.publish(n)
        return {"id": str(n.id)}

    @router.post("/request")
    async def request(body: RequestBody) -> dict[str, Any]:
        """Send a request and return its reply (CORE-223, CORE-314)."""
        n = notification(body, expects_reply=True)
        bus = services.engine.bus
        future = bus.open_request(n.id)
        try:
            bus.publish(n)
            try:
                async with asyncio.timeout(body.timeout):
                    reply = await future
            except TimeoutError:
                raise ApiError(
                    "CORE_API_REQUEST_TIMEOUT", f"No reply to {body.name} within {body.timeout} s"
                ) from None
        finally:
            bus.close_request(n.id)
        error = reply.payload.get("error") if len(reply.payload) == 1 else None
        if isinstance(error, dict) and set(error) == {"code", "message_key"}:
            return {"error": dict(error)}
        return {"payload": dict(reply.payload)}

    # --- pages ----------------------------------------------------------------------

    def pages_out() -> dict[str, Any]:
        pages = services.pages
        if pages is None:
            return {"current": "main", "pages": [{"id": "main", "title": "main"}]}
        return {
            "current": pages.page,
            "pages": [{"id": p.id, "title": p.title} for p in pages.page_infos()],
        }

    async def pages_after() -> dict[str, Any]:
        if services.pages is not None:
            await services.pages.idle()
        return pages_out()

    @router.get("/pages")
    async def list_pages() -> dict[str, Any]:
        """The pages and the current page (CORE-236)."""
        return pages_out()

    @router.post("/pages/next")
    async def next_page() -> dict[str, Any]:
        """Move to the next page (CORE-236)."""
        if services.pages is not None:
            services.pages.next("rest")
        return await pages_after()

    @router.post("/pages/prev")
    async def prev_page() -> dict[str, Any]:
        """Move to the previous page (CORE-236)."""
        if services.pages is not None:
            services.pages.prev("rest")
        return await pages_after()

    @router.post("/pages/{page}")
    async def set_page(page: str) -> dict[str, Any]:
        """Change to a page; an unknown page is 404 (CORE-236, CORE-315)."""
        ids = services.pages.ids if services.pages is not None else ["main"]
        if page not in ids:
            raise ApiError("CORE_API_NOT_FOUND", f"Unknown page {page!r}")
        if services.pages is not None:
            services.pages.set_page(page, "rest")
        return await pages_after()

    # --- shells ---------------------------------------------------------------------

    @router.post("/shell/reload")
    async def shell_reload() -> dict[str, Any]:
        """Reload every browser (``control`` ``reload`` ``page``, CORE-275)."""
        hub = services.hub
        if hub is None:
            return {"clients": 0}
        hub.control("reload", {"scope": "page"})
        return {"clients": len(hub.clients())}

    return router


def openapi_schema(router: APIRouter) -> dict[str, Any]:
    """Return the OpenAPI schema of the API routes (CORE-313).

    Args:
        router: The routes to describe.

    Returns:
        The schema, served as ``/api/openapi.json``.

    Implements:
        CORE-313: Core spec 0.22, §9.1 Remote-control API.
    """
    return get_openapi(title="MM3 admin API", version=__version__, routes=router.routes)


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return value
