"""Configuration history: saved versions, undo and redo of saves, section restore.

Every valid configuration that is saved becomes a numbered version under
``<state>/config-history/``: a directory holding the whole config set (main
file, included files, widget templates, with their paths relative to the
config directory) and ``meta.json``. History is append-only. Undo, redo and
restore build a new config set, validate it in a scratch directory, and only
then write it and record it as a new version; a running engine applies the
result through its file watcher.

Implements:
    CORE-150: Core spec 0.10, §6.8 Configuration history, undo and redo.
    CORE-153, CORE-154: Core spec 0.10, §6.8 Configuration history, undo and redo.
    CORE-155, CORE-156: Core spec 0.10, §6.8 Configuration history, undo and redo.
    CORE-216, CORE-217, CORE-218: Core spec 0.10, §6.8 Configuration history, undo and redo.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, SecretBytes, SecretStr

from mm3.core.config.diff import changed_sections, unsubstituted
from mm3.core.config.loader import dump_plain, include_target, rt_dump, rt_load
from mm3.core.config.records import ValidationRecord
from mm3.core.config.validate import ConfigResult, load_config
from mm3.core.publicconfig import is_secret_field
from mm3.core.registry import Registry

__all__ = [
    "HISTORY_DIR",
    "MASK",
    "TEMPLATE_DIR",
    "ConfigHistory",
    "ConfigSet",
    "HistoryRecorder",
    "HistoryStore",
    "OpResult",
    "Origin",
    "VersionMeta",
    "secret_pointers",
]

HISTORY_DIR = "config-history"
TEMPLATE_DIR = "widget-templates"
MASK = "**********"
MAX_AGE = timedelta(days=180)
TOP_SECTIONS = ("engine", "display", "layout", "pages", "backup")
Origin = Literal["initial", "admin", "file", "cli", "restore", "undo", "redo"]

EXIT_DONE, EXIT_NOTHING, EXIT_INVALID, EXIT_REFUSED = 0, 1, 2, 3


# --- config sets -----------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigSet:
    """The files of one configuration, by path relative to the config directory.

    Attributes:
        main: Relative path of the main file.
        files: Relative path to content.
    """

    main: str
    files: Mapping[str, bytes]

    def sha256(self) -> str:
        """Hash the whole set: every path and content, in path order.

        Returns:
            The hex SHA-256 recorded in ``meta.json`` (CORE-153).
        """
        h = hashlib.sha256()
        for rel in sorted(self.files):
            h.update(rel.encode("utf-8") + b"\0")
            h.update(hashlib.sha256(self.files[rel]).digest())
        return h.hexdigest()

    @classmethod
    def collect(cls, main: Path, files: Sequence[Path]) -> ConfigSet:
        """Read a config set from disk: the given files plus every widget template.

        Args:
            main: The main file.
            files: Main and included files (missing ones are skipped).

        Returns:
            The set. The env file is never part of it (CORE-153).
        """
        root = main.parent
        out: dict[str, bytes] = {}
        for path in [main, *files]:
            rel = os.path.normpath(os.path.relpath(path, root))
            if rel.startswith("..") or rel in out:
                continue
            with contextlib.suppress(OSError):
                out[rel] = Path(path).read_bytes()
        templates = root / TEMPLATE_DIR
        if templates.is_dir():
            for path in sorted(templates.rglob("*")):
                if path.is_file():
                    out[str(path.relative_to(root))] = path.read_bytes()
        return cls(os.path.normpath(os.path.relpath(main, root)), out)

    def materialize(self, root: Path) -> Path:
        """Write the set under a directory.

        Args:
            root: Target directory.

        Returns:
            Path of the main file there.
        """
        for rel, data in self.files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return root / self.main

    def with_file(self, rel: str, data: bytes | None) -> ConfigSet:
        """Return a copy with one file replaced, added or removed (``None``).

        Args:
            rel: Relative path.
            data: New content, or ``None`` to remove it.

        Returns:
            The new set.
        """
        files = dict(self.files)
        if data is None:
            files.pop(rel, None)
        else:
            files[rel] = data
        return ConfigSet(self.main, files)


# --- the store -------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionMeta:
    """``meta.json`` of a version (CORE-153).

    Attributes:
        version: Version number.
        time: Creation time, UTC, ISO 8601.
        origin: initial, admin, file, cli, restore, undo or redo.
        author: Admin client address, ``cli:<user>`` or ``file``.
        parent: Parent version (CORE-154), ``None`` for the first.
        sections: Changed sections (CORE-216).
        sha256: Hash of the config set.
        main: Relative path of the main file.
        restores: Version whose content an undo, redo or restore copied.
    """

    version: int
    time: str
    origin: str
    author: str
    parent: int | None
    sections: list[str]
    sha256: str
    main: str
    restores: int | None = None


class HistoryStore:
    """Versions on disk, append-only, under one lock.

    Args:
        state_dir: Engine state directory (``<state>``, core spec 3.1).
        now: Clock for version times and retention.

    Implements:
        CORE-153, CORE-217, CORE-218: Core spec 0.10, §6.8 Configuration history, undo and redo.
    """

    def __init__(self, state_dir: Path, *, now: Any = None) -> None:
        self.root = state_dir / HISTORY_DIR
        self._now = now or (lambda: datetime.now(UTC))

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o750)  # noqa: S103 - CORE-218 sets 0750

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Hold ``<state>/config-history/.lock`` (flock) for config and history writes.

        Yields:
            Nothing; the lock is held inside the block.
        """
        self._ensure()
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _dir(self, version: int) -> Path:
        return self.root / f"{version:06d}"

    def versions(self) -> list[int]:
        """Return the stored version numbers in ascending order.

        Returns:
            The numbers.
        """
        if not self.root.is_dir():
            return []
        return sorted(int(p.name) for p in self.root.iterdir() if p.is_dir() and p.name.isdigit())

    def meta(self, version: int) -> VersionMeta:
        """Read a version's metadata.

        Args:
            version: Version number.

        Returns:
            The metadata.

        Raises:
            KeyError: Unknown version.
        """
        path = self._dir(version) / "meta.json"
        if not path.is_file():
            raise KeyError(version)
        return VersionMeta(**json.loads(path.read_text()))

    def latest(self) -> VersionMeta | None:
        """Return the current (newest) version, if any.

        Returns:
            Its metadata.
        """
        versions = self.versions()
        return self.meta(versions[-1]) if versions else None

    def read_set(self, version: int) -> ConfigSet:
        """Read the config set stored with a version.

        Args:
            version: Version number.

        Returns:
            The set.
        """
        meta = self.meta(version)
        base = self._dir(version) / "files"
        files = {
            str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()
        }
        return ConfigSet(meta.main, files)

    def add(
        self,
        config_set: ConfigSet,
        *,
        origin: str,
        author: str,
        parent: int | None,
        sections: Sequence[str],
        restores: int | None = None,
    ) -> VersionMeta:
        """Store a new version; call with the lock held.

        Args:
            config_set: The files.
            origin: What created it.
            author: Who created it.
            parent: Parent version (CORE-154).
            sections: Changed sections.
            restores: Version whose content it copies, for undo, redo, restore.

        Returns:
            The new version's metadata.
        """
        self._ensure()
        latest = self.versions()
        number = (latest[-1] + 1) if latest else 1
        meta = VersionMeta(
            version=number,
            time=self._now().astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            origin=origin,
            author=author,
            parent=parent,
            sections=list(sections),
            sha256=config_set.sha256(),
            main=config_set.main,
            restores=restores,
        )
        tmp = Path(tempfile.mkdtemp(prefix=".new-", dir=self.root))
        try:
            for rel, data in config_set.files.items():
                target = tmp / "files" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.chmod(target, 0o640)
            (tmp / "meta.json").write_text(json.dumps(asdict(meta), indent=2) + "\n")
            os.chmod(tmp / "meta.json", 0o640)
            for d in [tmp, *(p for p in tmp.rglob("*") if p.is_dir())]:
                os.chmod(d, 0o750)  # noqa: S103 - CORE-218 sets 0750
            os.rename(tmp, self._dir(number))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return meta

    def prune(self, keep: int) -> list[int]:
        """Remove versions outside the retention rule (CORE-218).

        A version is kept while it is among the newest ``keep`` or younger
        than 180 days; the current version is never removed.

        Args:
            keep: ``engine.history.keep``.

        Returns:
            The versions removed.
        """
        versions = self.versions()
        newest = set(versions[-keep:]) | set(versions[-1:])
        cutoff = self._now() - MAX_AGE
        removed: list[int] = []
        for v in versions:
            if v in newest:
                continue
            when = datetime.fromisoformat(self.meta(v).time.replace("Z", "+00:00"))
            if when < cutoff:
                shutil.rmtree(self._dir(v))
                removed.append(v)
        return removed

    def redo_target(self) -> VersionMeta | None:
        """Return the version a redo would restore, if redo is offered (CORE-154).

        Redo is offered while the newest versions are undos and redos; it
        restores the content undone most recently.

        Returns:
            The version whose content a redo copies.
        """
        versions = self.versions()
        run: list[VersionMeta] = []
        for v in reversed(versions):
            meta = self.meta(v)
            if meta.origin not in ("undo", "redo"):
                break
            run.append(meta)
        run.reverse()
        if not run:
            return None
        stack: list[int] = []
        previous = run[0].version - 1
        for meta in run:
            if meta.origin == "undo":
                stack.append(previous)
            elif stack:
                stack.pop()
            previous = meta.version
        if not stack:
            return None
        try:
            meta = self.meta(stack[-1])
            # Point at the version that first had this content.
            while meta.origin in ("undo", "redo") and meta.restores is not None:
                meta = self.meta(meta.restores)
        except KeyError:
            return None
        return meta


