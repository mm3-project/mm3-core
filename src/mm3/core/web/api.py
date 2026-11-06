"""The engine's REST API under ``/api/v1`` on the admin listener (core spec 9.1).

This part serves the REST base of milestone 6: engine status, instances and
their health, instance controls, log levels, kiosk status and run-time
audio, notify and request, pages, shell reload and the OpenAPI schema. The
configuration, history, debug and preview routes follow in later parts.

Every route needs an admin token or session (CORE-62, CORE-253) and answers
errors as ``application/problem+json`` (CORE-298). Before the engine is
running and while it stops, routes answer 503 ``CORE_API_UNAVAILABLE``.

Implements:
    CORE-298: Core spec 0.25, §9.1 Remote-control API.
    CORE-305, CORE-306, CORE-308, CORE-313: Core spec 0.22, §9.1 Remote-control API.
    CORE-314, CORE-315: Core spec 0.23, §9.1 Remote-control API.
    CORE-112: Core spec 0.23, §10.5 Diagnostics and debugging.
    CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
    CORE-223: Core spec 0.13, §5.4 Request and reply.
    CORE-236, CORE-240: Core spec 0.14, §6.9 Pages and visibility.
    CORE-299: Core spec 0.22, §9.1 Remote-control API.
    CORE-311, CORE-116: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-316, CORE-318: Core spec 0.24, §10.5 Diagnostics and debugging.
    CORE-300, CORE-303, CORE-35: Core spec 0.22, §6.4 Web configuration editor.
    CORE-321: Core spec 0.25, §6.8 Configuration history, undo and redo.
    CORE-324: Core spec 0.26, §6.8 Configuration history, undo and redo.
    CORE-309: Core spec 0.26, §9.1 Remote-control API.
    CORE-301, CORE-302: Core spec 0.28, §6.4 Web configuration editor.
    CORE-307: Core spec 0.28, §6.6 Live layout preview.
    CORE-332: Core spec 0.29, §9.1 Remote-control API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from mm3.__about__ import __version__
from mm3.core.context import REQUEST_TIMEOUT_RANGE
from mm3.core.diag.bundle import Bundle, bundle_name, package_list
from mm3.core.diag.logfilter import LogFilter
from mm3.core.diag.secrets import Masker
from mm3.core.logging import get_logger, log_levels, numbered_records
from mm3.core.web.auth import WebAuth
from mm3.core.web.kiosk import KioskStatus
from mm3.core.web.modroutes import ModuleRoutes
from mm3.core.web.modroutes import register as register_module_routes
from mm3.core.web.problems import ApiError
from mm3.messages import MAX_PAYLOAD_BYTES, Notification, check_payload
from mm3.notifications import validate_payload

if TYPE_CHECKING:
    from mm3.core.config.editor import ConfigEditor
    from mm3.core.config.manager import ConfigManager
    from mm3.core.engine import Engine
    from mm3.core.pages import PageService
    from mm3.core.supervisor import ManagedInstance
    from mm3.core.web.kiosk import KioskAgents
    from mm3.core.web.mirrorpreview import MirrorPreview
    from mm3.core.web.ws import ClientHub

__all__ = ["ApiServices", "api_router", "check_api_auth", "openapi_schema", "status_object"]

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
        masker: Returns the masker for the running configuration's secrets
            (CORE-116, CORE-319).
        editor: The configuration editor and history (CORE-301, CORE-321).
        mirror: Show on mirror (CORE-307).
    """

    engine: Engine
    manager: ConfigManager | None = None
    pages: PageService | None = None
    hub: ClientHub | None = None
    agents: KioskAgents | None = None
    build: str = ""
    masker: Callable[[], Masker] = field(default=Masker)
    editor: ConfigEditor | None = None
    mirror: MirrorPreview | None = None


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


class FileBody(_Body):
    """One draft file: path relative to the config directory and its content (CORE-301)."""

    path: str = Field(min_length=1, max_length=512)
    content: str


