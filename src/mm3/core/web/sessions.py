"""Admin and display sessions, and the login lockout.

``/admin/login`` exchanges an admin token for an admin session; ``/pair``
exchanges a display token for a display session. A session is a random id
held in an HttpOnly, SameSite=Strict cookie; the engine keeps only its
SHA-256 in ``<state>/sessions.json`` (0600), so sessions survive a restart.
An admin session is valid for 30 days from its last use; a display session
until its token is revoked. Every check also asks the token store whether
the token still exists, so a revoked token ends its sessions at once.

Implements:
    CORE-253: Core spec 0.12, §10.1 Security.
    CORE-254: Core spec 0.12, §10.1 Security.
    CORE-62: Core spec 0.12, §10.1 Security.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mm3.core.web.tokens import TokenKind, TokenRecord, TokenStore

__all__ = [
    "ADMIN_COOKIE",
    "ADMIN_SESSION_TTL",
    "DISPLAY_COOKIE",
    "LOCKOUT",
    "LOGIN_FAILURES",
    "SESSIONS_FILE",
    "LoginLimiter",
    "Session",
    "SessionStore",
]

SESSIONS_FILE = "sessions.json"
ADMIN_COOKIE = "mm3_admin"
DISPLAY_COOKIE = "mm3_display"
ADMIN_SESSION_TTL = timedelta(days=30)
LOGIN_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
_TOUCH_EVERY = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    """A session as stored: the token it came from and its times.

    Attributes:
        token_id: Id of the token that opened it.
        kind: ``admin`` or ``display``.
        created: Creation time (UTC, ISO 8601).
        last_used: Last use (UTC, ISO 8601).
    """

    token_id: str
    kind: TokenKind
    created: str
    last_used: str


class SessionStore:
    """Sessions in ``<state>/sessions.json``, checked against the token store.

    Args:
        state_dir: Engine state directory.
        tokens: The token store.
        now: Clock, for tests.

    Implements:
        CORE-253: Core spec 0.12, §10.1 Security.
        CORE-254: Core spec 0.12, §10.1 Security.
    """

    def __init__(
        self, state_dir: Path, tokens: TokenStore, *, now: Callable[[], datetime] = _now
    ) -> None:
        self.path = state_dir / SESSIONS_FILE
        self.tokens = tokens
        self._now = now
        self._mutex = threading.Lock()
        self._sessions: dict[str, Session] = self._read()

    def _read(self) -> dict[str, Session]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return {}
        return {k: Session(**v) for k, v in raw.get("sessions", {}).items()}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"sessions": {k: asdict(v) for k, v in self._sessions.items()}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def create(self, token: TokenRecord) -> str:
        """Open a session for a verified token.

        Args:
            token: The record the token matched.

        Returns:
            The session id for the cookie; only its hash is stored.
        """
        sid = secrets.token_urlsafe(32)
        stamp = _iso(self._now())
        with self._mutex:
            self._sessions[_digest(sid)] = Session(token.id, token.kind, stamp, stamp)
            self._prune()
            self._write()
        return sid

    def check(self, session_id: str | None, kind: TokenKind) -> TokenRecord | None:
        """Return the token behind a valid session of the given kind.

        A valid admin session is younger than 30 days since its last use; any
        session ends when its token is revoked. Using a session refreshes
        its last use (written at most once an hour).

        Args:
            session_id: Value of the session cookie.
            kind: ``admin`` for the admin listener, ``display`` for the mirror.

        Returns:
            The token record, or ``None`` when the session is not valid.
        """
        if not session_id:
            return None
        key = _digest(session_id)
        now = self._now()
        with self._mutex:
            sess = self._sessions.get(key)
            if sess is None or sess.kind != kind:
                return None
            token = self.tokens.get(sess.token_id)
            expired = kind == "admin" and now - _parse(sess.last_used) > ADMIN_SESSION_TTL
            if token is None or token.kind != kind or expired:
                del self._sessions[key]
                self._write()
                return None
            if now - _parse(sess.last_used) >= _TOUCH_EVERY:
                self._sessions[key] = replace(sess, last_used=_iso(now))
                self._write()
        self.tokens.touch(token.id)
        return token

    def end(self, session_id: str | None) -> None:
        """End a session (logout).

        Args:
            session_id: Value of the session cookie.
        """
        if not session_id:
            return
        with self._mutex:
            if self._sessions.pop(_digest(session_id), None) is not None:
                self._write()

    def _prune(self) -> None:
        now = self._now()
        for key, sess in list(self._sessions.items()):
            token = self.tokens.get(sess.token_id)
            expired = sess.kind == "admin" and now - _parse(sess.last_used) > ADMIN_SESSION_TTL
            if token is None or expired:
                del self._sessions[key]


class LoginLimiter:
    """Locks logins from an address for 15 minutes after five failures (CORE-253).

    Args:
        now: Clock, for tests.

    Implements:
        CORE-253: Core spec 0.12, §10.1 Security.
    """

    def __init__(self, *, now: Callable[[], datetime] = _now) -> None:
        self._now = now
        self._failures: dict[str, list[datetime]] = {}
        self._locked_until: dict[str, datetime] = {}

    def locked(self, address: str) -> bool:
        """Return True while logins from this address are locked.

        Args:
            address: Client address.

        Returns:
            Whether the address is locked out.
        """
        until = self._locked_until.get(address)
        if until is None:
            return False
        if self._now() >= until:
            del self._locked_until[address]
            return False
        return True

    def failed(self, address: str) -> None:
        """Record a failed login; the fifth within 15 minutes locks the address.

        Args:
            address: Client address.
        """
        now = self._now()
        recent = [t for t in self._failures.get(address, []) if now - t < LOCKOUT]
        recent.append(now)
        if len(recent) >= LOGIN_FAILURES:
            self._locked_until[address] = now + LOCKOUT
            recent = []
        self._failures[address] = recent

    def succeeded(self, address: str) -> None:
        """Forget earlier failures of an address after a successful login.

        Args:
            address: Client address.
        """
        self._failures.pop(address, None)
