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
    CORE-61, CORE-62: Core spec 0.21, §10.1 Security.
    CORE-63: Core spec 0.38, §10.1 Security.
    CORE-253: Core spec 0.38, §10.1 Security.
    CORE-254: Core spec 0.12, §10.1 Security.
    CORE-378: Core spec 0.40, §10.1 Security.
    CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    CORE-298: Core spec 0.25, §9.1 Remote-control API.
    CORE-313: Core spec 0.22, §9.1 Remote-control API.
    CORE-304, CORE-311: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-309: Core spec 0.26, §9.1 Remote-control API.
    CORE-329, CORE-330: Core spec 0.29, §11.1 Engine additions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlsplit

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

from mm3.core.i18n import module_catalog_root
from mm3.core.i18n.catalogs import locale_chain
from mm3.core.logging import get_logger
from mm3.core.web.api import ApiServices, api_router, check_api_auth, openapi_schema
from mm3.core.web.auth import WebAuth, origin_matches
from mm3.core.web.extensions import (
    Extension,
    admin_extensions,
    data_attributes,
    extension_page_data,
    header_label,
)
from mm3.core.web.pages import (
    admin_config_page,
    admin_debug_page,
    admin_extension_page,
    admin_home_page,
    admin_texts,
    form_texts,
    language_names,
    login_page,
    message_page,
    pair_page,
)
from mm3.core.web.problems import ApiError, problem
from mm3.core.web.security import AllowList, Network, SecurityHeaders
from mm3.core.web.sessions import ADMIN_COOKIE, ADMIN_SESSION_TTL, DISPLAY_COOKIE
from mm3.core.web.shell import (
    IMMUTABLE,
    STATIC_DIR,
    VENDOR_DIR,
    ShellSources,
    add_shell_files,
    inside,
    shell_page,
)
from mm3.core.web.ws import DISPLAY_COOKIE_MAX_AGE

if TYPE_CHECKING:
    from mm3.core.web.adminws import AdminChannel
    from mm3.core.web.kiosk import KioskAgents
    from mm3.core.web.ws import ClientHub

__all__ = [
    "DISPLAY_COOKIE_MAX_AGE",
    "STATIC_DIR",
    "admin_app",
    "mirror_app",
    "protect",
    "socket_app",
]


MAX_FORM_BYTES = 4096
MAX_NEXT_BYTES = 1024
ADMIN_STATIC_DIR = Path(__file__).parent / "adminui"
"""The admin page's scripts and styles, served only on the admin listener (CORE-34)."""
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_log = get_logger(subsystem="web")


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def safe_next(value: str | None) -> str | None:
    """Return ``value`` when it is a path under ``/admin`` on this listener, else ``None``.

    The login page and the login form carry the page the browser asked for
    as ``next``. Only a local path under ``/admin`` is followed: a scheme or
    host, a protocol-relative ``//``, a backslash, a control character, a dot
    segment (also percent-encoded), and ``/admin/login`` or ``/admin/logout``
    themselves are refused, so the redirect can never leave the admin pages.

    Args:
        value: The ``next`` value as received.

    Returns:
        The path and query to redirect to, or ``None`` when it is not allowed.

    Implements:
        CORE-378: Core spec 0.40, §10.1 Security.
    """
    if not value or len(value) > MAX_NEXT_BYTES:
        return None
    if "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment or "#" in value:
        return None
    path = parts.path
    if path != "/admin" and not path.startswith("/admin/"):
        return None
    decoded = unquote(path)
    if "\\" in decoded or any(seg in (".", "..") for seg in decoded.split("/")):
        return None
    if decoded.rstrip("/") in ("/admin/login", "/admin/logout"):
        return None
    return value


