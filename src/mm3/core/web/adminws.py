"""The admin channel ``/admin/ws``: live status, logs and bus inspector.

The channel needs an admin session and an ``Origin`` matching the listener,
else it closes with 4403. Frames are ``{v: 1, kind, data}``. The admin sends
``subscribe`` ``{topic, filter}`` and ``unsubscribe`` ``{topic}``; the
engine sends ``status`` ``{status}``, ``log`` ``{record}``, ``bus``
``{entry}`` and ``dropped`` ``{topic, count}`` when an admin falls behind
its queue of 1 000 frames per topic (CORE-304).

``installer`` ``{status, operation}`` frames follow the installer service (CORE-356): at subscribe
one frame with ``operation`` null, then one for each operation not yet ``done``, ``rolled_back`` or
``failed``, oldest first; afterwards one for each operation whose state or steps changed and one
when ``status`` changed. The engine reads the installer every second (every 5 s while it cannot be
reached) while a subscription is open, never without one.

``status`` is sent at subscribe and then whenever a field other than
``uptime_s`` changes, compared at most once a second (CORE-317). ``logs``
first sends the newest 200 matching records of the ring buffer, oldest
first, then live ones (CORE-316). ``bus`` sends the captured entries and
then new ones; the inspector captures while a ``bus`` subscription is open
(CORE-115). A ``subscribe`` with an unknown topic, filter key or level is
ignored and logged as a warning (CORE-316).

The channel also tells which admin sessions have one open: Show on mirror
reverts when the session that pushed a draft has none left (CORE-307).

Implements:
    CORE-304, CORE-115: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-316, CORE-317: Core spec 0.24, §10.5 Diagnostics and debugging.
    CORE-307: Core spec 0.28, §6.6 Live layout preview.
    CORE-356: Core spec 0.44, §10.7 Installer service.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from mm3.core.diag.inspector import BusFilter, BusInspector
from mm3.core.diag.logfilter import LogFilter
from mm3.core.logging import get_logger, numbered_records, observe
from mm3.core.web.auth import WebAuth, origin_matches
from mm3.core.web.installer import FINAL_STATES, UNAVAILABLE, InstallerLink
from mm3.core.web.problems import ApiError
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.core.web.ws import MAX_FRAME_BYTES

__all__ = [
    "CLOSE_FORBIDDEN",
    "INITIAL_LOGS",
    "INSTALLER_AWAY_INTERVAL_S",
    "INSTALLER_INTERVAL_S",
    "QUEUE_FRAMES",
    "TOPICS",
    "AdminChannel",
]

CLOSE_FORBIDDEN = 4403
QUEUE_FRAMES = 1000
"""Frames queued per topic and admin before new ones are dropped (CORE-304)."""
INITIAL_LOGS = 200
"""Ring buffer records sent at a ``logs`` subscribe (CORE-304)."""
STATUS_INTERVAL_S = 1.0
"""How often the status object is compared (CORE-317)."""
INSTALLER_INTERVAL_S = 1.0
"""How often the installer is read while someone is subscribed (CORE-356)."""
INSTALLER_AWAY_INTERVAL_S = 5.0
"""The same while the installer cannot be reached (CORE-356)."""
TOPICS = ("status", "logs", "bus", "installer")

_log = get_logger(subsystem="admin")


def _frame(kind: str, data: Any) -> str:
    return json.dumps({"v": 1, "kind": kind, "data": data}, default=str)


def _without_uptime(status: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in status.items() if k != "uptime_s"}


@dataclass
class _Topic:
    queue: deque[str] = field(default_factory=deque)
    dropped: int = 0
    stop: Callable[[], None] | None = None


class _Admin:
    """One open admin channel."""

    def __init__(
        self, ws: WebSocket, loop: asyncio.AbstractEventLoop, sid: str | None = None
    ) -> None:
        self.ws = ws
        self.loop = loop
        self.sid = sid
        self.topics: dict[str, _Topic] = {}
        self.ready = asyncio.Event()

    def push(self, topic: str, text: str) -> None:
        t = self.topics.get(topic)
        if t is None:
            return
        if len(t.queue) >= QUEUE_FRAMES:
            t.dropped += 1
        else:
            t.queue.append(text)
        self.ready.set()

    def push_threadsafe(self, topic: str, text: str) -> None:
        self.loop.call_soon_threadsafe(self.push, topic, text)

    def close_topic(self, topic: str) -> None:
        t = self.topics.pop(topic, None)
        if t is not None and t.stop is not None:
            t.stop()

    async def sender(self) -> None:
        while True:
            await self.ready.wait()
            self.ready.clear()
            for topic, t in list(self.topics.items()):
                while t.queue:
                    await self.ws.send_text(t.queue.popleft())
                if t.dropped:
                    count, t.dropped = t.dropped, 0
                    await self.ws.send_text(_frame("dropped", {"topic": topic, "count": count}))


class AdminChannel:
    """Serve ``/admin/ws`` (CORE-304).

    Args:
        auth: Sessions, to check the admin.
        status: Returns the status object of CORE-305.
        inspector: The bus inspector.
        interval: Seconds between status comparisons (CORE-317).
        installer: The link to the installer service for the ``installer`` topic; without it
            the installer counts as unavailable (CORE-356).
        installer_interval: Seconds between readings of the installer (CORE-356).
        installer_away_interval: The same while it cannot be reached (CORE-356).

    Implements:
        CORE-304: Core spec 0.22, §10.5 Diagnostics and debugging.
        CORE-317: Core spec 0.24, §10.5 Diagnostics and debugging.
        CORE-356: Core spec 0.44, §10.7 Installer service.
    """

    def __init__(
        self,
        auth: WebAuth,
        status: Callable[[], dict[str, Any]],
        inspector: BusInspector,
        *,
        interval: float = STATUS_INTERVAL_S,
        installer: InstallerLink | None = None,
        installer_interval: float = INSTALLER_INTERVAL_S,
        installer_away_interval: float = INSTALLER_AWAY_INTERVAL_S,
    ) -> None:
        self.auth = auth
        self.status = status
        self.inspector = inspector
        self.interval = interval
        self.installer = installer
        self.installer_interval = installer_interval
        self.installer_away_interval = installer_away_interval
        self._open: set[_Admin] = set()
        self.session_closed: list[Callable[[str], None]] = []

    @property
    def connections(self) -> int:
        """Open admin channels."""
        return len(self._open)

    def has_session(self, sid: str) -> bool:
        """Whether the admin session ``sid`` has an open channel (CORE-307)."""
        return any(a.sid == sid for a in self._open)

    async def serve(self, ws: WebSocket) -> None:
        """Run one admin channel until it closes.

        Args:
            ws: The WebSocket.
        """
        check = await self.auth.check_admin(ws, "GET")
        allowed = check.outcome == "ok" and check.via == "session" and origin_matches(ws)
        await ws.accept()
        if not allowed:
            await ws.close(CLOSE_FORBIDDEN)
            return
        admin = _Admin(ws, asyncio.get_running_loop(), ws.cookies.get(ADMIN_COOKIE))
        self._open.add(admin)
        sender = asyncio.create_task(admin.sender(), name="mm3-admin-ws-sender")
        try:
            while True:
                text = await ws.receive_text()
                if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                    await ws.close(1009)
                    break
                self._handle(admin, text)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await sender
            for topic in list(admin.topics):
                admin.close_topic(topic)
            self._open.discard(admin)
            if admin.sid is not None and not self.has_session(admin.sid):
                for listener in list(self.session_closed):
                    listener(admin.sid)

    def _handle(self, admin: _Admin, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            _log.warning("admin_ws_invalid_frame")
            return
        data = frame.get("data") if isinstance(frame, dict) else None
        kind = frame.get("kind") if isinstance(frame, dict) else None
        if not isinstance(data, dict) or kind not in ("subscribe", "unsubscribe"):
            _log.warning("admin_ws_invalid_frame", kind=str(kind))
            return
        topic = data.get("topic")
        if topic not in TOPICS:
            _log.warning("admin_ws_unknown_topic", topic=str(topic))
            return
        if kind == "unsubscribe":
            admin.close_topic(topic)
            return
        try:
            self._subscribe(admin, topic, data.get("filter"))
        except ValueError as exc:
            _log.warning("admin_ws_invalid_filter", topic=topic, error=str(exc))

    def _subscribe(self, admin: _Admin, topic: str, raw: Any) -> None:
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("filter must be an object")
        if topic == "logs":
            log_filter = LogFilter.parse(raw)
            admin.close_topic(topic)
            admin.topics[topic] = _Topic()
            records = [r for _, r in numbered_records() if log_filter.matches(r)]
            for record in records[-INITIAL_LOGS:]:
                admin.push(topic, _frame("log", {"record": record}))

            def on_record(record: dict[str, Any]) -> None:
                if log_filter.matches(record):
                    admin.push_threadsafe(topic, _frame("log", {"record": record}))

            admin.topics[topic].stop = observe(on_record)
        elif topic == "bus":
            bus_filter = BusFilter.parse(raw)
            admin.close_topic(topic)
            admin.topics[topic] = _Topic()
            for entry in list(self.inspector.entries):
                if bus_filter.matches(entry):
                    admin.push(topic, _frame("bus", {"entry": entry}))

            def on_entry(entry: dict[str, Any]) -> None:
                if bus_filter.matches(entry):
                    admin.push(topic, _frame("bus", {"entry": entry}))

            admin.topics[topic].stop = self.inspector.subscribe(on_entry)
        else:
            if raw:
                raise ValueError(f"the {topic} topic takes no filter")
            admin.close_topic(topic)
            admin.topics[topic] = _Topic()
            loop = self._installer_loop if topic == "installer" else self._status_loop
            task = asyncio.create_task(loop(admin), name=f"mm3-admin-ws-{topic}")

            def stop() -> None:
                task.cancel()

            admin.topics[topic].stop = stop

    async def _status_loop(self, admin: _Admin) -> None:
        """Send ``status`` at subscribe and on change, compared once a second (CORE-317)."""
        last: dict[str, Any] | None = None
        while True:
            current = self.status()
            compared = _without_uptime(current)
            if compared != last:
                last = compared
                admin.push("status", _frame("status", {"status": current}))
            await asyncio.sleep(self.interval)

    async def _installer_loop(self, admin: _Admin) -> None:
        """Send ``installer`` frames: the start state, then every change (CORE-356).

        At subscribe: one frame with ``operation`` null, then one for each operation not yet
        final, oldest first. Then one frame for each operation whose state or steps changed
        since the last reading (the newest state, so a burst of steps is one frame) and, when
        only ``status`` changed, one with ``operation`` null. The installer is read every
        ``installer_interval`` seconds, every ``installer_away_interval`` while it cannot be
        reached; the loop ends with the subscription, so there is no reading without one.
        """
        seen: dict[str, tuple[Any, ...]] = {}
        last: dict[str, Any] | None = None
        first = True
        while True:
            snapshot = await self.installer.snapshot() if self.installer else UNAVAILABLE
            status = snapshot.status()
            operations: list[dict[str, Any]] = []
            if snapshot.available and self.installer is not None:
                try:
                    operations = await self.installer.operations()
                except ApiError:
                    operations = []
            oldest_first = list(reversed(operations))

            if first:
                _send_installer(admin, status, None)
                for op in oldest_first:
                    if op["state"] not in FINAL_STATES:
                        _send_installer(admin, status, op)
                first = False
            else:
                changed = [op for op in oldest_first if seen.get(str(op["id"])) != _signature(op)]
                for op in changed:
                    _send_installer(admin, status, op)
                if status != last and not changed:
                    _send_installer(admin, status, None)
            seen.update({str(op["id"]): _signature(op) for op in operations})
            last = status
            wait = self.installer_interval if snapshot.available else self.installer_away_interval
            await asyncio.sleep(wait)


def _send_installer(
    admin: _Admin, status: dict[str, Any], operation: dict[str, Any] | None
) -> None:
    """Queue one ``installer`` frame: ``{status, operation}`` (CORE-356)."""
    admin.push("installer", _frame("installer", {"status": status, "operation": operation}))


def _signature(op: dict[str, Any]) -> tuple[Any, ...]:
    """What makes an operation's frame new: its state and how many steps it has."""
    return (op.get("state"), len(op.get("steps") or []))
