"""The shell WebSocket: handshake, roles, frames, limits and delivery.

Every shell holds one connection: ``/ws`` on the mirror listener (roles
``kiosk`` and ``browser``) or ``/admin/preview/ws`` on the admin listener
(role ``preview``). The first frame is ``hello``; the engine answers with
``welcome``, one ``state`` frame per instance and a ``control`` ``page``
frame with the visible instances. After that the :class:`ClientHub`
carries frames both ways: it is the bus endpoint that delivers
notifications to browsers, the client channel through which backends reach
their own elements, and the shell side of the page service.

Implements:
    CORE-246, CORE-271: Core spec 0.19, §5.6 WebSocket protocol.
    CORE-288: Core spec 0.19, §6.2 Rules.
    CORE-247, CORE-248, CORE-249, CORE-251: Core spec 0.13, §5.6 WebSocket protocol.
    CORE-272, CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    CORE-275, CORE-276: Core spec 0.14, §5.6 WebSocket protocol.
    CORE-117: Core spec 0.14, §10.5 Diagnostics and debugging.
    CORE-21: Core spec 0.9, §5.3 Routing rules.
    CORE-223: Core spec 0.13, §5.4 Request and reply.
    CORE-254: Core spec 0.12, §10.1 Security.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import json
import secrets
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from mm3.__about__ import __version__
from mm3.core.bus import Bus
from mm3.core.context import REQUEST_TIMEOUT_RANGE
from mm3.core.logging import get_logger
from mm3.core.web.auth import WebAuth, origin_matches
from mm3.core.web.security import is_loopback
from mm3.core.web.sessions import DISPLAY_COOKIE
from mm3.core.web.view import ShellInstance, ShellView, content_version

if TYPE_CHECKING:
    from mm3.core.config.diff import ReloadPlan
    from mm3.core.config.validate import ConfigResult
    from mm3.core.pages import PageService
    from mm3.core.supervisor import State, Supervisor
from mm3.messages import ClientMessage, Notification, check_payload
from mm3.notifications import validate_payload

__all__ = [
    "CLOSE_FRAME_TOO_BIG",
    "CLOSE_OVERLOADED",
    "CLOSE_PROTOCOL",
    "CLOSE_ROLE",
    "HELLO_TIMEOUT_S",
    "MAX_FRAME_BYTES",
    "OUT_QUEUE_FRAMES",
    "PING_INTERVAL_S",
    "PING_TIMEOUT_S",
    "PROTOCOL",
    "ClientHub",
    "KioskKeys",
    "engine_build",
]

PROTOCOL = 1
DISPLAY_COOKIE_MAX_AGE = 400 * 24 * 3600
"""Lifetime of the display session cookie; browsers cap cookies at 400 days."""
MAX_FRAME_BYTES = 512 * 1024
OUT_QUEUE_FRAMES = 1000
HELLO_TIMEOUT_S = 10.0
PING_INTERVAL_S = 15.0
PING_TIMEOUT_S = 15.0
"""A ping every 15 s; a client that has not answered for 30 s is dropped (CORE-251)."""

CLOSE_PROTOCOL = 4400
CLOSE_ROLE = 4403
CLOSE_FRAME_TOO_BIG = 1009
CLOSE_OVERLOADED = 1013

REF_MAX = 64
ERROR_KEYS = {
    "reserved_name": "CORE_WS_RESERVED_NAME",
    "unknown_instance": "CORE_WS_UNKNOWN_INSTANCE",
    "invalid_payload": "CORE_WS_INVALID_PAYLOAD",
    "invalid_frame": "CORE_WS_INVALID_FRAME",
}
"""``error`` frame codes and their catalog keys (CORE-272)."""

REQUEST_TIMEOUT_KEY = "REQUEST_TIMEOUT"
Endpoint = Literal["mirror", "admin"]
Role = Literal["kiosk", "browser", "preview"]
_ROLES = ("kiosk", "browser", "preview")
_INBOUND = frozenset({"hello", "notify", "client", "request", "log", "visibility", "touch"})
_GESTURES = frozenset({"swipe_left", "swipe_right", "tap"})
_UNAVAILABLE = frozenset({"FAILED", "DISABLED"})
_ALWAYS_ACCEPTED = frozenset({"hello", "log"})

_log = get_logger(subsystem="ws")


def engine_build(static_dir: Path) -> str:
    """Return ``engine_build``: the version and a hash of the files under ``/core`` (CORE-273).

    Args:
        static_dir: Directory served at ``/core``.

    Returns:
        ``<version>+<first 12 hex digits of the SHA-256 of the files>``.

    Implements:
        CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    """
    h = hashlib.sha256()
    for path in sorted(p for p in static_dir.rglob("*") if p.is_file()):
        h.update(path.relative_to(static_dir).as_posix().encode("utf-8") + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return f"{__version__}+{h.hexdigest()[:12]}"


class KioskKeys:
    """The key that lets a kiosk shell claim the ``kiosk`` role (CORE-247, CORE-268).

    The engine issues a new key for each Chromium launch; issuing one makes
    the previous key invalid. The same key stays valid for reconnects of
    that launch.

    Implements:
        CORE-247: Core spec 0.13, §5.6 WebSocket protocol.
    """

    def __init__(self) -> None:
        self._current: str | None = None

    def issue(self) -> str:
        """Issue the key for the next Chromium launch.

        Returns:
            The key, for the start URL fragment (``/#kiosk=<key>``).
        """
        self._current = secrets.token_urlsafe(24)
        return self._current

    def valid(self, key: object) -> bool:
        """Check a key presented in ``hello``.

        Args:
            key: The ``key`` field.

        Returns:
            True for the current key.
        """
        return (
            isinstance(key, str)
            and self._current is not None
            and secrets.compare_digest(key, self._current)
        )


@dataclass
class _Client:
    """One connected shell."""

    ws: WebSocket
    client_id: str
    role: Role
    interactive: bool
    instances: set[str]
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    dropped_inbound: int = 0
    closing: int | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    viewport: tuple[int, int] = (0, 0)
    dpr: float = 1.0


def _frame(kind: str, data: Any, instance: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"v": 1, "kind": kind}
    if instance is not None:
        out["instance"] = instance
    out["data"] = data
    return out


def _matches(patterns: tuple[str, ...] | None, name: str) -> bool:
    return patterns is None or any(fnmatch.fnmatchcase(name, p) for p in patterns)


class _ProtocolError(Exception):
    """A first frame that breaks the handshake (close 4400)."""


class ClientHub:
    """Connected shells: bus endpoint, client channel and WebSocket handler.

    Args:
        view: Returns what shells are told about the mirror now.
        auth: Sessions and tokens, to verify roles.
        build: ``engine_build`` (CORE-273).
        kiosk_keys: The kiosk key (CORE-247).
        hello_timeout: Seconds to wait for ``hello`` (CORE-273).

    Implements:
        CORE-246, CORE-271, CORE-275, CORE-276: Core spec 0.19, §5.6 WebSocket protocol.
        CORE-247, CORE-248, CORE-249: Core spec 0.13, §5.6 WebSocket protocol.
        CORE-272, CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    """

    def __init__(
        self,
        view: Callable[[], ShellView],
        auth: WebAuth,
        build: str,
        *,
        kiosk_keys: KioskKeys | None = None,
        hello_timeout: float = HELLO_TIMEOUT_S,
    ) -> None:
        self.view = view
        self.auth = auth
        self.build = build
        self.kiosk_keys = kiosk_keys or KioskKeys()
        self.hello_timeout = hello_timeout
        self.bus: Bus | None = None
        self.pages: PageService | None = None
        self.status: Callable[[str], str] = lambda _iid: "ok"
        self.snapshots: dict[str, dict[str, Any]] = {}
        self._clients: dict[int, _Client] = {}

    def attach_pages(self, pages: PageService, supervisor: Supervisor) -> None:
        """Connect the page service and instance states (CORE-117, CORE-275).

        Args:
            pages: The page service; its changes become ``control`` frames.
            supervisor: Instance states; FAILED and DISABLED show the
                failure placeholder.
        """
        self.pages = pages
        pages.shells = self

        def status(iid: str) -> str:
            try:
                state = supervisor.get(iid).state
            except KeyError:
                return "ok"
            return "unavailable" if str(state) in _UNAVAILABLE else "ok"

        self.status = status

        def changed(iid: str, old: State, new: State) -> None:
            before = "unavailable" if str(old) in _UNAVAILABLE else "ok"
            after = "unavailable" if str(new) in _UNAVAILABLE else "ok"
            if before != after:
                self.control("status", {"status": after}, iid)

        supervisor.state_listeners.append(changed)

    # --- control frames (CORE-275) ----------------------------------------------------

    def control(self, action: str, data: Mapping[str, Any], instance: str | None = None) -> None:
        """Send a ``control`` frame to every shell (that shows ``instance``, if given).

        Args:
            action: The action (CORE-275).
            data: The action's fields.
            instance: The instance it concerns, if any.

        Implements:
            CORE-275: Core spec 0.14, §5.6 WebSocket protocol.
        """
        frame = _frame("control", {"action": action, **data}, instance)
        for c in self._clients.values():
            if instance is None or instance in c.instances:
                self._push(c, frame)

    def _page_data(self) -> dict[str, Any]:
        pages = self.pages
        view = self.view()
        if pages is None:
            ids = [p["id"] for p in view.pages]
            page = view.page
            visible = [i.id for i in view.instances]
            transition = "none"
        else:
            ids = pages.ids
            page = pages.page
            visible = [i for i in pages.visible_ids() if view.instance(i) is not None]
            transition = pages.transition
        return {
            "page": page,
            "index": ids.index(page) if page in ids else 0,
            "count": len(ids),
            "transition": transition,
            "visible": visible,
        }

    def page_changed(self, page: str, index: int, count: int, transition: str) -> None:
        """Send ``control`` ``page`` with the visible instances (CORE-237, CORE-275)."""
        self.control("page", self._page_data())

    def visibility_changed(self, instance_id: str, hidden: bool, fade: int) -> None:
        """Send ``control`` ``hide`` or ``show`` for one instance (CORE-259, CORE-275)."""
        self.control("hide" if hidden else "show", {"fade": fade}, instance_id)

    def resync(self) -> None:
        """Ask every shell to rebuild from a new ``welcome`` (CORE-275)."""
        self.control("reload", {"scope": "resync"})

    def styles(self, custom_css: str | None) -> None:
        """Tell every shell that ``/custom.css`` changed (CORE-262, CORE-275)."""
        self.control("styles", {"custom_css": custom_css})

    async def reloaded(self, result: ConfigResult, plan: ReloadPlan) -> None:
        """Tell the shells about a hot reload (layout listener, CORE-275).

        Changes of instances, configuration, appearance, position, pages,
        layout or display make every shell rebuild from a new ``welcome``
        (``reload`` ``resync``). A locale change alone sends ``locale`` to
        each affected instance (CORE-267), unless its header text changes
        with it; a changed ``custom.css`` alone sends ``styles``. The page
        service learns the new configuration first.

        Args:
            result: The new configuration.
            plan: What changed.

        Implements:
            CORE-275: Core spec 0.14, §5.6 WebSocket protocol.
            CORE-267: Core spec 0.15, §8.5 Catalogs at run time.
        """
        if self.pages is not None:
            self.pages.reload(result)
        shell_keys = {"timezone", "location", "input", "layout"}
        rebuild = bool(
            plan.stop
            or plan.replace
            or plan.reconfigure
            or plan.start
            or plan.relayout
            or plan.layout
            or plan.pages
            or plan.display
            or plan.engine_live & shell_keys
        )
        view = self.view()
        relocalized: list[ShellInstance] = []
        if not rebuild and (plan.locale or "locale" in plan.engine_live):
            for inst in view.instances:
                spec = next((i.spec for i in result.instances if i.id == inst.id), None)
                own = spec is not None and spec.locale is not None
                if inst.id in plan.locale or ("locale" in plan.engine_live and not own):
                    relocalized.append(inst)
            # A header text that follows the locale is appearance: rebuild.
            rebuild = any(inst.appearance.get("header") is not None for inst in relocalized)
        if rebuild:
            self.resync()
            return
        for inst in relocalized:
            self.control(
                "locale",
                {"locale": inst.locale, "config": dict(inst.config), "catalog": inst.catalog},
                inst.id,
            )
        if plan.custom_css:
            css = result.assets.custom_css
            self.styles(content_version(css) if css is not None else None)

    def attach(self, bus: Bus) -> None:
        """Start receiving notifications from the bus.

        Args:
            bus: The engine's bus.
        """
        self.bus = bus
        bus.add_endpoint(self)

    def clients(self) -> list[dict[str, Any]]:
        """Describe the connected shells (for diagnostics and tests).

        Returns:
            ``{client_id, role, interactive, instances, queued, dropped_inbound}`` per shell.
        """
        return [
            {
                "client_id": c.client_id,
                "role": c.role,
                "interactive": c.interactive,
                "instances": sorted(c.instances),
                "queued": len(c.queue),
                "dropped_inbound": c.dropped_inbound,
                "viewport": list(c.viewport),
            }
            for c in self._clients.values()
        ]

    # --- outbound -------------------------------------------------------------------

    def _push(self, client: _Client, frame: dict[str, Any]) -> None:
        """Queue a frame; a full queue closes the connection with 1013 (CORE-249)."""
        if client.closing is not None:
            return
        if frame["kind"] == "state":
            # A newer state frame replaces one still queued, sent as the full snapshot.
            instance = str(frame.get("instance"))
            for i, queued in enumerate(client.queue):
                if queued["kind"] == "state" and queued.get("instance") == instance:
                    del client.queue[i]
                    frame = _frame(
                        "state",
                        {"state": dict(self.snapshots.get(instance, {})), "delta": False},
                        instance,
                    )
                    break
        if len(client.queue) >= OUT_QUEUE_FRAMES:
            client.closing = CLOSE_OVERLOADED
            client.queue.clear()
            _log.warning("ws_queue_full", client=client.client_id, role=client.role)
        else:
            client.queue.append(frame)
        client.ready.set()

    def send_client(
        self, instance_id: str, type: str, payload: dict[str, Any], client: str | None
    ) -> None:
        """Deliver a ``client`` frame to the instance's elements (or one client's).

        Preview clients get no ``client`` frames (CORE-99).

        Args:
            instance_id: Sending instance.
            type: Message type.
            payload: Payload.
            client: Client id, or ``None`` for every client showing the instance.
        """
        frame = _frame("client", {"type": type, "payload": payload}, instance_id)
        for c in self._clients.values():
            if c.role == "preview" or instance_id not in c.instances:
                continue
            if client is None or c.client_id == client:
                self._push(c, frame)

    def send_state(self, instance_id: str, state: dict[str, Any], delta: bool) -> None:
        """Keep the instance's snapshot and send a ``state`` frame (CORE-271).

        Args:
            instance_id: The instance.
            state: Full snapshot, or a shallow delta.
            delta: Whether ``state`` is a delta.
        """
        if delta:
            self.snapshots.setdefault(instance_id, {}).update(state)
        else:
            self.snapshots[instance_id] = dict(state)
        frame = _frame("state", {"state": dict(state), "delta": delta}, instance_id)
        for c in self._clients.values():
            if instance_id in c.instances:
                self._push(c, frame)

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        """Send a notification to every shell once, with its matching instances (CORE-249).

        Requests and replies are not sent: only backends answer, and replies
        reach the shell that asked as ``reply`` frames.

        Args:
            n: The notification.
            exclude_instance: The sender instance, whose elements do not get it (CORE-21).
        """
        if n.expects_reply or n.is_reply:
            return
        view = self.view()
        data_json: dict[str, Any] | None = None
        for c in self._clients.values():
            if n.target is not None:
                ids = [n.target] if n.target in c.instances else []
            else:
                ids = []
                for iid in sorted(c.instances):
                    if iid == exclude_instance:
                        continue
                    inst = view.instance(iid)
                    if inst is not None and _matches(inst.subscribes, n.name):
                        ids.append(iid)
            if not ids:
                continue
            if data_json is None:
                data_json = n.model_dump(mode="json")
            self._push(c, _frame("notify", {"notification": data_json, "instances": ids}))

    async def _sender(self, client: _Client) -> None:
        while True:
            await client.ready.wait()
            client.ready.clear()
            while client.queue:
                frame = client.queue.popleft()
                await client.ws.send_text(json.dumps(frame, separators=(",", ":")))
            if client.closing is not None:
                await client.ws.close(client.closing)
                return

    # --- connection -------------------------------------------------------------------

    async def serve(self, ws: WebSocket, endpoint: Endpoint) -> None:
        """Run one shell connection from handshake to close.

        Args:
            ws: The WebSocket, not yet accepted.
            endpoint: ``mirror`` for ``/ws``, ``admin`` for ``/admin/preview/ws``.

        Implements:
            CORE-246, CORE-247: Core spec 0.19, §5.6 WebSocket protocol.
            CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
        """
        await ws.accept(headers=await self._refresh_display_cookie(ws, endpoint))
        try:
            hello = await self._hello(ws)
        except _ProtocolError as exc:
            _log.info("ws_handshake_refused", reason=str(exc))
            await ws.close(CLOSE_PROTOCOL)
            return
        except WebSocketDisconnect:
            return
        role: Role = hello["role"]
        interactive = await self._verify_role(ws, endpoint, role, hello)
        if interactive is None:
            _log.info("ws_role_refused", role=role, endpoint=endpoint)
            await ws.close(CLOSE_ROLE)
            return
        view = self.view()
        client = _Client(
            ws=ws,
            client_id=hello["client_id"],
            role=role,
            interactive=interactive,
            instances={i.id for i in view.instances},
            viewport=(hello["viewport"][0], hello["viewport"][1]),
            dpr=float(hello["dpr"]),
        )
        welcome = {
            "engine_build": self.build,
            "client_id": client.client_id,
            "role": role,
            "interactive": interactive,
            "layout": dict(view.layout),
            "page": self.pages.page if self.pages is not None else view.page,
            "pages": view.pages,
            "locale": view.locale,
            "timezone": view.timezone,
            "location": view.location,
            "touch": view.touch,
            "custom_css": view.custom_css,
            "instances": [i.welcome_entry(self.status(i.id)) for i in view.instances],
        }
        self._clients[id(client)] = client
        client.queue.append(_frame("welcome", welcome))
        for inst in view.instances:
            snapshot = dict(self.snapshots.get(inst.id, {}))
            client.queue.append(_frame("state", {"state": snapshot, "delta": False}, inst.id))
        client.queue.append(_frame("control", {"action": "page", **self._page_data()}))
        client.ready.set()
        _log.info("ws_connected", client=client.client_id, role=role, interactive=interactive)
        sender = asyncio.create_task(self._sender(client), name=f"mm3-ws-out:{client.client_id}")
        try:
            await self._receive(client)
        finally:
            self._clients.pop(id(client), None)
            sender.cancel()
            for task in list(client.tasks):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            if ws.application_state == WebSocketState.CONNECTED and client.closing is not None:
                with contextlib.suppress(Exception):
                    await ws.close(client.closing)
            _log.info("ws_disconnected", client=client.client_id, role=role)

    async def _refresh_display_cookie(
        self, ws: WebSocket, endpoint: Endpoint
    ) -> list[tuple[bytes, bytes]] | None:
        """Renew a valid display session cookie on the handshake response (CORE-254).

        The session lasts until its token is revoked; renewing the cookie at
        each connection keeps a paired display paired past the browser's
        cookie lifetime.
        """
        sid = ws.cookies.get(DISPLAY_COOKIE)
        if endpoint != "mirror" or not sid or await self.auth.display_session(ws) is None:
            return None
        cookie = (
            f"{DISPLAY_COOKIE}={sid}; Max-Age={DISPLAY_COOKIE_MAX_AGE}; Path=/; "
            "HttpOnly; SameSite=Strict"
        )
        return [(b"set-cookie", cookie.encode("latin-1"))]

    async def _hello(self, ws: WebSocket) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.hello_timeout):
                text = await ws.receive_text()
        except TimeoutError:
            raise _ProtocolError("no hello within the timeout") from None
        except (KeyError, RuntimeError) as exc:
            raise _ProtocolError("not a text frame") from exc
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise _ProtocolError("hello too large")
        try:
            frame = json.loads(text)
        except ValueError as exc:
            raise _ProtocolError("hello is not JSON") from exc
        if not isinstance(frame, dict) or frame.get("v") != 1 or frame.get("kind") != "hello":
            raise _ProtocolError("first frame is not hello")
        data = frame.get("data")
        if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
            raise _ProtocolError("unsupported protocol")
        try:
            UUID(str(data.get("client_id")))
        except ValueError as exc:
            raise _ProtocolError("client_id is not a UUID") from exc
        viewport = data.get("viewport")
        if (
            data.get("role") not in _ROLES
            or not isinstance(viewport, list)
            or len(viewport) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in viewport)
            or not isinstance(data.get("dpr"), int | float)
            or isinstance(data.get("dpr"), bool)
            or data["dpr"] <= 0
        ):
            raise _ProtocolError("invalid hello fields")
        data["client_id"] = str(UUID(str(data["client_id"])))
        return data

    async def _verify_role(
        self, ws: WebSocket, endpoint: Endpoint, role: Role, hello: Mapping[str, Any]
    ) -> bool | None:
        """Return whether the client is interactive, or ``None`` to refuse the role (CORE-247)."""
        if endpoint == "admin":
            if role != "preview" or not origin_matches(ws):
                return None
            check = await self.auth.check_admin(ws)
            return True if check.outcome == "ok" and check.via == "session" else None
        if role == "preview":
            return None
        if role == "kiosk":
            host = ws.client.host if ws.client else None
            return True if is_loopback(host) and self.kiosk_keys.valid(hello.get("key")) else None
        return await self.auth.display_session(ws) is not None

    # --- inbound ----------------------------------------------------------------------

    async def _receive(self, client: _Client) -> None:
        while client.closing is None:
            try:
                message = await client.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                raw = message.get("bytes") or b""
                if len(raw) > MAX_FRAME_BYTES:
                    self._close(client, CLOSE_FRAME_TOO_BIG)
                    return
                self._error(client, "invalid_frame", None, None)
                continue
            if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                self._close(client, CLOSE_FRAME_TOO_BIG)
                return
            self._handle(client, text)

    def _close(self, client: _Client, code: int) -> None:
        client.closing = code
        client.queue.clear()
        client.ready.set()

    def _error(self, client: _Client, code: str, ref: Any, instance: str | None) -> None:
        """Send an ``error`` frame (CORE-272)."""
        data = {"code": code, "message_key": ERROR_KEYS[code], "ref": ref}
        self._push(client, _frame("error", data, instance))

    def _handle(self, client: _Client, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            self._error(client, "invalid_frame", None, None)
            return
        if not isinstance(frame, dict):
            self._error(client, "invalid_frame", None, None)
            return
        ref = frame.get("ref")
        if not (ref is None or (isinstance(ref, str) and len(ref) <= REF_MAX)):
            ref = None
            self._error(client, "invalid_frame", None, None)
            return
        kind = frame.get("kind")
        instance = frame.get("instance")
        data = frame.get("data")
        if frame.get("v") != 1 or kind not in _INBOUND or not isinstance(data, dict):
            self._error(client, "invalid_frame", ref, None)
            return
        if not client.interactive and kind not in _ALWAYS_ACCEPTED:
            client.dropped_inbound += 1
            return
        if kind == "hello":
            self._error(client, "invalid_frame", ref, None)
            return
        if kind == "log":
            self._log_frame(client, instance, data)
            return
        if kind == "touch":
            self._touch_frame(client, data, ref)
            return
        if not isinstance(instance, str) or instance not in client.instances:
            self._error(client, "unknown_instance", ref, None)
            return
        if kind == "client":
            self._client_frame(client, instance, data, ref)
        elif kind == "notify":
            self._notify_frame(client, instance, data, ref)
        elif kind == "visibility":
            self._visibility_frame(client, instance, data, ref)
        else:
            self._request_frame(client, instance, data, ref)

    def _visibility_frame(
        self, client: _Client, instance: str, data: Mapping[str, Any], ref: Any
    ) -> None:
        """``this.hide()`` / ``this.show()``: the hide reason ``self`` (CORE-238, CORE-276).

        A preview client's ``visibility`` changes only its own view, so it is
        not applied.
        """
        hidden = data.get("hidden")
        fade = data.get("fade", 300)
        if (
            not isinstance(hidden, bool)
            or not isinstance(fade, int)
            or isinstance(fade, bool)
            or not 0 <= fade <= 60_000
        ):
            self._error(client, "invalid_frame", ref, instance)
            return
        if client.role != "preview" and self.pages is not None:
            self.pages.set_self(instance, hidden, fade)

    def _touch_frame(self, client: _Client, data: Mapping[str, Any], ref: Any) -> None:
        """A touch from an interactive client (CORE-235, CORE-260, CORE-276)."""
        gesture = data.get("gesture")
        if gesture not in _GESTURES:
            self._error(client, "invalid_frame", ref, None)
            return
        if client.role != "preview" and self.pages is not None and self.view().touch:
            self.pages.touch(gesture)

    def _source(self, instance: str) -> str:
        inst = self.view().instance(instance)
        module = inst.module if inst is not None else "unknown"
        return f"{module}/{instance}"

    def _client_frame(
        self, client: _Client, instance: str, data: Mapping[str, Any], ref: Any
    ) -> None:
        try:
            msg = ClientMessage(
                type=data.get("type"),  # type: ignore[arg-type]
                payload=data.get("payload") or {},
                client=client.client_id,
                trace=uuid4(),
            )
        except (ValidationError, ValueError, TypeError):
            self._error(client, "invalid_frame", ref, instance)
            return
        if self.bus is None or not self.bus.deliver_client_message(instance, msg):
            _log.debug("ws_client_message_dropped", instance=instance, type=msg.type)

    def _notification(
        self,
        instance: str,
        data: Mapping[str, Any],
        *,
        expects_reply: bool = False,
    ) -> Notification | str:
        """Build an element's notification, or return the error code (CORE-248)."""
        name = data.get("name")
        if not isinstance(name, str):
            return "invalid_frame"
        if name.startswith("CORE_"):
            return "reserved_name"
        payload = data.get("payload", {})
        target = data.get("target")
        if not isinstance(payload, dict) or not (target is None or isinstance(target, str)):
            return "invalid_frame"
        try:
            check_payload(payload)
        except ValueError:
            return "invalid_frame"
        try:
            validate_payload(name, payload)
        except ValueError:
            return "invalid_payload"
        nid = uuid4()
        try:
            return Notification(
                id=nid,
                trace=nid,
                ts=datetime.now(UTC),
                name=name,
                source=self._source(instance),
                target=target,
                expects_reply=expects_reply,
                payload=payload,
            )
        except (ValidationError, ValueError):
            return "invalid_frame"

    def _notify_frame(
        self, client: _Client, instance: str, data: Mapping[str, Any], ref: Any
    ) -> None:
        n = self._notification(instance, data)
        if isinstance(n, str):
            self._error(client, n, ref, instance)
            return
        if self.bus is not None:
            self.bus.publish(n, sender_instance=instance)

    def _request_frame(
        self, client: _Client, instance: str, data: Mapping[str, Any], ref: Any
    ) -> None:
        rid = data.get("rid")
        timeout = data.get("timeout", 5.0)
        low, high = REQUEST_TIMEOUT_RANGE
        if (
            not isinstance(rid, str | int)
            or isinstance(rid, bool)
            or not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not low <= timeout <= high
        ):
            self._error(client, "invalid_frame", ref, instance)
            return
        n = self._notification(instance, data, expects_reply=True)
        if isinstance(n, str):
            self._error(client, n, ref, instance)
            return
        task = asyncio.create_task(
            self._run_request(client, instance, rid, n, float(timeout)),
            name=f"mm3-ws-request:{instance}",
        )
        client.tasks.add(task)
        task.add_done_callback(client.tasks.discard)

    async def _run_request(
        self, client: _Client, instance: str, rid: str | int, n: Notification, wait: float
    ) -> None:
        """Ask the bus and answer with a ``reply`` frame (CORE-223, CORE-272)."""
        assert self.bus is not None  # noqa: S101 - request frames need the bus
        future = self.bus.open_request(n.id)
        try:
            self.bus.publish(n, sender_instance=instance)
            try:
                async with asyncio.timeout(wait):
                    reply = await future
            except TimeoutError:
                data: dict[str, Any] = {
                    "rid": rid,
                    "error": {"code": "timeout", "message_key": REQUEST_TIMEOUT_KEY},
                }
            else:
                error = reply.payload.get("error") if len(reply.payload) == 1 else None
                if isinstance(error, dict) and set(error) == {"code", "message_key"}:
                    data = {"rid": rid, "error": dict(error)}
                else:
                    data = {"rid": rid, "payload": dict(reply.payload)}
        finally:
            self.bus.close_request(n.id)
        self._push(client, _frame("reply", data, instance))

    def _log_frame(self, client: _Client, instance: Any, data: Mapping[str, Any]) -> None:
        level = data.get("level")
        message = data.get("message")
        if not isinstance(message, str):
            return
        fields: dict[str, Any] = {
            "client": client.client_id,
            "source": "browser",
            "instance": instance if isinstance(instance, str) else None,
            "browser_level": level if isinstance(level, str) else None,
            "message": message[:2000],
        }
        if isinstance(data.get("stack"), str):
            fields["stack"] = data["stack"][:8000]
        if isinstance(data.get("user_agent"), str):
            fields["user_agent"] = data["user_agent"][:300]
        _log.warning("browser_log", **fields)
