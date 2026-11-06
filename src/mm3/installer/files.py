"""Small file helpers: atomic writes, owners and read-only trees.

Files the installer writes for the engine keep owner ``mm3`` and their modes
(CORE-359), written without following symlinks (CORE-373); its own state is
mode 0600/0700 and owned by ``mm3-install``.

A restore writes whole trees into the engine's directories (CORE-363): every
step goes through an open directory descriptor with ``O_NOFOLLOW``, so a
symlink planted by the engine's user can at most spoil its own copy (CORE-373).

Implements:
    CORE-359: Core spec 0.35, §10.8 Backup and restore.
    CORE-363: Core spec 0.37, §10.8 Backup and restore.
    CORE-373: Core spec 0.37, §10.7 Installer service.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import secrets
import shutil
import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

__all__ = [
    "Owner",
    "atomic_copy",
    "atomic_write",
    "ensure_dir",
    "make_read_only",
    "open_dir",
    "open_subdir",
    "owner_of",
    "put_file",
    "read_json",
    "remove_tree",
    "replace_tree",
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


def _check_name(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise OSError(f"{name!r} is not a plain file name")


def open_subdir(
    dir_fd: int,
    parts: Sequence[str],
    *,
    create: bool = False,
    mode: int = 0o755,
    owner: Owner | None = None,
) -> int:
    """Open each of ``parts`` below an open directory, never following a symlink.

    Args:
        dir_fd: The directory to start from; it stays open.
        parts: Plain directory names below it.
        create: Create missing directories (with ``mode`` and ``owner``).
        mode: Mode of created directories.
        owner: Owner of created directories, or None to keep ours.

    Returns:
        A new descriptor of the last directory (a duplicate of ``dir_fd`` for
        no parts); the caller closes it.

    Raises:
        OSError: A part is a symlink, not a directory, or missing.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.dup(dir_fd)
    try:
        for part in parts:
            _check_name(part)
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode, dir_fd=fd)
                nxt = os.open(part, flags, dir_fd=fd)
                if owner is not None:
                    os.fchown(nxt, *owner)
                os.fchmod(nxt, mode)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_dir(
    root: Path,
    parts: Sequence[str] = (),
    *,
    create: bool = False,
    mode: int = 0o755,
    owner: Owner | None = None,
) -> int:
    """Open ``root`` and then each of ``parts`` below it, never following a symlink.

    ``root`` itself is opened with ``O_NOFOLLOW``; the parts are opened as in
    :func:`open_subdir`. The caller closes the returned descriptor.

    Args:
        root: The base directory (it must exist and not be a symlink).
        parts: Plain directory names below it.
        create: Create missing directories.
        mode: Mode of created directories.
        owner: Owner of created directories, or None to keep ours.

    Returns:
        A descriptor of the last directory.

    Raises:
        OSError: A part is a symlink, not a directory, or missing.
    """
    base = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return open_subdir(base, parts, create=create, mode=mode, owner=owner)
    finally:
        os.close(base)


def put_file(
    dir_fd: int,
    name: str,
    source: bytes | IO[bytes],
    *,
    mode: int = 0o600,
    owner: Owner | None = None,
) -> None:
    """Write a file into an open directory through a temporary file and a rename.

    The temporary file is created with ``O_EXCL | O_NOFOLLOW``, owner and mode
    are set on the open file and the rename stays inside the directory
    (CORE-373).

    Args:
        dir_fd: The directory (from :func:`open_dir`).
        name: The file name.
        source: The bytes, or a binary stream read to its end.
        mode: The file mode.
        owner: Owner to give the file before the rename, or None to keep ours.

    Raises:
        OSError: The write failed.
    """
    _check_name(name)
    tmp = f".{name}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as out:
            if isinstance(source, bytes):
                out.write(source)
            else:
                shutil.copyfileobj(source, out, 1 << 20)
            out.flush()
            os.fchmod(out.fileno(), mode)
            if owner is not None:
                os.fchown(out.fileno(), *owner)
            os.fsync(out.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


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
    dir_fd = open_dir(path.parent)
    try:
        put_file(dir_fd, path.name, data, mode=mode, owner=owner)
    finally:
        os.close(dir_fd)


def atomic_copy(src: Path, dest: Path, *, mode: int = 0o600, owner: Owner | None = None) -> None:
    """Copy a file to ``dest`` as :func:`atomic_write` writes one, streaming it.

    Args:
        src: The source file (read as is).
        dest: The target; its directory must exist and not be a symlink.
        mode: The file mode.
        owner: Owner to give the file, or None to keep ours.

    Raises:
        OSError: The copy failed.
    """
    dir_fd = open_dir(dest.parent)
    try:
        with src.open("rb") as fh:
            put_file(dir_fd, dest.name, fh, mode=mode, owner=owner)
    finally:
        os.close(dir_fd)


def remove_tree(parent_fd: int, name: str) -> None:
    """Remove ``name`` (a file or a tree) below an open directory; a missing one is fine.

    A symlink is removed itself, never followed.

    Args:
        parent_fd: The directory holding ``name``.
        name: The entry.
    """
    _check_name(name)
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def replace_tree(
    parent_fd: int,
    name: str,
    populate: Callable[[int], None],
    *,
    mode: int = 0o755,
    owner: Owner | None = None,
) -> None:
    """Replace the directory ``name`` below ``parent_fd`` by a new one, whole.

    The new tree is built beside it (``populate`` gets its descriptor), then the
    two are swapped by renames inside the parent and the old one is removed. A
    failure before the swap leaves the old tree untouched (CORE-363).

    Args:
        parent_fd: The directory holding ``name``.
        name: The directory to replace; it need not exist.
        populate: Fills the new directory.
        mode: Mode of the new directory.
        owner: Owner of the new directory, or None to keep ours.
    """
    _check_name(name)
    token = secrets.token_hex(8)
    new, old = f".{name}.new-{token}", f".{name}.old-{token}"
    os.mkdir(new, mode, dir_fd=parent_fd)
    try:
        fd = os.open(
            new, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
        try:
            if owner is not None:
                os.fchown(fd, *owner)
            os.fchmod(fd, mode)
            populate(fd)
        finally:
            os.close(fd)
        had_old = True
        try:
            os.rename(name, old, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except FileNotFoundError:
            had_old = False
        os.rename(new, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        remove_tree(parent_fd, new)
        raise
    if had_old:
        remove_tree(parent_fd, old)


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
