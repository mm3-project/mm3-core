"""The media folder: where it is, which files count, and reading them without links.

The media folder is ``<state>/media/`` with ``files/``, which the engine
writes, and ``shares/<name>/``, one per share, which the engine only reads
(CORE-404). Only the extensions of CORE-404 are listed or served, and a
symbolic link is never followed: every path component is opened relative to
its parent with ``O_NOFOLLOW``, so a link planted anywhere on the way makes
the file unreachable instead of leading elsewhere.

These functions block; the service and the routes call them through a
thread with a timeout, so a hung mount never blocks the event loop.

Implements:
    CORE-404: Core spec 0.48, §9.7 Media service.
"""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = [
    "CONTENT_TYPES",
    "MEDIA_EXTENSIONS",
    "MediaFile",
    "list_files",
    "media_root",
    "open_file",
    "prepare",
    "split_media_path",
]

CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}
"""The extensions of CORE-404 and the content type each is served with (CORE-400)."""

MEDIA_EXTENSIONS = frozenset(CONTENT_TYPES)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def media_root(state_dir: Path) -> Path:
    """Return the media folder ``<state>/media/`` (CORE-404)."""
    return state_dir / "media"


def split_media_path(rel: str) -> list[str] | None:
    """Split a path under the media folder into its parts, or ``None`` when it is not one.

    A path starts with ``files/`` or ``shares/<name>/``, is relative, and has
    no empty, ``.`` or ``..`` part and no backslash.

    Args:
        rel: The path, such as ``files/holidays/a.jpg``.

    Returns:
        The parts, or ``None``.
    """
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] == "files" or (parts[0] == "shares" and len(parts) >= 2):
        return parts
    return None


def _open_dir(root: Path, parts: list[str]) -> int:
    """Open the folder ``root/parts…`` without following a link on the way."""
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass(frozen=True)
class MediaFile:
    """An open file of the media folder.

    Attributes:
        fd: The open descriptor; the caller closes it.
        size: Size in bytes.
        modified: Modification time (seconds since the epoch).
        content_type: The content type of its extension.
    """

    fd: int
    size: int
    modified: float
    content_type: str


def open_file(root: Path, rel: str) -> MediaFile | None:
    """Open one file of the media folder for reading (CORE-400, CORE-404).

    Args:
        root: The media folder.
        rel: Its path under the folder, such as ``files/a.mp4``.

    Returns:
        The open file, or ``None`` when the path is not a regular file with an
        extension of CORE-404, or when any part of it is a symbolic link.

    Implements:
        CORE-404: Core spec 0.48, §9.7 Media service.
    """
    parts = split_media_path(rel)
    if parts is None or len(parts) < 2:
        return None
    ext = PurePosixPath(parts[-1]).suffix.lower()
    if ext not in MEDIA_EXTENSIONS:
        return None
    try:
        dfd = _open_dir(root, parts[:-1])
    except OSError:
        return None
    try:
        fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None
    os.set_blocking(fd, True)
    return MediaFile(fd, st.st_size, st.st_mtime, CONTENT_TYPES[ext])


def _walk(fd: int, prefix: str, out: list[str]) -> None:
    with os.scandir(fd) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if entry.is_symlink():
            continue
        rel = f"{prefix}/{entry.name}"
        if entry.is_dir(follow_symlinks=False):
            try:
                sub = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                continue
            try:
                _walk(sub, rel, out)
            finally:
                os.close(sub)
        elif entry.is_file(follow_symlinks=False):
            if PurePosixPath(entry.name).suffix.lower() in MEDIA_EXTENSIONS:
                out.append(rel)


def list_files(root: Path, rel: str) -> list[str]:
    """List the files of a ``files`` source: one file, or a folder with its subfolders.

    Args:
        root: The media folder.
        rel: The source's ``path``.

    Returns:
        Paths under the media folder in name order, subfolders included;
        only files with an extension of CORE-404 and never through a link.
        Empty when the path does not exist.

    Implements:
        CORE-404: Core spec 0.48, §9.7 Media service.
    """
    parts = split_media_path(rel)
    if parts is None:
        return []
    try:
        fd = _open_dir(root, parts)
    except NotADirectoryError:
        one = open_file(root, "/".join(parts))
        if one is None:
            return []
        os.close(one.fd)
        return ["/".join(parts)]
    except OSError:
        return []
    out: list[str] = []
    try:
        _walk(fd, "/".join(parts), out)
    finally:
        os.close(fd)
    return out


def prepare(root: Path) -> None:
    """Create ``files/`` and ``shares/`` under the media folder when they are missing."""
    for sub in ("files", "shares"):
        with contextlib.suppress(OSError):
            (root / sub).mkdir(parents=True, exist_ok=True)
