"""Small file helpers: atomic writes, owners and read-only trees.

Files the installer writes for the engine keep owner ``mm3`` and their modes
(CORE-359); its own state is mode 0600/0700 and owned by ``mm3-install``.

Implements:
    CORE-359: Core spec 0.35, §10.8 Backup and restore.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["Owner", "atomic_write", "make_read_only", "owner_of", "read_json", "write_json"]

Owner = tuple[int, int]
"""A uid and gid."""


def owner_of(user: str) -> Owner:
    """The uid and primary gid of ``user``.

    Raises:
        KeyError: No such user.
    """
    entry = pwd.getpwnam(user)
    return entry.pw_uid, entry.pw_gid


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600, owner: Owner | None = None) -> None:
    """Write ``data`` to ``path`` through a temporary file and a rename.

    Args:
        path: The target; its directory must exist.
        data: The bytes.
        mode: The file mode.
        owner: Owner to give the file before the rename, or None to keep ours.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        if owner is not None:
            os.chown(tmp, *owner)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any, *, mode: int = 0o600, owner: Owner | None = None) -> None:
    """Write ``value`` as JSON atomically (see :func:`atomic_write`)."""
    atomic_write(path, json.dumps(value, indent=1).encode() + b"\n", mode=mode, owner=owner)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from ``path``; ``default`` when the file is missing."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def make_read_only(root: Path, owner: Owner | None) -> None:
    """Give a tree to ``owner`` and remove every write bit (CORE-367).

    Symlinks are not followed: a link is re-owned itself and its target, which
    may lie outside the tree, is left alone.

    Args:
        root: The tree (a staged venv).
        owner: The new owner, or None to keep the current one.
    """
    paths = [root, *root.rglob("*")]
    for path in paths:
        if owner is not None:
            os.chown(path, *owner, follow_symlinks=False)
        if path.is_symlink():
            continue
        current = stat.S_IMODE(path.lstat().st_mode)
        # Drop setuid/setgid/sticky as well: nothing in a venv needs them.
        os.chmod(path, current & 0o555)
