"""Who is calling: admin tokens, admin sessions and display sessions.

The admin listener accepts an admin token as ``Authorization: Bearer`` or
an admin session cookie. Requests with an unsafe method that rely on the
session cookie (and the login and logout forms) must carry an ``Origin``
header that matches the listener, so another site cannot make the browser
act for the admin. The mirror listener knows display sessions only; a
display token or session grants nothing on the admin listener.

Implements:
    CORE-62: Core spec 0.11, §10.1 Security.
    CORE-253: Core spec 0.11, §10.1 Security.
    CORE-254: Core spec 0.11, §10.1 Security.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from starlette.requests import HTTPConnection

from mm3.core.web.sessions import (
    ADMIN_COOKIE,
    DISPLAY_COOKIE,
    LoginLimiter,
    SessionStore,
)
from mm3.core.web.tokens import TokenRecord, TokenStore

__all__ = ["SAFE_METHODS", "AdminCheck", "WebAuth", "origin_matches"]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

AdminOutcome = Literal["ok", "missing", "invalid", "origin"]


def origin_matches(conn: HTTPConnection) -> bool:
    """Return True when the ``Origin`` header names this listener.

    Args:
        conn: The request or WebSocket.

    Returns:
        Whether ``Origin`` equals ``<scheme>://<Host header>``.

    Implements:
        CORE-253: Core spec 0.11, §10.1 Security.
    """
    origin = conn.headers.get("origin")
    host = conn.headers.get("host")
    if not origin or not host:
        return False
    scheme = "https" if conn.url.scheme in ("https", "wss") else "http"
    return origin.rstrip("/").lower() == f"{scheme}://{host}".lower()


@dataclass(frozen=True)
class AdminCheck:
    """Outcome of checking a request on the admin listener.

    Attributes:
        outcome: ``ok``; ``missing`` (no token or session); ``invalid`` (a
            bearer token that does not match); ``origin`` (a session used by
            an unsafe request without a matching ``Origin``).
        token: The admin token behind the request when ``ok``.
        via: ``bearer`` or ``session`` when ``ok``.
    """

    outcome: AdminOutcome
    token: TokenRecord | None = None
    via: str | None = None


@dataclass
class WebAuth:
    """Token store, sessions and login lockout shared by both listeners.

    Attributes:
        tokens: Named tokens (CORE-62).
        sessions: Admin and display sessions (CORE-253, CORE-254).
        limiter: Login lockout (CORE-253).
    """

    tokens: TokenStore
    sessions: SessionStore
    limiter: LoginLimiter = field(default_factory=LoginLimiter)

    @classmethod
    def open(cls, state_dir: Path) -> WebAuth:
        """Open the stores in the engine's state directory.

        Args:
            state_dir: Engine state directory.

        Returns:
            The shared auth objects.
        """
        tokens = TokenStore(state_dir)
        return cls(tokens, SessionStore(state_dir, tokens))

    async def check_admin(self, conn: HTTPConnection, method: str = "GET") -> AdminCheck:
        """Check a request or WebSocket on the admin listener (CORE-62, CORE-253).

        Args:
            conn: The request or WebSocket.
            method: HTTP method; WebSockets pass ``GET``.

        Returns:
            The outcome.

        Implements:
            CORE-62: Core spec 0.11, §10.1 Security.
            CORE-253: Core spec 0.11, §10.1 Security.
        """
        auth = conn.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            rec = await asyncio.to_thread(self.tokens.verify, value.strip(), "admin")
            if rec is None:
                return AdminCheck("invalid")
            return AdminCheck("ok", rec, "bearer")
        sid = conn.cookies.get(ADMIN_COOKIE)
        if not sid:
            return AdminCheck("missing")
        rec = await asyncio.to_thread(self.sessions.check, sid, "admin")
        if rec is None:
            return AdminCheck("missing")
        if method.upper() not in SAFE_METHODS and not origin_matches(conn):
            return AdminCheck("origin")
        return AdminCheck("ok", rec, "session")

    async def display_session(self, conn: HTTPConnection) -> TokenRecord | None:
        """Return the display token behind the mirror listener's session cookie.

        Args:
            conn: The request or WebSocket.

        Returns:
            The token record, or ``None`` without a valid display session.

        Implements:
            CORE-254: Core spec 0.11, §10.1 Security.
        """
        sid = conn.cookies.get(DISPLAY_COOKIE)
        if not sid:
            return None
        return await asyncio.to_thread(self.sessions.check, sid, "display")
