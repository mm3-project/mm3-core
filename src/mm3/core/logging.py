"""Structured logging: the single adapter around structlog.

Records go to stdout as JSON lines (journald captures them under systemd),
or in a readable console format on a TTY, and into an in-memory ring buffer
of the last 5 000 records. Each record has ``ts``, ``level``, ``module``,
``instance``, ``event``, ``trace_id`` and, on errors, the exception with
traceback.

Run-time log levels per instance and per engine subsystem (CORE-112) are a
filter in front of the ring buffer: the structlog wrapper lets through the
most verbose level in use, and the filter drops what a record's own level
does not allow. A level more verbose than the default expires.

Implements:
    CORE-110, CORE-111: Core spec 0.4, §10.5 Diagnostics and debugging.
    CORE-112: Core spec 0.23, §10.5 Diagnostics and debugging.
    CORE-177: Core spec 0.4, §10.9 Dependency management.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TextIO

import structlog

from mm3.core.trace import current_trace

__all__ = [
    "LEVELS",
    "RING_SIZE",
    "LevelOverride",
    "LogLevels",
    "Logger",
    "configure_logging",
    "get_logger",
    "is_configured",
    "log_levels",
    "numbered_records",
    "observe",
    "recent_records",
    "set_log_level",
]

RING_SIZE = 5000
"""Records kept in memory for the admin debug page (CORE-110)."""

_ring: deque[tuple[int, dict[str, Any]]] = deque(maxlen=RING_SIZE)
_seq = 0
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
LEVELS = tuple(_LEVELS)
"""The levels, most verbose first (CORE-112)."""

Scope = Literal["instance", "logger"]


@dataclass(frozen=True)
class LevelOverride:
    """A run-time level for one instance or one engine logger.

    Attributes:
        level: ``debug``, ``info``, ``warning`` or ``error``.
        expires_at: When the default applies again; ``None`` when it does not expire.
    """

    level: str
    expires_at: datetime | None


class LogLevels:
    """The default level and the run-time overrides (CORE-112).

    Args:
        now: Wall clock; the system clock by default.

    Implements:
        CORE-112: Core spec 0.23, §10.5 Diagnostics and debugging.
    """

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self.now = now or (lambda: datetime.now(UTC))
        self.default = "info"
        self._overrides: dict[tuple[Scope, str], LevelOverride] = {}
        self.subsystems: set[str] = set()

    def set(self, scope: Scope, key: str, level: str | None, ttl: timedelta) -> LevelOverride:
        """Set or clear the level of an instance or engine logger.

        Args:
            scope: ``instance`` or ``logger``.
            key: Instance id or the logger's ``subsystem``.
            level: The level, or ``None`` for the default.
            ttl: How long a level more verbose than the default lasts (``debug_ttl``).

        Returns:
            The level that applies now and when it expires.

        Raises:
            ValueError: Unknown level.
        """
        if level is None:
            self._overrides.pop((scope, key), None)
            _apply()
            return LevelOverride(self.default, None)
        if level not in _LEVELS:
            raise ValueError(f"unknown log level {level!r}")
        raised = _LEVELS[level] < _LEVELS[self.default]
        override = LevelOverride(level, self.now() + ttl if raised else None)
        self._overrides[(scope, key)] = override
        _apply()
        return override

    def get(self, scope: Scope, key: str) -> LevelOverride:
        """Return the level that applies to an instance or engine logger now.

        Args:
            scope: ``instance`` or ``logger``.
            key: Instance id or subsystem.

        Returns:
            The override, or the default with no expiry.
        """
        self._expire()
        return self._overrides.get((scope, key), LevelOverride(self.default, None))

    def _expire(self) -> None:
        now = self.now()
        gone = [k for k, o in self._overrides.items() if o.expires_at and o.expires_at <= now]
        for key in gone:
            del self._overrides[key]
        if gone:
            _apply()

    def minimum(self) -> int:
        """Return the most verbose level in use, as a ``logging`` number."""
        levels = [_LEVELS[self.default], *(_LEVELS[o.level] for o in self._overrides.values())]
        return min(levels)

    def allows(self, method: str, event: MutableMapping[str, Any]) -> bool:
        """Return whether a record passes its instance's or logger's level.

        Args:
            method: The record's level.
            event: The record.

        Returns:
            True when the record is at or above the level that applies to it.
        """
        if self._overrides:
            self._expire()
        level = self.default
        instance = event.get("instance")
        subsystem = event.get("subsystem")
        if instance is not None and ("instance", str(instance)) in self._overrides:
            level = self._overrides[("instance", str(instance))].level
        elif subsystem is not None and ("logger", str(subsystem)) in self._overrides:
            level = self._overrides[("logger", str(subsystem))].level
        return _LEVELS.get(method, logging.ERROR) >= _LEVELS[level]


_levels = LogLevels()


def log_levels() -> LogLevels:
    """Return the engine's log levels (CORE-112)."""
    return _levels


