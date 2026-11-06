"""Backup archives: layout, manifest, sidecar, listing, verification and retention.

An archive is a gzip-compressed tar named ``<hostname>-<YYYYmmdd-HHMMSS>-<kind>.mm3backup``
with ``kind`` ``scheduled``, ``manual`` or ``pre`` (CORE-362). It holds
``manifest.json``, ``config/`` (``config/set/`` the config directory without the
env file, ``config/history/`` the configuration history), ``data/<instance>/``
(``files/`` and ``kv.json``), ``modules.json`` and ``secrets/``. An encrypted
archive is the whole ``.tar.gz`` encrypted with age; each archive has an
unencrypted sidecar ``<name>.mm3backup.json`` without secrets, so local and
remote lists work without the key (CORE-362).

``secrets`` is only ever stored encrypted: an unencrypted backup omits that
part and says so in its manifest (CORE-161). Backups made by the installer are
owned by it (mode 0600 in a 0700 directory); the engine never reads them and
reaches them only through the installer socket (CORE-361).

Implements:
    CORE-161: Core spec 0.37, §10.8 Backup and restore.
    CORE-162: Core spec 0.37, §10.8 Backup and restore.
    CORE-164: Core spec 0.37, §10.8 Backup and restore.
    CORE-360: Core spec 0.37, §10.8 Backup and restore.
    CORE-362: Core spec 0.37, §10.8 Backup and restore.
    CORE-366: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import stat
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO, Literal

from packaging.version import InvalidVersion, Version

from mm3.__about__ import MODULE_API_VERSION, __version__
from mm3.core.config.model import BackupKeep
from mm3.installer.age import AgeError, AgeTool, encryption_kind
from mm3.installer.files import atomic_copy, ensure_dir, read_json, write_json
from mm3.installer.paths import Layout

__all__ = [
    "ARCHIVE_SUFFIX",
    "KEEP_PRE",
    "PARTS",
    "Archive",
    "BackupError",
    "BackupService",
    "Created",
    "Entry",
    "Manifest",
    "Sidecar",
    "check_compatible",
    "select_kept",
]

ARCHIVE_SUFFIX = ".mm3backup"
SIDECAR_SUFFIX = ".mm3backup.json"
MANIFEST = "manifest.json"
MODULES_JSON = "modules.json"
FORMAT = 1
PARTS = ("config", "data", "modules", "secrets")
KEEP_PRE = 3
"""Pre-operation backups kept, outside ``backup.keep`` (CORE-366)."""
MAX_MEMBERS = 500_000
MAX_UNPACKED = 64 * 1024**3
"""Limits against an archive built to exhaust the disk or the file table."""
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.mm3backup$")
_HOST = re.compile(r"[^A-Za-z0-9.-]")
_STAMP = re.compile(r"-(\d{8}-\d{6})-(scheduled|manual|pre)\.mm3backup$")
_CACHE_DIRS = frozenset({"cache", ".cache"})

Kind = Literal["scheduled", "manual", "pre"]
KINDS: tuple[str, ...] = ("scheduled", "manual", "pre")
_LOCAL_SECRETS = {
    "mm3.env": "env_file",
    "tokens.json": "tokens_file",
    "auth.json": "auth_file",
    "rclone.conf": "rclone_conf",
}


class BackupError(Exception):
    """A backup or an archive is refused; the message is safe to show."""


@dataclass(frozen=True)
class Sidecar:
    """``<name>.mm3backup.json``: what a list shows without the key (CORE-362)."""

    created: str
    kind: str
    hostname: str
    engine_version: str
    api_version: str
    parts: list[str]
    size: int
    encrypted: bool
    sha256: str

    def to_json(self) -> dict[str, Any]:
        """The JSON form."""
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> Sidecar:
        """Read the JSON form.

        Raises:
            BackupError: Not a sidecar.
        """
        try:
            parts = [str(p) for p in data["parts"]]
            return cls(
                created=str(data["created"]),
                kind=str(data["kind"]),
                hostname=str(data["hostname"]),
                engine_version=str(data["engine_version"]),
                api_version=str(data["api_version"]),
                parts=parts,
                size=int(data["size"]),
                encrypted=bool(data["encrypted"]),
                sha256=str(data["sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError("not a backup sidecar") from exc

    @property
    def when(self) -> datetime:
        """The creation time."""
        return datetime.fromisoformat(self.created)


@dataclass(frozen=True)
class Entry:
    """A backup in a list: its file name and sidecar."""

    name: str
    sidecar: Sidecar

    def public(self) -> dict[str, Any]:
        """The JSON form of a list item."""
        return {"name": self.name, **self.sidecar.to_json()}


@dataclass(frozen=True)
class Created:
    """A backup just made.

    Attributes:
        entry: The archive and its sidecar.
        path: The archive file.
        plain: The unencrypted ``.tar.gz``, kept only when the caller asked, so
            a rollback within the same operation needs no key (CORE-163).
        omitted: Parts left out (``secrets`` of an unencrypted backup, CORE-161).
    """

    entry: Entry
    path: Path
    plain: Path | None = None
    omitted: tuple[str, ...] = ()


@dataclass
class Manifest:
    """``manifest.json`` (CORE-162, CORE-362)."""

    engine_version: str
    api_version: str
    hostname: str
    created: str
    kind: str
    parts: list[str]
    files: dict[str, str] = field(default_factory=dict)
    omitted: list[str] = field(default_factory=list)
    format: int = FORMAT

    def to_json(self) -> dict[str, Any]:
        """The JSON form."""
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> Manifest:
        """Read the JSON form.

        Raises:
            BackupError: Not a manifest this engine understands.
        """
        try:
            if int(data["format"]) != FORMAT:
                raise BackupError(f"manifest format {data['format']} is not supported")
            files = {str(k): str(v) for k, v in dict(data["files"]).items()}
            return cls(
                engine_version=str(data["engine_version"]),
                api_version=str(data["api_version"]),
                hostname=str(data["hostname"]),
                created=str(data["created"]),
                kind=str(data["kind"]),
                parts=[str(p) for p in data["parts"]],
                files=files,
                omitted=[str(p) for p in data.get("omitted", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError("the archive has no valid manifest") from exc


# --- compatibility ------------------------------------------------------------------


def _version(text: str) -> Version:
    try:
        return Version(text)
    except InvalidVersion as exc:
        raise BackupError(f"{text!r} is not a version") from exc


def check_compatible(manifest: Manifest) -> list[str]:
    """Check an archive against this engine (CORE-164).

    An archive from an older engine is restored; the config format is migrated
    when a migration exists (none does yet, so the files are used as they are).
    An archive from a newer engine, or from another Module API major, is
    refused.

    Args:
        manifest: The archive's manifest.

    Returns:
        Warnings to show (none block the restore).

    Raises:
        BackupError: The archive is from a newer engine or another Module API major.
    """
    theirs, ours = _version(manifest.engine_version), _version(__version__)
    if theirs > ours:
        raise BackupError(
            f"the backup is from a newer engine ({manifest.engine_version}, this is {__version__})"
        )
    if _version(manifest.api_version).major != _version(MODULE_API_VERSION).major:
        raise BackupError(
            f"the backup uses Module API {manifest.api_version}, "
            f"this engine {MODULE_API_VERSION} (another major)"
        )
    warnings = []
    if theirs < ours:
        warnings.append(f"the backup is from an older engine ({manifest.engine_version})")
    return warnings


# --- retention ----------------------------------------------------------------------


def select_kept(created: list[datetime], keep: BackupKeep, tz: tzinfo = UTC) -> set[int]:
    """Choose the backups retention keeps: daily, weekly and monthly (CORE-244, CORE-360).

    Of the newest ``keep.daily`` days, ``keep.weekly`` ISO weeks and
    ``keep.monthly`` months that have a backup, the newest backup of each is
    kept. A backup kept by any of the three is kept.

    Args:
        created: Creation times.
        keep: The counts.
        tz: The zone that decides where a day, week or month starts.

    Returns:
        The indexes into ``created`` to keep.
    """
    order = sorted(range(len(created)), key=lambda i: created[i], reverse=True)
    kept: set[int] = set()

    def pick(bucket: Callable[[datetime], Any], count: int) -> None:
        seen: list[Any] = []
        for i in order:
            key = bucket(created[i].astimezone(tz))
            if key in seen:
                continue
            if len(seen) >= count:
                break
            seen.append(key)
            kept.add(i)

    pick(lambda d: d.date(), keep.daily)
    pick(lambda d: tuple(d.isocalendar()[:2]), keep.weekly)
    pick(lambda d: (d.year, d.month), keep.monthly)
    return kept


# --- archives -----------------------------------------------------------------------


def _safe_name(raw: str) -> str:
    """Normalise a member name; refuse anything that could leave the target."""
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\0" in raw:
        raise BackupError(f"unsafe name in the archive: {raw!r}")
    return str(path)


class Archive:
    """An opened plain ``.tar.gz``: member listing, hashing and reading.

    Only regular files and directories are accepted; a link, device or any
    other member makes the archive invalid (CORE-363).

    Args:
        path: The decrypted archive.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._tar: tarfile.TarFile | None = None
        self.members: dict[str, tarfile.TarInfo] = {}

    def __enter__(self) -> Archive:
        try:
            self._tar = tarfile.open(self.path, "r:gz")
            total = 0
            for info in self._tar:
                name = _safe_name(info.name)
                if info.isdir():
                    continue
                if not info.isreg():
                    raise BackupError(f"the archive holds a link or special file: {name!r}")
                if name in self.members:
                    raise BackupError(f"duplicate member {name!r}")
                total += info.size
                if len(self.members) >= MAX_MEMBERS or total > MAX_UNPACKED:
                    raise BackupError("the archive is too large")
                self.members[name] = info
        except (tarfile.TarError, OSError, EOFError) as exc:
            self.__exit__(None, None, None)
            raise BackupError(f"not a readable backup archive: {exc}") from exc
        except BackupError:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    def open(self, name: str) -> IO[bytes]:
        """Open one member for reading."""
        if self._tar is None:
            raise BackupError("the archive is closed")
        info = self.members.get(name)
        fh = self._tar.extractfile(info) if info is not None else None
        if fh is None:
            raise BackupError(f"{name} is not in the archive")
        return fh

    def read(self, name: str, limit: int = 64 * 1024 * 1024) -> bytes:
        """Read a small member whole."""
        with self.open(name) as fh:
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise BackupError(f"{name} is too large")
        return data

    def names(self, prefix: str) -> Iterator[str]:
        """Member names below ``prefix/``, sorted."""
        head = prefix.rstrip("/") + "/"
        yield from sorted(n for n in self.members if n.startswith(head))

    def manifest(self) -> Manifest:
        """Read ``manifest.json``.

        Raises:
            BackupError: Missing or invalid.
        """
        if MANIFEST not in self.members:
            raise BackupError("the archive has no manifest")
        try:
            return Manifest.from_json(json.loads(self.read(MANIFEST)))
        except ValueError as exc:
            raise BackupError("the manifest is not JSON") from exc

    def verify(self) -> Manifest:
        """Check the manifest against every file (CORE-162).

        Returns:
            The manifest.

        Raises:
            BackupError: A file differs from its checksum, is missing, or is not listed.
        """
        manifest = self.manifest()
        listed = set(manifest.files)
        present = set(self.members) - {MANIFEST}
        if listed - present:
            raise BackupError(f"missing from the archive: {sorted(listed - present)[0]}")
        if present - listed:
            raise BackupError(f"not in the manifest: {sorted(present - listed)[0]}")
        for name in sorted(listed):
            with self.open(name) as fh:
                digest = _digest(fh)
            if digest != manifest.files[name]:
                raise BackupError(f"checksum of {name} differs: the archive is damaged")
        return manifest


