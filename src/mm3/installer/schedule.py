"""The running ``backup`` settings and the scheduler of backups (CORE-360, CORE-244).

The installer reads the ``backup`` block and ``engine.timezone`` of the running
configuration over the engine's admin socket (CORE-311) at start and every hour;
when the engine does not answer it reads the files in ``/etc/mm3``, so a backup
before an upgrade still knows ``encrypt_to``. A scheduled backup is an
operation like any other (CORE-136, CORE-355) submitted at ``backup.schedule``
in ``engine.timezone``. When the mirror was off at that time and the last
scheduled backup is older than 24 h, one runs shortly after the installer's
start (within 10 minutes). A change of the block applies from the next run.

Implements:
    CORE-360: Core spec 0.37, §10.8 Backup and restore.
    CORE-244: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from mm3.core.config.loader import include_target, node_to_json, rt_load
from mm3.core.config.model import BackupConfig
from mm3.core.logging import get_logger
from mm3.installer.files import read_json, write_json
from mm3.installer.paths import Layout

__all__ = [
    "CATCH_UP_DELAY_S",
    "CHECK_EVERY_S",
    "REFRESH_EVERY_S",
    "AdminSettings",
    "BackupScheduler",
    "Settings",
    "SettingsReader",
    "StatusFile",
    "parse_settings",
]

_log = get_logger("installer")
REFRESH_EVERY_S = 3600.0
CHECK_EVERY_S = 30.0
CATCH_UP_DELAY_S = 120.0
"""A missed run starts this long after the installer (well within 10 minutes)."""
_STALE = timedelta(hours=24)


@dataclass(frozen=True)
class Settings:
    """What the backup code needs from the running configuration."""

    backup: BackupConfig = field(default_factory=BackupConfig)
    timezone: str = "UTC"

    @property
    def tz(self) -> tzinfo:
        """The engine's time zone, UTC when the name is unknown."""
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return UTC


class SettingsReader(Protocol):
    """Reads the running settings (a fake in tests)."""

    def read(self) -> Settings:
        """The current settings; defaults when nothing can be read."""


def _documents(files: Mapping[str, str]) -> dict[str, Any]:
    docs: dict[str, Any] = {}
    for path, text in files.items():
        if path.endswith((".yaml", ".yml")):
            try:
                docs[path] = rt_load(text)
            except Exception as exc:  # an unreadable file only hides its settings
                _log.warning("backup_config_unreadable", path=path, reason=type(exc).__name__)
    return docs


def _resolve(docs: Mapping[str, Any], main: str, node: Any) -> Any:
    target = include_target(node)
    if target is None:
        return node
    rel = str(Path(main).parent / target) if "/" in main else target
    return docs.get(Path(rel).as_posix(), docs.get(target))


def parse_settings(files: Mapping[str, str], main: str = "config.yaml") -> Settings:
    """The ``backup`` block and the time zone of a config set (one ``!include`` level).

    Args:
        files: YAML files by path relative to the config directory.
        main: The main file.

    Returns:
        The settings; defaults for a missing or invalid block.
    """
    docs = _documents(files)
    root = docs.get(main)
    if root is None or not hasattr(root, "get"):
        return Settings()
    block = node_to_json(_resolve(docs, main, root.get("backup")))
    engine = node_to_json(_resolve(docs, main, root.get("engine")))
    zone = engine.get("timezone") if isinstance(engine, dict) else None
    try:
        backup = BackupConfig.model_validate(block or {})
    except ValidationError as exc:
        _log.warning("backup_config_invalid", errors=len(exc.errors()))
        backup = BackupConfig()
    return Settings(backup, zone if isinstance(zone, str) else "UTC")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self._path))
        self.sock = sock


class AdminSettings:
    """The production reader: admin socket first, then the files on disk (CORE-360).

    Args:
        layout: The installer's paths.
        timeout: Seconds per request.
    """

    def __init__(self, layout: Layout, timeout: float = 10.0) -> None:
        self._layout = layout
        self._timeout = timeout

    def _from_socket(self) -> Settings | None:
        conn = _UnixConnection(self._layout.admin_socket, self._timeout)
        try:
            conn.request("GET", "/api/v1/config")
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                return None
            data = json.loads(body)
            files = {
                str(item["path"]): str(item["content"])
                for item in data["files"]
                if isinstance(item, dict)
            }
            main = next((str(i["path"]) for i in data["files"] if i.get("role") == "main"), None)
            return parse_settings(files, main or "config.yaml")
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            return None
        finally:
            conn.close()

    def _from_disk(self) -> Settings:
        root = self._layout.engine_config
        files: dict[str, str] = {}
        try:
            for path in root.rglob("*.y*ml"):
                if path.is_file() and not path.is_symlink():
                    files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return Settings()
        return parse_settings(files)

    def read(self) -> Settings:
        """The running settings; the disk's when the engine does not answer."""
        return self._from_socket() or self._from_disk()


class StatusFile:
    """``backup.json``: the last scheduled run and its remote copy (CORE-360, CORE-365)."""

    def __init__(self, layout: Layout) -> None:
        self._path = layout.backup_status

    def read(self) -> dict[str, Any]:
        """The kept results (empty before the first run)."""
        data = read_json(self._path, {})
        return data if isinstance(data, dict) else {}

    def record(self, key: str, result: Mapping[str, Any]) -> None:
        """Keep the result of ``scheduled`` or ``remote``."""
        data = self.read()
        data[key] = dict(result)
        write_json(self._path, data)


class BackupScheduler:
    """Submits the scheduled backup at the right time (CORE-360).

    Args:
        reader: The running settings.
        last_scheduled: Time of the newest scheduled backup, or None.
        submit: Starts the backup; returns False when another operation runs (retried next check).
        now: Clock, for tests.
    """

    def __init__(
        self,
        reader: SettingsReader,
        last_scheduled: Callable[[], datetime | None],
        submit: Callable[[], bool],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader = reader
        self._last = last_scheduled
        self._submit = submit
        self._now = now
        self._started = now()
        self._settings: Settings | None = None
        self._read_at: datetime | None = None
        self._attempt: datetime | None = None

    def settings(self) -> Settings:
        """The settings, read at start and every hour."""
        now = self._now()
        if (
            self._settings is None
            or self._read_at is None
            or (now - self._read_at).total_seconds() >= REFRESH_EVERY_S
        ):
            self._settings = self._reader.read()
            self._read_at = now
        return self._settings

    def due(self) -> bool:
        """Whether a scheduled backup should be submitted now."""
        settings = self.settings()
        now = self._now()
        tz = settings.tz
        local = now.astimezone(tz)
        hour, minute = (int(x) for x in settings.backup.schedule.split(":"))
        slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local < slot:
            slot -= timedelta(days=1)
            late = True
        else:
            late = False
        if self._attempt is not None and self._attempt >= slot:
            return False
        last = self._last()
        if last is not None and last >= slot:
            return False
        on_time = not late and (local - slot) <= timedelta(minutes=10)
        stale = last is None or (now - last) > _STALE
        catch_up = stale and (now - self._started).total_seconds() >= CATCH_UP_DELAY_S
        return on_time or catch_up

    def tick(self) -> bool:
        """Check once; submit when due. Returns whether a backup was submitted."""
        if not self.due():
            return False
        if not self._submit():
            return False
        settings = self.settings()
        local = self._now().astimezone(settings.tz)
        self._attempt = local
        return True

    async def run(self) -> None:
        """Check every 30 s until cancelled."""
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                _log.exception("backup_schedule_failed")
            await asyncio.sleep(CHECK_EVERY_S)
