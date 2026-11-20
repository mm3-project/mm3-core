"""Network shares of the media folder: rclone sync and mounts (CORE-406, CORE-407).

The installer reads ``media.shares`` of the running configuration over the
admin socket once a minute (the same reader as the backup settings, CORE-360)
and keeps the state of every share in ``/var/lib/mm3-install/shares.json``.

**Sync shares** (CORE-406) are copied by ``rclone sync`` into
``shares/<name>/``, as installer operations of action ``media_sync``: when they
are due (every ``interval`` minutes, a new share at once) or on request. A
run checks the remote's type in ``rclone.conf``, measures the remote with
``rclone size`` against ``max_size_gb`` and then copies only the extensions of
CORE-404, without links. ``rclone`` runs through ``setpriv`` without
capabilities and with umask 022; its destination is the folder
``shares/<name>/`` opened here without following a link and owned by
``mm3-install`` (mode 0755), handed to it as ``/proc/self/fd/<n>``, so neither a
link the engine plants in ``shares/`` nor a swap of the folder can send the copy
elsewhere, and the engine can read the copy but not write it. A copy whose share
is gone is removed by the next pass.

**Mount shares** (CORE-407) are never written as unit files here: the wanted
mounts go to ``mounts/shares.json`` and, for ``smb``, a credentials file
``mounts/<name>.cred`` (0600), all in the installer's own state; the root
generator ``mm3-media-mounts`` turns them into
``var-lib-mm3-media-shares-<name>.mount`` and ``.automount`` at the next reload
of systemd, which the installer asks for through its polkit rule, together with
start and stop of those units. A share whose mount unit has failed is ``error``
with the reason ``mount``.

Implements:
    CORE-406: Core spec 0.57, §9.7 Media service.
    CORE-407: Core spec 0.57, §9.7 Media service.
"""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from mm3.core.config.media import MountShare, SyncShare
from mm3.core.logging import get_logger
from mm3.core.media.folder import MEDIA_EXTENSIONS
from mm3.installer.files import atomic_write, open_dir, read_json, remove_tree, write_json
from mm3.installer.paths import INSTALLER_USER, Layout
from mm3.installer.remotes import RCLONE, RcloneResult
from mm3.installer.runner import drop_argv
from mm3.installer.service import SYSTEMCTL

if TYPE_CHECKING:
    from mm3.installer.operations import Installer, Operation, Request
    from mm3.installer.schedule import SettingsReader

__all__ = [
    "CHECK_EVERY_S",
    "SYNC_TYPES",
    "UNIT_PREFIX",
    "DroppedRclone",
    "ShareError",
    "Shares",
    "SharesScheduler",
    "SystemdUnits",
    "Units",
    "path_ok",
    "unit_names",
]

_log = get_logger("installer")

CHECK_EVERY_S = 60.0
"""``media.shares`` is read and applied once a minute (CORE-406)."""
SYNC_TYPES = ("smb", "webdav", "sftp")
"""Remote types a sync share may name (CORE-406)."""
UNIT_PREFIX = "var-lib-mm3-media-shares-"
"""The share units are named after their mount point, as systemd requires (CORE-407)."""
RCLONE_TIMEOUT_S = 6 * 3600.0
_INCLUDE = "*.{" + ",".join(sorted(e.lstrip(".") for e in MEDIA_EXTENSIONS)) + "}"
_FILTERS = ["--include", _INCLUDE, "--ignore-case"]