# --- secrets in views ------------------------------------------------------------------


def _model_secrets(model: BaseModel, prefix: tuple[str | int, ...]) -> Iterator[tuple[Any, ...]]:
    for name, info in type(model).model_fields.items():
        key = info.alias or name
        value = getattr(model, name)
        path = (*prefix, key)
        if is_secret_field(info) or isinstance(value, SecretStr | SecretBytes):
            yield path
        elif isinstance(value, BaseModel):
            yield from _model_secrets(value, path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, BaseModel):
                    yield from _model_secrets(item, (*path, i))


def secret_pointers(result: ConfigResult) -> set[tuple[Any, ...]]:
    """Find the locations of secret fields in a validated configuration (CORE-218).

    Args:
        result: A valid configuration.

    Returns:
        Paths (tuples of keys and indices) of ``SecretStr`` fields and fields
        marked ``secret``, in the engine block and in module configs.
    """
    out: set[tuple[Any, ...]] = set()
    if result.config is not None:
        out |= set(_model_secrets(result.config.engine, ("engine",)))
    for inst in result.instances:
        if inst.config is not None:
            out |= set(_model_secrets(inst.config, ("modules", inst.index, "config")))
    return out


def _mask(data: Any, secrets: set[tuple[Any, ...]], path: tuple[Any, ...] = ()) -> Any:
    if path in secrets and not (isinstance(data, str) and data.startswith("!env ")):
        return MASK
    if isinstance(data, Mapping):
        return {k: _mask(v, secrets, (*path, k)) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask(v, secrets, (*path, i)) for i, v in enumerate(data)]
    return data


