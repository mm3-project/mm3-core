"""The engine end of the kiosk agent: ``/ws/kiosk-agent`` (CORE-268, CORE-277).

The engine writes an agent key at start (``kiosk.key`` in its runtime
directory, ``/run/mm3`` under systemd). The kiosk session connects from
loopback with that key as ``Authorization: Bearer <key>``, says ``hello``,
and then reports outputs, modes and the applied mode, Chromium's state and
failures. The engine sends the ``display`` block at connect and on every
change, a one-time kiosk key before each Chromium launch, and ``restart``
when the kiosk shell has been missing for 60 s.

For sound it sends the ``audio`` block (with any run-time volume and mute) at
connect and on every change, and records the agent's ``sinks`` reports: the
outputs, what was applied and whether the requested output was found.

Implements:
    CORE-144, CORE-145, CORE-268, CORE-269: Core spec 0.21, §6.7 Display output.
    CORE-295, CORE-296: Core spec 0.21, §6.7 Display output.
    CORE-277: Core spec 0.21, §5.6 WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import hmac
import json
import os
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from mm3.core.clock import Clock
from mm3.core.logging import get_logger
from mm3.core.web.security import is_loopback
from mm3.core.web.ws import CLOSE_PROTOCOL, CLOSE_ROLE, MAX_FRAME_BYTES, ClientHub

__all__ = [
    "AGENT_KEY_FILE",
    "KIOSK_GROUP",
    "SHELL_SILENCE_S",
    "AgentKey",
    "KioskAgents",
    "KioskStatus",
    "runtime_dir",
]

AGENT_KEY_FILE = "kiosk.key"
"""Name of the agent key file in the engine's runtime directory."""
KIOSK_GROUP = "mm3-kiosk"
"""Group that may read the agent key (the kiosk user's group)."""
SHELL_SILENCE_S = 60.0
"""A kiosk shell missing this long makes the engine ask for a restart (CORE-269)."""
PROTOCOL = 1

_log = get_logger(subsystem="kiosk")


def runtime_dir() -> Path:
    """Return the engine's runtime directory: ``$RUNTIME_DIRECTORY`` or ``/run/mm3``."""
    value = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0]
    return Path(value) if value else Path("/run/mm3")


class AgentKey:
    """The key the kiosk agent presents on ``/ws/kiosk-agent`` (CORE-268).

    Args:
        directory: Where ``kiosk.key`` is written.
    """

    def __init__(self, directory: Path) -> None:
        self.path = directory / AGENT_KEY_FILE
        self.value = secrets.token_urlsafe(32)

    def write(self) -> Path:
        """Write the key with mode 0640 and group ``mm3-kiosk``.

        The engine user is a member of that group (CORE-268). When the group is
        missing or the change fails, the engine logs the warning
        ``kiosk_key_group`` naming the group and keeps running: the key then
        stays readable by the engine's own group only and the kiosk agent
        cannot connect.

        Implements:
            CORE-268: Core spec 0.20, §6.7 Display output.

        Returns:
            The file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as f:
            f.write(self.value + "\n")
        os.chmod(tmp, 0o640)
        try:
            os.chown(tmp, -1, grp.getgrnam(KIOSK_GROUP).gr_gid)
        except KeyError:
            _log.warning("kiosk_key_group", group=KIOSK_GROUP, reason="group_missing")
        except OSError as exc:
            _log.warning(
                "kiosk_key_group", group=KIOSK_GROUP, reason="chown_failed", error=str(exc)
            )
        tmp.replace(self.path)
        return self.path

    def valid(self, authorization: str | None) -> bool:
        """Check an ``Authorization`` header.

        Args:
            authorization: The header value.

        Returns:
            True for ``Bearer <the key>``.
        """
        if not authorization or not authorization.startswith("Bearer "):
            return False
        return hmac.compare_digest(authorization[7:].strip(), self.value)


@dataclass
class KioskStatus:
    """What the engine knows about the kiosk (CORE-145, CORE-269).

    Attributes:
        connected: An agent is connected.
        cage_version: As the agent reported it.
        outputs: The last ``outputs`` list.
        applied: The applied ``{output, width, height, refresh, rotation, scale}``.
        viewport: The applied viewport in CSS pixels.
        dpr: The applied device pixel ratio.
        mismatch: The applied mode differs from the configured one.
        chromium: ``started``, ``exited``, ``failed`` or ``None``.
        restarts: Chromium restarts reported by the agent.
        failed: The agent stopped restarting Chromium.
        failure: The last display ``failure`` reason, if any.
        sinks: The last ``sinks`` list: ``{name, description, kind, available}``.
        audio: What the agent applied: ``{sink, volume, mute, matched}``.
        audio_mismatch: The requested output was not found or is unavailable
            (CORE-296); the agent kept the current default output.
        audio_failure: The last ``failure`` reason with area ``audio``, if any;
            cleared when the agent connects again.
    """

    connected: bool = False
    cage_version: str | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)
    applied: dict[str, Any] | None = None
    viewport: tuple[int, int] | None = None
    dpr: float | None = None
    mismatch: bool = False
    chromium: str | None = None
    restarts: int = 0
    failed: bool = False
    failure: str | None = None
    sinks: list[dict[str, Any]] = field(default_factory=list)
    audio: dict[str, Any] | None = None
    audio_mismatch: bool = False
    audio_failure: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the status for diagnostics (``GET /api/v1/status`` in milestone 4)."""
        return {
            "connected": self.connected,
            "cage_version": self.cage_version,
            "outputs": self.outputs,
            "applied": self.applied,
            "viewport": list(self.viewport) if self.viewport else None,
            "dpr": self.dpr,
            "mismatch": self.mismatch,
            "chromium": self.chromium,
            "restarts": self.restarts,
            "failed": self.failed,
            "failure": self.failure,
            "sinks": self.sinks,
            "audio": self.audio,
            "audio_mismatch": self.audio_mismatch,
            "audio_failure": self.audio_failure,
        }


