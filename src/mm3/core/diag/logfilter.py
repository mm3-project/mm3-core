"""Log filters of the admin channel and of ``GET /api/v1/debug/logs``.

``level`` keeps records of that level and above; ``module``, ``instance``
and ``trace`` keep records whose ``module``, ``instance`` or ``trace_id``
equals the value; ``text`` keeps records in which it occurs, ignoring case,
in ``event`` or in the string form of any other field. Filters combine; an
absent or empty key does not filter.

Implements:
    CORE-316: Core spec 0.24, §10.5 Diagnostics and debugging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mm3.core.logging import LEVELS

__all__ = ["FILTER_KEYS", "LogFilter"]

FILTER_KEYS = frozenset({"level", "module", "instance", "trace", "text"})
_RANK = {name: i for i, name in enumerate(LEVELS)}


@dataclass(frozen=True)
class LogFilter:
    """A parsed log filter (CORE-316).

    Attributes:
        level: Minimum level, or ``None``.
        module: Exact module, or ``None``.
        instance: Exact instance id, or ``None``.
        trace: Exact trace id, or ``None``.
        text: Case-insensitive substring, or ``None``.

    Implements:
        CORE-316: Core spec 0.24, §10.5 Diagnostics and debugging.
    """

    level: str | None = None
    module: str | None = None
    instance: str | None = None
    trace: str | None = None
    text: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> LogFilter:
        """Build a filter from its keys.

        Args:
            raw: The ``filter`` object or the query parameters (filter keys only).

        Returns:
            The filter.

        Raises:
            ValueError: An unknown key, a value that is not a string, or an unknown level.
        """
        raw = raw or {}
        unknown = set(raw) - FILTER_KEYS
        if unknown:
            raise ValueError(f"unknown filter key {sorted(unknown)[0]!r}")
        values: dict[str, str | None] = {}
        for key in FILTER_KEYS:
            value = raw.get(key)
            if value is None or value == "":
                values[key] = None
            elif not isinstance(value, str):
                raise ValueError(f"filter {key!r} must be a string")
            else:
                values[key] = value
        level = values["level"]
        if level is not None and level not in _RANK:
            raise ValueError(f"unknown level {level!r}")
        text = values["text"]
        return cls(
            level=level,
            module=values["module"],
            instance=values["instance"],
            trace=values["trace"],
            text=text.casefold() if text else None,
        )

    def matches(self, record: Mapping[str, Any]) -> bool:
        """Tell whether a log record passes the filter.

        Args:
            record: A record of the ring buffer.

        Returns:
            True when every given key matches.
        """
        if self.level is not None:
            rank = _RANK.get(str(record.get("level")), len(LEVELS))
            if rank < _RANK[self.level]:
                return False
        if self.module is not None and record.get("module") != self.module:
            return False
        if self.instance is not None and record.get("instance") != self.instance:
            return False
        if self.trace is not None and record.get("trace_id") != self.trace:
            return False
        if self.text is not None:
            return any(self.text in str(v).casefold() for v in record.values() if v is not None)
        return True