# --- operations ------------------------------------------------------------------------


@dataclass
class OpResult:
    """Outcome of a history operation (CORE-219 exit codes).

    Attributes:
        code: 0 done, 1 nothing to undo or redo, 2 validation failed and
            nothing applied, 3 refused (files on disk invalid and differ).
        version: The version created, if any.
        message: English explanation.
        records: Validation records when the code is 2 or 3.
    """

    code: int
    version: int | None = None
    message: str = ""
    records: list[ValidationRecord] = field(default_factory=list)


@dataclass
class _Loaded:
    set: ConfigSet
    result: ConfigResult


class ConfigHistory:
    """History operations on one configuration directory.

    Args:
        config: The main configuration file.
        env_file: The env file, used to validate.
        registry: Installed modules, used to validate.
        state_dir: Engine state directory.
        environ: Process environment for ``!env``.
        now: Clock, for tests.

    Implements:
        CORE-154, CORE-155: Core spec 0.10, §6.8 Configuration history, undo and redo.
        CORE-156, CORE-217: Core spec 0.10, §6.8 Configuration history, undo and redo.
    """

    def __init__(
        self,
        config: Path,
        *,
        env_file: Path,
        registry: Registry,
        state_dir: Path,
        environ: Mapping[str, str] | None = None,
        now: Any = None,
    ) -> None:
        self.config = config
        self.root = config.parent
        self.env_file = env_file
        self.registry = registry
        self.environ = environ
        self.store = HistoryStore(state_dir, now=now)

    # --- loading ----------------------------------------------------------------------

    def _validate_set(self, config_set: ConfigSet) -> ConfigResult:
        """Validate a config set in a scratch copy of the config directory."""
        with tempfile.TemporaryDirectory(prefix="mm3-config-") as tmp:
            main = config_set.materialize(Path(tmp))
            result = load_config(
                main, registry=self.registry, env_file=self.env_file, environ=self.environ
            )
            prefix = str(Path(tmp))
            result.records = [
                ValidationRecord(
                    r.file.replace(prefix, str(self.root), 1) if r.file else r.file,
                    r.line,
                    r.col,
                    r.severity,
                    r.path,
                    r.code,
                    r.params,
                    r.message,
                )
                for r in result.records
            ]
        return result

    def _disk(self) -> _Loaded:
        result = load_config(
            self.config, registry=self.registry, env_file=self.env_file, environ=self.environ
        )
        return _Loaded(ConfigSet.collect(self.config, result.loaded.files), result)

    def _version(self, version: int) -> _Loaded:
        config_set = self.store.read_set(version)
        return _Loaded(config_set, self._validate_set(config_set))

    def _keep(self, result: ConfigResult) -> int:
        return result.config.engine.history.keep if result.config is not None else 200

    # --- recording --------------------------------------------------------------------

    def record_disk(self, origin: str = "file", author: str = "file") -> VersionMeta | None:
        """Record the files on disk as a version when they differ from the latest.

        Call with the lock held. Invalid files are never recorded (CORE-209).

        Args:
            origin: Origin when a version is created (``initial`` when the
                history is empty).
            author: Author.

        Returns:
            The current version after the call, or ``None`` when the files
            are invalid and differ from it.

        Implements:
            CORE-153, CORE-217: Core spec 0.10, §6.8 Configuration history, undo and redo.
        """
        disk = self._disk()
        latest = self.store.latest()
        if latest is not None and latest.sha256 == disk.set.sha256():
            return latest
        if not disk.result.ok:
            return None
        if latest is None:
            meta = self.store.add(
                disk.set, origin="initial", author=author, parent=None, sections=[]
            )
        else:
            before = self._version(latest.version).result
            sections = changed_sections(before, disk.result) if before.ok else []
            meta = self.store.add(
                disk.set, origin=origin, author=author, parent=latest.version, sections=sections
            )
        self.store.prune(self._keep(disk.result))
        return meta

    # --- writing ----------------------------------------------------------------------

    def _write(self, target: ConfigSet, current: ConfigSet) -> None:
        """Write a config set into the config directory, atomically per file (CORE-217)."""
        for rel, data in target.files.items():
            path = self.root / rel
            if current.files.get(rel) == data and path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            st = path.stat() if path.exists() else None
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                if st is not None:
                    os.chmod(tmp, st.st_mode & 0o7777)
                    with contextlib.suppress(PermissionError):
                        os.chown(tmp, st.st_uid, st.st_gid)
                else:
                    os.chmod(tmp, 0o640)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
        for rel in current.files:
            if rel not in target.files and rel.startswith(TEMPLATE_DIR + os.sep):
                with contextlib.suppress(FileNotFoundError):
                    (self.root / rel).unlink()

    def _prepare(self, force: bool) -> tuple[OpResult | None, _Loaded | None]:
        """Bring history up to date with the disk; refuse when the disk is invalid."""
        disk = self._disk()
        latest = self.store.latest()
        if latest is not None and latest.sha256 != disk.set.sha256() and not disk.result.ok:
            if not force:
                return (
                    OpResult(
                        EXIT_REFUSED,
                        message="the files on disk differ from the current version and are "
                        "invalid; fix them or use --force to overwrite them",
                        records=disk.result.errors,
                    ),
                    None,
                )
            return None, _Loaded(
                self.store.read_set(latest.version), self._version(latest.version).result
            )
        current = self.record_disk()
        if current is None:
            return (
                OpResult(
                    EXIT_REFUSED,
                    message="the configuration on disk is invalid",
                    records=disk.result.errors,
                ),
                None,
            )
        return None, self._disk()

    def _apply(
        self,
        target: ConfigSet,
        current: _Loaded,
        *,
        origin: str,
        author: str,
        parent: int | None,
        restores: int | None,
    ) -> OpResult:
        """Validate a target set, then write it and record it (CORE-156)."""
        checked = self._validate_set(target)
        if not checked.ok:
            return OpResult(
                EXIT_INVALID,
                message="the result does not validate; nothing applied",
                records=checked.errors,
            )
        sections = changed_sections(current.result, checked) if current.result.ok else []
        self._write(target, self._disk().set)
        meta = self.store.add(
            target,
            origin=origin,
            author=author,
            parent=parent,
            sections=sections,
            restores=restores,
        )
        self.store.prune(self._keep(checked))
        return OpResult(EXIT_DONE, meta.version, f"version {meta.version}")

    def undo(self, author: str, *, force: bool = False) -> OpResult:
        """Undo the last save: a new version with the content of the current one's parent.

        Args:
            author: ``cli:<user>`` or the admin client address.
            force: Overwrite invalid files on disk.

        Returns:
            The outcome.

        Implements:
            CORE-154: Core spec 0.10, §6.8 Configuration history, undo and redo.
        """
        with self.store.lock():
            refused, current = self._prepare(force)
            if refused is not None or current is None:
                return refused or OpResult(EXIT_REFUSED)
            latest = self.store.latest()
            if latest is None or latest.parent is None:
                return OpResult(EXIT_NOTHING, message="nothing to undo")
            try:
                target = self.store.meta(latest.parent)
            except KeyError:
                return OpResult(EXIT_NOTHING, message="the version to undo to was removed")
            return self._apply(
                self.store.read_set(target.version),
                current,
                origin="undo",
                author=author,
                parent=target.parent,
                restores=target.version,
            )

    def redo(self, author: str, *, force: bool = False) -> OpResult:
        """Redo: a new version with the content undone most recently.

        Args:
            author: ``cli:<user>`` or the admin client address.
            force: Overwrite invalid files on disk.

        Returns:
            The outcome.

        Implements:
            CORE-154: Core spec 0.10, §6.8 Configuration history, undo and redo.
        """
        with self.store.lock():
            refused, current = self._prepare(force)
            if refused is not None or current is None:
                return refused or OpResult(EXIT_REFUSED)
            target = self.store.redo_target()
            if target is None:
                return OpResult(EXIT_NOTHING, message="nothing to redo")
            return self._apply(
                self.store.read_set(target.version),
                current,
                origin="redo",
                author=author,
                parent=target.parent,
                restores=target.version,
            )

    def restore(
        self, version: int, author: str, *, section: str | None = None, force: bool = False
    ) -> OpResult:
        """Restore a whole version, or one section of it (CORE-155).

        Args:
            version: The version to restore from.
            author: ``cli:<user>`` or the admin client address.
            section: ``engine``, ``display``, ``layout``, ``pages``, ``backup``,
                ``modules/<id>``, ``modules/_order`` or ``widget_templates/<name>``;
                ``None`` restores everything.
            force: Overwrite invalid files on disk.

        Returns:
            The outcome.

        Implements:
            CORE-155, CORE-156: Core spec 0.10, §6.8 Configuration history, undo and redo.
        """
        with self.store.lock():
            refused, current = self._prepare(force)
            if refused is not None or current is None:
                return refused or OpResult(EXIT_REFUSED)
            try:
                source = self._version(version)
            except KeyError:
                return OpResult(EXIT_INVALID, message=f"version {version} does not exist")
            latest = self.store.latest()
            parent = latest.version if latest is not None else None
            if section is None:
                target = source.set
            else:
                try:
                    target = _restore_section(current, source, section)
                except ValueError as exc:
                    return OpResult(EXIT_INVALID, message=str(exc))
            return self._apply(
                target, current, origin="restore", author=author, parent=parent, restores=version
            )

    # --- views ------------------------------------------------------------------------

    def history(self, limit: int | None = None) -> list[VersionMeta]:
        """List versions, newest first.

        Args:
            limit: At most this many.

        Returns:
            The metadata.
        """
        versions = list(reversed(self.store.versions()))
        if limit is not None:
            versions = versions[:limit]
        return [self.store.meta(v) for v in versions]

    def diff(self, a: int, b: int, *, section: str | None = None) -> dict[str, tuple[str, str]]:
        """Compare two versions, as a whole or for one section.

        Values are shown before ``!env`` substitution and secret fields are
        masked (CORE-218).

        Args:
            a: First version.
            b: Second version.
            section: Limit to one section.

        Returns:
            Changed section to ``(yaml_in_a, yaml_in_b)``.

        Implements:
            CORE-218: Core spec 0.10, §6.8 Configuration history, undo and redo.
        """
        va, vb = self._version(a), self._version(b)
        ra, rb = _section_values(va, mask=False), _section_values(vb, mask=False)
        sa, sb = _section_values(va), _section_values(vb)
        names = list(dict.fromkeys([*ra, *rb]))
        if section is not None:
            names = [section]
        out: dict[str, tuple[str, str]] = {}
        for name in names:
            if ra.get(name) != rb.get(name):
                out[name] = (dump_plain(sa.get(name)), dump_plain(sb.get(name)))
        return out