class _Refused(Exception):
    pass


class KioskAgents:
    """Serve ``/ws/kiosk-agent``: one agent at a time (CORE-268, CORE-277).

    Args:
        key: The agent key.
        hub: The shells: issues kiosk keys and tells whether a kiosk shell is connected.
        display: Returns the running ``display`` block as JSON data.
        applied: Called with ``(width, height)`` of the applied mode, or ``None``.
        clock: Time source for the shell watchdog.
        hello_timeout: Seconds to wait for ``hello``.
        silence: Seconds without a kiosk shell before ``restart`` (CORE-269).
        audio: Returns the running ``audio`` block as JSON data, or ``None``
            when the configuration has no such block (CORE-296).
    """

    def __init__(
        self,
        key: AgentKey,
        hub: ClientHub,
        display: Callable[[], Mapping[str, Any]],
        applied: Callable[[tuple[int, int] | None], Awaitable[Any]],
        clock: Clock,
        *,
        hello_timeout: float = 10.0,
        silence: float = SHELL_SILENCE_S,
        audio: Callable[[], Mapping[str, Any] | None] = lambda: None,
    ) -> None:
        self.key = key
        self.hub = hub
        self.display = display
        self.on_applied = applied
        self.clock = clock
        self.hello_timeout = hello_timeout
        self.silence = silence
        self.audio = audio
        self._runtime_audio: dict[str, Any] = {}
        self.status = KioskStatus()
        self._ws: WebSocket | None = None
        self._send_lock = asyncio.Lock()
        self._launched_at: float | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=50)

    # --- engine side ------------------------------------------------------------------

    def _kiosk_shell(self) -> bool:
        return any(c["role"] == "kiosk" for c in self.hub.clients())

    async def send(self, kind: str, data: Mapping[str, Any]) -> bool:
        """Send a frame to the connected agent.

        Args:
            kind: ``display``, ``audio``, ``launch`` or ``restart``.
            data: The frame's data.

        Returns:
            False when no agent is connected.
        """
        ws = self._ws
        if ws is None:
            return False
        async with self._send_lock:
            try:
                await ws.send_text(json.dumps({"v": 1, "kind": kind, "data": dict(data)}))
            except (WebSocketDisconnect, RuntimeError):
                return False
        return True

    async def display_changed(self) -> None:
        """Send the new ``display`` block (on hot reload, CORE-143)."""
        await self.send("display", {"display": dict(self.display())})

    def audio_block(self) -> dict[str, Any] | None:
        """Return the ``audio`` frame data: the configured block with run-time values on top.

        Returns:
            ``{sink, volume, mute}``; ``None`` when there is no block and no
            run-time volume or mute, so the kiosk keeps PipeWire's own state.
        """
        block = self.audio()
        if block is None and not self._runtime_audio:
            return None
        data: dict[str, Any] = {"sink": "auto", "volume": None, "mute": False}
        if block is not None:
            data.update({k: block[k] for k in ("sink", "volume", "mute") if k in block})
        data.update(self._runtime_audio)
        return data

    async def audio_changed(self) -> None:
        """Send the new ``audio`` block (on hot reload, CORE-296).

        The configured values are the new truth, so run-time volume and mute
        set before the change are dropped.
        """
        self._runtime_audio.clear()
        await self._send_audio()

    async def set_runtime_audio(
        self, *, volume: int | None = None, mute: bool | None = None
    ) -> bool:
        """Change volume or mute at run time without touching the configuration (CORE-296).

        The admin (milestone 6) and the MQTT bridge (milestone 7) call this. The
        values are never written to the configuration or its history, and the
        configured values apply again when the agent connects anew.

        Args:
            volume: New volume in percent, 0 to 100, or ``None`` to leave it.
            mute: New mute state, or ``None`` to leave it.

        Returns:
            False when no agent is connected.

        Raises:
            ValueError: ``volume`` is outside 0 to 100.
        """
        if volume is not None:
            if isinstance(volume, bool) or not 0 <= volume <= 100:
                raise ValueError("volume must be between 0 and 100")
            self._runtime_audio["volume"] = volume
        if mute is not None:
            self._runtime_audio["mute"] = bool(mute)
        return await self._send_audio()

    async def _send_audio(self) -> bool:
        data = self.audio_block()
        if data is None:
            return False
        return await self.send("audio", data)

    async def _launch(self) -> None:
        self._launched_at = self.clock.boottime()
        await self.send("launch", {"key": self.hub.kiosk_keys.issue()})

    async def watchdog(self) -> None:
        """Ask for a restart when the kiosk shell has been missing for ``silence`` (CORE-269)."""
        while True:
            await self.clock.sleep(min(5.0, self.silence / 4))
            if self._ws is None or self.status.chromium != "started":
                continue
            if self._kiosk_shell():
                self._launched_at = self.clock.boottime()
                continue
            since = self._launched_at if self._launched_at is not None else self.clock.boottime()
            if self.clock.boottime() - since >= self.silence:
                _log.warning("kiosk_shell_silent", seconds=self.silence)
                self._launched_at = self.clock.boottime()
                await self.send("restart", {"reason": "kiosk_shell_silent"})

    # --- the connection ---------------------------------------------------------------

    async def serve(self, ws: WebSocket) -> None:
        """Run one agent connection (CORE-268, CORE-277).

        Args:
            ws: The WebSocket, not yet accepted.
        """
        await ws.accept()
        host = ws.client.host if ws.client else None
        if not is_loopback(host) or not self.key.valid(ws.headers.get("authorization")):
            _log.warning("kiosk_agent_refused", host=host)
            await ws.close(CLOSE_ROLE)
            return
        if self._ws is not None:
            _log.warning("kiosk_agent_replaced")
            with contextlib.suppress(RuntimeError):
                await self._ws.close(1000)
        try:
            hello = await self._hello(ws)
        except _Refused as exc:
            _log.info("kiosk_agent_handshake_refused", reason=str(exc))
            await ws.close(CLOSE_PROTOCOL)
            return
        except WebSocketDisconnect:
            return
        self._ws = ws
        self.status.connected = True
        self.status.cage_version = str(hello.get("cage_version") or "") or None
        self.status.audio_failure = None
        self._runtime_audio.clear()
        _log.info("kiosk_agent_connected", cage_version=self.status.cage_version)
        await self.display_changed()
        await self._send_audio()
        await self._launch()
        try:
            while True:
                text = await ws.receive_text()
                if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                    break
                await self._frame(text)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if self._ws is ws:
                self._ws = None
                self.status.connected = False
                _log.warning("kiosk_agent_disconnected")

    async def _hello(self, ws: WebSocket) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.hello_timeout):
                text = await ws.receive_text()
        except TimeoutError:
            raise _Refused("no hello within the timeout") from None
        except (KeyError, RuntimeError) as exc:
            raise _Refused("not a text frame") from exc
        try:
            frame = json.loads(text)
        except ValueError as exc:
            raise _Refused("hello is not JSON") from exc
        if not isinstance(frame, dict) or frame.get("v") != 1 or frame.get("kind") != "hello":
            raise _Refused("first frame is not hello")
        data = frame.get("data")
        if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
            raise _Refused("unsupported protocol")
        return data

    async def _frame(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get("v") != 1:
            return
        kind, data = frame.get("kind"), frame.get("data")
        if not isinstance(data, dict):
            return
        self.events.append({"kind": kind, "data": data})
        if kind == "outputs":
            await self._outputs(data)
        elif kind == "chromium":
            await self._chromium(data)
        elif kind == "sinks":
            self._sinks(data)
        elif kind == "failure":
            area = data.get("area", "display")
            if area == "display":
                self.status.failure = str(data.get("reason"))
                _log.error("kiosk_display_failed", reason=self.status.failure)
            elif area == "audio":
                self.status.audio_failure = str(data.get("reason"))
                _log.error("kiosk_audio_failed", reason=self.status.audio_failure)

    async def _outputs(self, data: Mapping[str, Any]) -> None:
        """Record outputs and the applied mode; use the applied mode (CORE-144, CORE-145)."""
        outputs = data.get("outputs")
        applied = data.get("applied")
        viewport = data.get("viewport")
        self.status.outputs = list(outputs) if isinstance(outputs, list) else []
        self.status.applied = dict(applied) if isinstance(applied, dict) else None
        if isinstance(viewport, list) and len(viewport) == 2:
            self.status.viewport = (int(viewport[0]), int(viewport[1]))
        dpr = data.get("dpr")
        self.status.dpr = float(dpr) if isinstance(dpr, int | float) else None
        mode: tuple[int, int] | None = None
        if self.status.applied is not None:
            try:
                mode = (int(self.status.applied["width"]), int(self.status.applied["height"]))
            except (KeyError, TypeError, ValueError):
                mode = None
        want = self.display()
        self.status.mismatch = mode is not None and (
            (want.get("width") is not None and (want.get("width"), want.get("height")) != mode)
            or (
                want.get("refresh") is not None
                and self.status.applied is not None
                and round(float(self.status.applied.get("refresh") or 0)) != want.get("refresh")
            )
        )
        if self.status.mismatch:
            _log.warning("kiosk_mode_mismatch", wanted=dict(want), applied=self.status.applied)
        await self.on_applied(mode)

    def _sinks(self, data: Mapping[str, Any]) -> None:
        """Record the outputs and what the agent applied; note a mismatch (CORE-296)."""
        sinks = data.get("sinks")
        applied = data.get("applied")
        self.status.sinks = (
            [dict(s) for s in sinks if isinstance(s, dict)] if isinstance(sinks, list) else []
        )
        self.status.audio = dict(applied) if isinstance(applied, dict) else None
        mismatch = self.status.audio is not None and self.status.audio.get("matched") is False
        if mismatch and not self.status.audio_mismatch:
            _log.warning(
                "kiosk_audio_mismatch", wanted=self.audio_block(), applied=self.status.audio
            )
        self.status.audio_mismatch = mismatch

    async def _chromium(self, data: Mapping[str, Any]) -> None:
        """Chromium's state: a new key before a relaunch, failed after give-up (CORE-269)."""
        state = data.get("state")
        if state not in ("started", "exited", "failed"):
            return
        self.status.chromium = str(state)
        restarts = data.get("restarts")
        if isinstance(restarts, int):
            self.status.restarts = restarts
        if state == "exited":
            await self._launch()
        elif state == "failed":
            self.status.failed = True
            _log.error("kiosk_failed", restarts=self.status.restarts)
        else:
            self.status.failed = False
            self._launched_at = self.clock.boottime()
