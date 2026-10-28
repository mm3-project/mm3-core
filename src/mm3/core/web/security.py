"""Listener protection: source allow lists and security headers.

Both listeners always accept loopback; other sources need the listener's
allow list (``server.allow`` or ``admin.allow``). A refused source gets 403
(a WebSocket is closed before it is accepted) and is logged at info level,
at most once a minute per address. Every HTTP response carries the security
headers of CORE-63.

Implements:
    CORE-61: Core spec 0.11, §10.1 Security.
    CORE-63: Core spec 0.11, §10.1 Security.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Sequence
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mm3.core.logging import get_logger

__all__ = [
    "CSP",
    "CSP_PREVIEW",
    "PERMISSIONS_POLICY",
    "AllowList",
    "Network",
    "SecurityHeaders",
    "is_loopback",
    "security_headers",
]

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_CSP_BASE = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
)
CSP = _CSP_BASE + "frame-ancestors 'none'"
"""Content-Security-Policy of every response (CORE-63)."""

CSP_PREVIEW = _CSP_BASE + "frame-ancestors 'self'"
"""Content-Security-Policy of ``/admin/preview``, which the admin frames (CORE-63)."""

PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), usb=(), serial=(), bluetooth=(), payment=()"
)
"""Features denied to every page (CORE-63)."""

LOG_EVERY_S = 60.0
_log = get_logger(subsystem="web")


def is_loopback(host: str | None) -> bool:
    """Return True for a loopback client address (IPv4, IPv6 or IPv4-mapped IPv6).

    Args:
        host: Client address as the server reports it.

    Returns:
        Whether it is loopback; unknown or unparsable addresses are not.
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def _allowed(host: str | None, networks: Sequence[Network]) -> bool:
    if is_loopback(host):
        return True
    if not host or not networks:
        return False
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in networks)


class AllowList:
    """ASGI middleware that admits loopback plus the listener's allow list (CORE-61).

    Args:
        app: The application.
        networks: Returns the allow list now; read for every new connection,
            so a changed ``server.allow`` applies to connections made afterwards.
        listener: ``mirror`` or ``admin``, for the log.
        clock: Monotonic time source, for tests.

    Implements:
        CORE-61: Core spec 0.11, §10.1 Security.
    """

    def __init__(
        self,
        app: ASGIApp,
        networks: Callable[[], Sequence[Network]],
        *,
        listener: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.networks = networks
        self.listener = listener
        self.clock = clock
        self._logged: dict[str, float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse connections from sources that are not allowed."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else None
        if _allowed(host, self.networks()):
            await self.app(scope, receive, send)
            return
        self._log_refusal(host or "unknown", scope.get("path", ""))
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"Forbidden"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _log_refusal(self, host: str, path: str) -> None:
        now = self.clock()
        last = self._logged.get(host)
        if last is not None and now - last < LOG_EVERY_S:
            return
        self._logged[host] = now
        if len(self._logged) > 4096:
            self._logged = {h: t for h, t in self._logged.items() if now - t < LOG_EVERY_S}
        _log.info("web_source_refused", listener=self.listener, address=host, path=path)


def security_headers(path: str) -> list[tuple[bytes, bytes]]:
    """Return the security headers for a response to ``path`` (CORE-63).

    Args:
        path: Request path.

    Returns:
        Header pairs, without ``Cache-Control``.
    """
    preview = path == "/admin/preview" or path.startswith("/admin/preview/")
    return [
        (b"content-security-policy", (CSP_PREVIEW if preview else CSP).encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", PERMISSIONS_POLICY.encode()),
    ]


class SecurityHeaders:
    """ASGI middleware adding the headers of CORE-63 to every HTTP response.

    ``Cache-Control: no-store`` is added unless the route set its own
    ``Cache-Control`` (versioned static files, CORE-252).

    Args:
        app: The application.

    Implements:
        CORE-63: Core spec 0.11, §10.1 Security.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the headers to the response start message."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers(scope.get("path", ""))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[Any] = list(message.get("headers", []))
                names = {bytes(k).lower() for k, _ in headers}
                headers.extend((k, v) for k, v in extra if k not in names)
                if b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
