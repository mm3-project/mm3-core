"""Remote copies of backups through rclone (CORE-365).

rclone (Debian package ``rclone``) runs as a subprocess, only in the installer
service. The remotes live in ``/var/lib/mm3-install/rclone.conf`` (0600) and
only the types ``sftp``, ``smb``, ``webdav`` and ``s3`` with a fixed set of
parameters can be set. Secret parameters are shown as ``{"!masked": null}`` and
kept when sent back so (CORE-303); passwords are obscured with ``rclone
obscure`` through stdin, never on a command line. After a scheduled backup the
archive and then its sidecar are copied to ``backup.remote`` and ``backup.keep``
is applied there; a failure is raised to the caller, which logs it and keeps the
local backup (CORE-365).

Implements:
    CORE-365: Core spec 0.37, §10.8 Backup and restore.
    CORE-244: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import configparser
import contextlib
import io
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol

from mm3.core.config.model import BackupKeep
from mm3.installer.backup import ARCHIVE_SUFFIX, select_kept
from mm3.installer.files import atomic_write
from mm3.installer.paths import Layout

__all__ = [
    "MASKED",
    "REMOTE_NAME",
    "REMOTE_TYPES",
    "RcloneResult",
    "RcloneTool",
    "RemoteError",
    "Remotes",
    "SubprocessRclone",
]

RCLONE = "/usr/bin/rclone"
REMOTE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
MASKED = {"!masked": None}
"""How a secret parameter is shown and sent back (CORE-303, CORE-320)."""
_PASS_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
_STAMP = re.compile(r"-(\d{8}-\d{6})-(scheduled|manual|pre)\.mm3backup$")
_ERROR_TAIL = 400
MAX_VALUE = 8192

# Parameters per type: (required, optional, secret, obscured). "pass" is obscured by rclone obscure.
REMOTE_TYPES: dict[str, dict[str, tuple[str, ...]]] = {
    "sftp": {
        "required": ("host",),
        "optional": ("port", "user", "pass", "key_pem"),
        "secret": ("pass", "key_pem"),
        "obscured": ("pass",),
    },
    "smb": {
        "required": ("host",),
        "optional": ("port", "user", "pass", "domain"),
        "secret": ("pass",),
        "obscured": ("pass",),
    },
    "webdav": {
        "required": ("url",),
        "optional": ("vendor", "user", "pass"),
        "secret": ("pass",),
        "obscured": ("pass",),
    },
    "s3": {
        "required": (),
        "optional": ("provider", "access_key_id", "secret_access_key", "region", "endpoint"),
        "secret": ("secret_access_key",),
        "obscured": (),
    },
}


class RemoteError(Exception):
    """A remote or rclone step failed; the message is safe to show."""


@dataclass(frozen=True)
class RcloneResult:
    """A finished rclone command."""

    returncode: int
    stdout: bytes
    stderr: str


class RcloneTool(Protocol):
    """Runs rclone with the installer's config (a fake in tests)."""

    def run(
        self, args: list[str], *, stdin: bytes | None = None, timeout: float = 3600.0
    ) -> RcloneResult:
        """Run ``rclone --config <conf> <args>``."""


