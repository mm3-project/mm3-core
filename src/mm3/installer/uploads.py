"""Backup archives uploaded in the admin, staged until a restore uses them (CORE-361).

The engine streams an uploaded archive to ``POST /v1/backups/upload``; the installer writes it
into ``uploads/`` in its own state directory (mode 0600, as a temporary file and a rename), so
only the installer ever holds the bytes and a restore reads them from there. A staged file is
named by 32 hexadecimal characters. At most :data:`MAX_STAGED` files are kept: a new upload
removes the oldest first. A file older than :data:`MAX_AGE_S` is removed at each upload and at
start, and a restore that used one removes it when it ends.

Implements:
    CORE-361: Core spec 0.45, §10.8 Backup and restore.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from mm3.installer.backup import ARCHIVE_SUFFIX
from mm3.installer.paths import Layout

__all__ = ["MAX_AGE_S", "MAX_STAGED", "MAX_UPLOAD", "UPLOAD_ID", "Staging", "Uploads"]

MAX_UPLOAD = 2 * 1024**3
"""The largest archive the installer accepts, 2 GiB (CORE-361)."""
MAX_STAGED = 2
"""Staged files kept at once."""
MAX_AGE_S = 24 * 3600
"""Seconds after which a staged file is removed."""
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
"""The form of a staged upload's id."""
_TMP = ".part"


class Staging:
    """One upload being written: a temporary file that becomes a staged file on :meth:`commit`.

    Args:
        directory: The ``uploads/`` directory.
        file: The open temporary file.
        path: The temporary file's path.
    """

    def __init__(self, directory: Path, file: BinaryIO, path: Path) -> None:
        self._dir = directory
        self._file = file
        self._path = path
        self.size = 0

    def write(self, chunk: bytes) -> None:
        """Append ``chunk``."""
        self._file.write(chunk)
        self.size += len(chunk)

    def commit(self) -> str:
        """Close and rename the file; return its id."""
        self._file.close()
        upload_id = secrets.token_hex(16)
        os.rename(self._path, self._dir / f"{upload_id}{ARCHIVE_SUFFIX}")
        return upload_id

    def abort(self) -> None:
        """Close and delete the temporary file."""
        with contextlib.suppress(OSError):
            self._file.close()
        with contextlib.suppress(OSError):
            self._path.unlink()


class Uploads:
    """The staged uploads.

    Args:
        layout: The installer's paths.
        now: The wall clock in seconds, for tests.
    """

    def __init__(self, layout: Layout, now: Callable[[], float] = time.time) -> None:
        self._dir = layout.state / "uploads"
        self._now = now

    @property
    def directory(self) -> Path:
        """The ``uploads/`` directory."""
        return self._dir

    def begin(self) -> Staging:
        """Start an upload: make room, then open a temporary file (mode 0600, never a link)."""
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cleanup(keep_free=1)
        path = self._dir / f"{secrets.token_hex(8)}{_TMP}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return Staging(self._dir, os.fdopen(fd, "wb"), path)

    def path(self, upload_id: str) -> Path | None:
        """The staged file for an id, or ``None`` when the id is malformed or unknown."""
        if not UPLOAD_ID.match(upload_id):
            return None
        path = self._dir / f"{upload_id}{ARCHIVE_SUFFIX}"
        return path if path.is_file() else None

    def discard(self, upload_id: str) -> None:
        """Delete a staged file (after a restore that used it)."""
        path = self.path(upload_id)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    def cleanup(self, *, keep_free: int = 0, crashed: bool = False) -> None:
        """Delete stale uploads.

        Removes files older than :data:`MAX_AGE_S` and the oldest files beyond
        ``MAX_STAGED - keep_free``. At start (``crashed``) every temporary file is a leftover of
        a crash and goes too; later one may be an upload still being written, and stays.
        """
        if not self._dir.is_dir():
            return
        entries: list[tuple[float, Path]] = []
        for path in self._dir.iterdir():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if (crashed and path.name.endswith(_TMP)) or self._now() - mtime > MAX_AGE_S:
                with contextlib.suppress(OSError):
                    path.unlink()
            elif path.name.endswith(ARCHIVE_SUFFIX):
                entries.append((mtime, path))
        entries.sort()
        while len(entries) > MAX_STAGED - keep_free:
            _mtime, oldest = entries.pop(0)
            with contextlib.suppress(OSError):
                oldest.unlink()
