"""The two ASGI applications: the mirror listener and the admin listener.

The mirror listener serves the shell and its files; so far ``/pair``,
``/core/…`` and ``/ws``; the shell page (``/``), ``/i18n``, ``/modules`` and
``/custom.css`` follow. Anything else is 404, so ``/api`` and ``/admin``
never answer on the mirror display. The admin listener also carries the
preview shell's ``/admin/preview/ws`` (CORE-273).

The admin listener serves ``/admin`` and ``/api/*`` behind an admin token or
session; ``/admin/login`` and ``/admin/logout`` manage the session. With the
API services the REST routes of core 9.1 are mounted, with OpenAPI only as
``/api/openapi.json`` (CORE-313); every ``/api`` error, also outside those
routes, is ``application/problem+json`` (CORE-298).

Both applications are wrapped in the allow list (CORE-61) and the security
headers (CORE-63), the headers outermost so that refusals carry them too.

Implements:
    CORE-60: Core spec 0.12, §10 Security, packaging, deployment, quality.
    CORE-61, CORE-62, CORE-63: Core spec 0.21, §10.1 Security.
    CORE-253, CORE-254: Core spec 0.12, §10.1 Security.
    CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    CORE-298: Core spec 0.23, §9.1 Remote-control API.
    CORE-313: Core spec 0.22, §9.1 Remote-control API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp

from mm3.core.logging import get_logger
from mm3.core.web.api import ApiServices, api_router, check_api_auth, openapi_schema
from mm3.core.web.auth import WebAuth, origin_matches
from mm3.core.web.pages import admin_home_page, login_page, message_page, pair_page
from mm3.core.web.problems import ApiError, problem
from mm3.core.web.security import AllowList, Network, SecurityHeaders
from mm3.core.web.sessions import ADMIN_COOKIE, ADMIN_SESSION_TTL, DISPLAY_COOKIE
from mm3.core.web.shell import STATIC_DIR, ShellSources, add_shell_files, inside, shell_page
from mm3.core.web.ws import DISPLAY_COOKIE_MAX_AGE

if TYPE_CHECKING:
    from mm3.core.web.kiosk import KioskAgents
    from mm3.core.web.ws import ClientHub

__all__ = [
    "DISPLAY_COOKIE_MAX_AGE",
    "STATIC_DIR",
    "admin_app",
    "mirror_app",
    "protect",
]


MAX_FORM_BYTES = 4096
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_log = get_logger(subsystem="web")


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _form_field(request: Request, name: str) -> str | None:
    """Read one field of a small ``application/x-www-form-urlencoded`` body."""
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return None
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return None
    values = parse_qs(body.decode("utf-8", errors="replace"), max_num_fields=8).get(name)
    return values[0] if values else None


def _html(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(text, status_code=status)


def _bare() -> FastAPI:
    return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def protect(app: ASGIApp, networks: Callable[[], Sequence[Network]], listener: str) -> ASGIApp:
    """Wrap an application in the allow list and the security headers.

    Args:
        app: The application.
        networks: Returns the listener's allow list now.
        listener: ``mirror`` or ``admin``.

    Returns:
        The wrapped application.

    Implements:
        CORE-61, CORE-63: Core spec 0.21, §10.1 Security.
    """
    return SecurityHeaders(AllowList(app, networks, listener=listener))


def mirror_app(
    auth: WebAuth,
    hub: ClientHub | None = None,
    sources: ShellSources | None = None,
    agents: KioskAgents | None = None,
) -> FastAPI:
    """Build the mirror listener's application (without the protection wrappers).

    Args:
        auth: Token store and sessions.
        hub: Connected shells; ``/ws`` exists only with a hub.
        sources: The shell's files; without them only ``/core`` is served.
        agents: The kiosk agent; ``/ws/kiosk-agent`` exists only with it (CORE-268).

    Returns:
        The application.

    Implements:
        CORE-254: Core spec 0.12, §10.1 Security.
        CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    """
    app = _bare()

    if hub is not None:
        shells = hub

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await shells.serve(websocket, "mirror")

    if agents is not None:
        kiosk = agents

        @app.websocket("/ws/kiosk-agent")
        async def kiosk_agent(websocket: WebSocket) -> None:
            await kiosk.serve(websocket)

    @app.get("/pair")
    async def pair_get() -> HTMLResponse:
        return _html(pair_page())

    @app.post("/pair")
    async def pair_post(request: Request) -> Response:
        if not origin_matches(request):
            return _html(message_page("CORE_WEB_PAIR_TITLE", "CORE_WEB_FORBIDDEN"), 403)
        token = await _form_field(request, "token") or ""
        rec = await asyncio.to_thread(auth.tokens.verify, token, "display")
        if rec is None:
            _log.info("web_pair_failed", address=_client(request))
            return _html(message_page("CORE_WEB_PAIR_TITLE", "CORE_WEB_PAIR_FAILED"), 403)
        sid = await asyncio.to_thread(auth.sessions.create, rec)
        _log.info("web_display_paired", token=rec.name, address=_client(request))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            DISPLAY_COOKIE,
            sid,
            max_age=DISPLAY_COOKIE_MAX_AGE,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    if sources is not None:

        @app.get("/")
        async def shell() -> HTMLResponse:
            return _html(shell_page("mirror"))

        add_shell_files(app, sources)
    else:

        @app.get("/core/{path:path}")
        async def core(path: str) -> Response:
            target = inside(STATIC_DIR, path)
            return Response(status_code=404) if target is None else FileResponse(target)

    return app


def _api_errors(app: FastAPI) -> None:
    """Answer every ``/api`` error as a problem document (CORE-298)."""

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> Response:
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
            for e in exc.errors()
        )
        return problem("CORE_API_INVALID_REQUEST", problems or "Invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api"):
            if exc.status_code == 404:
                return problem("CORE_API_NOT_FOUND", "Not found")
            return problem("CORE_API_INVALID_REQUEST", str(exc.detail))
        return await http_exception_handler(request, exc)


def admin_app(
    auth: WebAuth,
    hub: ClientHub | None = None,
    sources: ShellSources | None = None,
    api: ApiServices | None = None,
) -> FastAPI:
    """Build the admin listener's application (without the protection wrappers).

    Args:
        auth: Token store, sessions and login lockout.
        hub: Connected shells; ``/admin/preview/ws`` exists only with a hub.
        sources: The shell's files, for the preview shell at ``/admin/preview``.
        api: What the REST API reads and controls; without it every
            authenticated ``/api`` request is 404.

    Returns:
        The application.

    Implements:
        CORE-62: Core spec 0.12, §10.1 Security.
        CORE-253: Core spec 0.12, §10.1 Security.
        CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
        CORE-298: Core spec 0.23, §9.1 Remote-control API.
        CORE-313: Core spec 0.22, §9.1 Remote-control API.
    """
    app = _bare()
    _api_errors(app)

    if hub is not None:
        shells = hub

        @app.websocket("/admin/preview/ws")
        async def preview_ws(websocket: WebSocket) -> None:
            await shells.serve(websocket, "admin")

    @app.get("/admin/login")
    async def login_get() -> HTMLResponse:
        return _html(login_page())

    @app.post("/admin/login")
    async def login_post(request: Request) -> Response:
        address = _client(request)
        if not origin_matches(request):
            return _html(message_page("CORE_WEB_LOGIN_TITLE", "CORE_WEB_FORBIDDEN"), 403)
        if auth.limiter.locked(address):
            return _html(login_page("CORE_WEB_LOGIN_LOCKED"), 429)
        token = await _form_field(request, "token") or ""
        rec = await asyncio.to_thread(auth.tokens.verify, token, "admin")
        if rec is None:
            auth.limiter.failed(address)
            _log.info("web_login_failed", address=address)
            if auth.limiter.locked(address):
                _log.warning("web_login_locked", address=address)
                return _html(login_page("CORE_WEB_LOGIN_LOCKED"), 429)
            return _html(login_page("CORE_WEB_LOGIN_FAILED"), 401)
        auth.limiter.succeeded(address)
        sid = await asyncio.to_thread(auth.sessions.create, rec)
        _log.info("web_login", token=rec.name, address=address)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            ADMIN_COOKIE,
            sid,
            max_age=int(ADMIN_SESSION_TTL.total_seconds()),
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/admin/logout")
    async def logout(request: Request) -> Response:
        if not origin_matches(request):
            return _html(message_page("CORE_WEB_ADMIN_TITLE", "CORE_WEB_FORBIDDEN"), 403)
        await asyncio.to_thread(auth.sessions.end, request.cookies.get(ADMIN_COOKIE))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    if sources is not None:

        @app.get("/admin/preview")
        async def preview(request: Request) -> Response:
            check = await auth.check_admin(request, request.method)
            if check.outcome != "ok":
                return RedirectResponse("/admin/login", status_code=303)
            return _html(shell_page("preview"))

        add_shell_files(app, sources)

    @app.get("/admin")
    async def admin_home(request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return RedirectResponse("/admin/login", status_code=303)
        return _html(admin_home_page())

    if api is not None:
        router = api_router(auth, api)
        app.include_router(router)
        schema = openapi_schema(router)

        @app.get("/api/openapi.json", include_in_schema=False)
        async def openapi(request: Request) -> Response:
            await check_api_auth(auth, request)
            return JSONResponse(schema)

    @app.api_route("/api/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
    async def api_fallback(request: Request, path: str) -> Response:
        await check_api_auth(auth, request)
        return problem("CORE_API_NOT_FOUND", f"No route /api/{path}")

    return app