class SubprocessRclone:
    """The production runner.

    Args:
        layout: The installer's paths (config file, home, cache).
        binary: Path of rclone.
    """

    def __init__(self, layout: Layout, binary: str = RCLONE) -> None:
        self._layout = layout
        self._binary = binary

    def run(
        self, args: list[str], *, stdin: bytes | None = None, timeout: float = 3600.0
    ) -> RcloneResult:
        """Run rclone; a missing binary or a timeout is a failed result.

        Returns:
            Exit code, standard output and the tail of standard error.
        """
        lay = self._layout
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(lay.state),
            "XDG_CACHE_HOME": str(lay.state / "cache"),
            "TMPDIR": str(lay.tmp),
            "LANG": "C.UTF-8",
        }
        env.update({k: os.environ[k] for k in _PASS_ENV if k in os.environ})
        try:
            done = subprocess.run(  # noqa: S603 - fixed binary, no shell
                [self._binary, "--config", str(lay.rclone_conf), *args],
                env=env,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return RcloneResult(127, b"", "rclone is not installed (Debian package rclone)")
        except subprocess.TimeoutExpired:
            return RcloneResult(124, b"", f"rclone timed out after {timeout:.0f} s")
        tail = done.stderr.decode("utf-8", errors="replace").strip()[-_ERROR_TAIL:]
        return RcloneResult(done.returncode, done.stdout, tail)


def _where(remote: str, path: str, name: str = "") -> str:
    base = path.strip("/")
    leaf = f"{base}/{name}" if base and name else (name or base)
    return f"{remote}:{leaf}"


def _check_path(path: str) -> str:
    if (
        len(path) > 1024
        or any(ord(c) < 32 for c in path)
        or ".." in path.replace("\\", "/").split("/")
    ):
        raise RemoteError("the remote path is not valid")
    return path


class Remotes:
    """The rclone remotes and the copies on them (CORE-365).

    Args:
        layout: The installer's paths.
        rclone: Runs rclone.
    """

    def __init__(self, layout: Layout, rclone: RcloneTool) -> None:
        self.layout = layout
        self.rclone = rclone

    # --- the config file ------------------------------------------------------------

    def _read(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        with contextlib.suppress(FileNotFoundError):
            parser.read_string(self.layout.rclone_conf.read_text(encoding="utf-8"))
        return parser

    def _write(self, parser: configparser.ConfigParser) -> None:
        out = io.StringIO()
        parser.write(out)
        atomic_write(self.layout.rclone_conf, out.getvalue().encode("utf-8"), mode=0o600)

    # --- managing remotes -----------------------------------------------------------

    def listing(self) -> list[dict[str, Any]]:
        """The remotes, secrets masked (CORE-365)."""
        parser = self._read()
        return [self._public(name, dict(parser[name])) for name in parser.sections()]

    def get(self, name: str) -> dict[str, Any]:
        """One remote, secrets masked.

        Raises:
            RemoteError: No such remote.
        """
        parser = self._read()
        if name not in parser:
            raise RemoteError(f"no remote named {name}")
        return self._public(name, dict(parser[name]))

    @staticmethod
    def _public(name: str, values: dict[str, str]) -> dict[str, Any]:
        kind = values.pop("type", "")
        spec = REMOTE_TYPES.get(kind, {})
        secret = spec.get("secret", ())
        params = {k: (dict(MASKED) if k in secret else v) for k, v in values.items()}
        return {"name": name, "type": kind, "params": params}

    def put(self, name: str, kind: str, params: Mapping[str, Any]) -> None:
        """Create or replace a remote.

        A secret parameter sent as ``{"!masked": null}`` keeps its stored value;
        a ``pass`` is obscured with ``rclone obscure`` (stdin, not a command line).

        Raises:
            RemoteError: A bad name, type or parameter, or rclone could not obscure.
        """
        if not REMOTE_NAME.match(name):
            raise RemoteError("a remote name is letters, digits, _, . and -, up to 64")
        spec = REMOTE_TYPES.get(kind)
        if spec is None:
            raise RemoteError(f"type must be one of {', '.join(REMOTE_TYPES)}")
        allowed = {*spec["required"], *spec["optional"]}
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise RemoteError(f"{kind} has no parameter {unknown[0]!r}")
        parser = self._read()
        old = dict(parser[name]) if name in parser and parser[name].get("type") == kind else {}
        values: dict[str, str] = {"type": kind}
        for key, value in params.items():
            if value == MASKED:
                if key not in old:
                    raise RemoteError(f"{key} has no stored value to keep")
                values[key] = old[key]
                continue
            if not isinstance(value, str) or len(value) > MAX_VALUE:
                raise RemoteError(f"{key} must be a text of at most {MAX_VALUE} characters")
            if key != "key_pem" and any(ord(c) < 32 for c in value):
                raise RemoteError(f"{key} holds a control character")
            if key in spec["obscured"]:
                value = self._obscure(value)
            elif key == "key_pem":
                value = value.strip().replace("\r", "").replace("\n", "\\n")
            values[key] = value
        missing = [k for k in spec["required"] if not values.get(k)]
        if missing:
            raise RemoteError(f"{kind} needs {', '.join(missing)}")
        parser[name] = values
        self._write(parser)

    def _obscure(self, password: str) -> str:
        done = self.rclone.run(["obscure", "-"], stdin=password.encode("utf-8"), timeout=60.0)
        if done.returncode != 0:
            raise RemoteError(f"rclone could not obscure the password: {done.stderr}")
        return done.stdout.decode("utf-8").strip()

    def delete(self, name: str) -> None:
        """Remove a remote.

        Raises:
            RemoteError: No such remote.
        """
        parser = self._read()
        if name not in parser:
            raise RemoteError(f"no remote named {name}")
        parser.remove_section(name)
        self._write(parser)

    def has(self, name: str) -> bool:
        """Whether a remote exists."""
        return name in self._read()

    def test(self, name: str, path: str = "") -> None:
        """Check a remote the way a backup will use it: create the path and list it.

        Raises:
            RemoteError: The remote does not answer.
        """
        self._need(name)
        where = _where(name, _check_path(path))
        for args in (["mkdir", where], ["lsf", "--max-depth", "1", where]):
            self._ok(self.rclone.run(args, timeout=120.0))

    # --- the copies -----------------------------------------------------------------

    def _need(self, name: str) -> None:
        if not REMOTE_NAME.match(name) or not self.has(name):
            raise RemoteError(f"no remote named {name}")

    @staticmethod
    def _ok(done: RcloneResult) -> RcloneResult:
        if done.returncode != 0:
            raise RemoteError(done.stderr or f"rclone exited with {done.returncode}")
        return done

    def push(self, remote: str, path: str, archive: Path, sidecar: Path) -> None:
        """Copy an archive, then its sidecar, so a listed sidecar means a complete archive.

        Raises:
            RemoteError: The copy failed.
        """
        self._need(remote)
        _check_path(path)
        for file in (archive, sidecar):
            self._ok(self.rclone.run(["copyto", str(file), _where(remote, path, file.name)]))

    def names(self, remote: str, path: str) -> list[str]:
        """File names on the remote path.

        Raises:
            RemoteError: The remote does not answer.
        """
        self._need(remote)
        done = self._ok(
            self.rclone.run(
                ["lsjson", "--files-only", _where(remote, _check_path(path))], timeout=300.0
            )
        )
        try:
            return sorted(str(item["Name"]) for item in json.loads(done.stdout or b"[]"))
        except (ValueError, KeyError, TypeError) as exc:
            raise RemoteError("rclone gave an unreadable listing") from exc

    def sidecar(self, remote: str, path: str, name: str) -> dict[str, Any] | None:
        """The sidecar of a remote archive, or None when it has none."""
        done = self.rclone.run(
            ["cat", _where(remote, _check_path(path), f"{name}.json")], timeout=120.0
        )
        if done.returncode != 0:
            return None
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def fetch(self, remote: str, path: str, name: str, dest: Path) -> None:
        """Copy a remote archive to ``dest`` (a restore source).

        Raises:
            RemoteError: The copy failed.
        """
        self._need(remote)
        if not name.endswith(ARCHIVE_SUFFIX) or "/" in name:
            raise RemoteError(f"{name!r} is not a backup file name")
        self._ok(self.rclone.run(["copyto", _where(remote, _check_path(path), name), str(dest)]))

    def prune(self, remote: str, path: str, keep: BackupKeep, tz: tzinfo = UTC) -> list[str]:
        """Apply ``backup.keep`` to the scheduled backups on the remote (CORE-365).

        Returns:
            The archive names removed.

        Raises:
            RemoteError: The listing or a deletion failed.
        """
        archives = []
        for name in self.names(remote, path):
            match = _STAMP.search(name)
            if match and match.group(2) == "scheduled":
                when = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
                archives.append((name, when))
        kept = select_kept([w for _, w in archives], keep, tz)
        gone = []
        for index, (name, _when) in enumerate(archives):
            if index in kept:
                continue
            for leaf in (name, f"{name}.json"):
                done = self.rclone.run(["deletefile", _where(remote, path, leaf)], timeout=120.0)
                if done.returncode not in (0, 4):  # 4: file not found
                    raise RemoteError(done.stderr or f"could not delete {leaf}")
            gone.append(name)
        return gone