class FileOps(_Body):
    """A JSON Patch on one file's document (CORE-301, CORE-320)."""

    file: str = Field(min_length=1, max_length=512)
    patch: list[dict[str, Any]]


class PatchBody(_Body):
    """``POST /api/v1/config/patch`` (CORE-301)."""

    base: str
    files: list[FileBody] = Field(default_factory=list)
    ops: list[FileOps] = Field(default_factory=list)


class DraftBody(_Body):
    """``POST /api/v1/config/validate`` and ``PUT /api/v1/config`` (CORE-302)."""

    base: str | None = None
    files: list[FileBody] = Field(default_factory=list)


class LocaleBody(_Body):
    """``PUT /api/v1/session/locale``: a shipped language or null (CORE-309)."""

    locale: Literal["en", "it", "fr", "es", "de"] | None


class RestoreBody(_Body):
    """``POST /api/v1/config/restore`` (CORE-321)."""

    version: StrictInt = Field(ge=1)
    section: str | None = None


class GridBody(_Body):
    """The ``grid`` of a layout object (CORE-274)."""

    columns: StrictInt = Field(ge=1, le=48)
    rows: StrictInt = Field(ge=1, le=48)
    gap: float = Field(ge=0, le=500)
    padding: float = Field(ge=0, le=500)


class PlacementBody(_Body):
    """One instance on one page of a layout object (CORE-274)."""

    instance: str = Field(min_length=1, max_length=64)
    col: StrictInt = Field(ge=1, le=48)
    row: StrictInt = Field(ge=1, le=48)
    width: StrictInt = Field(ge=1, le=48)
    height: StrictInt = Field(ge=1, le=48)
    z: StrictInt = Field(ge=-1000, le=1000)
    preset: str | None = Field(default=None, max_length=32)
    stack_index: StrictInt = Field(default=0, ge=0)
    stack_size: StrictInt = Field(default=1, ge=1)
    area: tuple[float, float]


class LayoutBody(_Body):
    """``POST /api/v1/preview/mirror``: a draft layout object (CORE-274, CORE-307)."""

    viewport: tuple[StrictInt, StrictInt]
    cell: tuple[float, float]
    grid: GridBody
    pages: dict[str, list[PlacementBody]] = Field(max_length=32)


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
        CORE-298: Core spec 0.25, §9.1 Remote-control API.
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


