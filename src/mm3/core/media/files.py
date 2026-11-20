"""Files of the media folder for the admin: list, upload, new folder, delete (CORE-405).

Every function here blocks and is called through :func:`run_folder`, which
runs it on a small executor of its own with a timeout: a share folder on a
mount that hangs answers 503 instead of blocking the event loop, and the
threads it leaves stuck never take the engine's default executor (CORE-407).

Paths are those of the media folder (CORE-404): ``files/…`` or
``shares/<name>/…``, and ``shares`` itself for the list of shares. Every
component is opened relative to its parent with ``O_NOFOLLOW``, so a link
planted on the way is never followed; links are never listed. Writes are
allowed only under ``files/``. An upload is written as a hidden temporary file
in its folder and then linked (or, with ``overwrite``, renamed) to its name,
so a file appears whole or not at all.

Implements:
    CORE-404: Core spec 0.48, §9.7 Media service.
    CORE-405: Core spec 0.55, §9.7 Media service.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import secrets
import shutil
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar

from mm3.core.media.folder import MEDIA_EXTENSIONS, split_media_path

__all__ = [
    "FOLDER_TIMEOUT_S",
    "FileRefused",
    "Upload",
    "delete_path",
    "entry_kind",
    "list_folder",
    "make_folder",
    "run_folder",
    "start_upload",
    "valid_name",
]

FOLDER_TIMEOUT_S = 5.0
"""A folder of the media folder that does not answer in 5 s is 503 (CORE-405, CORE-407)."""

MAX_NAME_BYTES = 255

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_MODE = 0o640
_DIR_MODE = 0o750

_VIDEO = frozenset({".mp4", ".m4v", ".webm"})
_AUDIO = frozenset({".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"})

Kind = Literal["folder", "video", "audio", "image"]
Reason = Literal["invalid", "not_found", "forbidden", "conflict", "unavailable"]

_T = TypeVar("_T")

# A few threads only: a hung mount may keep each one forever, and that must not
# reach the default executor that the rest of the engine uses.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mm3-media-folder")


class FileRefused(Exception):
    """A file route refused: the reason maps to an answer of CORE-405.

    Attributes:
        reason: ``invalid`` (422), ``not_found`` (404), ``forbidden`` (403),
            ``conflict`` (409) or ``unavailable`` (503).
        detail: A short English sentence for the problem's ``detail``.
    """

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


async def run_folder(fn: Callable[..., _T], *args: Any) -> _T:
    """Run one blocking folder call with the 5 s timeout of CORE-405 and CORE-407.

    Args:
        fn: The blocking call.
        *args: Its arguments.

    Returns:
        What it returned.

    Raises:
        FileRefused: ``unavailable`` when it did not answer within 5 s.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, fn, *args), FOLDER_TIMEOUT_S)
    except TimeoutError:
        raise FileRefused("unavailable", "The folder did not answer in 5 s") from None


def entry_kind(name: str) -> Kind | None:
    """The ``kind`` of a file by its extension, or ``None`` for one not listed (CORE-404)."""
    ext = PurePosixPath(name).suffix.lower()
    if ext not in MEDIA_EXTENSIONS:
        return None
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    return "image"


def valid_name(name: str) -> bool:
    """Whether ``name`` is one path part an upload may have (CORE-405)."""
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and not name.startswith(".")
        and len(name.encode("utf-8", "surrogatepass")) <= MAX_NAME_BYTES
    )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entry(name: str, st: os.stat_result) -> dict[str, Any] | None:
    if stat.S_ISDIR(st.st_mode):
        return {"name": name, "kind": "folder", "size": None, "modified": _iso(st.st_mtime)}
    if stat.S_ISREG(st.st_mode):
        kind = entry_kind(name)
        if kind is None:
            return None
        return {"name": name, "kind": kind, "size": st.st_size, "modified": _iso(st.st_mtime)}
    return None


def _parts(rel: str, *, listing: bool = False) -> list[str]:
    """The parts of a media folder path, or ``invalid``; ``shares`` alone only for a listing."""
    if listing and rel.rstrip("/") == "shares":
        return ["shares"]
    parts = split_media_path(rel)
    if parts is None:
        raise FileRefused("invalid", "A path starts with files/ or shares/<name>/")
    return parts


def _writable(parts: list[str]) -> None:
    if parts[0] != "files":
        raise FileRefused("forbidden", "Only files/ can be written; shares are read-only")


def _open_dir(root: Path, parts: list[str]) -> int:
    """Open ``root/parts…`` as a folder without following a link; ``not_found`` otherwise."""
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError:
        raise FileRefused("not_found", "The media folder does not exist") from None
    try:
        for part in parts:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise FileRefused("not_found", "No such folder") from None
    return fd


def list_folder(root: Path, rel: str) -> list[dict[str, Any]]:
    """List one folder of ``files/`` or ``shares/`` as ``[{name, kind, size, modified}]``.

    Args:
        root: The media folder.
        rel: The folder, such as ``files`` or ``shares/nas/2024``.

    Returns:
        Its entries in name order: folders, and files with an extension of
        CORE-404; links and anything else are left out.

    Raises:
        FileRefused: ``invalid`` path, or ``not_found`` when it is not a folder.

    Implements:
        CORE-405: Core spec 0.55, §9.7 Media service.
    """
    fd = _open_dir(root, _parts(rel, listing=True))
    out: list[dict[str, Any]] = []
    try:
        with os.scandir(fd) as it:
            names = sorted(e.name for e in it)
        for name in names:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            item = _entry(name, st)
            if item is not None:
                out.append(item)
    finally:
        os.close(fd)
    return out


def _stat_entry(dfd: int, name: str) -> dict[str, Any]:
    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    item = _entry(name, st)
    if item is None:  # pragma: no cover - only what we just wrote is read back
        raise FileRefused("not_found", "No such file")
    return item


