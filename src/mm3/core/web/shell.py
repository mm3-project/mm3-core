"""The shell page and the files it loads.

``/`` serves the shell; ``/core/…`` the engine's scripts, theme and
vendored libraries (lit-html, Roboto Condensed); ``/modules/<module>/
<version>/…`` the ``static/`` directory of an installed module;
``/appearance/<instance>.css`` an instance's ``appearance.css``; and
``/custom.css`` the user's stylesheet. The admin listener serves the same
shell in preview mode at ``/admin/preview`` and the same files, so the
preview loads exactly what the kiosk loads.

Implements:
    CORE-06: Core spec 0.14, §3 Engine components.
    CORE-252: Core spec 0.14, §5.6 WebSocket protocol.
    CORE-261, CORE-262: Core spec 0.14, §6.10 Theme and custom CSS.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

from mm3.core.config.validate import ConfigResult
from mm3.core.web.view import ShellView, content_version

__all__ = [
    "IMMUTABLE",
    "STATIC_DIR",
    "VENDOR_DIR",
    "ShellSources",
    "add_shell_files",
    "inside",
    "shell_page",
]

STATIC_DIR = Path(__file__).parent / "static"
"""Engine files served under ``/core/``."""
VENDOR_DIR = Path(__file__).parent / "vendor"
"""Vendored front-end libraries served under ``/core/vendor/`` (core spec 10.9)."""

IMMUTABLE = "public, max-age=31536000, immutable"
"""``Cache-Control`` of versioned static files (CORE-252)."""

ShellMode = Literal["mirror", "preview"]


@dataclass(frozen=True)
class ShellSources:
    """Where the shell's files come from.

    Attributes:
        view: What shells are told now (module files, appearance CSS).
        current: The running configuration (custom.css).
    """

    view: Callable[[], ShellView]
    current: Callable[[], ConfigResult]


def shell_page(mode: ShellMode) -> str:
    """Return the shell page: no inline script or style (CORE-63).

    Args:
        mode: ``mirror`` for ``/``, ``preview`` for ``/admin/preview``.

    Returns:
        The HTML.
    """
    return (
        "<!doctype html>\n"
        f'<html lang="en" data-mode="{mode}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>MM3</title>\n"
        '<link rel="icon" href="data:,">\n'
        '<link rel="stylesheet" href="/core/theme.css">\n'
        '<link rel="stylesheet" href="/core/shell.css">\n'
        '<script type="module" src="/core/shell.js"></script>\n'
        '</head>\n<body>\n<main id="mm3-grid"></main>\n</body>\n</html>\n'
    )


def inside(base: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``base``; ``None`` when it escapes or is not a file."""
    try:
        target = (base / rel).resolve()
        root = base.resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def add_shell_files(app: FastAPI, sources: ShellSources) -> None:
    """Add the shell's files: ``/core``, ``/modules``, ``/appearance``, ``/custom.css``.

    ``/`` and ``/admin/preview`` are added by the listener applications.

    Args:
        app: The mirror or the admin application.
        sources: The view and the running configuration.

    Implements:
        CORE-252: Core spec 0.14, §5.6 WebSocket protocol.
        CORE-262: Core spec 0.14, §6.10 Theme and custom CSS.
    """
    view, current = sources.view, sources.current

    def not_found() -> Response:
        return Response(status_code=404)

    @app.get("/core/vendor/{path:path}")
    async def vendor(path: str) -> Response:
        target = inside(VENDOR_DIR, path)
        if target is None or target.name in ("vendor.json", "LICENSE"):
            return not_found()
        return FileResponse(target)

    @app.get("/core/{path:path}")
    async def core(path: str) -> Response:
        target = inside(STATIC_DIR, path)
        return not_found() if target is None else FileResponse(target)

    @app.get("/modules/{module}/{version}/{path:path}")
    async def module_file(module: str, version: str, path: str) -> Response:
        files = view().files.get(module)
        if files is None or files.static is None or files.version != version:
            return not_found()
        target = inside(files.static, path)
        if target is None:
            return not_found()
        return FileResponse(target, headers={"Cache-Control": IMMUTABLE})

    @app.get("/appearance/{name}")
    async def appearance(name: str, request: Request) -> Response:
        if not name.endswith(".css"):
            return not_found()
        inst = view().instance(name.removesuffix(".css"))
        css = inst.appearance_css if inst is not None else None
        if css is None:
            return not_found()
        versioned = request.query_params.get("v") == content_version(css)
        headers = {"Cache-Control": IMMUTABLE} if versioned else {}
        return Response(css, media_type="text/css", headers=headers)

    @app.get("/custom.css")
    async def custom_css(request: Request) -> Response:
        data = current().assets.custom_css
        if data is None:
            return not_found()
        versioned = request.query_params.get("v") == content_version(data)
        headers = {"Cache-Control": IMMUTABLE} if versioned else {}
        return Response(data, media_type="text/css", headers=headers)