def _section_values(loaded: _Loaded, *, mask: bool = True) -> dict[str, Any]:
    result = loaded.result
    data = unsubstituted(result.loaded)
    if not isinstance(data, Mapping):
        return {}
    masked = _mask(data, secret_pointers(result)) if mask and result.ok else data
    out: dict[str, Any] = {s: masked.get(s) for s in TOP_SECTIONS if masked.get(s) is not None}
    raw_items = masked.get("modules")
    items: list[Any] = raw_items if isinstance(raw_items, list) else []
    for inst in result.instances:
        out[f"modules/{inst.id}"] = items[inst.index]
    out["modules/_order"] = [i.id for i in result.instances]
    for rel, content in sorted(loaded.set.files.items()):
        if rel.startswith(TEMPLATE_DIR + os.sep):
            name = rel.split(os.sep, 1)[1]
            out[f"widget_templates/{name}"] = content.decode("utf-8", errors="replace")
    return out


# --- section restore -------------------------------------------------------------------


def _doc_holding(config_set: ConfigSet, key: str) -> tuple[str, Any, bool]:
    """Find the file holding a top-level key: ``(file, doc, included)``."""
    main = rt_load(config_set.files[config_set.main].decode("utf-8")) or {}
    node = main.get(key) if hasattr(main, "get") else None
    inc = include_target(node) if node is not None else None
    if inc is not None:
        rel = os.path.normpath(os.path.join(os.path.dirname(config_set.main), inc))
        data = config_set.files.get(rel)
        doc = rt_load(data.decode("utf-8")) if data is not None else None
        return rel, doc, True
    return config_set.main, main, False


