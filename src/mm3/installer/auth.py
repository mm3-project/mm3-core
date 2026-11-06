"""Re-authentication: the install passphrase, TOTP and the lock.

The passphrase's argon2 hash and the TOTP secret live in
``/var/lib/mm3-install/auth.json`` (mode 0600) and are set only from a root
peer (CORE-369). Every listed operation needs the passphrase, and the TOTP code
when enabled, each code good once (CORE-132, CORE-369); five failures lock the
installer for 15 minutes. The failures and the lock are kept in ``lock.json``
so a restart does not reset them. This is the single adapter for pyotp (core spec 10.9).

Implements:
    CORE-132: Core spec 0.33, §10.7 Installer service.
    CORE-369: Core spec 0.37, §10.7 Installer service.
"""

from __future__ import annotations

import hmac
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp

from mm3.core.web.tokens import hash_secret, verify_secret
from mm3.installer.files import read_json, write_json
from mm3.installer.paths import Layout

__all__ = [
    "LOCK_AFTER",
    "LOCK_FOR",
    "MIN_PASSPHRASE",
    "Auth",
    "AuthError",
    "LockedError",
    "TotpSetup",
]

LOCK_AFTER = 5
"""Failures that lock the installer (CORE-132)."""
LOCK_FOR = timedelta(minutes=15)
MIN_PASSPHRASE = 12
"""Shortest passphrase accepted by ``mm3 installer passphrase``."""
ISSUER = "MM3 installer"


class AuthError(PermissionError):
    """Re-authentication failed or is not set up; the message says why."""


class LockedError(PermissionError):
    """The installer is locked after too many failures.

    Args:
        until: When the lock ends.
    """

    def __init__(self, until: datetime) -> None:
        super().__init__(f"installer locked until {until.isoformat()}")
        self.until = until


@dataclass(frozen=True)
class TotpSetup:
    """A TOTP secret waiting for its first code (CORE-369)."""

    secret: str
    uri: str


def totp_step(secret: str, code: str, now: datetime) -> int | None:
    """The time step whose code is ``code`` (one step of clock drift either way).

    Returns:
        The step, or None when the code matches none of them.
    """
    otp = pyotp.TOTP(secret)
    current = otp.timecode(now)
    code = code.strip()
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(otp.generate_otp(step), code):
            return step
    return None


def _now() -> datetime:
    return datetime.now(UTC)


class Auth:
    """The passphrase, TOTP and failure lock.

    Args:
        layout: The installer's paths.
        now: Clock, for tests.
    """

    def __init__(self, layout: Layout, *, now: Callable[[], datetime] = _now) -> None:
        self._auth = layout.auth_file
        self._lock = layout.lock_file
        self._now = now
        self._mutex = threading.Lock()

    # --- files -------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        data = read_json(self._auth, {})
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        write_json(self._auth, data, mode=0o600)

    def _failures(self) -> tuple[int, datetime | None]:
        data = read_json(self._lock, {})
        until = data.get("locked_until") if isinstance(data, dict) else None
        count = int(data.get("failures", 0)) if isinstance(data, dict) else 0
        return count, datetime.fromisoformat(until) if until else None

    def _set_failures(self, count: int, until: datetime | None) -> None:
        write_json(
            self._lock,
            {"failures": count, "locked_until": until.isoformat() if until else None},
            mode=0o600,
        )

    # --- state -------------------------------------------------------------------

    @property
    def has_passphrase(self) -> bool:
        """Whether a passphrase is set."""
        return bool(self._read().get("passphrase"))

    @property
    def totp_enabled(self) -> bool:
        """Whether TOTP codes are required."""
        return bool(self._read().get("totp"))

    def locked_until(self) -> datetime | None:
        """The end of the current lock, or None."""
        _, until = self._failures()
        return until if until and until > self._now() else None

    # --- setup (root peers only; the server checks the peer) ----------------------

    def set_passphrase(self, passphrase: str) -> None:
        """Set or replace the passphrase.

        Raises:
            ValueError: Shorter than :data:`MIN_PASSPHRASE` characters.
        """
        if len(passphrase) < MIN_PASSPHRASE:
            raise ValueError(f"the passphrase needs at least {MIN_PASSPHRASE} characters")
        with self._mutex:
            data = self._read()
            data["passphrase"] = hash_secret(passphrase)
            self._write(data)

    def begin_totp(self) -> TotpSetup:
        """Start enabling TOTP: a new secret, active only after :meth:`confirm_totp`."""
        secret = pyotp.random_base32()
        with self._mutex:
            data = self._read()
            data["totp_pending"] = secret
            self._write(data)
        uri = pyotp.TOTP(secret).provisioning_uri(name="mm3", issuer_name=ISSUER)
        return TotpSetup(secret, uri)

    def confirm_totp(self, code: str) -> None:
        """Enable TOTP once a valid code for the pending secret arrives.

        Raises:
            AuthError: No pending secret, or a wrong code.
        """
        with self._mutex:
            data = self._read()
            pending = data.get("totp_pending")
            if not pending:
                raise AuthError("no TOTP setup is pending; start with totp enable")
            step = totp_step(pending, code, self._now())
            if step is None:
                raise AuthError("wrong TOTP code")
            data["totp"] = pending
            data["totp_last_step"] = step
            del data["totp_pending"]
            self._write(data)

    def disable_totp(self) -> None:
        """Stop requiring TOTP codes."""
        with self._mutex:
            data = self._read()
            data.pop("totp", None)
            data.pop("totp_pending", None)
            data.pop("totp_last_step", None)
            self._write(data)

    # --- checks --------------------------------------------------------------------

    def check(self, passphrase: str | None, totp: str | None) -> None:
        """Re-authenticate one operation (CORE-132).

        Runs one argon2 verification; call it off the event loop.

        Raises:
            LockedError: Locked after too many failures.
            AuthError: No passphrase set, or a wrong passphrase or code (counted).
        """
        with self._mutex:
            now = self._now()
            count, until = self._failures()
            if until and until > now:
                raise LockedError(until)
            data = self._read()
            stored = data.get("passphrase")
            if not stored:
                raise AuthError(
                    "no install passphrase is set; run: "
                    "sudo /opt/mm3/installer/bin/mm3 installer passphrase"
                )
            ok = bool(passphrase) and verify_secret(stored, passphrase or "")
            secret = data.get("totp")
            step: int | None = None
            if ok and secret:
                step = totp_step(secret, totp or "", now)
                # A code is good once: not for its own step again, nor an older one.
                ok = step is not None and step > int(data.get("totp_last_step", -1))
            if ok:
                if step is not None:
                    data["totp_last_step"] = step
                    self._write(data)
                if count or until:
                    self._set_failures(0, None)
                return
            count = 0 if until and until <= now else count
            count += 1
            if count >= LOCK_AFTER:
                self._set_failures(0, now + LOCK_FOR)
                raise LockedError(now + LOCK_FOR)
            self._set_failures(count, None)
            raise AuthError("wrong passphrase or TOTP code")
