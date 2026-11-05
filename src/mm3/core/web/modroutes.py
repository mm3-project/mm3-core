"""Module routes: what a module registers on ``ctx.router`` (CORE-332, MOD-74).

Requests to ``/api/v1/modules/<instance id>/<path>`` that no engine route
answers come here, after the API's own authentication. The instance's
routes are served from the end of ``start()`` until it stops; before that,
and once it has stopped, the path answers 503 ``CORE_API_UNAVAILABLE``. An
instance whose module does not declare ``api.routes`` has no such paths
(404). The engine's own sub-paths of an instance stay the engine's: a module
route on one of them is dropped with a warning, once.

Each request runs the module's routes as a small FastAPI application on
the event loop under the handler budget; a handler over budget is cancelled,
counted like a handler timeout (CORE-193) and answered with 504
``CORE_API_REQUEST_TIMEOUT``. An exception in a handler answers 500 with an
empty body and is logged; the instance keeps running.

Implements:
    CORE-332: Core spec 0.29, §9.1 Remote-control API.
    MOD-74: Module Contract 1.0 rev 13, §8 Capabilities and grants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response
from starlette.types import Message

from mm3.core.logging import get_logger
from mm3.core.web.problems import ApiError

if TYPE_CHECKING:
    from mm3.core.supervisor import ManagedInstance, Supervisor

__all__ = ["RESERVED", "ModuleRoutes"]

RESERVED = frozenset({"health", "show", "hide", "restart", "stop", "start", "log-level"})
"""The engine's own sub-paths of an instance (CORE-332)."""

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _first_segment(path: str) -> str:
    return path.lstrip("/").split("/", 1)[0]


class ModuleRoutes:
    """Serve the routes of every instance (one application per router).

    Args:
        supervisor: The instances.

    Implements:
        CORE-332: Core spec 0.29, §9.1 Remote-control API.
    """

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor

    def application(self, inst: ManagedInstance, router: APIRouter) -> FastAPI:
        """The application for one router, built once; reserved paths are dropped."""
        # Kept on the router itself, so it goes away with the incarnation.
        app: FastAPI | None = getattr(router, "_mm3_app", None)
        if app is not None and getattr(app, "_mm3_count", -1) == len(router.routes):
            return app  # rebuilt only when the module added routes
        kept = APIRouter()
        for route in router.routes:
            path = getattr(route, "path", "")
            if _first_segment(path) in RESERVED:
                get_logger(inst.spec.module, inst.instance_id).warning(
                    "route_reserved_path", path=path
                )
                continue
            kept.routes.append(route)
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        app.include_router(kept)
        app._mm3_count = len(router.routes)  # type: ignore[attr-defined]
        router._mm3_app = app  # type: ignore[attr-defined]
        return app

    async def dispatch(self, iid: str, rest: str, request: Request) -> Response:
        """Answer one request for ``/api/v1/modules/<iid>/<rest>``.

        Args:
            iid: Instance id.
            rest: The path below the instance prefix.
            request: The request (body not read yet).

        Returns:
            The module's answer.

        Raises:
            ApiError: Unknown instance or path (404), routes not live (503),
                handler over budget (504).
        """
        sup = self.supervisor
        try:
            inst = sup.get(iid)
        except KeyError:
            raise ApiError("CORE_API_NOT_FOUND", f"No instance {iid}") from None
        entry = sup.registry.get(inst.spec.module)
        cls = entry.cls if entry is not None else None
        if cls is None or "api.routes" not in cls.capabilities or _first_segment(rest) in RESERVED:
            raise ApiError("CORE_API_NOT_FOUND", f"No route {rest} for instance {iid}")
        router = inst.routes()
        if router is None:
            raise ApiError("CORE_API_UNAVAILABLE", f"Instance {iid} is not running")
        app = self.application(inst, router)
        scope: dict[str, Any] = dict(request.scope)
        scope["path"] = "/" + rest
        scope["raw_path"] = ("/" + rest).encode()
        scope["root_path"] = ""
        status = 500
        headers: list[tuple[bytes, bytes]] = []
        body = bytearray()

        async def send(message: Message) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        budget = sup.settings.timeouts.handler
        cm = sup.clock.timeout(budget)
        try:
            async with cm:
                await app(scope, request.receive, send)
        except TimeoutError:
            if not cm.expired():
                raise
            inst.route_finished(timed_out=True)
            raise ApiError(
                "CORE_API_REQUEST_TIMEOUT", f"The route did not answer within {budget} s"
            ) from None
        except Exception as exc:
            inst.route_finished(timed_out=False)
            get_logger(inst.spec.module, inst.instance_id).exception(
                "route_error", method=request.method, path="/" + rest, error=type(exc).__name__
            )
            return Response(status_code=500)
        inst.route_finished(timed_out=False)
        out = Response(content=bytes(body), status_code=status)
        out.raw_headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
        out.headers["content-length"] = str(len(body))
        return out


def register(router: APIRouter, routes: ModuleRoutes) -> None:
    """Add the catch-all route for module routes to the API router (last).

    Args:
        router: The ``/api/v1`` router, after the engine's own routes.
        routes: The dispatcher.
    """

    @router.api_route("/modules/{iid}/{rest:path}", methods=_METHODS, include_in_schema=False)
    async def module_route(iid: str, rest: str, request: Request) -> Response:
        return await routes.dispatch(iid, rest, request)