def _login_redirect(request: Request) -> RedirectResponse:
    """Answer an admin page without a session: 303 to the login with ``next`` (CORE-378)."""
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/admin/login?next={quote(target, safe='/')}", status_code=303)


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
        CORE-61: Core spec 0.21, §10.1 Security.
        CORE-63: Core spec 0.38, §10.1 Security.
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
    channel: AdminChannel | None = None,
    locale: Callable[[], str] = lambda: "en",
) -> FastAPI:
    """Build the admin listener's application (without the protection wrappers).

    Args:
        auth: Token store, sessions and login lockout.
        hub: Connected shells; ``/admin/preview/ws`` exists only with a hub.
        sources: The shell's files, for the preview shell at ``/admin/preview``.
        api: What the REST API reads and controls; without it every
            authenticated ``/api`` request is 404.
        channel: The admin channel ``/admin/ws`` (CORE-304).
        locale: Returns the language of the admin pages (``engine.locale``).

    Returns:
        The application.

    Implements:
        CORE-62: Core spec 0.12, §10.1 Security.
        CORE-253: Core spec 0.38, §10.1 Security.
        CORE-378: Core spec 0.40, §10.1 Security.
        CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
        CORE-298: Core spec 0.25, §9.1 Remote-control API.
        CORE-313: Core spec 0.22, §9.1 Remote-control API.
        CORE-304: Core spec 0.22, §10.5 Diagnostics and debugging.
    """
    app = _bare()
    _api_errors(app)

    if channel is not None:
        admin_channel = channel

        @app.websocket("/admin/ws")
        async def admin_ws(websocket: WebSocket) -> None:
            await admin_channel.serve(websocket)

    if hub is not None:
        shells = hub

        @app.websocket("/admin/preview/ws")
        async def preview_ws(websocket: WebSocket) -> None:
            await shells.serve(websocket, "admin")

    @app.get("/admin/login")
    async def login_get(request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome == "ok" and check.via == "session":
            return RedirectResponse("/admin", status_code=303)  # CORE-378
        return _html(login_page(next_path=safe_next(request.query_params.get("next"))))

    @app.post("/admin/login")
    async def login_post(request: Request) -> Response:
        address = _client(request)
        if not origin_matches(request):
            return _html(message_page("CORE_WEB_LOGIN_TITLE", "CORE_WEB_FORBIDDEN"), 403)
        landing = safe_next(await _form_field(request, "next"))
        if auth.limiter.locked(address):
            return _html(login_page("CORE_WEB_LOGIN_LOCKED", landing), 429)
        token = await _form_field(request, "token") or ""
        rec = await asyncio.to_thread(auth.tokens.verify, token, "admin")
        if rec is None:
            auth.limiter.failed(address)
            _log.info("web_login_failed", address=address)
            if auth.limiter.locked(address):
                _log.warning("web_login_locked", address=address)
                return _html(login_page("CORE_WEB_LOGIN_LOCKED", landing), 429)
            return _html(login_page("CORE_WEB_LOGIN_FAILED", landing), 401)
        auth.limiter.succeeded(address)
        sid = await asyncio.to_thread(auth.sessions.create, rec)
        _log.info("web_login", token=rec.name, address=address)
        response = RedirectResponse(landing or "/admin", status_code=303)  # CORE-378
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
                return _login_redirect(request)
            return _html(shell_page("preview"))

        add_shell_files(app, sources)

    # --- admin extensions (CORE-329) ---------------------------------------------------

    def extensions() -> list[Extension]:
        if api is None or api.manager is None:
            return []
        return admin_extensions(api.engine.supervisor.registry, api.manager.current)

    async def admin_language(request: Request, via: str | None) -> tuple[str, str | None]:
        # CORE-309: the session's language, else engine.locale; never Accept-Language.
        picked = None
        if via == "session":
            picked = await asyncio.to_thread(
                auth.sessions.locale, request.cookies.get(ADMIN_COOKIE)
            )
        return picked or locale(), picked

    async def extension_links(request: Request, via: str | None) -> list[tuple[str, str]]:
        exts = extensions()
        if not exts:
            return []
        lang, _ = await admin_language(request, via)
        chain = locale_chain(None, lang)
        i18n = api.engine.i18n if api is not None else None
        return [(f"/admin/ext/{e.module}", header_label(i18n, e.module, chain)) for e in exts]

    def find_extension(module: str) -> Extension | None:
        return next((e for e in extensions() if e.module == module), None)

    @app.get("/admin/ext/{module}")
    async def admin_extension(module: str, request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return _login_redirect(request)
        ext = find_extension(module)
        if ext is None:
            return Response(status_code=404)
        current = api.manager.current if api is not None and api.manager is not None else None
        enabled = {i.id: i.enabled for i in current.instances} if current is not None else {}
        data = extension_page_data(
            ext, api.engine.supervisor if api is not None else None, lambda i: enabled.get(i, True)
        )
        links = await extension_links(request, check.via)
        return _html(admin_extension_page(f"/admin/ext/{module}", links, data_attributes(data)))

    @app.get("/admin/ext/{module}/i18n.json")
    async def admin_extension_texts(module: str, request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return Response(status_code=401)
        ext = find_extension(module)
        if ext is None or api is None or api.engine.i18n is None:
            return Response(status_code=404)
        lang, _ = await admin_language(request, check.via)
        # CORE-330: the module's catalog along the admin language's chain, then the core's.
        _version, data = api.engine.i18n.merged(module, locale_chain(None, lang))
        return Response(data, media_type="application/json")

    @app.get("/admin/ext/{module}/{version}/{path:path}")
    async def admin_extension_file(
        module: str, version: str, path: str, request: Request
    ) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return Response(status_code=401)
        ext = find_extension(module)
        if ext is None or ext.files.static is None or ext.files.version != version:
            return Response(status_code=404)
        target = inside(ext.files.static, path)
        if target is None:
            return Response(status_code=404)
        return FileResponse(target, headers={"Cache-Control": IMMUTABLE})

    @app.get("/admin/debug")
    async def admin_debug(request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return _login_redirect(request)
        return _html(admin_debug_page(await extension_links(request, check.via)))

    @app.get("/admin/config")
    async def admin_config(request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return _login_redirect(request)
        return _html(admin_config_page(await extension_links(request, check.via)))

    @app.get("/admin/static/{path:path}")
    async def admin_static(path: str) -> Response:
        if path == "lit-html.js":  # the vendored lit-html, for the admin's own templates
            return FileResponse(VENDOR_DIR / "lit-html" / "lit-html.js")
        if path == "codemirror.js":  # the vendored CodeMirror bundle (CORE-323)
            return FileResponse(ADMIN_STATIC_DIR / "vendor" / "codemirror" / "codemirror.js")
        if path.startswith("vendor/"):
            return Response(status_code=404)
        target = inside(ADMIN_STATIC_DIR, path)
        return Response(status_code=404) if target is None else FileResponse(target)

    @app.get("/admin/i18n.json")
    async def admin_i18n(request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return Response(status_code=401)
        lang, picked = await admin_language(request, check.via)
        roots: dict[str, Path | None] = {}
        if api is not None:
            for entry in api.engine.supervisor.registry.entries():
                if entry.cls is not None:
                    roots[entry.name] = module_catalog_root(entry.cls)
        return JSONResponse(
            {
                "locale": lang,
                "picked": picked,
                "languages": language_names(),
                "messages": admin_texts(lang),
                "forms": form_texts(lang, roots),
            }
        )

    @app.get("/admin")
    async def admin_home(request: Request) -> Response:
        check = await auth.check_admin(request, request.method)
        if check.outcome != "ok":
            return _login_redirect(request)
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


def socket_app(api: ApiServices) -> FastAPI:
    """Build the application of the local admin socket (CORE-311).

    It serves the REST API without a token, because only ``mm3`` and members
    of group ``mm3`` can open the socket; there are no ``/admin`` pages and no
    WebSockets.

    Args:
        api: What the REST API reads and controls.

    Returns:
        The application.

    Implements:
        CORE-311: Core spec 0.22, §10.5 Diagnostics and debugging.
    """
    app = _bare()
    _api_errors(app)
    router = api_router(None, api)
    app.include_router(router)
    schema = openapi_schema(router)

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi() -> Response:
        return JSONResponse(schema)

    @app.api_route("/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
    async def fallback(path: str) -> Response:
        return problem("CORE_API_NOT_FOUND", f"No route /{path}")

    return app