class ShareError(Exception):
    """A share run stopped; ``reason`` is the share's ``error`` (CORE-406)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def unit_names(name: str) -> tuple[str, str]:
    """``(mount, automount)`` unit names of a share (CORE-407)."""
    return f"{UNIT_PREFIX}{name}.mount", f"{UNIT_PREFIX}{name}.automount"


def path_ok(path: str) -> bool:
    """Whether a share path can stand in a unit file (CORE-407; the generator checks too)."""
    return (
        0 < len(path) <= 1024
        and path == path.strip(" ")
        and "\\" not in path
        and not any(ord(c) < 32 or ord(c) == 127 for c in path)
    )


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- what runs on the machine (fakes in tests) ------------------------------------------


class Units(Protocol):
    """``systemctl`` for the share units (a fake in tests)."""

    def reload(self) -> bool:
        """``systemctl daemon-reload``; whether it worked."""

    def start(self, unit: str) -> bool:
        """Start a unit; whether it worked."""

    def stop(self, unit: str) -> bool:
        """Stop a unit; whether it worked."""

    def failed(self, unit: str) -> bool:
        """Whether the unit is in the state ``failed``."""


class SystemdUnits:
    """The production :class:`Units`, through the installer's polkit rule (CORE-407)."""

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(  # noqa: S603 - fixed binary, checked unit names
                [SYSTEMCTL, "--no-ask-password", *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def reload(self) -> bool:
        """``systemctl daemon-reload``."""
        done = self._run("daemon-reload")
        return done is not None and done.returncode == 0

    def start(self, unit: str) -> bool:
        """``systemctl start <unit>``."""
        done = self._run("start", unit)
        return done is not None and done.returncode == 0

    def stop(self, unit: str) -> bool:
        """``systemctl stop <unit>``."""
        done = self._run("stop", unit)
        return done is not None and done.returncode == 0

    def failed(self, unit: str) -> bool:
        """``systemctl is-failed <unit>`` exits 0 only for a failed unit."""
        done = self._run("is-failed", "--quiet", unit)
        return done is not None and done.returncode == 0


class ShareRclone(Protocol):
    """Runs rclone for the shares (a fake in tests)."""

    def run(
        self, args: list[str], *, pass_fds: tuple[int, ...] = (), timeout: float = 3600.0
    ) -> RcloneResult:
        """Run rclone with ``args``; ``pass_fds`` stay open in the child."""


class DroppedRclone:
    """rclone without capabilities and with umask 022 (CORE-406).

    ``setpriv`` keeps the user ``mm3-install`` (so ``rclone.conf`` stays
    readable) and clears the inheritable and ambient capabilities with
    ``no_new_privs``, so the copy has none of the installer's rights.

    Args:
        layout: The installer's paths.
    """

    def __init__(self, layout: Layout) -> None:
        self._layout = layout

    def run(
        self, args: list[str], *, pass_fds: tuple[int, ...] = (), timeout: float = 3600.0
    ) -> RcloneResult:
        """Run ``rclone --config <rclone.conf> <args>``; a failure is a result, never raised."""
        lay = self._layout
        argv = drop_argv(INSTALLER_USER, [RCLONE, "--config", str(lay.rclone_conf), *args])
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(lay.state),
            "XDG_CACHE_HOME": str(lay.state / "cache"),
            "TMPDIR": str(lay.tmp),
            "LANG": "C.UTF-8",
        }
        try:
            done = subprocess.run(  # noqa: S603 - fixed binary, no shell
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                pass_fds=pass_fds,
                umask=0o022,
            )
        except FileNotFoundError:
            return RcloneResult(127, b"", "rclone or setpriv is not installed")
        except subprocess.TimeoutExpired:
            return RcloneResult(124, b"", f"rclone timed out after {timeout:.0f} s")
        tail = done.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        return RcloneResult(done.returncode, done.stdout, tail)


# --- the shares -------------------------------------------------------------------------


@dataclass
class _Cred:
    obscured: str
    text: bytes


class Shares:
    """Applies ``media.shares``, keeps their states and runs the syncs (CORE-406, CORE-407).

    Args:
        layout: The installer's paths.
        reader: The running settings (``media.shares``).
        rclone: Runs rclone without capabilities.
        units: ``systemctl`` for the share units.
        now: Clock, for tests.
    """

    def __init__(
        self,
        layout: Layout,
        reader: SettingsReader,
        rclone: ShareRclone,
        units: Units,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.layout = layout
        self.reader = reader
        self.rclone = rclone
        self.units = units
        self._now = now
        self.wanted: dict[str, SyncShare | MountShare] = {}
        self._reloaded = False
        self._creds: dict[str, _Cred] = {}
        self._uid = os.getuid()

    # --- state ----------------------------------------------------------------------

    def _states(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.layout.state / "shares.json", {})
        return data if isinstance(data, dict) else {}

    def _set(self, name: str, **values: Any) -> None:
        states = self._states()
        entry = states.get(name, {})
        entry.update(values)
        states[name] = entry
        write_json(self.layout.state / "shares.json", states)

    def sync_names(self) -> list[str]:
        """The configured sync shares, as last read."""
        return sorted(n for n, s in self.wanted.items() if isinstance(s, SyncShare))

    def listing(self) -> list[dict[str, Any]]:
        """``[{name, method, state, last_sync, size, error}]`` of the configured shares."""
        states = self._states()
        out = []
        for name in sorted(self.wanted):
            share = self.wanted[name]
            got = states.get(name, {})
            same = got.get("method") == share.method
            sync = share.method == "sync"
            out.append(
                {
                    "name": name,
                    "method": share.method,
                    "state": got.get("state", "idle" if sync else "ok") if same else "idle",
                    "last_sync": got.get("last_sync") if same and sync else None,
                    "size": got.get("size") if same and sync else None,
                    "error": got.get("error") if same else None,
                }
            )
        return out

    # --- rclone.conf ----------------------------------------------------------------

    def _remote(self, name: str) -> dict[str, str] | None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        with contextlib.suppress(FileNotFoundError):
            parser.read_string(self.layout.rclone_conf.read_text(encoding="utf-8"))
        return dict(parser[name]) if name in parser else None

    def _reveal(self, obscured: str) -> str:
        done = self.rclone.run(["reveal", obscured], timeout=60.0)
        if done.returncode != 0:
            raise ShareError("remote", "rclone could not decode the remote's password")
        return done.stdout.decode("utf-8").strip()

    # --- the pass once a minute -----------------------------------------------------

    def tick(self) -> list[str]:
        """Read ``media.shares``, apply the mounts, check them and clean old copies.

        Returns:
            The sync shares that are due now.
        """
        self.wanted = dict(self.reader.read().shares)
        self.apply_mounts()
        self._check_mounts()
        self._remove_old_copies()
        return self.due()

    def due(self) -> list[str]:
        """The sync shares whose ``interval`` has passed since their last run."""
        states = self._states()
        now = self._now()
        out = []
        for name, share in sorted(self.wanted.items()):
            if not isinstance(share, SyncShare):
                continue
            got = states.get(name, {})
            if got.get("method") == "sync" and got.get("state") == "syncing":
                continue  # its operation runs
            ran = got.get("last_run") if got.get("method") == "sync" else None
            try:
                last = datetime.fromisoformat(str(ran).replace("Z", "+00:00"))
            except ValueError:
                out.append(name)  # never run: a share just added is due at once
                continue
            if now - last >= timedelta(minutes=share.interval):
                out.append(name)
        return out

    # --- mounts (CORE-407) ----------------------------------------------------------

    def _mount_entry(self, name: str, share: MountShare) -> tuple[dict[str, str], bytes | None]:
        if not path_ok(share.path):
            raise ShareError("path", "the share path cannot stand in a unit file")
        entry = {"name": name, "type": share.type, "host": share.host, "path": share.path}
        if share.type == "nfs":
            return entry, None
        remote = self._remote(share.remote or "")
        if remote is None or remote.get("type") != "smb":
            raise ShareError("remote", f"no rclone remote {share.remote!r} of type smb")
        obscured = remote.get("pass", "")
        cached = self._creds.get(name)
        if cached is None or cached.obscured != obscured:
            # Revealed only when the remote's password changed (CORE-407).
            password = self._reveal(obscured) if obscured else ""
            lines = [f"username={remote.get('user', '')}", f"password={password}"]
            if remote.get("domain"):
                lines.append(f"domain={remote['domain']}")
            cached = _Cred(obscured, ("\n".join(lines) + "\n").encode("utf-8"))
            self._creds[name] = cached
        return entry, cached.text

    def apply_mounts(self) -> None:
        """Write ``mounts/shares.json`` and the credentials; reload, start and stop units."""
        mounts = self.layout.state / "mounts"
        mounts.mkdir(mode=0o700, exist_ok=True)
        entries: list[dict[str, str]] = []
        creds: dict[str, bytes] = {}
        for name, share in sorted(self.wanted.items()):
            if not isinstance(share, MountShare):
                continue
            try:
                entry, cred = self._mount_entry(name, share)
            except ShareError as exc:
                self._set(name, method="mount", state="error", error=exc.reason)
                continue
            entries.append(entry)
            if cred is not None:
                creds[name] = cred
        for name, text in creds.items():
            path = mounts / f"{name}.cred"
            if not path.is_file() or path.read_bytes() != text:
                atomic_write(path, text, mode=0o600)
        for path in mounts.glob("*.cred"):
            if path.stem not in creds:
                path.unlink(missing_ok=True)
        wanted_file = mounts / "shares.json"
        before = read_json(wanted_file, [])
        before_names = {
            str(e["name"]) for e in before if isinstance(e, dict) and isinstance(e.get("name"), str)
        }
        new_names = {e["name"] for e in entries}
        if before != entries:
            write_json(wanted_file, entries)
        if before != entries or not self._reloaded:
            self._reloaded = self.units.reload()
            for gone in sorted(before_names - new_names):
                mount, automount = unit_names(gone)
                self.units.stop(automount)
                self.units.stop(mount)
            for name in sorted(new_names):
                self.units.start(unit_names(name)[1])
        states = self._states()
        changed = False
        for name in list(states):
            if name not in self.wanted:
                del states[name]
                changed = True
        if changed:
            write_json(self.layout.state / "shares.json", states)

    def _check_mounts(self) -> None:
        wanted = read_json(self.layout.state / "mounts" / "shares.json", [])
        for entry in wanted:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str):
                continue
            if self.units.failed(unit_names(name)[0]):
                self._set(name, method="mount", state="error", error="mount")
            else:
                self._set(name, method="mount", state="ok", error=None)

    # --- the copies (CORE-406) ------------------------------------------------------

    def _shares_fd(self) -> int:
        """``/var/lib/mm3/media/shares`` opened without following a link."""
        return open_dir(self.layout.engine_state, ["media", "shares"])

    def _own_dir(self, parent_fd: int, name: str) -> int:
        """``shares/<name>/`` as a folder of ``mm3-install`` (0755); anything else is replaced."""
        try:
            st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None and not (stat.S_ISDIR(st.st_mode) and st.st_uid == self._uid):
            # A link, a file or a folder the engine made: never written through.
            remove_tree(parent_fd, name)
            st = None
        if st is None:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
        fst = os.fstat(fd)
        if fst.st_uid != self._uid:  # pragma: no cover - a race with another writer
            os.close(fd)
            raise ShareError("failed", "the share folder changed while it was opened")
        os.fchmod(fd, 0o755)
        return fd

    def _remove_old_copies(self) -> None:
        keep = {n for n, s in self.wanted.items() if isinstance(s, SyncShare)}
        try:
            fd = self._shares_fd()
        except OSError:
            return
        try:
            for entry in os.scandir(fd):
                if entry.name in keep or entry.name in self.wanted:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                # Only copies this installer made; mount points and the engine's own
                # folders stay.
                if stat.S_ISDIR(st.st_mode) and st.st_uid == self._uid:
                    remove_tree(fd, entry.name)
                    _log.info("media_share_copy_removed", share=entry.name)
        finally:
            os.close(fd)

    def sync(self, inst: Installer, op: Operation, request: Request) -> None:
        """Run one sync as the operation ``op`` (CORE-406).

        Raises:
            OperationError: The run stopped; the share has the state ``error``.
        """
        from mm3.installer.operations import OperationError

        name = request.name or ""
        self.wanted = dict(self.reader.read().shares)
        share = self.wanted.get(name)
        if not isinstance(share, SyncShare):
            raise OperationError(f"no sync share named {name!r}")
        self._set(name, method="sync", state="syncing", last_run=_iso(self._now()))
        try:
            size = self._copy(inst, op, name, share)
        except ShareError as exc:
            self._set(name, method="sync", state="error", error=exc.reason)
            raise OperationError(f"{name}: {exc}") from exc
        except OSError as exc:
            self._set(name, method="sync", state="error", error="failed")
            raise OperationError(f"{name}: {exc.strerror or exc}") from exc
        self._set(
            name, method="sync", state="ok", error=None, last_sync=_iso(self._now()), size=size
        )
        inst.step(op, "done", f"{name}: {size} bytes")

    def _copy(self, inst: Installer, op: Operation, name: str, share: SyncShare) -> int:
        remote = self._remote(share.remote)
        if remote is None or remote.get("type") not in SYNC_TYPES:
            raise ShareError(
                "remote", f"no rclone remote {share.remote!r} of type smb, webdav or sftp"
            )
        if any(ord(c) < 32 for c in share.path) or ".." in share.path.split("/"):
            raise ShareError("path", "the share path is not valid")
        source = f"{share.remote}:{share.path.strip('/')}"
        links = ["--sftp-skip-links"] if remote.get("type") == "sftp" else []
        inst.step(op, "verifying", f"{name}: measuring {share.remote}")
        sized = self.rclone.run(["size", "--json", *_FILTERS, *links, source], timeout=600.0)
        if sized.returncode != 0:
            raise ShareError("failed", sized.stderr or "rclone size failed")
        try:
            size = int(json.loads(sized.stdout)["bytes"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ShareError("failed", "rclone size gave no byte count") from exc
        if size > share.max_size_gb * 1024**3:
            raise ShareError("too_large", f"{size} bytes is above {share.max_size_gb} GB")
        parent = self._shares_fd()
        try:
            dest = self._own_dir(parent, name)
        finally:
            os.close(parent)
        try:
            inst.step(op, "verifying", f"{name}: copying {size} bytes")
            done = self.rclone.run(
                [
                    "sync",
                    *_FILTERS,
                    *links,
                    "--skip-links",
                    "--delete-excluded",
                    source,
                    f"/proc/self/fd/{dest}",
                ],
                pass_fds=(dest,),
                timeout=RCLONE_TIMEOUT_S,
            )
        finally:
            os.close(dest)
        if done.returncode != 0:
            raise ShareError("failed", done.stderr or f"rclone exited with {done.returncode}")
        return size


class SharesScheduler:
    """Once a minute: apply the shares and submit the due syncs (CORE-406).

    Args:
        shares: The shares.
        submit: Starts a ``media_sync`` operation for a share; False while the
            installer is busy (the share waits for the next minute).
    """

    def __init__(self, shares: Shares, submit: Callable[[str], bool]) -> None:
        self.shares = shares
        self._submit = submit

    def tick(self) -> list[str]:
        """One pass; returns the shares whose sync was submitted."""
        started = []
        for name in self.shares.tick():
            if not self._submit(name):
                break  # busy: the rest wait for the next minute
            started.append(name)
        return started

    async def run(self) -> None:
        """Run :meth:`tick` every minute until cancelled."""
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                _log.exception("media_shares_failed")
            await asyncio.sleep(CHECK_EVERY_S)
