"""Backup and restore as installer operations (CORE-160 to CORE-166, CORE-359 to CORE-366).

``BackupRunner`` is the orchestration: it makes backups (manual and scheduled),
takes the pre-operation backup before every risky operation, restores with an
automatic rollback and keeps the remote copies. The pieces it drives are
:class:`~mm3.installer.backup.BackupService` (archives), age, rclone
(:class:`~mm3.installer.remotes.Remotes`), :class:`~mm3.installer.restore.Restorer`
and the :class:`~mm3.installer.operations.Installer` (venv, engine, steps).

Restore order (CORE-162, CORE-363): open and verify the archive, check
compatibility, preview, take the pre-restore backup, build the new venv when
``modules`` is selected, stop ``mm3.service``, write ``secrets``, ``data`` and
``config``, switch the venv, start the engine and check it. When the engine
is not READY within 60 s or ``mm3 diag check`` fails, the plain copy of the
pre-restore backup is put back and the engine started again (CORE-163); the
plain copy exists only in the operation's private directory, so no key is
needed.

The new venv is built before the engine stops, so the engine is down only for
the file writes and the start, not for a long build; nothing is switched
before the files are written.

Implements:
    CORE-160: Core spec 0.37, §10.8 Backup and restore.
    CORE-162: Core spec 0.37, §10.8 Backup and restore.
    CORE-163: Core spec 0.37, §10.8 Backup and restore.
    CORE-164: Core spec 0.37, §10.8 Backup and restore.
    CORE-165: Core spec 0.37, §10.8 Backup and restore.
    CORE-360: Core spec 0.37, §10.8 Backup and restore.
    CORE-361: Core spec 0.45, §10.8 Backup and restore.
    CORE-363: Core spec 0.37, §10.8 Backup and restore.
    CORE-364: Core spec 0.37, §10.8 Backup and restore.
    CORE-365: Core spec 0.37, §10.8 Backup and restore.
    CORE-366: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import json
import secrets
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mm3.core.logging import get_logger
from mm3.installer.age import AgeError, encryption_kind
from mm3.installer.backup import (
    ARCHIVE_SUFFIX,
    Archive,
    BackupError,
    BackupService,
    Manifest,
    check_compatible,
    remove_work,
)
from mm3.installer.files import atomic_write
from mm3.installer.operations import Installer, Operation, OperationError, Request
from mm3.installer.paths import Layout
from mm3.installer.remotes import RemoteError, Remotes
from mm3.installer.restore import Restorer, Selection, describe
from mm3.installer.schedule import SettingsReader, StatusFile
from mm3.installer.sections import SectionError
from mm3.installer.uploads import Uploads

__all__ = ["BackupRunner", "default_parts"]

_log = get_logger("installer")
PRE_PARTS = ["config", "data", "modules"]


def default_parts(encrypt_to: str | None, parts: list[str] | None) -> list[str]:
    """``backup.parts``, else config, data and modules (plus secrets with a key) (CORE-244)."""
    if parts:
        return list(parts)
    return [*PRE_PARTS, *(["secrets"] if encrypt_to else [])]


class BackupRunner:
    """Backups and restores (see the module docstring).

    Args:
        layout: The installer's paths.
        service: Archives.
        restorer: Writes the parts of an archive.
        remotes: rclone remotes and copies.
        reader: The running ``backup`` settings.
        status: Where the last results are kept.
        hostname: The host name, for the other-host warning (CORE-362).
        now: Clock, for tests.
    """

    def __init__(
        self,
        layout: Layout,
        service: BackupService,
        restorer: Restorer,
        remotes: Remotes,
        reader: SettingsReader,
        status: StatusFile,
        *,
        hostname: Callable[[], str] = socket.gethostname,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.layout = layout
        self.service = service
        self.restorer = restorer
        self.remotes = remotes
        self.reader = reader
        self.status = status
        self._hostname = hostname
        self._now = now
        self.uploads = Uploads(layout)

    # --- small helpers --------------------------------------------------------------

    def _work(self, op_id: str, tag: str = "bk") -> Path:
        work = self.layout.tmp / f"{tag}-{op_id}"
        remove_work(work)
        work.mkdir(mode=0o700, parents=True)
        return work

    def last_scheduled(self) -> datetime | None:
        """The time of the newest scheduled backup on this mirror."""
        times = [e.sidecar.when for e in self.service.listing() if e.sidecar.kind == "scheduled"]
        return max(times) if times else None

    # --- inspecting an archive ------------------------------------------------------

    def _source(self, source: dict[str, Any], work: Path) -> Path:
        """Return the archive file for a source (CORE-361).

        A source is a local name, a root-given path, a remote name or a staged upload.
        """
        name = source.get("name")
        if source.get("upload") is not None:
            staged = self.uploads.path(str(source["upload"]))
            if staged is None:
                raise BackupError("the uploaded backup is gone; upload it again")
            return staged
        if source.get("path"):
            path = Path(str(source["path"]))
            if not path.is_absolute() or not path.is_file():
                raise BackupError(f"{path} is not a backup file")
            return path
        if not isinstance(name, str):
            raise BackupError("the source needs a backup name")
        if source.get("remote"):
            cfg = self.reader.read().backup.remote
            if cfg is None:
                raise BackupError("backup.remote is not set")
            dest = work / "remote.mm3backup"
            try:
                self.remotes.fetch(cfg.name, cfg.path, name, dest)
            except RemoteError as exc:
                raise BackupError(f"could not fetch {name}: {exc}") from exc
            return dest
        return self.service.find(name)[1]

    def _secret(self, source: Path, key: str | None) -> str | None:
        if key:
            return key
        if encryption_kind(source) == "key":
            return self.service.kept_identity()
        return None

    def _warnings(self, manifest: Manifest) -> list[str]:
        warnings = check_compatible(manifest)
        if manifest.hostname != self._hostname():
            warnings.append(f"the backup is from another host ({manifest.hostname})")
        return warnings

    def inspect(
        self,
        source: dict[str, Any],
        key: str | None,
        *,
        parts: list[str] | None = None,
        instance: str | None = None,
        installed: list[dict[str, str]] | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Open, verify and describe an archive (``verify`` and ``preview`` routes).

        Args:
            source: ``{name}``, ``{name, remote: true}`` or ``{path}``.
            key: An age identity or the archive passphrase, for an encrypted archive.
            parts: The parts a restore would take (preview only).
            instance: One module instance (preview only).
            installed: The modules installed now (preview only).
            preview: Add what a restore would change.

        Returns:
            The manifest summary, and for a preview ``parts`` and ``lines``.

        Raises:
            BackupError: The archive is refused.
        """
        work = self._work(secrets.token_hex(6), "inspect")
        try:
            file = self._source(source, work)
            plain = self.service.open_plain(file, self._secret(file, key), work)
            with Archive(plain) as archive:
                manifest = archive.verify()
                warnings = self._warnings(manifest)
                out: dict[str, Any] = {
                    "ok": True,
                    "encrypted": plain != file,
                    "hostname": manifest.hostname,
                    "created": manifest.created,
                    "kind": manifest.kind,
                    "engine_version": manifest.engine_version,
                    "api_version": manifest.api_version,
                    "parts": manifest.parts,
                    "omitted": manifest.omitted,
                    "files": len(manifest.files),
                    "warnings": warnings,
                }
                if preview:
                    selection = Selection.build(manifest, parts, instance)
                    result = self.restorer.preview(archive, selection, installed or [], warnings)
                    out["preview"] = result["parts"]
                    out["lines"] = describe(result)
            return out
        finally:
            remove_work(work)

    # --- keys -----------------------------------------------------------------------

    def keygen(self, *, keep: bool) -> dict[str, str]:
        """A new age identity (CORE-364).

        Args:
            keep: Keep the identity on the mirror as ``backup.key`` (0600).

        Returns:
            ``{public, identity}``; the identity is shown here once.

        Raises:
            BackupError: age could not make a key.
        """
        try:
            identity = self.service.age.generate_identity()
        except AgeError as exc:
            raise BackupError(str(exc)) from exc
        if keep:
            atomic_write(self.layout.backup_key, identity.text.encode("utf-8"), mode=0o600)
        return {"public": identity.public, "identity": identity.text}

    # --- listing --------------------------------------------------------------------

    def remote_listing(self) -> list[dict[str, Any]]:
        """The backups on ``backup.remote``, from their sidecars (CORE-362, CORE-365).

        Raises:
            BackupError: No remote is set, or it does not answer.
        """
        cfg = self.reader.read().backup.remote
        if cfg is None:
            raise BackupError("backup.remote is not set")
        try:
            names = [
                n for n in self.remotes.names(cfg.name, cfg.path) if n.endswith(ARCHIVE_SUFFIX)
            ]
            out = []
            for name in names:
                meta = self.remotes.sidecar(cfg.name, cfg.path, name)
                out.append({"name": name, **(meta or {})})
        except RemoteError as exc:
            raise BackupError(str(exc)) from exc
        out.sort(key=lambda e: str(e.get("created", "")), reverse=True)
        return out

    # --- backup operations ----------------------------------------------------------

    def pre_operation(self, inst: Installer, op: Operation) -> None:
        """The backup before a risky operation (CORE-366); on failure the operation does not start.

        Raises:
            OperationError: The backup could not be made.
        """
        work = self._work(op.id)
        try:
            self._pre(inst, op, work, keep_plain=False)
        finally:
            remove_work(work)

    def _pre(
        self,
        inst: Installer,
        op: Operation,
        work: Path,
        *,
        keep_plain: bool,
        need_secrets: bool = False,
    ) -> Any:
        cfg = self.reader.read().backup
        parts = [*PRE_PARTS, *(["secrets"] if cfg.encrypt_to else [])]
        passphrase = None
        if need_secrets and not cfg.encrypt_to:
            # The restore replaces secrets, so the rollback needs them: they go into
            # this archive under a passphrase nobody keeps, used only from the plain
            # copy in the private work directory (CORE-163).
            parts.append("secrets")
            passphrase = secrets.token_urlsafe(24)
        inst.step(op, "backing_up", "pre-operation backup")
        try:
            created = self.service.create(
                "pre",
                parts,
                work,
                recipient=cfg.encrypt_to,
                passphrase=passphrase,
                keep_plain=keep_plain,
            )
        except BackupError as exc:
            raise OperationError(
                f"the pre-operation backup failed, nothing was changed: {exc}"
            ) from exc
        inst.step(op, "backing_up", f"pre-operation backup {created.entry.name}")
        return created

    def backup(self, inst: Installer, op: Operation, request: Request) -> None:
        """A manual or scheduled backup (CORE-160, CORE-360, CORE-361).

        Raises:
            OperationError: The backup failed.
        """
        opts = request.options
        if opts.get("pre"):
            # An engine upgrade by mm3-install.sh asks for its pre-operation backup (CORE-366).
            work = self._work(op.id)
            try:
                created = self._pre(inst, op, work, keep_plain=False)
            finally:
                remove_work(work)
            op.to = created.entry.name
            inst.step(op, "done", created.entry.name)
            return
        scheduled = bool(opts.get("scheduled"))
        settings = self.reader.read()
        cfg = settings.backup
        recipient = opts.get("recipient")
        if recipient is None and (scheduled or opts.get("use_configured_key")):
            recipient = cfg.encrypt_to
        passphrase = opts.get("archive_passphrase")
        parts = default_parts(
            recipient or (cfg.encrypt_to if scheduled else None),
            list(opts["parts"])
            if opts.get("parts")
            else (list(cfg.parts) if scheduled and cfg.parts else None),
        )
        if not (recipient or passphrase):
            parts = [p for p in parts if p != "secrets"] if not opts.get("parts") else parts
        work = self._work(op.id)
        try:
            inst.step(op, "backing_up", "making the archive")
            try:
                created = self.service.create(
                    "scheduled" if scheduled else "manual",
                    parts,
                    work,
                    recipient=recipient,
                    passphrase=passphrase,
                )
            except BackupError as exc:
                if scheduled:
                    self.status.record("scheduled", self._result(False, None, str(exc)))
                raise OperationError(str(exc)) from exc
            op.to = created.entry.name
            note = (
                f" (left out: {', '.join(created.omitted)}, not encrypted)"
                if created.omitted
                else ""
            )
            inst.step(op, "backing_up", f"{created.entry.name}{note}")
            if scheduled:
                self.status.record(
                    "scheduled", self._result(True, created.entry.name, None, created.omitted)
                )
                self._after_scheduled(inst, op, created.path, settings, created.entry.name)
            inst.step(op, "done", created.entry.name)
        finally:
            remove_work(work)

    def _result(
        self, ok: bool, name: str | None, error: str | None, omitted: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        return {
            "at": self._now().isoformat(),
            "ok": ok,
            "name": name,
            "error": error,
            "omitted": list(omitted),
        }

    def _after_scheduled(
        self, inst: Installer, op: Operation, archive: Path, settings: Any, name: str
    ) -> None:
        cfg = settings.backup
        gone = self.service.prune(cfg.keep, settings.tz)
        if gone:
            inst.step(op, "backing_up", f"removed {len(gone)} old backups (keep)")
        if cfg.remote is None:
            return
        try:
            self.remotes.push(cfg.remote.name, cfg.remote.path, archive, Path(f"{archive}.json"))
            removed = self.remotes.prune(cfg.remote.name, cfg.remote.path, cfg.keep, settings.tz)
        except RemoteError as exc:
            # The local backup stays; the failure is logged and shown (CORE-365).
            _log.warning("backup_remote_failed", remote=cfg.remote.name, reason=str(exc))
            self.status.record("remote", self._result(False, name, str(exc)))
            inst.step(op, "backing_up", f"remote copy to {cfg.remote.name} failed: {exc}")
            return
        self.status.record("remote", self._result(True, name, None))
        inst.step(
            op,
            "backing_up",
            f"copied to {cfg.remote.name}" + (f", removed {len(removed)} there" if removed else ""),
        )

    # --- restore --------------------------------------------------------------------

    def restore(self, inst: Installer, op: Operation, request: Request) -> None:
        """Restore parts of a backup with automatic rollback (CORE-162, CORE-163, CORE-363).

        Raises:
            OperationError: Nothing was changed, or the rollback also failed.
        """
        opts = request.options
        work = self._work(op.id, "rs")
        try:
            self._restore(inst, op, opts, work)
        except BackupError as exc:
            raise OperationError(str(exc)) from exc
        finally:
            remove_work(work)
            staged = opts["source"].get("upload")
            if staged is not None:
                self.uploads.discard(str(staged))  # CORE-361: a staged file ends with its restore

    def _restore(self, inst: Installer, op: Operation, opts: dict[str, Any], work: Path) -> None:
        inst.step(op, "verifying", "opening the backup")
        file = self._source(opts["source"], work)
        plain = self.service.open_plain(file, self._secret(file, opts.get("key")), work)
        with Archive(plain) as archive:
            manifest = archive.verify()
            warnings = self._warnings(manifest)
            selection = Selection.build(manifest, opts.get("parts"), opts.get("instance"))
            installed = inst.installed(inst.store.kept())
            preview = self.restorer.preview(archive, selection, installed, warnings)
            for line in describe(preview):
                inst.step(op, "verifying", line)
            wanted = []
            if "modules" in selection.parts:
                info = preview["parts"]["modules"]
                wanted = [(m["name"], m["to"]) for m in info["install"]]
                for rec in info["manual"]:
                    inst.step(
                        op,
                        "verifying",
                        f"install by hand: {rec['name']} {rec['version']} ({rec['origin']})",
                    )
            work_pre = work / "pre"
            created = self._pre(
                inst, op, work_pre, keep_plain=True, need_secrets="secrets" in selection.parts
            )
            old = inst.builder.current()
            new = None
            if wanted:
                new = inst.stage_exact(op, wanted)
            self._write_and_start(inst, op, archive, selection, created.plain, old, new)

    def _write_and_start(
        self,
        inst: Installer,
        op: Operation,
        archive: Archive,
        selection: Selection,
        pre_plain: Path | None,
        old: Path | None,
        new: Path | None,
    ) -> None:
        inst.step(op, "restoring", "stopping mm3.service")
        if not inst.engine.stop():
            if new is not None:
                inst.builder.discard(new)
            raise OperationError("could not stop mm3.service; nothing was changed")
        switched = False
        reason: str | None = None
        try:
            for line in self.restorer.apply(archive, selection):
                inst.step(op, "restoring", line)
            if new is not None:
                inst.step(op, "switching", f"venv {new.name}")
                inst.builder.switch(new)
                switched = True
            if "config" in selection.parts:
                self._marker(op)
        except (OSError, BackupError, SectionError) as exc:
            reason = f"the restore failed while writing: {exc}"
        if reason is None:
            inst.step(op, "restarting", "starting mm3.service")
            if not inst.engine.start():
                reason = "the engine was not READY within 60 s"
            else:
                inst.step(op, "checking", "mm3 diag check")
                venv = inst.builder.current()
                result = inst.engine.diag_check(venv) if venv is not None else None
                if result is not None and result.returncode != 0:
                    reason = (
                        f"mm3 diag check failed (exit {result.returncode}): {result.output.strip()}"
                    )
        if reason is None:
            inst.step(op, "done", "restored")
            if switched:
                inst.builder.prune()
            return
        self._roll_back(inst, op, reason, pre_plain, old, new if switched else None)
        if new is not None:
            inst.builder.discard(new)

    def _marker(self, op: Operation) -> None:
        data = json.dumps({"at": self._now().isoformat(), "operation": op.id}).encode()
        atomic_write(self.layout.restore_marker, data, mode=0o640, owner=self.restorer.owner)

    def _roll_back(
        self,
        inst: Installer,
        op: Operation,
        reason: str,
        pre_plain: Path | None,
        old: Path | None,
        switched_to: Path | None,
    ) -> None:
        """Put the pre-restore backup back and start the engine again (CORE-163)."""
        inst.step(op, "restoring", f"{reason}; putting the pre-restore backup back")
        try:
            inst.engine.stop()
            if pre_plain is None:
                raise OperationError("no pre-restore backup")
            with Archive(pre_plain) as pre:
                manifest = pre.verify()
                self.restorer.apply(pre, Selection.build(manifest, None, None))
            if switched_to is not None and old is not None:
                inst.builder.switch(old)
            self._marker(op)
        except (OSError, BackupError, SectionError, OperationError) as exc:
            op.error = f"{reason}; the rollback failed too ({exc}); the pre-restore backup is kept"
            inst.step(op, "failed", op.error)
            return
        back = inst.engine.start()
        op.error = reason if back else f"{reason}; after rollback the engine is still not READY"
        inst.step(op, "rolled_back", op.error)
