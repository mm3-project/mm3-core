"""What the module under test did: notifications, client messages, state, logs.

Implements:
    -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mm3.messages import Notification

__all__ = ["ClientRecord", "LogRecords", "NotificationLog"]


class NotificationLog(list[Notification]):
    """Notifications the instance sent (requests and replies included)."""

    def names(self) -> list[str]:
        """Names in sending order."""
        return [n.name for n in self]

    def of(self, name: str) -> list[Notification]:
        """All notifications with a name."""
        return [n for n in self if n.name == name]

    def last(self, name: str | None = None) -> Notification | None:
        """The latest notification, optionally with a name; ``None`` if none."""
        for n in reversed(self):
            if name is None or n.name == name:
                return n
        return None


@dataclass(frozen=True)
class ClientRecord:
    """A message the backend sent to its front-end with ``send_to_client``.

    Attributes:
        type: Module-private message type.
        payload: Payload.
        client: Target client id, ``None`` for every client.
    """

    type: str
    payload: dict[str, Any]
    client: str | None


class LogRecords(list[dict[str, Any]]):
    """Log records of the instance (``event``, ``level`` and the fields)."""

    def events(self, level: str | None = None) -> list[str]:
        """Event names, optionally of one level."""
        return [r.get("event", "") for r in self if level is None or r.get("level") == level]

    def at_least(self, level: str) -> list[dict[str, Any]]:
        """Records at ``level`` or above."""
        order = ["debug", "info", "warning", "error", "critical"]
        floor = order.index(level)
        return [r for r in self if r.get("level") in order[floor:]]
