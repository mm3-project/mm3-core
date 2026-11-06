"""Restoring the parts of a verified backup (CORE-162, CORE-163, CORE-165, CORE-363).

``preview`` says, part by part, what a restore would change. ``apply`` writes
the selected parts in the order of CORE-363: ``secrets``, ``data`` (each
selected instance's directory replaced whole) and ``config`` (the whole set,
or for one instance only its section, the rest kept). ``modules`` is the
venv's business (the installer rebuilds it, CORE-354). Every write goes
through an open directory descriptor without following symlinks (CORE-373);
files for the engine keep owner ``mm3`` (CORE-359).

The configuration history of an archive is restored only into an empty history
(a fresh mirror): history is append-only, so on a mirror that has one the
restored files simply become its next version, which the engine records with
reason ``restore`` (CORE-363, CORE-153).

Implements:
    CORE-162: Core spec 0.37, §10.8 Backup and restore.
    CORE-163: Core spec 0.37, §10.8 Backup and restore.
    CORE-165: Core spec 0.37, §10.8 Backup and restore.
    CORE-363: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mm3.installer.backup import MODULES_JSON, PARTS, Archive, BackupError, Manifest
from mm3.installer.files import (
    Owner,
    atomic_write,
    open_dir,
    open_subdir,
    put_file,
    remove_tree,
    replace_tree,
)
from mm3.installer.paths import Layout
from mm3.installer.sections import INSTANCE_ID, SectionError, restore_instance

__all__ = ["Restorer", "Selection", "archive_modules", "describe", "instances_in"]

_SECRET_NAMES = ("mm3.env", "tokens.json", "auth.json", "rclone.conf")


@dataclass(frozen=True)
class Selection:
    """What to restore: parts, and optionally a single module instance (CORE-165).

    Attributes:
        parts: The parts, from ``config``, ``data``, ``modules``, ``secrets``.
        instance: A module instance id; then only ``config`` and ``data`` apply.
    """

    parts: tuple[str, ...]
    instance: str | None = None

    @classmethod
    def build(
        cls, manifest: Manifest, parts: Iterable[str] | None, instance: str | None
    ) -> Selection:
        """Validate a request against what the archive holds.

        Args:
            manifest: The archive's manifest.
            parts: The parts asked for, or None for every part the archive holds.
            instance: A module instance id, or None.

        Returns:
            The selection.

        Raises:
            BackupError: A part the archive does not hold, an unknown part, or
                ``modules`` / ``secrets`` together with an instance.
        """
        chosen = list(dict.fromkeys(parts if parts is not None else manifest.parts))
        unknown = [p for p in chosen if p not in PARTS]
        if unknown or not chosen:
            raise BackupError(f"parts must be a non-empty list of {', '.join(PARTS)}")
        missing = [p for p in chosen if p not in manifest.parts]
        if missing:
            note = (
                " (left out of that backup: it was not encrypted)" if "secrets" in missing else ""
            )
            raise BackupError(f"the backup has no {', '.join(missing)}{note}")
        if instance is not None:
            if not INSTANCE_ID.match(instance):
                raise BackupError(f"{instance!r} is not a module instance id")
            if parts is None:
                chosen = [p for p in chosen if p in ("config", "data")]
            if not chosen or any(p not in ("config", "data") for p in chosen):
                raise BackupError("one instance can be restored from config and data only")
        return cls(tuple(chosen), instance)


def instances_in(archive: Archive) -> list[str]:
    """The instance ids that have data in an archive."""
    found = {
        PurePosixPath(n).parts[1]
        for n in archive.members
        if n.startswith("data/") and len(PurePosixPath(n).parts) >= 3
    }
    return sorted(found)


class Restorer:
    """Writes the parts of a backup into the engine's and the installer's directories.

    Args:
        layout: The installer's paths.
        engine_owner: Owner for files and directories written for the engine
            (``mm3``), or None to keep ours (tests).
    """

    def __init__(self, layout: Layout, engine_owner: Owner | None = None) -> None:
        self.layout = layout
        self.owner = engine_owner

    # --- reading the current system -------------------------------------------------

    def _current_config(self) -> dict[str, bytes]:
        root = self.layout.engine_config
        out: dict[str, bytes] = {}
        if not root.is_dir() or root.is_symlink():
            return out
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            here = Path(dirpath)
            dirs[:] = [d for d in dirs if not (here / d).is_symlink()]
            for name in files:
                path = here / name
                if name.startswith(".") or path.is_symlink() or path == self.layout.env_file:
                    continue
                out[path.relative_to(root).as_posix()] = path.read_bytes()
        return out

    @staticmethod
    def _archive_config(archive: Archive) -> dict[str, bytes]:
        return {n[len("config/set/") :]: archive.read(n) for n in archive.names("config/set")}

    def _history_empty(self) -> bool:
        path = self.layout.history_dir
        if not path.is_dir() or path.is_symlink():
            return True
        return not any(p.name.isdigit() for p in path.iterdir())

    # --- the preview ----------------------------------------------------------------

    def preview(
        self,
        archive: Archive,
        selection: Selection,
        installed: list[dict[str, str]],
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """What a restore would change, part by part (CORE-162).

        Args:
            archive: The verified archive.
            selection: What to restore.
            installed: The modules installed now, ``[{name, version, origin}]``.
            warnings: Compatibility warnings to carry along (CORE-164).

        Returns:
            ``{parts: {...}, warnings: [...]}`` with one entry per selected part.

        Raises:
            BackupError: The instance is not in the backup.
        """
        out: dict[str, Any] = {}
        inst = selection.instance
        if "config" in selection.parts:
            theirs, ours = self._archive_config(archive), self._current_config()
            if inst is None:
                out["config"] = {
                    "added": sorted(set(theirs) - set(ours)),
                    "changed": sorted(k for k in theirs if k in ours and ours[k] != theirs[k]),
                    "removed": sorted(set(ours) - set(theirs)),
                    "history": "restored" if self._history_empty() else "kept",
                }
            else:
                try:
                    restore_instance(ours, theirs, inst)
                except SectionError as exc:
                    raise BackupError(str(exc)) from exc
                out["config"] = {"instance": inst}
        if "data" in selection.parts:
            have = instances_in(archive)
            if inst is not None:
                if inst not in have:
                    raise BackupError(f"the backup has no data of instance {inst!r}")
                have = [inst]
            out["data"] = {
                "instances": [
                    {
                        "id": i,
                        "state": "replaced" if (self.layout.instance_data / i).exists() else "new",
                    }
                    for i in have
                ]
            }
        if "modules" in selection.parts:
            out["modules"] = self._modules_preview(archive, installed)
        if "secrets" in selection.parts:
            names = [n[len("secrets/") :] for n in archive.names("secrets")]
            out["secrets"] = {"replaced": [n for n in names if n in _SECRET_NAMES]}
        return {"parts": out, "warnings": list(warnings or [])}

    @staticmethod
    def _modules_preview(archive: Archive, installed: list[dict[str, str]]) -> dict[str, Any]:
        wanted = _modules(archive)
        have = {m["name"].lower().replace("_", "-"): m for m in installed}
        install, manual, same = [], [], 0
        for rec in wanted:
            key = rec["name"].lower().replace("_", "-")
            now = have.get(key)
            if now is not None and now["version"] == rec["version"]:
                same += 1
            elif rec["origin"] == "catalog":
                install.append(
                    {
                        "name": rec["name"],
                        "from": now["version"] if now else None,
                        "to": rec["version"],
                    }
                )
            else:
                manual.append(rec)
        return {"install": install, "manual": manual, "unchanged": same}

    # --- writing --------------------------------------------------------------------

    def apply(self, archive: Archive, selection: Selection) -> list[str]:
        """Write ``secrets``, ``data`` and ``config``, in that order (CORE-363).

        The caller has stopped the engine and taken the pre-restore backup.

        Args:
            archive: The verified archive.
            selection: What to restore (``modules`` is not written here).

        Returns:
            One line per part written, for the operation's steps.

        Raises:
            BackupError: The backup lacks the instance, or the config is not YAML we can edit.
            OSError: A write failed (the caller rolls back, CORE-163).
        """
        done: list[str] = []
        if "secrets" in selection.parts:
            done.append(self._secrets(archive))
        if "data" in selection.parts:
            done.append(self._data(archive, selection.instance))
        if "config" in selection.parts:
            done.append(self._config(archive, selection.instance))
        return done

    def _mode(self, archive: Archive, name: str, ceiling: int = 0o640) -> int:
        return (archive.members[name].mode & ceiling) or 0o600

    def _secrets(self, archive: Archive) -> str:
        lay = self.layout
        written = []
        for name in archive.names("secrets"):
            short = name[len("secrets/") :]
            if short not in _SECRET_NAMES:
                continue
            data = archive.read(name)
            if short == "mm3.env":
                self._put(lay.engine_config, short, data, 0o600)
            elif short == "tokens.json":
                self._put(lay.engine_state, short, data, 0o600)
            elif short == "auth.json":
                atomic_write(lay.auth_file, data, mode=0o600)
            else:
                atomic_write(lay.rclone_conf, data, mode=0o600)
            written.append(short)
        return "secrets: " + (", ".join(written) or "none")

    def _put(self, directory: Path, name: str, data: bytes, mode: int) -> None:
        fd = open_dir(directory)
        try:
            put_file(fd, name, data, mode=mode, owner=self.owner)
        finally:
            os.close(fd)

    def _data(self, archive: Archive, only: str | None) -> str:
        lay = self.layout
        instances = [only] if only else instances_in(archive)
        if only and only not in instances_in(archive):
            raise BackupError(f"the backup has no data of instance {only!r}")
        for inst in instances:
            if not INSTANCE_ID.match(inst):
                raise BackupError(f"unsafe instance name in the archive: {inst!r}")
        modules_fd = open_dir(
            lay.engine_state, ["modules"], create=True, mode=0o750, owner=self.owner
        )
        kv_fd = open_dir(lay.engine_state, ["kv"], create=True, mode=0o750, owner=self.owner)
        try:
            for inst in instances:
                prefix = f"data/{inst}/files/"
                members = [n for n in archive.names(f"data/{inst}/files")]

                def populate(
                    root_fd: int, members: list[str] = members, prefix: str = prefix
                ) -> None:
                    for name in members:
                        parts = PurePosixPath(name[len(prefix) :]).parts
                        sub = open_subdir(
                            root_fd, parts[:-1], create=True, mode=0o750, owner=self.owner
                        )
                        try:
                            with archive.open(name) as fh:
                                put_file(
                                    sub,
                                    parts[-1],
                                    fh,
                                    mode=self._mode(archive, name),
                                    owner=self.owner,
                                )
                        finally:
                            os.close(sub)

                replace_tree(modules_fd, inst, populate, mode=0o750, owner=self.owner)
                kv_name = f"data/{inst}/kv.json"
                if kv_name in archive.members:
                    with archive.open(kv_name) as fh:
                        put_file(
                            kv_fd,
                            f"{inst}.json",
                            fh,
                            mode=self._mode(archive, kv_name),
                            owner=self.owner,
                        )
                else:
                    remove_tree(kv_fd, f"{inst}.json")
        finally:
            os.close(modules_fd)
            os.close(kv_fd)
        return "data: " + ", ".join(instances)

    def _config(self, archive: Archive, only: str | None) -> str:
        lay = self.layout
        theirs = self._archive_config(archive)
        if only is not None:
            try:
                changed = restore_instance(self._current_config(), theirs, only)
            except SectionError as exc:
                raise BackupError(str(exc)) from exc
            for rel, data in changed.items():
                self._put_rel(lay.engine_config, rel, data, 0o640)
            return f"config: instance {only}"
        ours = self._current_config()
        for rel in sorted(theirs):
            member = f"config/set/{rel}"
            self._put_rel(lay.engine_config, rel, theirs[rel], self._mode(archive, member))
        for rel in sorted(set(ours) - set(theirs)):
            self._remove_rel(lay.engine_config, rel)
        history = "kept"
        if self._history_empty() and any(True for _ in archive.names("config/history")):
            self._history(archive)
            history = "restored"
        return f"config: {len(theirs)} files, history {history}"

    def _put_rel(self, root: Path, rel: str, data: bytes, mode: int) -> None:
        parts = PurePosixPath(rel).parts
        fd = open_dir(root, parts[:-1], create=True, mode=0o750, owner=self.owner)
        try:
            put_file(fd, parts[-1], data, mode=mode, owner=self.owner)
        finally:
            os.close(fd)

    def _remove_rel(self, root: Path, rel: str) -> None:
        parts = PurePosixPath(rel).parts
        try:
            fd = open_dir(root, parts[:-1])
        except FileNotFoundError:
            return
        try:
            remove_tree(fd, parts[-1])
        finally:
            os.close(fd)

    def _history(self, archive: Archive) -> None:
        root_fd = open_dir(
            self.layout.engine_state, ["config-history"], create=True, mode=0o750, owner=self.owner
        )
        try:
            for name in archive.names("config/history"):
                parts = PurePosixPath(name[len("config/history/") :]).parts
                if parts[-1] == ".lock":
                    continue
                sub = open_subdir(root_fd, parts[:-1], create=True, mode=0o750, owner=self.owner)
                try:
                    with archive.open(name) as fh:
                        put_file(sub, parts[-1], fh, mode=0o640, owner=self.owner)
                finally:
                    os.close(sub)
        finally:
            os.close(root_fd)


def _modules(archive: Archive) -> list[dict[str, str]]:
    """``modules.json`` of an archive, checked."""
    if MODULES_JSON not in archive.members:
        return []
    try:
        data = json.loads(archive.read(MODULES_JSON))
        return [
            {"name": str(m["name"]), "version": str(m["version"]), "origin": str(m["origin"])}
            for m in data
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError("modules.json is not valid") from exc


def archive_modules(archive: Archive) -> list[dict[str, str]]:
    """The modules a backup recorded: ``[{name, version, origin}]`` (CORE-362)."""
    return _modules(archive)


def describe(preview: Mapping[str, Any]) -> list[str]:
    """One readable line per part of a preview, for the operation's steps and the CLI."""
    lines = []
    for part, info in preview["parts"].items():
        if part == "config" and "instance" in info:
            lines.append(f"config: the section of {info['instance']}")
        elif part == "config":
            lines.append(
                f"config: {len(info['added'])} added, {len(info['changed'])} changed, "
                f"{len(info['removed'])} removed; history {info['history']}"
            )
        elif part == "data":
            lines.append(
                "data: " + ", ".join(f"{i['id']} ({i['state']})" for i in info["instances"])
            )
        elif part == "modules":
            lines.append(
                f"modules: {len(info['install'])} to install, {len(info['manual'])} for manual "
                f"install, {info['unchanged']} unchanged"
            )
        elif part == "secrets":
            lines.append("secrets: " + (", ".join(info["replaced"]) or "none"))
    for warning in preview.get("warnings", []):
        lines.append(f"warning: {warning}")
    return lines
