"""The token store: named admin and display tokens as argon2 hashes.

Tokens live in ``<state>/tokens.json`` (mode 0600), never in the
configuration. Each record holds the token's name, kind (``admin`` or
``display``), its argon2 hash, and creation and last-use times. A token is
shown once, when it is created; it has the form ``mm3_<id>_<secret>``, where
``<id>`` picks the record so that verifying a token costs one argon2 check.

The engine and ``mm3 token`` change the file under one lock
(``<state>/tokens.lock``, flock) and the engine re-reads it when it changes,
so ``mm3 token revoke`` takes effect at once for a running engine. This is
the single adapter for argon2-cffi (core spec 10.9).

Implements:
    CORE-62: Core spec 0.11, §10.1 Security.
    -: Core spec 0.11, §10.9 Dependency management.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = [
    "LAST_USE_RESOLUTION",
    "NAME_PATTERN",
    "TOKENS_FILE",
    "TokenError",
    "TokenKind",
    "TokenRecord",
    "TokenStore",
]

TOKENS_FILE = "tokens.json"
LOCK_FILE = "tokens.lock"
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
LAST_USE_RESOLUTION = timedelta(minutes=1)
"""Last-use times are written at most this often per token."""

TokenKind = Literal["admin", "display"]

_NAME_RE = re.compile(NAME_PATTERN)
_TOKEN_RE = re.compile(r"^mm3_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
_hasher = PasswordHasher()


class TokenError(ValueError):
    """A token operation that cannot be done (bad or duplicate name, unknown token)."""


@dataclass(frozen=True)
class TokenRecord:
    """One stored token; the secret itself is never kept.

    Attributes:
        id: Public part of the token, used to find the record.
        name: Name given with ``mm3 token new``.
        kind: ``admin`` or ``display``.
        hash: argon2 hash of the whole token.
        created: Creation time (UTC, ISO 8601).
        last_used: Last successful use (UTC, ISO 8601), ``None`` if never used.
    """

    id: str
    name: str
    kind: TokenKind
    hash: str
    created: str
    last_used: str | None = None

    def public(self) -> dict[str, Any]:
        """Return the record without its hash, for ``mm3 token list``."""
        data = asdict(self)
        data.pop("hash")
        return data


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


class TokenStore:
    """Named tokens in ``<state>/tokens.json``.

    Args:
        state_dir: Engine state directory (core spec 3.1).
        now: Clock, for tests.

    Implements:
        CORE-62: Core spec 0.11, §10.1 Security.
    """

    def __init__(self, state_dir: Path, *, now: Callable[[], datetime] = _now) -> None:
        self.path = state_dir / TOKENS_FILE
        self._lock_path = state_dir / LOCK_FILE
        self._now = now
        self._mutex = threading.Lock()
        self._records: dict[str, TokenRecord] = {}
        self._stamp: tuple[int, int, int] | None = None

    # --- file -------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._mutex:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _load(self) -> dict[str, TokenRecord]:
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return self._records
        records: dict[str, TokenRecord] = {}
        if stamp is not None:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            for item in raw.get("tokens", []):
                rec = TokenRecord(**item)
                records[rec.id] = rec
        self._records, self._stamp = records, stamp
        return records

    def _save(self, records: dict[str, TokenRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"tokens": [asdict(r) for r in records.values()]}, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._records, self._stamp = dict(records), self._file_stamp()

    # --- operations -------------------------------------------------------------

    def new(self, name: str, kind: TokenKind = "admin") -> str:
        """Create a token and return it; it is never shown again.

        Args:
            name: Unique name, ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``.
            kind: ``admin`` or ``display`` (CORE-254).

        Returns:
            The token.

        Raises:
            TokenError: The name is invalid or already used.
        """
        if not _NAME_RE.fullmatch(name):
            raise TokenError(f"invalid token name {name!r}: use {NAME_PATTERN}")
        with self._locked():
            records = dict(self._load())
            if any(r.name == name for r in records.values()):
                raise TokenError(f"a token named {name!r} already exists")
            tid = secrets.token_hex(6)
            while tid in records:
                tid = secrets.token_hex(6)
            token = f"mm3_{tid}_{secrets.token_urlsafe(32)}"
            records[tid] = TokenRecord(
                id=tid, name=name, kind=kind, hash=_hasher.hash(token), created=_iso(self._now())
            )
            self._save(records)
        return token

    def all(self) -> list[TokenRecord]:
        """Return every token, oldest first."""
        with self._locked():
            return sorted(self._load().values(), key=lambda r: r.created)

    def revoke(self, name: str) -> TokenRecord:
        """Remove a token by name; sessions made with it end at once (CORE-62).

        Args:
            name: Token name.

        Returns:
            The removed record.

        Raises:
            TokenError: No token has that name.
        """
        with self._locked():
            records = dict(self._load())
            for tid, rec in records.items():
                if rec.name == name:
                    del records[tid]
                    self._save(records)
                    return rec
        raise TokenError(f"no token named {name!r}")

    def get(self, token_id: str) -> TokenRecord | None:
        """Return the record with this id if it still exists (re-reads a changed file).

        Args:
            token_id: Record id.

        Returns:
            The record, or ``None`` when it was revoked.
        """
        with self._mutex:
            return self._load().get(token_id)

    def verify(self, token: str, kind: TokenKind | None = None) -> TokenRecord | None:
        """Check a token and record its use.

        This runs one argon2 verification; call it off the event loop.

        Args:
            token: The token as presented.
            kind: Required kind; ``None`` accepts any.

        Returns:
            The matching record, or ``None`` when the token is unknown,
            revoked, wrong or of another kind.
        """
        match = _TOKEN_RE.match(token.strip())
        if match is None:
            return None
        rec = self.get(match.group(1))
        if rec is None or (kind is not None and rec.kind != kind):
            return None
        try:
            _hasher.verify(rec.hash, token.strip())
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        self.touch(rec.id)
        return self.get(rec.id) or rec

    def touch(self, token_id: str) -> None:
        """Record a use of a token, at most once a minute per token.

        Args:
            token_id: Record id.
        """
        now = self._now()
        rec = self.get(token_id)
        if rec is None:
            return
        if rec.last_used is not None and now - _parse(rec.last_used) < LAST_USE_RESOLUTION:
            return
        with self._locked():
            records = dict(self._load())
            if token_id in records:
                records[token_id] = replace(records[token_id], last_used=_iso(now))
                self._save(records)