# --- the service --------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(fh: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := fh.read(1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return _digest(fh)


class _Hashing(io.RawIOBase):
    """Reads exactly ``size`` bytes of a file and hashes them."""

    def __init__(self, fh: BinaryIO, size: int) -> None:
        self._fh = fh
        self._left = size
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._left <= 0:
            return 0
        data = self._fh.read(min(len(buffer), self._left))
        if not data:
            raise BackupError("a file shrank while it was backed up")
        buffer[: len(data)] = data
        self._left -= len(data)
        self.digest.update(data)
        return len(data)


class BackupService:
    """Make, list, find and verify backups (CORE-360, CORE-362, CORE-366).

    Args:
        layout: The installer's paths.
        age: Encryption (CORE-161, CORE-364).
        modules: The installed modules as ``[{name, version, origin}]`` for ``modules.json``.
        hostname: The host name for archive names.
        now: Clock, for tests.
        owner: Owner of the backup files, or None to keep ours.
    """

    def __init__(
        self,
        layout: Layout,
        age: AgeTool,
        modules: Callable[[], list[dict[str, str]]],
        *,
        hostname: Callable[[], str] = socket.gethostname,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.layout = layout
        self.age = age
        self._modules = modules
        self._hostname = hostname
        self._now = now

    # --- directories ----------------------------------------------------------------

    def _dirs(self) -> None:
        ensure_dir(self.layout.backups, 0o700)
        ensure_dir(self.layout.pre_backups, 0o700)

    # --- collecting what goes in ----------------------------------------------------

    def _walk(
        self, root: Path, *, skip: Callable[[Path], bool] = lambda p: False
    ) -> Iterator[Path]:
        """Regular files below ``root``, never following a link; hidden temp files skipped."""
        if not root.is_dir() or root.is_symlink():
            return
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            here = Path(dirpath)
            dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink() and not skip(here / d))
            for name in sorted(files):
                path = here / name
                if name.startswith(".") or path.is_symlink() or skip(path):
                    continue
                if stat.S_ISREG(path.lstat().st_mode):
                    yield path

    def _entries(self, parts: list[str]) -> Iterator[tuple[str, Path | bytes]]:
        lay = self.layout
        if "config" in parts:
            for path in self._walk(lay.engine_config, skip=lambda p: p == lay.env_file):
                yield f"config/set/{path.relative_to(lay.engine_config).as_posix()}", path
            for path in self._walk(lay.history_dir):
                yield f"config/history/{path.relative_to(lay.history_dir).as_posix()}", path
        if "data" in parts:
            data = lay.instance_data
            if data.is_dir() and not data.is_symlink():
                for inst in sorted(data.iterdir()):
                    if inst.name.startswith(".") or inst.is_symlink() or not inst.is_dir():
                        continue
                    for path in self._walk(inst, skip=lambda p: p.name in _CACHE_DIRS):
                        rel = path.relative_to(inst).as_posix()
                        yield f"data/{inst.name}/files/{rel}", path
            kv = lay.instance_kv
            if kv.is_dir() and not kv.is_symlink():
                for path in sorted(kv.glob("*.json")):
                    if not path.name.startswith(".") and not path.is_symlink():
                        yield f"data/{path.stem}/kv.json", path
        if "modules" in parts:
            yield MODULES_JSON, json.dumps(self._modules(), indent=1).encode() + b"\n"
        if "secrets" in parts:
            for name, attr in _LOCAL_SECRETS.items():
                path = getattr(lay, attr)
                if path.is_file() and not path.is_symlink():
                    yield f"secrets/{name}", path

    # --- creating -------------------------------------------------------------------

    def _name(self, directory: Path, kind: str) -> tuple[str, datetime]:
        host = _HOST.sub("-", self._hostname()) or "mm3"
        when = self._now().astimezone(UTC).replace(microsecond=0)
        for _ in range(120):
            name = f"{host}-{when:%Y%m%d-%H%M%S}-{kind}{ARCHIVE_SUFFIX}"
            if not (directory / name).exists():
                return name, when
            when += timedelta(seconds=1)
        raise BackupError("could not find a free backup name")

    def create(
        self,
        kind: Kind,
        parts: list[str],
        work: Path,
        *,
        recipient: str | None = None,
        passphrase: str | None = None,
        keep_plain: bool = False,
    ) -> Created:
        """Make a backup (CORE-161, CORE-360, CORE-362, CORE-366).

        Args:
            kind: ``scheduled`` and ``manual`` go to the backups directory,
                ``pre`` to its ``pre`` subdirectory.
            parts: The parts to include; ``secrets`` is left out of an
                unencrypted backup and the manifest says so.
            work: A private directory for the staging files.
            recipient: An age public key to encrypt to.
            passphrase: An archive passphrase to encrypt with.
            keep_plain: Leave the unencrypted ``.tar.gz`` in ``work``.

        Returns:
            The new archive.

        Raises:
            BackupError: Bad parts or encryption settings, or the archive
                could not be written.
        """
        if kind not in KINDS:
            raise BackupError(f"unknown backup kind {kind!r}")
        if recipient and passphrase:
            raise BackupError("encrypt to a key or with a passphrase, not both")
        wanted = list(dict.fromkeys(parts))
        bad = [p for p in wanted if p not in PARTS]
        if bad or not wanted:
            raise BackupError(f"parts must be a non-empty list of {', '.join(PARTS)}")
        encrypted = bool(recipient or passphrase)
        omitted = [p for p in wanted if p == "secrets" and not encrypted]
        included = [p for p in wanted if p not in omitted]
        if not included:
            raise BackupError("secrets are stored only in an encrypted backup")
        self._dirs()
        directory = self.layout.pre_backups if kind == "pre" else self.layout.backups
        name, when = self._name(directory, kind)
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        plain = work / f"{secrets.token_hex(6)}.tar.gz"
        try:
            self._write_tar(plain, kind, included, omitted, when)
            final = plain
            if encrypted:
                final = work / f"{secrets.token_hex(6)}.age"
                try:
                    if recipient:
                        self.age.encrypt_to(plain, final, recipient)
                    else:
                        self.age.encrypt_passphrase(plain, final, passphrase or "")
                except AgeError as exc:
                    raise BackupError(f"encryption failed: {exc}") from exc
            sidecar = Sidecar(
                created=when.isoformat(),
                kind=kind,
                hostname=self._hostname(),
                engine_version=__version__,
                api_version=MODULE_API_VERSION,
                parts=included,
                size=final.stat().st_size,
                encrypted=encrypted,
                sha256=_sha256(final),
            )
            atomic_copy(final, directory / name, mode=0o600)
            write_json(directory / f"{name}.json", sidecar.to_json(), mode=0o600)
            if final != plain:
                final.unlink(missing_ok=True)
        except BaseException:
            plain.unlink(missing_ok=True)
            raise
        if not keep_plain:
            plain.unlink(missing_ok=True)
        if kind == "pre":
            self.prune_pre()
        return Created(
            Entry(name, sidecar), directory / name, plain if keep_plain else None, tuple(omitted)
        )

    def _write_tar(
        self, path: Path, kind: str, parts: list[str], omitted: list[str], when: datetime
    ) -> None:
        manifest = Manifest(
            engine_version=__version__,
            api_version=MODULE_API_VERSION,
            hostname=self._hostname(),
            created=when.isoformat(),
            kind=kind,
            parts=parts,
            omitted=omitted,
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for arcname, source in self._entries(parts):
                if isinstance(source, bytes):
                    info = tarfile.TarInfo(arcname)
                    info.size = len(source)
                    info.mode = 0o600
                    info.mtime = int(when.timestamp())
                    tar.addfile(info, io.BytesIO(source))
                    manifest.files[arcname] = hashlib.sha256(source).hexdigest()
                    continue
                self._add_file(tar, arcname, source, manifest)
            body = json.dumps(manifest.to_json(), indent=1).encode() + b"\n"
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(body)
            info.mode = 0o600
            info.mtime = int(when.timestamp())
            tar.addfile(info, io.BytesIO(body))

    @staticmethod
    def _add_file(tar: tarfile.TarFile, arcname: str, source: Path, manifest: Manifest) -> None:
        # One open, one fstat: a file that grows while it is read is cut at the
        # size seen, one that shrinks fails the backup (it is tried again later).
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                return
            member = tarfile.TarInfo(arcname)
            member.size = info.st_size
            member.mode = stat.S_IMODE(info.st_mode) & 0o640 or 0o600
            member.mtime = int(info.st_mtime)
            reader = _Hashing(fh, info.st_size)
            tar.addfile(member, io.BufferedReader(reader))
            manifest.files[arcname] = reader.digest.hexdigest()

    # --- listing and finding --------------------------------------------------------

    def _list_dir(self, directory: Path) -> list[Entry]:
        out = []
        try:
            names = sorted(directory.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return []
        for path in names:
            if not path.name.endswith(ARCHIVE_SUFFIX) or not _NAME.match(path.name):
                continue
            data = read_json(directory / f"{path.name}.json")
            try:
                out.append(Entry(path.name, Sidecar.from_json(data)))
            except BackupError:
                continue  # an archive without a readable sidecar is not listed
        return out

    def listing(self) -> list[Entry]:
        """Local backups, newest first: scheduled and manual, then pre-operation."""
        entries = self._list_dir(self.layout.backups) + self._list_dir(self.layout.pre_backups)
        return sorted(entries, key=lambda e: e.sidecar.when, reverse=True)

    def find(self, name: str) -> tuple[Entry, Path]:
        """Find a local backup by its file name.

        Raises:
            BackupError: Not a backup name or not found.
        """
        if not _NAME.match(name):
            raise BackupError(f"{name!r} is not a backup file name")
        for directory in (self.layout.backups, self.layout.pre_backups):
            path = directory / name
            if path.is_file() and not path.is_symlink():
                for entry in self._list_dir(directory):
                    if entry.name == name:
                        return entry, path
        raise BackupError(f"no backup named {name}")

    # --- retention ------------------------------------------------------------------

    def prune_pre(self, keep: int = KEEP_PRE) -> list[str]:
        """Keep the newest ``keep`` pre-operation backups (CORE-366).

        Returns:
            The names removed.
        """
        entries = sorted(
            self._list_dir(self.layout.pre_backups), key=lambda e: e.sidecar.when, reverse=True
        )
        return self._remove(self.layout.pre_backups, entries[keep:])

    def prune(self, keep: BackupKeep, tz: tzinfo = UTC) -> list[str]:
        """Apply ``backup.keep`` to the scheduled backups (CORE-360).

        Manual and pre-operation backups are not touched.

        Returns:
            The names removed.
        """
        scheduled = [
            e for e in self._list_dir(self.layout.backups) if e.sidecar.kind == "scheduled"
        ]
        kept = select_kept([e.sidecar.when for e in scheduled], keep, tz)
        return self._remove(
            self.layout.backups, [e for i, e in enumerate(scheduled) if i not in kept]
        )

    def _remove(self, directory: Path, entries: list[Entry]) -> list[str]:
        gone = []
        for entry in entries:
            for path in (directory / entry.name, directory / f"{entry.name}.json"):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            gone.append(entry.name)
        return gone

    # --- opening for restore --------------------------------------------------------

    def open_plain(self, source: Path, secret: str | None, work: Path) -> Path:
        """Give the plain ``.tar.gz`` of an archive, decrypting it when needed (CORE-362, CORE-364).

        Args:
            source: The archive file.
            secret: An age identity or the archive passphrase, for an encrypted archive.
            work: A private directory for the decrypted copy.

        Returns:
            The plain file (``source`` itself when it is not encrypted).

        Raises:
            BackupError: The archive is encrypted and no key was given, or the key is wrong.
        """
        if encryption_kind(source) == "none":
            return source
        if not secret:
            raise BackupError("the backup is encrypted: send its age key or passphrase")
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        plain = work / f"{secrets.token_hex(6)}.tar.gz"
        try:
            self.age.decrypt(source, plain, secret, work)
        except AgeError as exc:
            raise BackupError(f"could not decrypt the backup: {exc}") from exc
        return plain

    def kept_identity(self) -> str | None:
        """The age identity kept on the mirror on request (CORE-364), or None."""
        try:
            return self.layout.backup_key.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None


def remove_work(path: Path) -> None:
    """Remove a private work directory."""
    shutil.rmtree(path, ignore_errors=True)