def make_folder(root: Path, rel: str) -> dict[str, Any]:
    """Create one folder under ``files/`` in an existing parent (CORE-405).

    Args:
        root: The media folder.
        rel: The new folder, such as ``files/holidays``.

    Returns:
        The new entry.

    Raises:
        FileRefused: ``invalid``, ``forbidden`` (not under ``files/``),
            ``not_found`` (no parent) or ``conflict`` (the name exists).

    Implements:
        CORE-405: Core spec 0.55, §9.7 Media service.
    """
    parts = _parts(rel)
    _writable(parts)
    if len(parts) < 2 or not valid_name(parts[-1]):
        raise FileRefused("invalid", "A new folder needs a name under files/")
    dfd = _open_dir(root, parts[:-1])
    try:
        try:
            os.mkdir(parts[-1], _DIR_MODE, dir_fd=dfd)
        except FileExistsError:
            raise FileRefused("conflict", "That name already exists") from None
        return _stat_entry(dfd, parts[-1])
    finally:
        os.close(dfd)


def delete_path(root: Path, rel: str, recursive: bool) -> None:
    """Delete a file, or a folder (a folder that is not empty only with ``recursive``).

    Args:
        root: The media folder.
        rel: What to delete, under ``files/``; ``files/`` itself never.
        recursive: Whether a folder that is not empty is deleted with its content.

    Raises:
        FileRefused: ``invalid`` (also for ``files`` itself), ``forbidden``,
            ``not_found`` (also for a link) or ``conflict`` (a folder not empty).

    Implements:
        CORE-405: Core spec 0.55, §9.7 Media service.
    """
    parts = _parts(rel)
    _writable(parts)
    if len(parts) < 2:
        raise FileRefused("invalid", "files/ itself cannot be deleted")
    dfd = _open_dir(root, parts[:-1])
    name = parts[-1]
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            raise FileRefused("not_found", "No such file or folder") from None
        if stat.S_ISDIR(st.st_mode):
            try:
                os.rmdir(name, dir_fd=dfd)
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                if not recursive:
                    raise FileRefused(
                        "conflict", "The folder is not empty; delete it with recursive=true"
                    ) from None
                # rmtree with dir_fd never follows a link inside the folder.
                shutil.rmtree(name, dir_fd=dfd)
        elif stat.S_ISREG(st.st_mode):
            os.unlink(name, dir_fd=dfd)
        else:
            raise FileRefused("not_found", "No such file or folder")
    finally:
        os.close(dfd)


@dataclass
class Upload:
    """An upload being written: a hidden temporary file in its folder (CORE-405).

    Attributes:
        dfd: The open folder.
        name: The file's final name.
        tmp: The temporary file's name in the folder.
        fd: The temporary file, open for writing.
        overwrite: Whether an existing file of that name is replaced.
        written: Bytes written so far.
    """

    dfd: int
    name: str
    tmp: str
    fd: int
    overwrite: bool
    written: int = 0

    def write(self, data: bytes) -> None:
        """Append ``data`` to the temporary file."""
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]
        self.written += len(data)

    def finish(self) -> dict[str, Any]:
        """Give the file its name; return the new entry.

        Raises:
            FileRefused: ``conflict`` when the name exists (without ``overwrite``)
                or is a folder.
        """
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = -1
        try:
            if self.overwrite:
                os.rename(self.tmp, self.name, src_dir_fd=self.dfd, dst_dir_fd=self.dfd)
            else:
                # link() refuses an existing name atomically; rename() would replace it.
                os.link(
                    self.tmp,
                    self.name,
                    src_dir_fd=self.dfd,
                    dst_dir_fd=self.dfd,
                    follow_symlinks=False,
                )
        except (FileExistsError, IsADirectoryError, NotADirectoryError):
            raise FileRefused("conflict", "That name already exists") from None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(self.tmp, dir_fd=self.dfd)
        return _stat_entry(self.dfd, self.name)

    def abort(self) -> None:
        """Remove the temporary file; nothing appears."""
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1
        with contextlib.suppress(OSError):
            os.unlink(self.tmp, dir_fd=self.dfd)

    def close(self) -> None:
        """Close the folder (after :meth:`finish` or :meth:`abort`)."""
        with contextlib.suppress(OSError):
            os.close(self.dfd)


def start_upload(root: Path, rel: str, name: str, overwrite: bool) -> Upload:
    """Check an upload and open its temporary file (CORE-405).

    Args:
        root: The media folder.
        rel: The existing folder under ``files/``.
        name: The file's name: one path part with an extension of CORE-404.
        overwrite: Whether an existing file of that name is replaced.

    Returns:
        The open upload.

    Raises:
        FileRefused: ``invalid`` (path, name or extension), ``forbidden``,
            ``not_found`` (no such folder) or ``conflict`` (the name exists).

    Implements:
        CORE-405: Core spec 0.55, §9.7 Media service.
    """
    parts = _parts(rel)
    _writable(parts)
    if not valid_name(name):
        raise FileRefused("invalid", "The name must be one path part, not starting with '.'")
    if entry_kind(name) is None:
        raise FileRefused("invalid", "That extension is not a media file")
    dfd = _open_dir(root, parts)
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not overwrite or not stat.S_ISREG(st.st_mode):
                raise FileRefused("conflict", "That name already exists")
        tmp = f".{secrets.token_hex(6)}.upload"
        fd = os.open(tmp, _TMP_FLAGS, _FILE_MODE, dir_fd=dfd)
    except BaseException:
        os.close(dfd)
        raise
    return Upload(dfd=dfd, name=name, tmp=tmp, fd=fd, overwrite=overwrite)
