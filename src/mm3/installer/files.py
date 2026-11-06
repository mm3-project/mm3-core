"""Small file helpers: atomic writes, owners and read-only trees.

Files the installer writes for the engine keep owner ``mm3`` and their modes
(CORE-359), written without following symlinks (CORE-373); its own state is
mode 0600/0700 and owned by ``mm3-install``.

Implements:
    CORE-359: Core spec 0.35, §10.8 Backup and restore.
    CORE-373: Core spec 0.37, §10.7 Installer service.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import secrets
import stat
from pathlib import Path
from typing import Any

__all__ = [
    "Owner",
    "atomic_write",
    "ensure_dir",
    "make_read_only",
    "owner_of",
    "read_json",
    "write_json",
]

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

    Nothing here follows a symlink or changes an owner or mode by path
    (CORE-373): the directory is opened with ``O_NOFOLLOW``, the temporary
    file is created relative to it with ``O_EXCL | O_NOFOLLOW``, owner and
    mode are set on the open file, and the rename stays inside the directory.
    So a user who can change the directory (the engine's ``mm3`` in
    ``/var/lib/mm3``) can at most spoil its own copy, never redirect the
    installer's rights to another file.

    Args:
        path: The target; its directory must exist and not be a symlink.
        data: The bytes.
        mode: The file mode.
        owner: Owner to give the file before the rename, or None to keep ours.

    Raises:
        OSError: The directory is a symlink or the write failed.
    """
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tmp = f".{path.name}.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fchmod(out.fileno(), mode)
                if owner is not None:
                    os.fchown(out.fileno(), *owner)
                os.fsync(out.fileno())
            os.replace(tmp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
    finally:
        os.close(dir_fd)


def ensure_dir(path: Path, mode: int) -> None:
    """Create ``path`` (one level) if missing, refusing a symlink (CORE-373).

    Raises:
        OSError: ``path`` is a symlink or not a directory.
    """
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, mode)
    os.close(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC))


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
