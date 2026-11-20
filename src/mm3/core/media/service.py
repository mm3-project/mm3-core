"""The media service: supervised ``ffmpeg`` children and their viewers.

A child starts when the first viewer asks for a source and stops 30 s after
the last viewer has gone (CORE-397). An MJPEG viewer is an open
``stream.mjpg`` response; an HLS viewer is a client address that fetched the
playlist or a segment in the last :data:`HLS_LEASE_S` seconds, because HLS
players hold no connection open. At most ``media.limits.processes`` children
run, and at most ``media.limits.transcode`` of them transcode; a request
beyond either limit is refused with 503 and ``Retry-After: 5``.

A child that exits with an error is restarted with backoff while it has
viewers (1, 2, 4 … 60 s); after 5 failures in 10 minutes the source has the
state ``error`` until the next hot reload. A copied HLS source whose input is
not browser-playable gets the state ``error`` with the reason ``codec``
(CORE-401). ``ffmpeg``'s standard error goes to the log at debug level with
URL credentials and the configuration's secrets masked (CORE-397, CORE-403).

The state of every source is sent as ``CORE_MEDIA_CHANGED`` on change, at
most once a second, and is the body of ``GET /api/v1/media`` (CORE-412). The
takeover (CORE-410) comes in a later step of milestone 8: until then
``takeover`` is always null.

A ``camera`` source follows the owner of its device (CORE-389, CORE-402):
owned by ``vision`` it relays the frames of the local feed (CORE-395) with
no ``ffmpeg``; owned by ``media`` it runs an ``ffmpeg`` child on the V4L2
device; owned by ``none`` it is ``unavailable``. A relay is not a child and
does not count against the limits. While the feed cannot be read the source
is ``idle`` with the reason ``feed``, never ``error``: a new viewer is
refused with 503 and open viewers stay while the relay connects again every
1 to 5 s. A hot reload that changes the owner ends the source: the device is
released within 5 s, and after a change to ``media`` the device is opened no
earlier than 5 s after the reload, so that Vision has let it go (CORE-396).

Implements:
    CORE-396: Core spec 0.47, §9.5 Local camera feed.
    CORE-397: Core spec 0.51, §9.7 Media service.
    CORE-400: Core spec 0.51, §9.7 Media service.
    CORE-401: Core spec 0.48, §9.7 Media service.
    CORE-402: Core spec 0.52, §9.7 Media service.
    CORE-403: Core spec 0.48, §9.7 Media service.
    CORE-412: Core spec 0.51, §9.7 Media service.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import os
import re
import shutil
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mm3.core.clock import Clock
from mm3.core.config.devices import ResolvedDevice, ResolvedOwner
from mm3.core.config.media import MediaConfig, MediaSource
from mm3.core.devices import feed_path, resolved
from mm3.core.feed import FeedClosed, FeedError, FeedReader
from mm3.core.logging import get_logger
from mm3.core.media.command import (
    ADTS_HINT,
    HLS_PLAYLIST,
    Delivery,
    Encoder,
    delivery,
    ffmpeg_command,
    mask_url_credentials,
    parse_codecs,
    unplayable,
)
from mm3.core.media.folder import media_root, prepare

if TYPE_CHECKING:
    from mm3.core.config.diff import ReloadPlan
    from mm3.core.config.validate import ConfigResult
    from mm3.core.diag.secrets import Masker

__all__ = [
    "BACKOFF_MAX_S",
    "FAILURES_FOR_ERROR",
    "FAILURE_WINDOW_S",
    "FEED_RETRY_MAX_S",
    "HLS_LEASE_S",
    "IDLE_STOP_S",
    "OWNER_SETTLE_S",
    "RETRY_AFTER_S",
    "MediaRefused",
    "MediaService",
    "MjpegViewer",
    "SourceState",
]

SourceState = Literal["idle", "running", "error", "unavailable"]

IDLE_STOP_S = 30.0
"""A child stops this long after its last viewer left (CORE-397)."""
HLS_LEASE_S = 10.0
"""An HLS client counts as a viewer this long after its last request."""
RETRY_AFTER_S = 5
"""``Retry-After`` of a request beyond the limits (CORE-397)."""
BACKOFF_MAX_S = 60.0
FAILURES_FOR_ERROR = 5
FAILURE_WINDOW_S = 600.0
"""Five failures within ten minutes give the state ``error`` (CORE-397)."""
STABLE_S = 60.0
"""A child that ran this long resets the backoff."""
STOP_GRACE_S = 5.0
"""How long a stopping child gets before it is killed."""
CAMERA_STOP_GRACE_S = 3.0
"""A camera child gets less, so the device is free within 5 s (CORE-402)."""
OWNER_SETTLE_S = 5.0
"""After an owner change to ``media`` the device is opened this long after the reload."""
FEED_CONNECT_S = 2.0
"""How long a viewer's request waits for the feed to connect and send its header."""
FEED_RETRY_MIN_S = 1.0
FEED_RETRY_MAX_S = 5.0
"""A relay that lost its feed connects again every 1 to 5 s (CORE-402)."""
NICE = 10
EVENT_INTERVAL_S = 1.0
"""``CORE_MEDIA_CHANGED`` is sent at most once a second (CORE-412)."""
PLAYLIST_WAIT_S = 10.0
"""How long a first playlist request waits for the child's first segment."""
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_CODEC_LINES = 200
DEFAULT_RENDER_NODE = Path("/dev/dri/renderD128")
KIOSK_GROUP = "mm3-kiosk"
MEDIA_GROUPS = ("render", "video")

