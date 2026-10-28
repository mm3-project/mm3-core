"""Structured logging: the single adapter around structlog.

Records go to stdout as JSON lines (journald captures them under systemd),
or in a readable console format on a TTY, and into an in-memory ring buffer
of the last 5 000 records. Each record has ``ts``, ``level``, ``module``,
``instance``, ``event``, ``trace_id`` and, on errors, the exception with
traceback.

Implements:
    CORE-110, CORE-111: Core spec 0.4, §10.5 Diagnostics and debugging.
    CORE-177: Core spec 0.4, §10.9 Dependency management.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, TextIO

import structlog

from mm3.core.trace import current_trace

__all__ = [
    "RING_SIZE",
    "Logger",
    "configure_logging",
    "get_logger",
    "recent_records",
    "set_log_level",
]

RING_SIZE = 5000
"""Records kept in memory for the admin debug page (CORE-110)."""

_ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _add_common(_: Any, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    event["level"] = "warning" if method == "warn" else method
    event.setdefault("module", "core")
    event.setdefault("instance", None)
    trace = current_trace()
    event.setdefault("trace_id", str(trace) if trace else None)
    return event


def _to_ring(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    _ring.append(dict(event))
    return event


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
    out = stream or sys.stdout
    use_json = json if json is not None else not out.isatty()
    renderer: Any = (
        structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            _add_common,
            structlog.processors.format_exc_info,
            _to_ring,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level]),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )


def set_log_level(level: str) -> None:
    """Change the minimum log level live (``engine.log_level`` on hot reload).

    Args:
        level: ``debug``, ``info``, ``warning`` or ``error``.
    """
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level]))


def recent_records() -> list[dict[str, Any]]:
    """Return the records in the ring buffer, oldest first.

    Returns:
        Copies of the last records (at most :data:`RING_SIZE`).
    """
    return list(_ring)


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
        fields: Extra bound fields, e.g. ``subsystem="bus"``.

    Returns:
        The logger.
    """
    return Logger(module, instance, **fields)
