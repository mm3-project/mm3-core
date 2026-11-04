"""The bus inspector: what passed on the bus and the shell WebSockets.

The inspector captures only while at least one admin has a ``bus``
subscription open, and keeps the last 1 000 entries, so it costs nothing
in normal operation (CORE-115). Each entry is ``{ts, channel, name, source,
target, size, trace, payload}`` with ``channel`` ``bus``, ``ws`` or ``mqtt``
(CORE-304); requests and their replies share the trace, so the admin pairs
them (CORE-223). Secret values in payloads are masked before capture, and
``size`` is that of the original payload (CORE-319).

Implements:
    CORE-115: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-304: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-319: Core spec 0.24, §10.5 Diagnostics and debugging.
"""

from __future__ import annotations

import fnmatch
import json
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mm3.core.diag.secrets import Masker
from mm3.messages import Notification

__all__ = ["CAPTURE_SIZE", "BusFilter", "BusInspector"]

CAPTURE_SIZE = 1000
"""Entries kept while the inspector is open (CORE-115)."""

Listener = Callable[[dict[str, Any]], None]


def _size(payload: Any) -> int:
    try:
        return len(json.dumps(payload, separators=(",", ":"), default=str).encode())
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class BusFilter:
    """The ``bus`` topic filter: ``name`` (a glob) and ``source`` (CORE-304).

    Attributes:
        name: Glob on the entry name, or ``None``.
        source: Exact source, or ``None``.
    """

    name: str | None = None
    source: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> BusFilter:
        """Build the filter.

        Args:
            raw: The ``filter`` object.

        Returns:
            The filter.

        Raises:
            ValueError: An unknown key or a value that is not a string.
        """
        raw = raw or {}
        unknown = set(raw) - {"name", "source"}
        if unknown:
            raise ValueError(f"unknown filter key {sorted(unknown)[0]!r}")
        out: dict[str, str | None] = {}
        for key in ("name", "source"):
            value = raw.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"filter {key!r} must be a string")
            out[key] = value or None
        return cls(**out)

    def matches(self, entry: Mapping[str, Any]) -> bool:
        """Tell whether an entry passes the filter."""
        if self.name is not None and not fnmatch.fnmatchcase(str(entry.get("name")), self.name):
            return False
        return self.source is None or entry.get("source") == self.source


class BusInspector:
    """Capture notifications and shell frames while someone looks (CORE-115).

    It is a bus endpoint (it sees every notification and reply) and the
    frame observer of the shell hub.

    Args:
        masker: Returns the masker for the running configuration.
        now: Wall clock.

    Implements:
        CORE-115: Core spec 0.22, §10.5 Diagnostics and debugging.
    """

    def __init__(
        self,
        masker: Callable[[], Masker] = Masker,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.masker = masker
        self.now = now or (lambda: datetime.now(UTC))
        self.entries: deque[dict[str, Any]] = deque(maxlen=CAPTURE_SIZE)
        self._listeners: list[Listener] = []

    @property
    def capturing(self) -> bool:
        """At least one ``bus`` subscription is open."""
        return bool(self._listeners)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Start receiving new entries; capture runs while anyone listens.

        Args:
            listener: Called with each new entry.

        Returns:
            Stops the subscription; with the last one gone the buffer is cleared.
        """
        self._listeners.append(listener)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)
            if not self._listeners:
                self.entries.clear()

        return stop

    def _capture(
        self,
        channel: str,
        name: str,
        source: str,
        target: str | None,
        trace: str | None,
        payload: Any,
    ) -> None:
        entry = {
            "ts": self.now().isoformat(timespec="milliseconds"),
            "channel": channel,
            "name": name,
            "source": source,
            "target": target,
            "size": _size(payload),
            "trace": trace,
            "payload": self.masker().mask(payload),
        }
        self.entries.append(entry)
        for listener in list(self._listeners):
            listener(dict(entry))

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        """Bus endpoint: capture a notification, request or reply.

        Args:
            n: The notification.
            exclude_instance: Unused.
        """
        if not self._listeners:
            return
        self._capture("bus", n.name, n.source, n.target, str(n.trace), dict(n.payload))

    def frame(self, direction: str, client: str, frame: Mapping[str, Any]) -> None:
        """Hub observer: capture a shell WebSocket frame (channel ``ws``).

        Args:
            direction: ``out`` (engine to shell) or ``in``.
            client: The shell's client id.
            frame: The frame.
        """
        if not self._listeners:
            return
        kind = str(frame.get("kind"))
        instance = frame.get("instance")
        shell = f"shell/{client}"
        source, target = ("core", shell) if direction == "out" else (shell, "core")
        if isinstance(instance, str):
            target = instance if direction == "in" else f"{shell}/{instance}"
        self._capture("ws", kind, source, target, None, frame.get("data"))
