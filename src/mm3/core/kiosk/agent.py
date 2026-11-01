"""``mm3 kiosk session``: the kiosk agent that runs inside cage (CORE-143, CORE-268..270).

The session checks the cage version, then connects to ``/ws/kiosk-agent``
with the agent key. It applies every ``display`` block with ``wlr-randr``
and reports the outputs, the modes and what it applied; it starts Chromium
with each kiosk key the engine sends and supervises it: restarts with a
backoff from 1 s to 30 s, and gives up after 5 restarts in 10 minutes.

The agent also owns the kiosk's sound: it applies every ``audio`` block with
``pactl`` (default output, card profile, volume, mute) and reports the
outputs and what it applied in ``sinks`` frames, again whenever an output
appears, disappears or changes availability.

Implements:
    CORE-143, CORE-144, CORE-145: Core spec 0.21, §6.7 Display output.
    CORE-268, CORE-269, CORE-270: Core spec 0.21, §6.7 Display output.
    CORE-295, CORE-296: Core spec 0.21, §6.7 Display output.
    CORE-277: Core spec 0.21, §5.6 WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mm3.core.kiosk.audio import (
    Card,
    Resolution,
    Sink,
    mute_command,
    parse_cards,
    parse_sinks,
    profile_command,
    resolve_sink,
    set_default_command,
    volume_command,
)
from mm3.core.kiosk.chromium import chromium_command
from mm3.core.kiosk.randr import Output, apply_command, choose_mode, parse_outputs, pick_output
from mm3.core.logging import get_logger

__all__ = [
    "BACKOFF_MAX_S",
    "BACKOFF_MIN_S",
    "GIVE_UP_RESTARTS",
    "GIVE_UP_WINDOW_S",
    "AgentLink",
    "KioskSession",
    "Process",
    "Runner",
    "SubprocessRunner",
]

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
"""Chromium restarts wait from 1 s, doubling, up to 30 s (CORE-269)."""
GIVE_UP_RESTARTS = 5
GIVE_UP_WINDOW_S = 600.0
"""After 5 restarts in 10 minutes the agent stops trying (CORE-269)."""
POLL_OUTPUTS_S = 10.0
"""How often outputs are listed again to notice hot-plugs (CORE-144, CORE-296)."""

_log = get_logger(subsystem="kiosk-agent")


class Process(Protocol):
    """A started process."""

    async def wait(self) -> int:
        """Wait for it to exit and return its code."""
        ...

    def terminate(self) -> None:
        """Ask it to exit."""
        ...


class Runner(Protocol):
    """Runs commands: ``wlr-randr`` to completion, Chromium in the background."""

    async def run(self, cmd: Sequence[str]) -> tuple[int, str, str]:
        """Run to completion; return ``(code, stdout, stderr)``."""
        ...

    async def spawn(self, cmd: Sequence[str]) -> Process:
        """Start in the background."""
        ...


class SubprocessRunner:
    """The real :class:`Runner`: asyncio subprocesses, no shell."""

    async def run(self, cmd: Sequence[str]) -> tuple[int, str, str]:
        """Run a command to completion."""
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def spawn(self, cmd: Sequence[str]) -> Process:
        """Start a command in the background."""
        return await asyncio.create_subprocess_exec(*cmd)


class AgentLink(Protocol):
    """The agent's WebSocket to the engine."""

    async def send(self, message: str) -> None:
        """Send a text frame."""
        ...

    async def recv(self) -> str | bytes:
        """Receive a frame."""
        ...

    async def close(self) -> None:
        """Close."""
        ...


Connect = Callable[[str, Mapping[str, str]], Awaitable[AgentLink]]


async def websocket_connect(url: str, headers: Mapping[str, str]) -> AgentLink:
    """Open the agent WebSocket (the single adapter around ``websockets``)."""
    from websockets.asyncio.client import connect

    link: AgentLink = await connect(url, additional_headers=dict(headers), max_size=512 * 1024)
    return link


@dataclass
class _Chromium:
    process: Process | None = None
    relaunch: bool = False