HLS_FILE = re.compile(r"^(index\.m3u8|init\.mp4|seg\d{1,9}\.m4s)$")
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

_log = get_logger(subsystem="media")


class MediaRefused(Exception):
    """A delivery request that cannot be served.

    Attributes:
        status: The HTTP status: 404, 403 or 503.
        reason: A short reason, such as ``limit``, ``error`` or ``images``.
        retry_after: Seconds for ``Retry-After``, or ``None``.
    """

    def __init__(self, status: int, reason: str, retry_after: int | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


@dataclass
class _Child:
    proc: asyncio.subprocess.Process
    started: datetime
    started_at: float
    transcode: bool
    encoder: Encoder | None
    out_dir: Path | None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    stopping: bool = False
    fatal: str | None = None
    produced: bool = False


@dataclass
class _Relay:
    """The relay of a camera owned by ``vision``: a reader of the local feed (CORE-402)."""

    reader: FeedReader | None
    task: asyncio.Task[None] | None = None


@dataclass
class _Source:
    name: str
    config: MediaSource
    how: Delivery
    owner: ResolvedOwner | None = None
    device_path: str | None = None
    not_before: float = 0.0
    relay: _Relay | None = None
    state: SourceState = "idle"
    reason: str | None = None
    child: _Child | None = None
    mjpeg_viewers: int = 0
    leases: dict[str, float] = field(default_factory=dict)
    idle_since: float | None = None
    failures: deque[float] = field(default_factory=deque)
    consecutive: int = 0
    restart: asyncio.Task[None] | None = None
    vaapi_failed: bool = False
    adts: bool = False
    frame: bytes | None = None
    seq: int = 0
    generation: int = 0
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    @property
    def viewers(self) -> int:
        return self.mjpeg_viewers + len(self.leases)

    @property
    def active(self) -> bool:
        """A child, a pending restart or a relay holds the source."""
        return self.child is not None or self.restart is not None or self.relay is not None


class MjpegViewer:
    """One open ``stream.mjpg`` response (CORE-400).

    The newest frame wins: a slow viewer skips frames instead of queueing them.
    """

    def __init__(self, service: MediaService, source: _Source) -> None:
        self._service = service
        self._source = source
        self._generation = source.generation
        self._closed = False

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames until the source stops for good or the viewer closes."""
        src = self._source
        last = -1 if src.frame is not None else src.seq
        while not self._closed:
            async with src.cond:
                await src.cond.wait_for(partial(self._moved, last))
            if src.generation != self._generation:
                return
            last = src.seq
            if src.frame is not None:
                yield src.frame

    def _moved(self, last: int) -> bool:
        return self._source.seq != last or self._source.generation != self._generation

    def close(self) -> None:
        """The response ended: the viewer leaves."""
        if not self._closed:
            self._closed = True
            self._service._viewer_left(self._source)


def _lower_priority() -> None:  # pragma: no cover - runs in the child before exec
    os.nice(NICE)


class MediaService:
    """The media service of the engine (``mm3.core.media``, CORE-397).

    Args:
        runtime: The engine's runtime directory; HLS segments live in its
            ``media/<source>/`` (memory, CORE-400).
        state_dir: The engine's state directory; the media folder is its ``media/``.
        clock: The engine clock (timers and timestamps).
        emit: Publishes an engine notification (``Supervisor.emit``).
        ffmpeg: Path of ``ffmpeg``; looked up on ``PATH`` by default.
        render_node: The VA-API render node (CORE-401).
        masker: Returns the masker of the running configuration's secrets.
        playlist_wait: How long a first playlist request waits for the child.
    """

    def __init__(
        self,
        runtime: Path,
        state_dir: Path,
        clock: Clock,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        *,
        ffmpeg: str | None = None,
        render_node: Path = DEFAULT_RENDER_NODE,
        masker: Callable[[], Masker] | None = None,
        playlist_wait: float = PLAYLIST_WAIT_S,
    ) -> None:
        self.runtime = runtime
        self.hls_root = runtime / "media"
        self.root = media_root(state_dir)
        self.clock = clock
        self.emit = emit
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self.render_node = render_node
        self.masker = masker
        self.playlist_wait = playlist_wait
        self.limits_processes = 4
        self.limits_transcode = 1
        self.sources: dict[str, _Source] = {}
        self.images: dict[str, bool] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._pending: asyncio.Task[None] | None = None
        self._last_sent: dict[str, Any] | None = None
        self._last_emit = float("-inf")
        self._closed = False

    # --- configuration ------------------------------------------------------------

    def configure(self, result: ConfigResult) -> None:
        """Take the ``media`` and ``devices`` keys at start (CORE-397, CORE-398).

        Creates ``files/`` and ``shares/`` of the media folder, clears HLS
        folders left by an earlier run and reports the groups of CORE-401.

        Args:
            result: The running configuration.
        """
        media = result.config.media if result.config is not None else MediaConfig()
        self._apply(media, resolved(result), settle=False)
        prepare(self.root)
        with contextlib.suppress(OSError):
            shutil.rmtree(self.hls_root)
        if self.sources:
            self.report_groups()
        if self.sources and self.ffmpeg is None:
            _log.warning("media_ffmpeg_missing", sources=len(self.sources))

    def _apply(
        self, media: MediaConfig, devices: list[ResolvedDevice], *, settle: bool = True
    ) -> list[_Source]:
        """Install a new ``media`` key; return the sources to stop (changed or removed).

        A ``camera`` source is also changed when the resolved owner or the
        path of its device changed (CORE-396, CORE-402).
        """
        self.limits_processes = media.limits.processes
        self.limits_transcode = media.limits.transcode
        self.images = {d.name: d.config.images for d in devices}
        by_name: Mapping[str, ResolvedDevice] = {d.name: d for d in devices}
        now = self.clock.boottime()
        gone: list[_Source] = []
        fresh: dict[str, _Source] = {}
        for name, cfg in media.sources.items():
            owner: ResolvedOwner | None = None
            path: str | None = None
            if cfg.kind == "camera":
                dev = by_name.get(cfg.device or "")
                owner = dev.owner if dev is not None else "none"
                path = dev.config.path if dev is not None else None
            old = self.sources.get(name)
            if (
                old is not None
                and old.config == cfg
                and old.owner == owner
                and old.device_path == path
            ):
                fresh[name] = old
                continue
            if old is not None:
                gone.append(old)
            src = _Source(name, cfg, delivery(cfg), owner=owner, device_path=path)
            if settle and owner == "media" and (old is None or old.owner != "media"):
                src.not_before = now + OWNER_SETTLE_S  # CORE-396: the old owner lets go first
            fresh[name] = src
        gone += [s for n, s in self.sources.items() if n not in fresh]
        self.sources = fresh
        for src in fresh.values():
            src.state, src.reason = self._resting_state(src)
        return gone

    def _resting_state(self, src: _Source) -> tuple[SourceState, str | None]:
        """The state of a source with no child, before any failure."""
        if src.config.kind == "camera":
            if src.owner == "vision":
                return "idle", None  # a relay needs no ffmpeg (CORE-402)
            if src.owner != "media":
                return "unavailable", "owner"
            if self.ffmpeg is None:
                return "unavailable", "ffmpeg"
            return "idle", None
        if src.config.kind in ("rtsp", "stream") and self.ffmpeg is None:
            return "unavailable", "ffmpeg"
        return "idle", None

    async def reloaded(self, new: ConfigResult, plan: ReloadPlan) -> None:
        """Layout listener: apply a hot reload (``media`` is live, CORE-398).

        Every applied reload clears the state ``error`` of CORE-397; sources
        whose configuration changed or that were removed are stopped, and
        their viewers end.

        Args:
            new: The configuration now running.
            plan: What the reload changed.
        """
        if new.config is None:
            return
        gone = self._apply(new.config.media, resolved(new))
        for src in gone:
            await self._stop_source(src)
        for src in self.sources.values():
            src.failures.clear()
            src.consecutive = 0
            if src.state == "error" and not src.active:
                src.state, src.reason = self._resting_state(src)
        self._changed()

    def report_groups(self) -> None:
        """Warn when the engine is not in the groups ``render`` and ``video`` (CORE-401).

        Only on systems with the kiosk (the group ``mm3-kiosk`` exists), and
        only for groups that exist, like the group check of CORE-268.

        Implements:
            CORE-401: Core spec 0.48, §9.7 Media service.
        """
        try:
            grp.getgrnam(KIOSK_GROUP)
        except KeyError:
            return
        mine = set(os.getgroups()) | {os.getgid()}
        for name in MEDIA_GROUPS:
            try:
                gid = grp.getgrnam(name).gr_gid
            except KeyError:
                continue
            if gid not in mine:
                _log.warning("media_group", group=name, reason="not_member")

    async def close(self) -> None:
        """Stop every child (engine shutdown)."""
        self._closed = True
        for task in (self._reaper, self._pending):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for src in list(self.sources.values()):
            await self._stop_source(src)

    # --- state (CORE-412) --------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        """The payload of ``CORE_MEDIA_CHANGED`` (CORE-412).

        Implements:
            CORE-412: Core spec 0.51, §9.7 Media service.
        """
        return {
            "takeover": None,
            "sources": [
                {"name": s.name, "state": s.state, "viewers": s.viewers}
                for s in self.sources.values()
            ],
        }

    def status(self) -> dict[str, Any]:
        """The body of ``GET /api/v1/media`` (CORE-412).

        Returns:
            The payload, each source with its ``reason`` (null unless it is in
            ``error`` or ``unavailable``), plus the running children.

        Implements:
            CORE-412: Core spec 0.51, §9.7 Media service.
        """
        body = self.payload()
        for entry in body["sources"]:
            entry["reason"] = self.sources[entry["name"]].reason
        body["children"] = [
            {
                "source": s.name,
                "pid": s.child.proc.pid,
                "transcode": s.child.transcode,
                "started": s.child.started.isoformat(),
            }
            for s in self.sources.values()
            if s.child is not None
        ]
        return body

    def _changed(self) -> None:
        """Send ``CORE_MEDIA_CHANGED`` when the state changed, at most once a second."""
        if self.emit is None or self._closed:
            return
        payload = self.payload()
        if payload == self._last_sent:
            return
        wait = self._last_emit + EVENT_INTERVAL_S - self.clock.boottime()
        if wait <= 0:
            self._send(payload)
        elif self._pending is None:
            self._pending = asyncio.create_task(self._send_later(wait), name="mm3-media-event")

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.emit is not None  # noqa: S101 - checked by the caller
        self._last_sent = payload
        self._last_emit = self.clock.boottime()
        self.emit("CORE_MEDIA_CHANGED", payload)

    async def _send_later(self, wait: float) -> None:
        try:
            await self.clock.sleep(wait)
        finally:
            self._pending = None
        payload = self.payload()
        if payload != self._last_sent and not self._closed:
            self._send(payload)

    # --- viewers -----------------------------------------------------------------

    def source(self, name: str) -> _Source:
        """Return a configured source.

        Raises:
            MediaRefused: 404 for an unknown source.
        """
        src = self.sources.get(name)
        if src is None:
            raise MediaRefused(404, "unknown")
        return src

    def _admit(self, name: str, how: Delivery) -> _Source:
        """Check that a source can be delivered this way now (CORE-397, CORE-400)."""
        src = self.source(name)
        if src.how != how:
            raise MediaRefused(404, "delivery")
        if src.config.kind == "camera":
            device = src.config.device or ""
            if not self.images.get(device, False):
                raise MediaRefused(403, "images")  # CORE-392
        if src.state in ("error", "unavailable"):
            raise MediaRefused(503, src.state)
        if src.relay is not None and src.relay.reader is None:
            raise MediaRefused(503, "feed", RETRY_AFTER_S)  # CORE-402: reconnecting
        return src

    async def open_mjpeg(self, name: str) -> MjpegViewer:
        """Admit an MJPEG viewer and start the child when it is the first.

        Args:
            name: The source.

        Returns:
            The viewer; close it when the response ends.

        Raises:
            MediaRefused: Unknown source (404), camera images off (403), a
                source in ``error`` or ``unavailable``, or the limits (503).
        """
        src = self._admit(name, "mjpeg")
        src.mjpeg_viewers += 1
        try:
            await self._ensure_child(src)
        except MediaRefused:
            src.mjpeg_viewers -= 1
            raise
        src.idle_since = None
        self._ensure_reaper()
        self._changed()
        return MjpegViewer(self, src)

    async def hls_file(self, name: str, client: str, file: str) -> Path:
        """Admit an HLS request and return the file to send.

        The first request of a source starts its child and waits up to
        ``playlist_wait`` seconds for the playlist. Every request renews the
        client's lease as a viewer.

        Args:
            name: The source.
            client: The client's address, which identifies the viewer.
            file: ``index.m3u8``, ``init.mp4`` or ``seg<n>.m4s``.

        Returns:
            The file in the source's folder under ``/run/mm3/media/``.

        Raises:
            MediaRefused: As :meth:`open_mjpeg`, and 404 for a file that is
                not there (yet).
        """
        if not HLS_FILE.match(file):
            raise MediaRefused(404, "file")
        src = self._admit(name, "hls")
        had = src.leases.get(client)
        src.leases[client] = self.clock.boottime() + HLS_LEASE_S
        try:
            await self._ensure_child(src)
        except MediaRefused:
            if had is None:
                src.leases.pop(client, None)
            raise
        src.idle_since = None
        self._ensure_reaper()
        self._changed()
        out = self.hls_root / name
        if file == HLS_PLAYLIST:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.playlist_wait
            while not (out / file).exists():
                if src.state == "error":
                    raise MediaRefused(503, "error")
                if loop.time() >= deadline:
                    raise MediaRefused(503, "starting", RETRY_AFTER_S)
                await asyncio.sleep(0.1)
            if src.child is not None:
                src.child.produced = True
        target = out / file
        if not target.is_file():
            raise MediaRefused(404, "file")
        return target

    def _viewer_left(self, src: _Source) -> None:
        src.mjpeg_viewers = max(0, src.mjpeg_viewers - 1)
        self._changed()

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="mm3-media-reaper")

    async def _reap(self) -> None:
        """Expire HLS leases and stop children 30 s after their last viewer (CORE-397)."""
        while not self._closed:
            await self.clock.sleep(1.0)
            now = self.clock.boottime()
            busy = False
            for src in list(self.sources.values()):
                before = src.viewers
                for client, until in list(src.leases.items()):
                    if until <= now:
                        del src.leases[client]
                if src.viewers != before:
                    self._changed()
                if not src.active:
                    src.idle_since = None
                    continue
                busy = True
                if src.viewers:
                    src.idle_since = None
                elif src.idle_since is None:
                    src.idle_since = now
                elif now - src.idle_since >= IDLE_STOP_S:
                    _log.info("media_idle_stop", source=src.name)
                    await self._stop_source(src, final=False)
            if not busy:
                self._reaper = None
                return

    # --- children ----------------------------------------------------------------

    def _running(self, *, transcode: bool = False) -> int:
        return sum(
            1
            for s in self.sources.values()
            if s.child is not None and (s.child.transcode or not transcode)
        )

    async def _ensure_child(self, src: _Source) -> None:
        """Start the source's child unless it runs or waits for its restart (CORE-397).

        A camera owned by ``vision`` gets a relay instead (CORE-402); one
        owned by ``media`` waits for the end of an owner change (CORE-396).
        """
        if src.active:
            return
        if src.owner == "vision":
            await self._open_relay(src)
            return
        wait = src.not_before - self.clock.boottime()
        if wait > 0:
            await self.clock.sleep(wait)
            if self.sources.get(src.name) is not src:
                raise MediaRefused(503, "reload", RETRY_AFTER_S)
            if src.active:
                return
        if self._running() >= self.limits_processes:
            raise MediaRefused(503, "limit", RETRY_AFTER_S)
        if src.config.transcode and self._running(transcode=True) >= self.limits_transcode:
            raise MediaRefused(503, "transcode_limit", RETRY_AFTER_S)
        await self._spawn(src)

    def _encoder(self, src: _Source) -> Encoder | None:
        """VA-API on the render node, else ``libx264`` (CORE-401)."""
        if not src.config.transcode or src.how != "hls":
            return None
        node = self.render_node
        if not src.vaapi_failed and node.exists() and os.access(node, os.R_OK | os.W_OK):
            return "vaapi"
        return "x264"

    async def _spawn(self, src: _Source) -> None:
        assert self.ffmpeg is not None  # noqa: S101 - an unavailable source is not admitted
        out_dir: Path | None = None
        if src.how == "hls":
            out_dir = self.hls_root / src.name
            with contextlib.suppress(OSError):
                shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        encoder = self._encoder(src)
        argv = ffmpeg_command(
            self.ffmpeg,
            src.config,
            src.how,
            out_dir,
            encoder=encoder or "x264",
            render_node=self.render_node if encoder == "vaapi" else None,
            adts=src.adts,
            device_path=src.device_path,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE
                if src.how == "mjpeg"
                else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_lower_priority,
            )
        except OSError as exc:
            _log.warning("media_spawn_failed", source=src.name, error=str(exc))
            src.state, src.reason = "unavailable", "ffmpeg"
            self._changed()
            raise MediaRefused(503, "unavailable") from exc
        child = _Child(
            proc=proc,
            started=self.clock.wall(),
            started_at=self.clock.boottime(),
            transcode=src.config.transcode,
            encoder=encoder,
            out_dir=out_dir,
        )
        src.child = child
        src.state, src.reason = "running", None
        _log.info(
            "media_child_started",
            source=src.name,
            pid=proc.pid,
            delivery=src.how,
            encoder=encoder or "copy",
        )
        child.tasks.append(asyncio.create_task(self._read_stderr(src, child)))
        if src.how == "mjpeg":
            child.tasks.append(asyncio.create_task(self._read_frames(src, child)))
        child.tasks.append(asyncio.create_task(self._watch(src, child)))
        self._changed()

    # --- relays of the local feed (CORE-402) ------------------------------------------

    async def _connect_feed(self, src: _Source) -> FeedReader:
        device = src.config.device or ""
        return await FeedReader.connect(
            feed_path(self.runtime, device), device, timeout=FEED_CONNECT_S
        )

    async def _open_relay(self, src: _Source) -> None:
        """Connect to the owner's feed for the first viewer (CORE-402).

        Raises:
            MediaRefused: 503 with ``Retry-After: 5`` when the feed cannot be
                read; the source is then ``idle`` with the reason ``feed``.
        """
        try:
            reader = await self._connect_feed(src)
        except FeedError as exc:
            _log.info("media_feed_unavailable", source=src.name, error=str(exc))
            src.state, src.reason = "idle", "feed"
            self._changed()
            raise MediaRefused(503, "feed", RETRY_AFTER_S) from exc
        if self.sources.get(src.name) is not src or src.active:
            await reader.close()
            return
        relay = _Relay(reader)
        relay.task = asyncio.create_task(
            self._relay(src, relay), name=f"mm3-media-relay-{src.name}"
        )
        src.relay = relay
        src.state, src.reason = "running", None
        _log.info("media_relay_started", source=src.name, device=src.config.device)
        self._changed()

    async def _relay(self, src: _Source, relay: _Relay) -> None:
        """Hand the feed's frames to the viewers, newest first; reconnect when it drops."""
        delay = FEED_RETRY_MIN_S
        while True:
            reader = relay.reader
            if reader is not None:
                try:
                    while True:
                        frame = await reader.frame()
                        delay = FEED_RETRY_MIN_S
                        async with src.cond:
                            src.frame = frame
                            src.seq += 1
                            src.cond.notify_all()
                except FeedError as exc:
                    closed = isinstance(exc, FeedClosed)
                    _log.info("media_feed_lost", source=src.name, closed=closed, error=str(exc))
                await reader.close()
                relay.reader = None
                src.frame = None
                src.state, src.reason = "idle", "feed"
                self._changed()
            if src.viewers == 0:
                src.relay = None
                await self._end(src, "idle", "feed")
                self._changed()
                return
            await self.clock.sleep(delay)
            delay = min(FEED_RETRY_MAX_S, delay * 2)
            if src.viewers == 0:
                continue
            try:
                relay.reader = await self._connect_feed(src)
            except FeedError:
                continue
            src.state, src.reason = "running", None
            _log.info("media_relay_reconnected", source=src.name)
            self._changed()

    async def _close_relay(self, src: _Source) -> None:
        relay = src.relay
        if relay is None:
            return
        src.relay = None
        if relay.task is not None and relay.task is not asyncio.current_task():
            relay.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await relay.task
        if relay.reader is not None:
            await relay.reader.close()
        _log.info("media_relay_stopped", source=src.name)

    def _mask(self, line: str) -> str:
        line = mask_url_credentials(line)
        if self.masker is not None:
            line = self.masker().mask_text(line)
        return line

    async def _read_stderr(self, src: _Source, child: _Child) -> None:
        """Log ``ffmpeg``'s standard error at debug level, masked; check the codecs (CORE-401)."""
        stream = child.proc.stderr
        assert stream is not None  # noqa: S101 - opened with PIPE
        head: list[str] = []
        checked = src.how != "hls" or src.config.transcode
        while True:
            raw = await stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            _log.debug("media_ffmpeg", source=src.name, line=self._mask(line))
            if ADTS_HINT in line and not src.adts and src.how == "hls" and not child.transcode:
                child.fatal = "adts"  # run again with the filter, not a failure
                self._terminate(child)
                continue
            if checked:
                continue
            head.append(line)
            codecs = parse_codecs(head)
            if codecs.complete or len(head) >= MAX_CODEC_LINES:
                checked = True
                if unplayable(codecs, audio=src.config.has_audio):
                    _log.warning(
                        "media_codec",
                        source=src.name,
                        video=codecs.video,
                        audio=codecs.audio,
                    )
                    child.fatal = "codec"
                    self._terminate(child)

    async def _read_frames(self, src: _Source, child: _Child) -> None:
        """Split ``image2pipe`` output into JPEG frames; the newest one wins (CORE-400)."""
        stream = child.proc.stdout
        assert stream is not None  # noqa: S101 - opened with PIPE
        buf = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            buf += chunk
            newest: bytes | None = None
            while True:
                start = buf.find(_SOI)
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(_EOI, start + 2)
                if end < 0:
                    if start:
                        del buf[:start]
                    break
                newest = bytes(buf[start : end + 2])
                del buf[: end + 2]
            if len(buf) > MAX_FRAME_BYTES:
                buf.clear()
            if newest is not None:
                child.produced = True
                async with src.cond:
                    src.frame = newest
                    src.seq += 1
                    src.cond.notify_all()

    def _terminate(self, child: _Child) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.proc.terminate()

    async def _watch(self, src: _Source, child: _Child) -> None:
        """Wait for the child to exit and decide what follows (CORE-397, CORE-401)."""
        code = await child.proc.wait()
        for task in child.tasks:
            if task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, 2.0)
        if child.out_dir is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(child.out_dir)  # CORE-400: nothing stays after the child
        if src.child is child:
            src.child = None
        _log.info("media_child_exited", source=src.name, pid=child.proc.pid, code=code)
        if child.stopping:
            return
        if child.fatal == "adts" and not src.adts:
            src.adts = True
            src.state, src.reason = "idle", None
            if src.viewers:
                try:
                    await self._ensure_child(src)
                except MediaRefused:
                    await self._end(src, "idle", None)
            else:
                await self._end(src, "idle", None)
        elif child.fatal == "codec":
            await self._end(src, "error", "codec")
        elif code == 0:
            await self._end(src, "idle", None)
        else:
            await self._failed(src, child)
        self._changed()

    async def _end(self, src: _Source, state: SourceState, reason: str | None) -> None:
        """The source stops for good: its viewers end."""
        src.state, src.reason = state, reason
        src.frame = None
        src.leases.clear()
        async with src.cond:
            src.generation += 1
            src.cond.notify_all()

    async def _failed(self, src: _Source, child: _Child) -> None:
        now = self.clock.boottime()
        if child.encoder == "vaapi" and not child.produced:
            src.vaapi_failed = True  # CORE-401: fall back to libx264
        if now - child.started_at >= STABLE_S:
            src.consecutive = 0
        src.consecutive += 1
        src.failures.append(now)
        while src.failures and now - src.failures[0] > FAILURE_WINDOW_S:
            src.failures.popleft()
        if len(src.failures) >= FAILURES_FOR_ERROR:
            _log.warning("media_source_error", source=src.name, failures=len(src.failures))
            await self._end(src, "error", "failed")
            return
        if src.viewers == 0:
            await self._end(src, "idle", None)
            return
        delay = min(BACKOFF_MAX_S, 2.0 ** (src.consecutive - 1))
        src.state, src.reason = "idle", "restarting"
        src.restart = asyncio.create_task(self._restart_later(src, delay))

    async def _restart_later(self, src: _Source, delay: float) -> None:
        try:
            await self.clock.sleep(delay)
        finally:
            src.restart = None
        if self._closed or self.sources.get(src.name) is not src or src.child is not None:
            return
        if src.viewers == 0 or src.state == "error":
            await self._end(src, src.state if src.state == "error" else "idle", src.reason)
            self._changed()
            return
        try:
            await self._ensure_child(src)
        except MediaRefused:
            await self._end(src, "idle", None)
            self._changed()

    async def _stop_source(self, src: _Source, *, final: bool = True) -> None:
        """Stop a source's child (idle stop, reload or shutdown); its viewers end."""
        if src.restart is not None:
            src.restart.cancel()
            src.restart = None
        await self._close_relay(src)
        child = src.child
        grace = CAMERA_STOP_GRACE_S if src.config.kind == "camera" else STOP_GRACE_S
        if child is not None:
            child.stopping = True
            self._terminate(child)
            try:
                await asyncio.wait_for(child.proc.wait(), grace)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    child.proc.kill()
                await child.proc.wait()
            watcher = child.tasks[-1]
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        src.idle_since = None
        state, reason = self._resting_state(src)
        if not final and src.state == "error":
            state, reason = src.state, src.reason
        await self._end(src, state, reason)
        self._changed()