def _set_text(config_set: ConfigSet, rel: str, doc: Any) -> ConfigSet:
    return config_set.with_file(rel, rt_dump(doc).encode("utf-8"))


def _restore_section(current: _Loaded, source: _Loaded, section: str) -> ConfigSet:
    """Build the config set with one section taken from another version (CORE-155)."""
    cur = current.set
    if section in TOP_SECTIONS:
        _, src_doc, src_inc = _doc_holding(source.set, section)
        value = src_doc if src_inc else (src_doc.get(section) if src_doc is not None else None)
        rel, doc, included = _doc_holding(cur, section)
        if included:
            if value is None:
                raise ValueError(f"{section} is not in that version")
            return cur.with_file(rel, rt_dump(value).encode("utf-8"))
        if value is None:
            doc.pop(section, None)
        else:
            doc[section] = value
        return _set_text(cur, rel, doc)
    if section.startswith("widget_templates/"):
        rel = os.path.join(TEMPLATE_DIR, section.split("/", 1)[1])
        return cur.with_file(rel, source.set.files.get(rel))
    if section == "modules/_order" or section.startswith("modules/"):
        _, src_doc, src_inc = _doc_holding(source.set, "modules")
        src_list = src_doc if src_inc else (src_doc or {}).get("modules")
        rel, doc, included = _doc_holding(cur, "modules")
        cur_list = doc if included else doc.get("modules")
        if cur_list is None:
            if included:
                raise ValueError("the modules list is missing")
            doc["modules"] = cur_list = rt_load("[]")
        src_ids = {i.id: i.index for i in source.result.instances}
        cur_ids = {i.id: i.index for i in current.result.instances}
        if section == "modules/_order":
            order = [i.id for i in source.result.instances]
            id_at = {idx: iid for iid, idx in cur_ids.items()}
            items = list(cur_list)

            def rank(k: int) -> int:
                iid = id_at.get(k)
                return order.index(iid) if iid in order else len(order) + k

            reordered = [items[k] for k in sorted(range(len(items)), key=rank)]
            cur_list.clear()
            cur_list.extend(reordered)
        else:
            iid = section.split("/", 1)[1]
            if iid not in src_ids or src_list is None:
                raise ValueError(f"{section} is not in that version")
            item = src_list[src_ids[iid]]
            if iid in cur_ids:
                cur_list[cur_ids[iid]] = item
            else:
                cur_list.append(item)
        if included:
            return cur.with_file(rel, rt_dump(cur_list).encode("utf-8"))
        return _set_text(cur, rel, doc)
    raise ValueError(f"unknown section {section!r}")