def _apply() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(_levels.minimum()))


def _filter(_: Any, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    if not _levels.allows("warning" if method == "warn" else method, event):
        raise structlog.DropEvent
    return event


def _add_common(_: Any, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    event["level"] = "warning" if method == "warn" else method
    event.setdefault("module", "core")
    event.setdefault("instance", None)
    trace = current_trace()
    event.setdefault("trace_id", str(trace) if trace else None)
    return event


_observers: list[Callable[[dict[str, Any]], None]] = []


def _to_ring(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    global _seq
    record = dict(event)
    _seq += 1
    _ring.append((_seq, record))
    for observer in list(_observers):
        observer(dict(record))
    return event


def observe(callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Call ``callback`` with a copy of every record from now on.

    Used by the test kit to capture an instance's records.

    Args:
        callback: Receives each record.

    Returns:
        A function that stops the observation.
    """
    _observers.append(callback)

    def stop() -> None:
        if callback in _observers:
            _observers.remove(callback)

    return stop


def is_configured() -> bool:
    """Return whether :func:`configure_logging` set up the MM3 processors."""
    return _to_ring in structlog.get_config().get("processors", [])


def configure_logging(
    level: str = "info", *, stream: TextIO | None = None, json: bool | None = None
) -> None:
    """Configure structlog for the engine.

    Args:
        level: Minimum level (``engine.log_level``).
        stream: Output stream; defaults to stdout.
        json: Force JSON (True) or console (False) output; default is JSON
            unless the stream is a TTY.
    """
    _levels.default = level
    out = stream or sys.stdout
    use_json = json if json is not None else not out.isatty()
    renderer: Any = (
        structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            _add_common,
            _filter,
            structlog.processors.format_exc_info,
            _to_ring,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min(_LEVELS[level], _levels.minimum())),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )


def set_log_level(level: str) -> None:
    """Change the minimum log level live (``engine.log_level`` on hot reload).

    Args:
        level: ``debug``, ``info``, ``warning`` or ``error``.
    """
    _levels.default = level
    _apply()


def recent_records() -> list[dict[str, Any]]:
    """Return the records in the ring buffer, oldest first.

    Returns:
        Copies of the last records (at most :data:`RING_SIZE`).
    """
    return [dict(r) for _, r in _ring]


def numbered_records() -> list[tuple[int, dict[str, Any]]]:
    """Return the ring buffer with a sequence number per record, oldest first.

    The number grows by one per record and is never reused, so it can mark a
    position for paging (CORE-299).

    Returns:
        ``(seq, record)`` pairs; the records are copies.
    """
    return [(n, dict(r)) for n, r in _ring]


class Logger:
    """MM3 logger: ``debug/info/warning/error/exception(event, **fields)``.

    Args:
        module: Module name, or ``"core"`` for engine subsystems.
        instance: Instance id, if any.
        fields: Extra fields bound to every record.
    """

    def __init__(self, module: str, instance: str | None = None, **fields: Any) -> None:
        self._fields = {"module": module, "instance": instance, **fields}

    def _log(self, method: str, event: str, fields: dict[str, Any]) -> None:
        logger = structlog.get_logger()
        getattr(logger, method)(event, **{**self._fields, **fields})

    def debug(self, event: str, **fields: Any) -> None:
        """Log at debug level."""
        self._log("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        """Log at info level."""
        self._log("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        """Log at warning level."""
        self._log("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        """Log at error level."""
        self._log("error", event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        """Log at error level with the current exception and traceback."""
        self._log("error", event, {"exc_info": True, **fields})


def get_logger(module: str = "core", instance: str | None = None, **fields: Any) -> Logger:
    """Return a logger bound to a module and instance.

    Args:
        module: Module name or ``"core"``.
        instance: Instance id.
        fields: Extra bound fields, e.g. ``subsystem="bus"``; a subsystem
            becomes an engine logger whose level can be set (CORE-112).

    Returns:
        The logger.
    """
    subsystem = fields.get("subsystem")
    if isinstance(subsystem, str):
        _levels.subsystems.add(subsystem)
    return Logger(module, instance, **fields)