class KioskSession:
    """The kiosk agent.

    Args:
        base_url: The mirror listener, e.g. ``http://127.0.0.1:8080``.
        agent_key: The key from ``/run/mm3/kiosk.key``.
        cage_version: The version ``cage -v`` printed, for ``hello``.
        profile: Chromium's profile directory.
        runner: Runs ``wlr-randr`` and Chromium.
        connect: Opens the WebSocket.
        chromium: The Chromium executable.
        wlr_randr: The wlr-randr executable.
        pactl: The pactl executable.
        sleep: ``asyncio.sleep`` (tests pass a faster one).
        now: A monotonic clock in seconds.
    """

    def __init__(
        self,
        base_url: str,
        agent_key: str,
        cage_version: str,
        profile: Path,
        *,
        runner: Runner | None = None,
        connect: Connect = websocket_connect,
        chromium: str = "chromium",
        wlr_randr: str = "wlr-randr",
        pactl: str = "pactl",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_key = agent_key
        self.cage_version = cage_version
        self.profile = profile
        self.runner = runner or SubprocessRunner()
        self.connect = connect
        self.chromium_binary = chromium
        self.wlr_randr = wlr_randr
        self.pactl = pactl
        self.sleep = sleep
        self.now = now
        self.link: AgentLink | None = None
        self.display: dict[str, Any] = {}
        self.audio: dict[str, Any] | None = None
        self.launch_key: str | None = None
        self.restarts = 0
        self.failed = False
        self._key_event = asyncio.Event()
        self._chromium = _Chromium()
        self._exits: deque[float] = deque()
        self._last_outputs: str | None = None
        self._last_sinks: str | None = None
        self._audio_failure: str | None = None

    @property
    def agent_url(self) -> str:
        """``ws://…/ws/kiosk-agent`` on the mirror listener."""
        return self.base_url.replace("http", "ws", 1) + "/ws/kiosk-agent"

    # --- frames ---------------------------------------------------------------------

    async def send(self, kind: str, data: Mapping[str, Any]) -> None:
        """Send a frame to the engine, if connected."""
        if self.link is None:
            return
        with contextlib.suppress(Exception):
            await self.link.send(json.dumps({"v": 1, "kind": kind, "data": dict(data)}))

    async def _connect(self) -> AgentLink:
        """Connect, waiting with a backoff until the mirror listener answers (CORE-269)."""
        delay = BACKOFF_MIN_S
        while True:
            try:
                link = await self.connect(
                    self.agent_url, {"Authorization": f"Bearer {self.agent_key}"}
                )
            except Exception as exc:  # the engine is not up yet
                _log.info("kiosk_agent_waiting", error=str(exc), retry_s=delay)
                await self.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
                continue
            await link.send(
                json.dumps(
                    {
                        "v": 1,
                        "kind": "hello",
                        "data": {"protocol": 1, "cage_version": self.cage_version},
                    }
                )
            )
            return link

    async def run(self) -> None:
        """Run until cancelled: keep the link up, apply displays, supervise Chromium."""
        supervisor = asyncio.create_task(self.supervise(), name="mm3-kiosk-chromium")
        poller = asyncio.create_task(self._poll_outputs(), name="mm3-kiosk-outputs")
        try:
            while True:
                self.link = await self._connect()
                _log.info("kiosk_agent_connected")
                self._last_sinks = None
                try:
                    await self.report_sinks()
                    while True:
                        raw = await self.link.recv()
                        await self.handle(raw if isinstance(raw, str) else raw.decode())
                except Exception as exc:  # link lost: reconnect
                    _log.warning("kiosk_agent_link_lost", error=str(exc))
                finally:
                    link, self.link = self.link, None
                    if link is not None:  # leave no half-open link behind, also when cancelled
                        with contextlib.suppress(Exception):
                            await link.close()
                await self.sleep(BACKOFF_MIN_S)
        finally:
            supervisor.cancel()
            poller.cancel()
            proc = self._chromium.process
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()

    async def handle(self, text: str) -> None:
        """Handle one frame from the engine (CORE-277)."""
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        kind, data = frame.get("kind"), frame.get("data")
        if not isinstance(data, dict):
            return
        if kind == "display":
            display = data.get("display")
            self.display = dict(display) if isinstance(display, dict) else {}
            await self.apply_display()
        elif kind == "audio":
            self.audio = dict(data)
            await self.apply_audio()
        elif kind == "launch" and isinstance(data.get("key"), str):
            self.launch_key = data["key"]
            proc = self._chromium.process
            if proc is not None:
                # A new key invalidates the running shell's key: start again with it.
                self._chromium.relaunch = True
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            self._key_event.set()
        elif kind == "restart":
            _log.warning("kiosk_restart_requested", reason=data.get("reason"))
            proc = self._chromium.process
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()

    # --- display ----------------------------------------------------------------------

    async def _list(self) -> list[Output]:
        code, out, err = await self.runner.run([self.wlr_randr, "--json"])
        if code != 0:
            raise RuntimeError(err.strip() or f"wlr-randr exited with {code}")
        return parse_outputs(out)

    async def apply_display(self) -> None:
        """Apply the ``display`` block and report the result (CORE-143..145)."""
        d = self.display
        try:
            outputs = await self._list()
        except (RuntimeError, ValueError, OSError) as exc:
            await self.send("failure", {"reason": f"cannot list outputs: {exc}"})
            return
        output = pick_output(outputs, d.get("output"))
        if output is None:
            await self.send("failure", {"reason": f"output {d.get('output')} not found"})
            await self.report(outputs, None)
            return
        mode = choose_mode(output, d.get("width"), d.get("height"), d.get("refresh"))
        if mode is None:
            await self.send("failure", {"reason": f"output {output.name} lists no mode"})
            await self.report(outputs, None)
            return
        rotation = int(d.get("rotation") or 0)
        scale = float(d.get("scale") or 1.0)
        code, _, err = await self.runner.run(
            apply_command(output, mode, rotation, scale, self.wlr_randr)
        )
        if code != 0:
            await self.send("failure", {"reason": err.strip() or f"wlr-randr exited with {code}"})
        with contextlib.suppress(RuntimeError, ValueError, OSError):
            outputs = await self._list()
        current = next((o for o in outputs if o.name == output.name), output)
        applied_mode = current.current or mode
        applied = {
            "output": output.name,
            "width": applied_mode.width,
            "height": applied_mode.height,
            "refresh": round(applied_mode.refresh, 3),
            "rotation": rotation,
            "scale": scale,
        }
        await self.report(outputs, applied)

    async def report(self, outputs: Sequence[Output], applied: Mapping[str, Any] | None) -> None:
        """Send ``outputs`` (CORE-144)."""
        viewport = None
        if applied is not None:
            w, h = applied["width"], applied["height"]
            if applied["rotation"] in (90, 270):
                w, h = h, w
            viewport = [math.floor(w / applied["scale"]), math.floor(h / applied["scale"])]
        data = {
            "outputs": [o.as_dict() for o in outputs],
            "applied": dict(applied) if applied is not None else None,
            "viewport": viewport,
            "dpr": applied["scale"] if applied is not None else None,
        }
        self._last_outputs = json.dumps(data["outputs"], sort_keys=True)
        await self.send("outputs", data)

    async def _poll_outputs(self) -> None:
        """Notice hot-plugs: apply the display and the audio block again when they change."""
        while True:
            await self.sleep(POLL_OUTPUTS_S)
            if self.link is None:
                continue
            await self._poll_display()
            await self._poll_audio()

    async def _poll_display(self) -> None:
        """Apply the display again when the outputs change, e.g. a monitor is plugged in."""
        if not self.display and self._last_outputs is None:
            return
        try:
            outputs = await self._list()
        except (RuntimeError, ValueError, OSError):
            return
        listed = json.dumps([o.as_dict() for o in outputs], sort_keys=True)
        if listed != self._last_outputs:
            await self.apply_display()

    # --- audio ------------------------------------------------------------------------

    async def _pactl_json(self, what: str) -> str:
        code, out, err = await self.runner.run([self.pactl, "--format=json", "list", what])
        if code != 0:
            raise RuntimeError(err.strip() or f"pactl exited with {code}")
        return out

    async def _list_audio(self) -> tuple[list[Sink], list[Card]]:
        """List cards and outputs; raises when the PipeWire server cannot be reached (CORE-295)."""
        cards = parse_cards(await self._pactl_json("cards"))
        sinks = parse_sinks(await self._pactl_json("sinks"), cards)
        return sinks, cards

    async def _default_sink(self) -> str | None:
        """Return the name of the default output, or ``None``."""
        code, out, _ = await self.runner.run([self.pactl, "get-default-sink"])
        name = out.strip()
        return name if code == 0 and name else None

    async def _audio_failed(self, reason: str) -> None:
        """Send a ``failure`` with area ``audio``, once per distinct reason (CORE-277)."""
        if reason != self._audio_failure:
            self._audio_failure = reason
            await self.send("failure", {"area": "audio", "reason": reason})

    async def _pactl_run(self, cmd: Sequence[str]) -> bool:
        code, _, err = await self.runner.run(cmd)
        if code != 0:
            await self._audio_failed(err.strip() or f"{cmd[1]} exited with {code}")
        return code == 0

    async def _send_sinks(
        self,
        sinks: Sequence[Sink],
        in_use: Sink | None,
        matched: bool,
    ) -> None:
        applied = {
            "sink": in_use.name if in_use is not None else None,
            "volume": in_use.volume if in_use is not None else None,
            "mute": in_use.mute if in_use is not None else False,
            "matched": matched,
        }
        self._last_sinks = json.dumps([(s.name, s.available) for s in sinks], sort_keys=True)
        await self.send("sinks", {"sinks": [s.as_dict() for s in sinks], "applied": applied})

    async def _unreachable(self, exc: Exception) -> None:
        """PipeWire cannot be reached: empty list and a failure; the kiosk keeps running."""
        await self._audio_failed(f"cannot reach the PipeWire server: {exc}")
        wants = (self.audio or {}).get("sink", "auto")
        await self._send_sinks([], None, matched=wants == "auto")

    async def report_sinks(self) -> None:
        """Report the outputs without changing anything (after ``hello``, CORE-277)."""
        try:
            sinks, _ = await self._list_audio()
        except (RuntimeError, ValueError, OSError) as exc:
            await self._unreachable(exc)
            return
        self._audio_failure = None
        default = await self._default_sink()
        in_use = next((s for s in sinks if s.name == default), None)
        await self._send_sinks(sinks, in_use, matched=True)

    async def apply_audio(self) -> None:
        """Apply the ``audio`` block and report the result (CORE-295, CORE-296).

        The output is chosen as :func:`~mm3.core.kiosk.audio.resolve_sink`
        says; a card profile that has to change first is switched with
        ``pactl set-card-profile``. A missing or unavailable output leaves the
        current default as it is and is reported with ``matched: false``.
        """
        block = self.audio or {}
        want = str(block.get("sink") or "auto")
        volume = block.get("volume")
        mute = bool(block.get("mute", False))
        try:
            sinks, cards = await self._list_audio()
            resolution = resolve_sink(want, sinks, cards)
            if resolution.switch is not None:
                card, profile = resolution.switch
                if await self._pactl_run(profile_command(self.pactl, card, profile)):
                    sinks, cards = await self._list_audio()
                    resolution = resolve_sink(want, sinks, cards)
                else:
                    resolution = Resolution(matched=False)
        except (RuntimeError, ValueError, OSError) as exc:
            await self._unreachable(exc)
            return
        self._audio_failure = None
        target = resolution.sink
        if target is None and want == "auto":
            default = await self._default_sink()
            target = next((s for s in sinks if s.name == default), None)
        if target is not None:
            if resolution.sink is not None:
                await self._pactl_run(set_default_command(self.pactl, target.name))
            if isinstance(volume, int) and not isinstance(volume, bool):
                await self._pactl_run(volume_command(self.pactl, target.name, volume))
            await self._pactl_run(mute_command(self.pactl, target.name, mute))
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                sinks, _ = await self._list_audio()
            target = next((s for s in sinks if s.name == target.name), target)
        else:
            default = await self._default_sink()
            target = next((s for s in sinks if s.name == default), None)
        await self._send_sinks(sinks, target, resolution.matched)

    async def _poll_audio(self) -> None:
        """Report again when an output appears, disappears or changes availability."""
        try:
            sinks, _ = await self._list_audio()
        except (RuntimeError, ValueError, OSError) as exc:
            if self._last_sinks != "[]":
                await self._unreachable(exc)
            return
        listed = json.dumps([(s.name, s.available) for s in sinks], sort_keys=True)
        if listed == self._last_sinks:
            return
        if self.audio is not None:
            await self.apply_audio()
        else:
            await self.report_sinks()

    # --- Chromium ---------------------------------------------------------------------

    def start_url(self, key: str) -> str:
        """The shell URL with the kiosk key in the fragment (CORE-268)."""
        return f"{self.base_url}/#kiosk={key}"

    def command(self, key: str) -> list[str]:
        """The Chromium command for a key (CORE-270)."""
        return chromium_command(self.start_url(key), self.profile, self.chromium_binary)

    async def supervise(self) -> None:
        """Start Chromium with each key; restart it with a backoff; give up (CORE-269)."""
        delay = BACKOFF_MIN_S
        while True:
            await self._key_event.wait()
            self._key_event.clear()
            key = self.launch_key
            if key is None:
                continue
            self.profile.mkdir(parents=True, exist_ok=True)
            started = self.now()
            try:
                proc = await self.runner.spawn(self.command(key))
            except OSError as exc:
                await self.send("failure", {"reason": f"cannot start Chromium: {exc}"})
                proc = None
            if proc is not None:
                self._chromium = _Chromium(proc)
                await self.send("chromium", {"state": "started", "restarts": self.restarts})
                code = await proc.wait()
                _log.warning("chromium_exited", code=code)
                relaunch = self._chromium.relaunch
                self._chromium = _Chromium()
                if relaunch:
                    self._key_event.set()  # a new key arrived: start with it, no backoff
                    continue
                if self.now() - started > 60:
                    delay = BACKOFF_MIN_S
            end = self.now()
            self._exits.append(end)
            while self._exits and end - self._exits[0] > GIVE_UP_WINDOW_S:
                self._exits.popleft()
            if len(self._exits) > GIVE_UP_RESTARTS:
                self.failed = True
                _log.error("chromium_given_up", restarts=self.restarts)
                await self.send("chromium", {"state": "failed", "restarts": self.restarts})
                return
            await self.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
            self.restarts += 1
            await self.send("chromium", {"state": "exited", "restarts": self.restarts})