def api_router(auth: WebAuth | None, services: ApiServices) -> APIRouter:
    """Build the ``/api/v1`` routes of the REST base.

    Args:
        auth: Token store and sessions; ``None`` on the local admin socket,
            where the socket's permissions are the authentication (CORE-311).
        services: What the routes read and control.

    Returns:
        The router; with ``auth`` every route needs admin authentication.

    Implements:
        CORE-298: Core spec 0.25, §9.1 Remote-control API.
    """

    async def authenticated(request: Request) -> None:
        if auth is not None:
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

    def status() -> dict[str, Any]:
        return status_object(services)

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

    # --- Show on mirror (CORE-307) -----------------------------------------------------

    @router.post("/preview/mirror", status_code=204)
    async def push_mirror(body: LayoutBody, request: Request) -> Response:
        """Show a draft layout on the kiosk shell only, for 60 s after the last push."""
        from mm3.core.web.mirrorpreview import NoKioskError
        from mm3.core.web.sessions import ADMIN_COOKIE

        mirror = services.mirror
        if mirror is None:
            raise ApiError("CORE_API_NO_KIOSK", "No kiosk shell is connected")
        bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        session = None if bearer else request.cookies.get(ADMIN_COOKIE)
        try:
            mirror.push(body.model_dump(mode="json"), session)
        except NoKioskError:
            raise ApiError("CORE_API_NO_KIOSK", "No kiosk shell is connected") from None
        return Response(status_code=204)

    @router.delete("/preview/mirror", status_code=204)
    async def end_mirror() -> Response:
        """Send the running layout to the kiosk again."""
        if services.mirror is not None:
            services.mirror.clear("delete")
        return Response(status_code=204)

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

    # --- configuration editor and history (CORE-300..303, CORE-321) -------------------

    def editor() -> ConfigEditor:
        if services.editor is None:
            raise ApiError("CORE_API_NOT_FOUND", "The configuration editor is not available")
        return services.editor

    async def run(call: Callable[[], Any]) -> Any:
        from mm3.core.config.editor import EditorError

        try:
            return await asyncio.to_thread(call)
        except EditorError as exc:
            raise ApiError(exc.code, exc.detail, errors=exc.errors, extra=exc.extra) from None

    def author(request: Request) -> str:
        return request.client.host if request.client else "admin"

    def dumped(files: list[FileBody]) -> list[dict[str, Any]]:
        return [f.model_dump() for f in files]

    @router.get("/config", response_model=None)
    async def get_config() -> Response:
        """The config set with its ETag, secrets masked (CORE-301, CORE-303)."""
        view = await run(editor().view)
        return Response(
            json.dumps(view), media_type="application/json", headers={"ETag": view["etag"]}
        )

    @router.get("/config/schema")
    async def get_schema() -> dict[str, Any]:
        """One JSON Schema of the engine and every installed module (6.4, CORE-37)."""
        from mm3.core.config.schema import config_schema

        return config_schema(sup.registry)

    @router.post("/config/patch")
    async def post_patch(body: PatchBody) -> dict[str, Any]:
        """Apply JSON Patch and YAML edits to the draft; nothing written (CORE-301)."""
        ops = [o.model_dump() for o in body.ops]
        result: dict[str, Any] = await run(
            lambda: editor().patch(body.base, dumped(body.files), ops)
        )
        return result

    @router.post("/config/validate")
    async def post_validate(body: DraftBody) -> dict[str, Any]:
        """Records and the diff preview; nothing written (CORE-302)."""
        result: dict[str, Any] = await run(lambda: editor().validate(body.base, dumped(body.files)))
        return result

    @router.put("/config")
    async def put_config(
        body: DraftBody, request: Request, if_match: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Validate and save as a new history version (CORE-302, CORE-300)."""
        who = author(request)
        result: dict[str, Any] = await run(
            lambda: editor().save(if_match, body.base, dumped(body.files), who)
        )
        _log.info("api_config_saved", version=result.get("version"), author=who)
        return result

    @router.get("/config/history")
    async def get_history(request: Request) -> dict[str, Any]:
        """Versions newest first, paged (CORE-321, CORE-299)."""
        query = dict(request.query_params)
        unknown = set(query) - {"limit", "before"}
        if unknown:
            raise ApiError("CORE_API_INVALID_REQUEST", f"Unknown parameter {sorted(unknown)[0]!r}")
        try:
            limit = int(query.get("limit", PAGE_DEFAULT))
            before = int(query["before"]) if query.get("before") else None
        except ValueError as exc:
            raise ApiError("CORE_API_INVALID_REQUEST", str(exc)) from None
        if not 1 <= limit <= PAGE_MAX:
            raise ApiError("CORE_API_INVALID_REQUEST", f"limit must be 1 to {PAGE_MAX}")
        page: dict[str, Any] = await run(lambda: editor().history_page(limit, before))
        return page

    @router.get("/config/history/{version}")
    async def get_version(version: int) -> dict[str, Any]:
        """The files of one version, secrets masked, for Open in editor (CORE-324)."""
        files: dict[str, Any] = await run(lambda: editor().version_files(version))
        return files

    @router.get("/config/history/{a}/diff/{b}")
    async def get_history_diff(a: int, b: int, section: str | None = None) -> list[dict[str, str]]:
        """The diff between two versions, whole or per section (CORE-321)."""
        diff: list[dict[str, str]] = await run(lambda: editor().history_diff(a, b, section))
        return diff

    @router.post("/config/undo")
    async def post_undo(
        request: Request, if_match: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Undo the last save (CORE-154, CORE-321)."""
        who = author(request)
        result: dict[str, Any] = await run(lambda: editor().undo(if_match, who))
        return result

    @router.post("/config/redo")
    async def post_redo(
        request: Request, if_match: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Redo the save undone most recently (CORE-154, CORE-321)."""
        who = author(request)
        result: dict[str, Any] = await run(lambda: editor().redo(if_match, who))
        return result

    @router.post("/config/restore")
    async def post_restore(
        body: RestoreBody, request: Request, if_match: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Restore a version or one section of it (CORE-155, CORE-321)."""
        who = author(request)
        result: dict[str, Any] = await run(
            lambda: editor().restore(if_match, body.version, body.section, who)
        )
        return result

    # --- admin language (CORE-309) ---------------------------------------------------

    @router.put("/session/locale")
    async def put_locale(body: LocaleBody, request: Request) -> dict[str, Any]:
        """Keep the admin language with the admin session (CORE-309)."""
        from mm3.core.web.sessions import ADMIN_COOKIE

        sid = request.cookies.get(ADMIN_COOKIE)
        bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        if auth is None or bearer or not sid:
            raise ApiError("CORE_API_INVALID_REQUEST", "The language is kept with an admin session")
        if not await asyncio.to_thread(auth.sessions.set_locale, sid, body.locale):
            raise ApiError("CORE_API_INVALID_REQUEST", "No admin session")
        engine_locale = sup.settings.locale
        return {"locale": body.locale, "effective": body.locale or engine_locale}

    # --- diagnostics ----------------------------------------------------------------

    @router.get("/debug/logs", response_model=None)
    async def debug_logs(request: Request) -> dict[str, Any] | Response:
        """The ring buffer, newest first, filtered and paged (CORE-299, CORE-316)."""
        query = dict(request.query_params)
        unknown = set(query) - {"limit", "before", *LOG_FILTER_KEYS}
        if unknown:
            raise ApiError("CORE_API_INVALID_REQUEST", f"Unknown parameter {sorted(unknown)[0]!r}")
        try:
            log_filter = LogFilter.parse({k: v for k, v in query.items() if k in LOG_FILTER_KEYS})
            limit = int(query.get("limit", PAGE_DEFAULT))
            before = int(query["before"]) if query.get("before") else None
        except ValueError as exc:
            raise ApiError("CORE_API_INVALID_REQUEST", str(exc)) from None
        if not 1 <= limit <= PAGE_MAX:
            raise ApiError("CORE_API_INVALID_REQUEST", f"limit must be 1 to {PAGE_MAX}")
        items, next_cursor = page_logs(log_filter, limit, before)
        if "application/x-ndjson" in request.headers.get("accept", ""):
            body = "".join(json.dumps(r, default=str) + "\n" for r in items)
            headers = {}
            if next_cursor is not None:
                headers["Link"] = f'<{request.url.path}?before={next_cursor}>; rel="next"'
            return Response(body, media_type="application/x-ndjson", headers=headers)
        return {"items": items, "next": next_cursor}

    @router.get("/debug/bundle", response_class=Response)
    async def debug_bundle() -> Response:
        """The diagnostic bundle (CORE-311, CORE-318)."""
        name, data = engine_bundle(services)
        _log.info("api_diag_bundle", bytes=len(data))
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # --- shells ---------------------------------------------------------------------

    @router.post("/shell/reload")
    async def shell_reload() -> dict[str, Any]:
        """Reload every browser (``control`` ``reload`` ``page``, CORE-275)."""
        hub = services.hub
        if hub is None:
            return {"clients": 0}
        hub.control("reload", {"scope": "page"})
        return {"clients": len(hub.clients())}

    # Module routes last, so the engine's own routes answer first (CORE-332).
    register_module_routes(router, ModuleRoutes(sup))
    return router


PAGE_DEFAULT = 50
PAGE_MAX = 500
LOG_FILTER_KEYS = ("level", "module", "instance", "trace", "text")


def page_logs(
    log_filter: LogFilter, limit: int, before: int | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Return one page of matching ring-buffer records, newest first (CORE-299).

    Args:
        log_filter: The filter (CORE-316).
        limit: Records per page.
        before: Cursor of the page, or ``None`` for the newest.

    Returns:
        The records and the cursor of the next page, or ``None``.

    Implements:
        CORE-299: Core spec 0.22, §9.1 Remote-control API.
    """
    items: list[dict[str, Any]] = []
    last: int | None = None
    more = False
    for seq, record in reversed(numbered_records()):
        if before is not None and seq >= before:
            continue
        if not log_filter.matches(record):
            continue
        if len(items) == limit:
            more = True
            break
        items.append(record)
        last = seq
    return items, (str(last) if more and last is not None else None)


def config_files(services: ApiServices) -> tuple[list[Path], Path | None]:
    """Return the files of the running config set and the config directory."""
    manager = services.manager
    if manager is None:
        return [], None
    files = {p for p in manager.watched_files() if p != manager.env_file and p.is_file()}
    for directory in manager.watched_directories():
        if directory.is_dir():
            files.update(p for p in directory.rglob("*") if p.is_file())
    return sorted(files), manager.current.path.parent


def engine_bundle(services: ApiServices) -> tuple[str, bytes]:
    """Build the full diagnostic bundle from the running engine (CORE-318).

    Args:
        services: What the API reads.

    Returns:
        The file name and the zip.

    Implements:
        CORE-318: Core spec 0.24, §10.5 Diagnostics and debugging.
        CORE-116: Core spec 0.22, §10.5 Diagnostics and debugging.
    """
    bundle = Bundle(masker=services.masker())
    sup = services.engine.supervisor
    bundle.modules = {i.spec.module: sup.module_version(i.spec.module) for i in sup.instances()}
    bundle.add_bytes("packages.txt", package_list().encode("utf-8"))
    files, config_dir = config_files(services)
    if config_dir is None:
        bundle.missing.append("config/")
    else:
        bundle.add_config(files, config_dir)
    records = [r for _, r in numbered_records()]
    bundle.add_jsonl("logs.jsonl", records)
    bundle.add_json("health.json", [i.health() for i in sup.instances()])
    bundle.add_jsonl("browser-errors.jsonl", [r for r in records if r.get("source") == "browser"])
    clients = services.hub.clients() if services.hub is not None else []
    kiosk = services.agents.status.api_object() if services.agents is not None else None
    bundle.add_json(
        "kiosk.json",
        {
            "kiosk": kiosk,
            "clients": [
                {k: c.get(k) for k in ("role", "address", "user_agent", "since", "viewport")}
                for c in clients
            ],
        },
    )
    return bundle_name(bundle.now), bundle.build()


def status_object(services: ApiServices) -> dict[str, Any]:
    """Return the object of ``GET /api/v1/status`` and the ``status`` topic (CORE-305).

    Args:
        services: What the API reads.

    Returns:
        ``{version, engine_build, started_at, uptime_s, suspended, config,
        page, clients, kiosk, mqtt}``; ``mqtt`` is ``{connected, since, error}`` or ``None``
        without a bridge.

    Implements:
        CORE-305: Core spec 0.22, §9.1 Remote-control API.
        CORE-339: Core spec 0.34, §9.3 MQTT bridge.
    """
    engine = services.engine
    started = engine.started_at
    uptime = (engine.clock.wall() - started).total_seconds() if started else 0.0
    clients = services.hub.clients() if services.hub is not None else []
    agents = services.agents
    pages = services.pages
    if pages is None:
        page = {"id": "main", "index": 0, "count": 1}
    else:
        page = {"id": pages.page, "index": pages.ids.index(pages.page), "count": len(pages.ids)}
    return {
        "version": __version__,
        "engine_build": services.build,
        "started_at": started.isoformat() if started else None,
        "uptime_s": round(max(uptime, 0.0), 3),
        "suspended": engine.supervisor.suspended,
        "config": services.manager.status() if services.manager is not None else None,
        "page": page,
        "clients": [
            {k: c.get(k) for k in ("role", "address", "user_agent", "since")} for c in clients
        ],
        "kiosk": agents.status.api_object() if agents is not None else None,
        "mqtt": engine.mqtt.status(),
    }


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