# --- the engine's recorder -------------------------------------------------------------


class HistoryRecorder:
    """Records the versions the engine applies (``VersionRecorder`` for hot reload).

    When the config set on disk equals the latest version (for example after
    ``mm3 config undo`` wrote it), no new version is made (CORE-217).

    Args:
        history: The history of the engine's configuration.

    Implements:
        CORE-153, CORE-217: Core spec 0.10, §6.8 Configuration history, undo and redo.
    """

    def __init__(self, history: ConfigHistory) -> None:
        self.history = history

    def _record(self, result: ConfigResult, sections: list[str], origin: str) -> int:
        store = self.history.store
        with store.lock():
            config_set = ConfigSet.collect(result.path, result.loaded.files)
            latest = store.latest()
            if latest is not None and latest.sha256 == config_set.sha256():
                return latest.version
            if latest is None:
                meta = store.add(
                    config_set, origin="initial", author="file", parent=None, sections=[]
                )
            else:
                meta = store.add(
                    config_set,
                    origin="file",
                    author="file",
                    parent=latest.version,
                    sections=sections,
                )
            keep = result.config.engine.history.keep if result.config is not None else 200
            store.prune(keep)
            return meta.version

    async def record(
        self, result: ConfigResult, sections: list[str], origin: str, changed: bool = True
    ) -> int:
        """Record a valid configuration; returns its version number.

        Args:
            result: The configuration.
            sections: Its changed sections.
            origin: What caused the reload.
            changed: Unused; the config set's hash decides (CORE-217).

        Returns:
            The version number.
        """
        return await asyncio.to_thread(self._record, result, sections, origin)
