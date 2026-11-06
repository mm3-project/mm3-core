"""Install, update, remove and restore previous: one operation at a time.

An operation passes the states ``queued``, ``verifying``, ``building``,
``switching``, ``restarting``, ``checking`` and ends ``done``,
``rolled_back`` or ``failed`` (CORE-355). It is kept with its steps in the
installer's state directory, so it outlives the engine's restart, and every
request is logged with time, client, package, versions and outcome (CORE-136).

- **verifying**: the installer's own index copy is verified again (CORE-351);
  the release must be listed and compatible (CORE-124, CORE-131); the wheel is
  downloaded from PyPI, its SHA-256 checked against the index and its PEP 740
  provenance verified against the entry's publisher (CORE-352). Unlisted
  sources (``pypi``, ``local``) are accepted only from a root peer (CORE-353)
  and recorded with their origin.
- **building**: a staged venv with the change applied, built without the
  installer's rights, made read-only, frozen and compared with the current one
  (CORE-354, CORE-367); then the import test (CORE-134).
- **switching**, **restarting**, **checking**: the symlink moves, the engine
  restarts and must be READY within 60 s, then ``mm3 diag check`` must pass;
  otherwise the symlink moves back and the engine restarts again (CORE-135,
  CORE-357). The last 3 venvs are kept.

Implements:
    CORE-130, CORE-131, CORE-134, CORE-135, CORE-136: Core spec 0.33,
        §10.7 Installer service.
    CORE-126, CORE-124, CORE-352, CORE-353: Core spec 0.33, §10.6 Module catalog.
    CORE-354: Core spec 0.33, §10.7 Installer service.
    CORE-355: Core spec 0.35, §10.7 Installer service.
    CORE-367: Core spec 0.35, §10.7 Installer service.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import Version

from mm3.core.catalog import (
    CatalogError,
    Entry,
    Index,
    Release,
    compatible,
    find_entry,
    newest_compatible,
)
from mm3.core.logging import get_logger
from mm3.installer.fetch import LIMIT_PROVENANCE, LIMIT_WHEEL, Fetcher, FetchError
from mm3.installer.files import read_json, write_json
from mm3.installer.paths import Layout
from mm3.installer.service import Engine
from mm3.installer.store import CatalogStore
from mm3.installer.venv import BuildError, Freeze, VenvBuilder, conflicts, only_required_by
from mm3.installer.verify import Verification, VerifyError

__all__ = [
    "KEPT_OPERATIONS",
    "Action",
    "BusyError",
    "Installer",
    "ModuleRecord",
    "Operation",
    "OperationError",
    "Origin",
    "Request",
    "Source",
    "State",
]

Action = Literal["install", "update", "remove", "restore_previous"]
Source = Literal["catalog", "pypi", "local"]
Origin = Literal["catalog", "pypi", "local"]
State = Literal[
    "queued",
    "verifying",
    "building",
    "switching",
    "restarting",
    "checking",
    "done",
    "rolled_back",
    "failed",
]
FINAL: frozenset[str] = frozenset({"done", "rolled_back", "failed"})
KEPT_OPERATIONS = 100
"""Operations listed by ``GET /v1/operations`` (CORE-355)."""

_log = get_logger("installer")


class OperationError(Exception):
    """An operation failed; the message is its ``error``."""


class BusyError(RuntimeError):
    """Another operation is running (409)."""


@dataclass(frozen=True)
class Request:
    """A validated ``POST /v1/operations`` body.

    Args:
        action: What to do.
        name: The module's PyPI name (not for ``restore_previous``).
        version: A version for ``install``; the newest compatible by default.
        source: ``catalog``, or ``pypi`` / ``local`` from a root peer.
        path: The wheel file for ``local``.
        client: Who asked: the admin client address or ``cli``.
        peer_uid: The socket peer's uid (``SO_PEERCRED``).
    """

    action: Action
    name: str | None = None
    version: str | None = None
    source: Source = "catalog"
    path: str | None = None
    client: str = ""
    peer_uid: int = -1


@dataclass(frozen=True)
class ModuleRecord:
    """A module installed by the installer: version, origin and kept wheel."""

    name: str
    version: str
    origin: Origin
    wheel: str | None = None


@dataclass
class Operation:
    """One operation and its steps (CORE-355)."""

    id: str
    action: Action
    name: str | None
    client: str
    peer_uid: int
    source: Source
    created_at: str
    state: State = "queued"
    from_: str | None = None
    to: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    finished_at: str | None = None

    def public(self) -> dict[str, Any]:
        """The JSON form of ``GET /v1/operations/{id}``."""
        out = asdict(self)
        out["from"] = out.pop("from_")
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Operation:
        """Read the JSON form."""
        data = dict(data)
        data["from_"] = data.pop("from", None)
        return cls(**data)


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pick_wheel(release: Release) -> str:
    """The first wheel of ``release`` this interpreter can install.

    Raises:
        OperationError: No compatible wheel.
    """
    supported = set(sys_tags())
    for file in release.files:
        try:
            _, _, _, tags = parse_wheel_filename(file.filename)
        except InvalidWheelFilename:
            continue
        if tags & supported:
            return file.filename
    raise OperationError(f"no wheel of {release.version} fits this system")


class Installer:
    """The operation runner behind the protocol of CORE-355.

    Args:
        layout: The installer's paths.
        store: The verified index (CORE-351).
        fetcher: Downloads (CORE-370).
        verification: Provenance checks (CORE-352).
        builder: Staged venvs (CORE-134, CORE-367).
        engine: Restart and checks (CORE-357).
        now: Clock, for tests.
    """

    def __init__(
        self,
        layout: Layout,
        store: CatalogStore,
        fetcher: Fetcher,
        verification: Verification,
        builder: VenvBuilder,
        engine: Engine,
        *,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.layout = layout
        self.store = store
        self.fetcher = fetcher
        self.verification = verification
        self.builder = builder
        self.engine = engine
        self._now = now
        self._mutex = threading.Lock()
        self._running: Operation | None = None
        self.listeners: list[Callable[[Operation, dict[str, Any] | None], None]] = []
        layout.operations.mkdir(mode=0o700, parents=True, exist_ok=True)
        layout.wheels.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fail_interrupted()

    # --- kept operations ------------------------------------------------------------

    def _path(self, op_id: str) -> Path:
        return self.layout.operations / f"{op_id}.json"

    def _save(self, op: Operation) -> None:
        write_json(self._path(op.id), op.public())

    def get(self, op_id: str) -> Operation | None:
        """A kept operation, or None."""
        if not op_id.isalnum():
            return None
        data = read_json(self._path(op_id))
        return Operation.from_json(data) if isinstance(data, dict) else None

    def events_path(self, op_id: str) -> Path:
        """The NDJSON file of an operation's steps."""
        return self.layout.operations / f"{op_id}.events.ndjson"

    def recent(self, limit: int = KEPT_OPERATIONS) -> list[Operation]:
        """The last ``limit`` operations, newest first."""
        ops = []
        for path in self.layout.operations.glob("*.json"):
            data = read_json(path)
            if isinstance(data, dict):
                ops.append(Operation.from_json(data))
        ops.sort(key=lambda o: o.created_at, reverse=True)
        return ops[:limit]

    def _trim(self) -> None:
        for op in self.recent(limit=10_000)[KEPT_OPERATIONS:]:
            self._path(op.id).unlink(missing_ok=True)
            self.events_path(op.id).unlink(missing_ok=True)

    def _fail_interrupted(self) -> None:
        # An operation left unfinished by a crash of the installer cannot be
        # resumed safely: it is marked failed and the symlink is left as is.
        for op in self.recent(limit=10_000):
            if op.state not in FINAL:
                op.state = "failed"
                op.error = "interrupted: the installer stopped during the operation"
                op.finished_at = self._now().isoformat()
                self._save(op)

    @property
    def busy(self) -> bool:
        """Whether an operation is running."""
        return self._running is not None

    # --- modules.json ---------------------------------------------------------------

    def _records(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.layout.modules_file, {})
        venvs = data.get("venvs", {}) if isinstance(data, dict) else {}
        return venvs if isinstance(venvs, dict) else {}

    def records(self, venv: Path | None) -> dict[str, ModuleRecord]:
        """The modules the installer recorded for ``venv``, by normalized name."""
        if venv is None:
            return {}
        raw = self._records().get(venv.name, {})
        return {k: ModuleRecord(**v) for k, v in raw.items()}

    def _set_records(self, venv: Path, records: dict[str, ModuleRecord]) -> None:
        all_ = self._records()
        all_[venv.name] = {k: asdict(v) for k, v in records.items()}
        kept = {p.name for p in self.builder.kept()}
        all_ = {k: v for k, v in all_.items() if k in kept}
        write_json(self.layout.modules_file, {"venvs": all_})
        self._drop_unused_wheels(all_)

    def _drop_unused_wheels(self, all_: dict[str, dict[str, Any]]) -> None:
        used = {r.get("wheel") for recs in all_.values() for r in recs.values()}
        for wheel in self.layout.wheels.glob("*.whl"):
            if wheel.name not in used:
                wheel.unlink(missing_ok=True)

    def installed(self, index: Index | None) -> list[dict[str, str]]:
        """Installed modules with their origin, for ``GET /v1/status`` (CORE-355).

        Modules the installer recorded keep their origin; others in the venv
        (from ``mm3-install.sh``) are ``catalog`` when the index lists them,
        else ``pypi``.

        Raises:
            BuildError: The current venv cannot be listed.
        """
        current = self.builder.current()
        if current is None:
            return []
        records = self.records(current)
        out = []
        for key, dist in sorted(self._freeze_cached(current).items()):
            if not dist.module:
                continue
            rec = records.get(key)
            if rec is not None:
                origin: str = rec.origin
            elif index is not None and find_entry(index, dist.name) is not None:
                origin = "catalog"
            else:
                origin = "pypi"
            out.append({"name": dist.name, "version": dist.version, "origin": origin})
        return out

    _freeze_cache: tuple[str, Freeze] | None = None

    def _freeze_cached(self, venv: Path) -> Freeze:
        cached = self._freeze_cache
        if cached is not None and cached[0] == venv.name:
            return cached[1]
        freeze = self.builder.freeze(venv)
        self._freeze_cache = (venv.name, freeze)
        return freeze

    # --- submit and run -------------------------------------------------------------

    def submit(self, request: Request) -> Operation:
        """Accept an operation; the caller runs :meth:`run` for it.

        Re-authentication and peer checks are the caller's (the server); this
        refuses a second operation while one runs.

        Raises:
            BusyError: An operation is running.
        """
        with self._mutex:
            if self._running is not None:
                raise BusyError(f"operation {self._running.id} is running")
            op = Operation(
                id=secrets.token_hex(8),
                action=request.action,
                name=request.name,
                client=request.client,
                peer_uid=request.peer_uid,
                source=request.source,
                created_at=self._now().isoformat(),
            )
            self._running = op
        self._save(op)
        self._trim()
        self._step(op, "queued", "accepted")
        return op

    def _step(self, op: Operation, state: State, detail: str) -> None:
        op.state = state
        step = {"at": self._now().isoformat(), "state": state, "detail": detail}
        op.steps.append(step)
        self._save(op)
        with self.events_path(op.id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(step) + "\n")
        for listener in list(self.listeners):
            listener(op, step)

    def run(self, op: Operation, request: Request) -> Operation:
        """Run an accepted operation to its end (blocking; call off the loop)."""
        try:
            if request.action == "restore_previous":
                self._restore_previous(op)
            else:
                self._change(op, request)
        except (OperationError, BuildError, CatalogError, FetchError, VerifyError) as exc:
            self._fail(op, str(exc))
        except Exception as exc:  # the operation must always end
            _log.exception("installer_operation_crashed", id=op.id)
            self._fail(op, f"internal error: {exc}")
        finally:
            op.finished_at = self._now().isoformat()
            self._save(op)
            _log.info(
                "installer_operation",
                id=op.id,
                client=op.client,
                peer_uid=op.peer_uid,
                action=op.action,
                package=op.name,
                source=op.source,
                from_version=op.from_,
                to_version=op.to,
                outcome=op.state,
                error=op.error,
            )
            self._freeze_cache = None
            shutil.rmtree(self.layout.tmp / f"op-{op.id}", ignore_errors=True)
            with self._mutex:
                self._running = None
            for listener in list(self.listeners):
                listener(op, None)
        return op

    def _fail(self, op: Operation, error: str) -> None:
        op.error = error
        self._step(op, "failed", error)

    # --- the pipeline ---------------------------------------------------------------

    def _change(self, op: Operation, request: Request) -> None:
        current = self.builder.current()
        if current is None:
            raise OperationError(f"{self.layout.venv_link} is not a symlink to a venv")
        name = request.name or ""
        key = canonicalize_name(name)
        self._step(op, "verifying", f"checking {name}")
        old = self.builder.freeze(current)
        installed = old.get(key)
        if request.action == "install" and installed is not None:
            raise OperationError(f"{name} {installed.version} is installed; use update")
        if request.action in ("update", "remove") and installed is None:
            raise OperationError(f"{name} is not installed")
        op.from_ = installed.version if installed else None
        records = self.records(current)
        work = self._work(op)
        # Kept wheels live in the installer's private state; the build child
        # reads read-only copies in the operation's work directory.
        local_wheels: dict[str, Path] = {}
        for k, r in records.items():
            if r.wheel is not None and k != key:
                copy = work / r.wheel
                shutil.copyfile(self.layout.wheels / r.wheel, copy)
                copy.chmod(0o644)
                local_wheels[k] = copy
        drop: set[str] = {key}
        new_req: list[str] = []
        record: ModuleRecord | None = None
        wheel_file: Path | None = None
        if request.action == "remove":
            drop |= only_required_by(old, key)
            self._step(
                op, "verifying", f"removing {name} and {len(drop) - 1} packages only it used"
            )
        else:
            wheel_file, record = self._obtain(
                op, request, installed.version if installed else None, work
            )
            new_req = [str(wheel_file)]
        self._step(op, "building", "building the staged venv")
        pins = self.builder.pins(old, drop=drop, local_wheels=local_wheels)
        new_venv = self.builder.build([*pins, *new_req], self._now())
        try:
            new = self.builder.freeze(new_venv)
            bad = conflicts(old, new, drop)
            if bad:
                raise OperationError("would change packages others need: " + "; ".join(bad))
            if record is not None and canonicalize_name(record.name) not in new:
                raise OperationError(f"{record.name} is missing from the new venv")
            self._step(op, "building", "import test")
            self.builder.import_test(new_venv, record.name if record else None)
        except BaseException:
            self.builder.discard(new_venv)
            raise
        new_records = {k: v for k, v in records.items() if k not in drop}
        if record is not None:
            new_records[canonicalize_name(record.name)] = record
        self._set_records(new_venv, new_records)
        self._switch_and_check(op, current, new_venv)

    def _work(self, op: Operation) -> Path:
        self.layout.tmp.mkdir(mode=0o755, parents=True, exist_ok=True)
        work = self.layout.tmp / f"op-{op.id}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(mode=0o755)
        return work

    def _obtain(
        self, op: Operation, request: Request, installed: str | None, work: Path
    ) -> tuple[Path, ModuleRecord]:
        """Find, download and verify the wheel; return it and its record."""
        name = request.name or ""
        if request.source == "local":
            return self._local(op, request, work)
        if request.source == "catalog":
            entry = find_entry(self._index(), name)
            if entry is None:
                raise OperationError(f"{name} is not in the catalog index")
            release = self._release(entry, request, installed)
            op.to = release.version
            filename = pick_wheel(release)
            expected = next(f.sha256 for f in release.files if f.filename == filename)
            wheel = self._download(entry.pypi, release.version, filename, work)
            if _sha256(wheel) != expected:
                raise OperationError(f"{filename}: SHA-256 differs from the index")
            self._step(op, "verifying", f"{filename}: SHA-256 matches the index")
            self._provenance(entry, release.version, filename, wheel)
            self._step(
                op, "verifying", f"{filename}: provenance verified ({entry.publisher.repository})"
            )
            return self._keep(wheel), ModuleRecord(
                entry.pypi, release.version, "catalog", wheel.name
            )
        # Unlisted from PyPI, root only: no index, no provenance check.
        version = request.version or self._pypi_latest(name)
        op.to = version
        meta = self._pypi_release(name, version)
        files = [u for u in meta if u.get("packagetype") == "bdist_wheel"]
        supported = set(sys_tags())
        chosen = next(
            (u for u in files if parse_wheel_filename(u["filename"])[3] & supported), None
        )
        if chosen is None:
            raise OperationError(f"{name} {version} has no wheel for this system")
        wheel = self._download(name, version, chosen["filename"], work)
        if _sha256(wheel) != chosen.get("digests", {}).get("sha256"):
            raise OperationError(f"{chosen['filename']}: SHA-256 differs from PyPI's")
        self._step(op, "verifying", f"{chosen['filename']}: unlisted, from PyPI")
        return self._keep(wheel), ModuleRecord(name, version, "pypi", wheel.name)

    def _local(self, op: Operation, request: Request, work: Path) -> tuple[Path, ModuleRecord]:
        source = Path(request.path or "")
        try:
            dist, version, _, _ = parse_wheel_filename(source.name)
        except InvalidWheelFilename as exc:
            raise OperationError(f"{source.name} is not a wheel file name") from exc
        if canonicalize_name(dist) != canonicalize_name(request.name or dist):
            raise OperationError(f"{source.name} is not a wheel of {request.name}")
        if not source.is_file():
            raise OperationError(f"{source} is not a file")
        wheel = work / source.name
        shutil.copyfile(source, wheel)
        op.to = str(version)
        self._step(op, "verifying", f"{source.name}: unlisted, local wheel")
        return self._keep(wheel), ModuleRecord(str(dist), str(version), "local", wheel.name)

    def _keep(self, wheel: Path) -> Path:
        kept = self.layout.wheels / wheel.name
        shutil.copyfile(wheel, kept)
        kept.chmod(0o600)
        # The build child reads the work directory copy (0644, owned by the
        # installer, so it cannot be swapped); the kept copy stays private.
        wheel.chmod(0o644)
        return wheel

    def _index(self) -> Index:
        try:
            return self.store.verified()
        except CatalogError:
            # Expired or missing: one refresh, then the check again.
            self.store.refresh()
            return self.store.verified()

    @staticmethod
    def _release(entry: Entry, request: Request, installed: str | None) -> Release:
        if request.version:
            release = entry.release(request.version)
            if release is None:
                raise OperationError(f"{entry.pypi} {request.version} is not in the index")
            if not compatible(release):
                raise OperationError(
                    f"{entry.pypi} {release.version} supports Module API {release.api}, "
                    "not this engine's"
                )
        else:
            found = newest_compatible(entry)
            if found is None:
                raise OperationError(f"no release of {entry.pypi} is compatible with this engine")
            release = found
        if installed is not None and Version(release.version) <= Version(installed):
            raise OperationError(f"{entry.pypi} {installed} is already the newest compatible")
        return release

    def _pypi_release(self, name: str, version: str) -> list[dict[str, Any]]:
        url = f"https://pypi.org/pypi/{name}/{version}/json"
        data = json.loads(self.fetcher.get(url, LIMIT_PROVENANCE, accept="application/json"))
        urls = data.get("urls", []) if isinstance(data, dict) else []
        return [u for u in urls if isinstance(u, dict)]

    def _pypi_latest(self, name: str) -> str:
        url = f"https://pypi.org/pypi/{name}/json"
        data = json.loads(self.fetcher.get(url, LIMIT_PROVENANCE, accept="application/json"))
        return str(data["info"]["version"])

    def _download(self, name: str, version: str, filename: str, work: Path) -> Path:
        urls = self._pypi_release(name, version)
        match = next((u for u in urls if u.get("filename") == filename), None)
        if match is None or not isinstance(match.get("url"), str):
            raise OperationError(f"PyPI has no file {filename} for {name} {version}")
        dest = work / filename
        self.fetcher.download(match["url"], dest, LIMIT_WHEEL)
        return dest

    def _provenance(self, entry: Entry, version: str, filename: str, wheel: Path) -> None:
        url = f"https://pypi.org/integrity/{entry.pypi}/{version}/{filename}/provenance"
        raw = self.fetcher.get(
            url, LIMIT_PROVENANCE, accept="application/vnd.pypi.integrity.v1+json"
        )
        self.verification.verify_wheel(wheel, raw, entry.publisher)

    def _switch_and_check(self, op: Operation, old: Path, new: Path) -> None:
        self._step(op, "switching", f"venv {new.name}")
        self.builder.switch(new)
        self._step(op, "restarting", "restarting mm3.service")
        ready = self.engine.restart()
        reason = None
        if not ready:
            reason = "the engine was not READY within 60 s"
        else:
            self._step(op, "checking", "mm3 diag check")
            result = self.engine.diag_check(new)
            if result.returncode != 0:
                reason = (
                    f"mm3 diag check failed (exit {result.returncode}): {result.output.strip()}"
                )
        if reason is None:
            self._step(op, "done", f"running venv {new.name}")
            self.builder.prune()
            return
        self.builder.switch(old)
        back = self.engine.restart()
        self.builder.discard(new)
        op.error = reason if back else f"{reason}; after rollback the engine is still not READY"
        self._step(op, "rolled_back", op.error)

    def _restore_previous(self, op: Operation) -> None:
        current = self.builder.current()
        previous = self.builder.previous()
        if current is None or previous is None:
            raise OperationError("there is no previous venv to restore")
        op.from_, op.to = current.name, previous.name
        self._step(op, "verifying", f"restoring venv {previous.name}")
        # The previous venv was READY before; a failed check rolls forward
        # again to the venv that was running.
        self._step(op, "switching", f"venv {previous.name}")
        self.builder.switch(previous)
        self._step(op, "restarting", "restarting mm3.service")
        ready = self.engine.restart()
        reason = None
        if not ready:
            reason = "the engine was not READY within 60 s"
        else:
            self._step(op, "checking", "mm3 diag check")
            result = self.engine.diag_check(previous)
            if result.returncode != 0:
                reason = (
                    f"mm3 diag check failed (exit {result.returncode}): {result.output.strip()}"
                )
        if reason is None:
            self._step(op, "done", f"running venv {previous.name}")
            return
        self.builder.switch(current)
        back = self.engine.restart()
        op.error = reason if back else f"{reason}; after rollback the engine is still not READY"
        self._step(op, "rolled_back", op.error)
