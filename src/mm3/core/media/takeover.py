"""The full-screen takeover of the media service (CORE-410, CORE-411, CORE-418).

One takeover runs at a time and a new one replaces it. It is started by
``MEDIA_TAKEOVER`` (from a module with the capability ``media.takeover`` and
a ``media`` grant for the source, from MQTT when imported, or from the
engine), by ``POST /api/v1/media/takeover`` and by the Home Assistant select
``media_takeover``; it is stopped by ``MEDIA_STOP``, ``DELETE
/api/v1/media/takeover`` and the select's ``none``.

It ends:

- when ``duration`` has passed (default ``media.takeover.duration``, at most
  ``max_duration``);
- when the media ends: a source whose child exits with code 0 (CORE-397), or a
  video or audio file after its length, read with ``ffprobe`` within 5 s when
  the takeover starts; ``until`` is the earlier of the two ends. An image
  file and a live source end only by duration;
- on a stop, on a touch on the kiosk (``CORE_INTERACTION``), and on a hot
  reload that removes the source or changes its configuration.

While it runs it holds the page pause of holder ``media`` (CORE-336). The
shells learn it from ``welcome.takeover`` and the ``control`` action
``takeover`` (CORE-418); the preview never shows it.

Implements:
    CORE-410: Core spec 0.54, §9.7 Media service.
    CORE-411: Core spec 0.54, §9.7 Media service.
    CORE-418: Core spec 0.54, §9.7 Media service.
    CORE-407: Core spec 0.57, §9.7 Media service.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from mm3.core.logging import get_logger
from mm3.core.media.command import ffprobe_command, parse_duration
from mm3.core.media.files import FileRefused, run_folder
from mm3.core.media.folder import MEDIA_EXTENSIONS, media_url, open_file, split_media_path

if TYPE_CHECKING:
    from mm3.core.config.diff import ReloadPlan
    from mm3.core.config.validate import ConfigResult
    from mm3.core.media.service import MediaService, _Source
    from mm3.core.supervisor import Supervisor
    from mm3.messages import Notification

__all__ = [
    "CAPABILITY",
    "HOLDER",
    "IMAGE_EXTENSIONS",
    "PROBE_TIMEOUT_S",
    "Takeover",
    "TakeoverRefused",
    "TakeoverService",
]

CAPABILITY = "media.takeover"
"""The capability a module needs for ``MEDIA_TAKEOVER`` and ``MEDIA_STOP`` (CORE-411)."""
HOLDER = "media"
"""The holder of the page pause while a takeover runs (CORE-410, CORE-336)."""
PROBE_TIMEOUT_S = 5.0
"""A file's length must be read within this time, else the file ends by duration."""
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"})
"""Image files end only by duration (CORE-410)."""
LIVE_KINDS = frozenset({"rtsp", "camera"})

_log = get_logger(subsystem="media")


class TakeoverRefused(Exception):
    """A takeover that cannot start (CORE-411): 422 from REST, dropped otherwise.

    Attributes:
        reason: ``unknown`` (no such source), ``duration``, ``file`` (not a
            ``files`` source, missing, or outside the source) or
            ``file_required`` (a ``files`` source on a folder without ``file``).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Shells(Protocol):
    """What the takeover tells the shells (CORE-418)."""

    def control_takeover(self, takeover: dict[str, Any] | None) -> None:
        """Send ``control`` ``takeover`` to kiosk and browser clients."""
        ...


class PauseHolds(Protocol):
    """The page pause holds of CORE-336."""

    def pause_hold(self, holder: str, seconds: float | None = None) -> None:
        """Set the hold of a holder."""
        ...

    def resume(self, holder: str) -> None:
        """Clear the hold of a holder."""
        ...


@dataclass
class Takeover:
    """The running takeover.

    Attributes:
        id: Its id (given, or 12 hex characters).
        source: The source name.
        file: The ``file`` as given, relative to the source's ``path``.
        rel: For a ``files`` source, the file's path under the media folder.
        src: The source object it shows; a reload that replaces it ends the takeover.
        started: Start time (UTC).
        until: End time (UTC): the earlier of the end by duration and the file's end.
        owner: Who started it: an instance id, ``rest``, ``mqtt``, ``ha`` or ``core``.
        by_file: Whether the file's end comes before the end by duration.
    """

    id: str
    source: str
    file: str | None
    rel: str | None
    src: _Source
    started: datetime
    until: datetime
    owner: str
    by_file: bool = False
    timer: asyncio.Task[None] | None = None


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def is_single_file(path: str) -> bool:
    """Whether a ``files`` source's ``path`` names one file (it can start without ``file``).

    Decided by the extension of its last part (CORE-404), so the list of the
    Home Assistant select needs no file system access; a start still checks
    the file itself.
    """
    return PurePosixPath(path).suffix.lower() in MEDIA_EXTENSIONS


def _open_check(root: Any, rel: str) -> int | None:
    one = open_file(root, rel)
    return None if one is None else one.fd


class TakeoverService:
    """One takeover at a time, above every preset on every display (CORE-410).

    Args:
        media: The media service; its sources, folder and state event.
        supervisor: Instances, for the capability and the grant of a module.
        pages: The page service, for the pause hold of holder ``media``.
        shells: The shells, told of every start and end (CORE-418).
        ffprobe: Path of ``ffprobe``; looked up on ``PATH`` by default.
        probe_timeout: How long the length of a file may take to read.

    Implements:
        CORE-410: Core spec 0.54, §9.7 Media service.
        CORE-411: Core spec 0.54, §9.7 Media service.
    """

    def __init__(
        self,
        media: MediaService,
        *,
        supervisor: Supervisor | None = None,
        pages: PauseHolds | None = None,
        shells: Shells | None = None,
        ffprobe: str | None = None,
        probe_timeout: float = PROBE_TIMEOUT_S,
    ) -> None:
        self.media = media
        self.supervisor = supervisor
        self.pages = pages
        self.shells = shells
        self.ffprobe = ffprobe if ffprobe is not None else shutil.which("ffprobe")
        self.probe_timeout = probe_timeout
        self.current: Takeover | None = None
        self.listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last: asyncio.Task[Any] | None = None
        media.takeover_state = self.payload
        media.ended_listeners.append(self._source_ended)

    # --- what others read -------------------------------------------------------------

    def payload(self) -> dict[str, Any] | None:
        """The ``takeover`` of ``CORE_MEDIA_CHANGED`` and ``GET /api/v1/media`` (CORE-412)."""
        t = self.current
        if t is None:
            return None
        out: dict[str, Any] = {"id": t.id, "source": t.source}
        if t.file is not None:
            out["file"] = t.file
        out["started"] = _iso(t.started)
        out["until"] = _iso(t.until)
        return out

    def shell_object(self) -> dict[str, Any] | None:
        """The ``takeover`` of ``welcome`` and ``control`` ``takeover`` (CORE-418).

        Implements:
            CORE-418: Core spec 0.54, §9.7 Media service.
        """
        t = self.current
        if t is None:
            return None
        return {"id": t.id, "src": self.src_url(t), "fit": "contain", "until": _iso(t.until)}

    @staticmethod
    def src_url(t: Takeover) -> str:
        """The path on the engine's origin the shell plays (CORE-400, CORE-418)."""
        return media_url(t.source, t.src.how, t.rel)

    def select_options(self) -> list[str]:
        """The sources the Home Assistant select offers: those that start without a file."""
        out: list[str] = []
        for name, src in self.media.sources.items():
            cfg = src.config
            if cfg.kind != "files" or (cfg.path is not None and is_single_file(cfg.path)):
                out.append(name)
        return out

    @property
    def playing(self) -> str | None:
        """The source of the running takeover, or ``None`` (sensor ``media_playing``)."""
        return self.current.source if self.current is not None else None

    # --- start and stop -----------------------------------------------------------------

    async def start(
        self,
        source: str,
        *,
        file: str | None = None,
        duration: float | None = None,
        takeover_id: str | None = None,
        owner: str = "core",
    ) -> str:
        """Start a takeover, replacing the running one (CORE-410, CORE-411).

        Args:
            source: A source of the ``media`` key.
            file: For a ``files`` source: a file relative to its ``path``.
            duration: Seconds; default ``media.takeover.duration``; shortened
                to ``max_duration``.
            takeover_id: The id; 12 hex characters are chosen when it is missing.
            owner: Who starts it.

        Returns:
            The takeover id.

        Raises:
            TakeoverRefused: An unknown source, a duration of 0 or less, or a
                file that cannot be shown.
        """
        media = self.media
        src = media.sources.get(source)
        if src is None:
            raise TakeoverRefused("unknown")
        default, longest = media.takeover_durations
        seconds = float(default) if duration is None else float(duration)
        if not seconds > 0:
            raise TakeoverRefused("duration")
        seconds = min(seconds, float(longest))
        rel = await self._resolve_file(src, file)
        length = await self._probe(rel) if rel is not None else None
        tid = takeover_id or secrets.token_hex(6)
        async with self._lock:
            if media.sources.get(source) is not src:
                raise TakeoverRefused("unknown")  # a reload replaced it meanwhile
            now = media.clock.wall()
            ends = seconds if length is None else min(seconds, length)
            old = self.current
            if old is not None:
                self._cancel_timer(old)
                _log.info("media_takeover_replaced", id=old.id, source=old.source, by=tid)
            t = Takeover(
                tid,
                source,
                file,
                rel,
                src,
                now,
                now + timedelta(seconds=ends),
                owner,
                by_file=length is not None and length < seconds,
            )
            self.current = t
            t.timer = asyncio.create_task(self._expire(t, ends), name="mm3-media-takeover")
            if old is None and self.pages is not None:
                self.pages.pause_hold(HOLDER)
        _log.info(
            "media_takeover_started", id=tid, source=source, owner=owner, seconds=round(ends, 3)
        )
        self._announce()
        return tid

    async def stop(self, takeover_id: str | None = None, *, owner: str | None = None) -> bool:
        """Stop the running takeover (CORE-411).

        Args:
            takeover_id: Only this takeover; another id changes nothing.
            owner: With an instance id and no ``takeover_id``: only a takeover
                that instance started (Module Contract MOD-101).

        Returns:
            Whether a takeover ended.
        """
        t = self.current
        if t is None:
            return False
        if takeover_id is not None and takeover_id != t.id:
            return False
        if takeover_id is None and owner is not None and t.owner != owner:
            return False
        await self._end(t, "stop")
        return True

    async def _end(self, t: Takeover, reason: str) -> None:
        async with self._lock:
            if self.current is not t:
                return
            self.current = None
            self._cancel_timer(t)
            if self.pages is not None:
                self.pages.resume(HOLDER)
        _log.info("media_takeover_ended", id=t.id, source=t.source, reason=reason)
        self._announce()

    def _cancel_timer(self, t: Takeover) -> None:
        timer, t.timer = t.timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _expire(self, t: Takeover, seconds: float) -> None:
        await self.media.clock.sleep(seconds)
        t.timer = None
        await self._end(t, "media" if t.by_file else "duration")

    def _announce(self) -> None:
        if self.shells is not None:
            self.shells.control_takeover(self.shell_object())
        self.media.state_changed()
        for listener in list(self.listeners):
            listener()

    # --- files (CORE-404) ---------------------------------------------------------------

    async def _resolve_file(self, src: _Source, file: str | None) -> str | None:
        """Return the file's path under the media folder, or ``None`` for a stream."""
        cfg = src.config
        if cfg.kind != "files":
            if file is not None:
                raise TakeoverRefused("file")
            return None
        base = (cfg.path or "").rstrip("/")
        if file is None:
            rel = base
            reason = "file_required"
        else:
            rel = f"{base}/{file.strip('/')}" if file.strip("/") else ""
            reason = "file"
        if not rel or split_media_path(rel) is None:
            raise TakeoverRefused(reason)
        try:
            # The folder threads with their 5 s timeout: a hung mount never takes the
            # engine's default threads (CORE-407).
            fd = await run_folder(_open_check, self.media.root, rel)
        except FileRefused:
            raise TakeoverRefused(reason) from None
        if fd is None:
            raise TakeoverRefused(reason)
        os.close(fd)
        return rel

    async def _probe(self, rel: str) -> float | None:
        """Read a video or audio file's length with ``ffprobe`` (CORE-410)."""
        if PurePosixPath(rel).suffix.lower() in IMAGE_EXTENSIONS or self.ffprobe is None:
            return None
        try:
            one = await run_folder(open_file, self.media.root, rel)
        except FileRefused:
            return None
        if one is None:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *ffprobe_command(self.ffprobe, one.fd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(one.fd,),
            )
        except OSError as exc:
            _log.warning("media_ffprobe_failed", error=type(exc).__name__)
            return None
        finally:
            os.close(one.fd)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.probe_timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            _log.info("media_ffprobe_timeout", file=rel)
            return None
        if proc.returncode != 0:
            return None
        return parse_duration(out.decode("utf-8", "replace"))

    # --- ends ---------------------------------------------------------------------------

    def _source_ended(self, name: str) -> None:
        """A child of a source exited with code 0: a stream that ends ends the takeover."""
        t = self.current
        if t is not None and t.source == name and t.rel is None:
            self._spawn(self._end(t, "media"))

    async def reloaded(self, new: ConfigResult, plan: ReloadPlan) -> None:
        """Layout listener, after the media service: end a takeover whose source went.

        Args:
            new: The configuration now running.
            plan: What the reload changed.
        """
        t = self.current
        if t is not None and self.media.sources.get(t.source) is not t.src:
            await self._end(t, "reload")

    async def close(self) -> None:
        """Engine shutdown: end the takeover without telling anyone."""
        t, self.current = self.current, None
        if t is not None:
            self._cancel_timer(t)
        for task in list(self._tasks):
            task.cancel()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro, name="mm3-media-takeover-end")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _enqueue(self, make: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Run a start or stop command after the one before it, so they keep their order.

        ``make`` builds the coroutine only when its turn comes, so a command that is
        cancelled while it waits (engine shutdown) leaves no coroutine never awaited.
        """
        prev = self._last

        async def run() -> None:
            if prev is not None:
                await asyncio.wait({prev})
            await make()

        task = asyncio.create_task(run(), name="mm3-media-takeover-command")
        self._last = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def submit(
        self,
        source: str,
        *,
        file: str | None = None,
        duration: float | None = None,
        takeover_id: str | None = None,
        owner: str = "core",
    ) -> None:
        """Start a takeover like ``MEDIA_TAKEOVER``: queued in order, a refusal is logged.

        This is what ``ctx.media.takeover()`` calls (Module Contract MOD-101); the
        caller has checked the capability and the grant.

        Args:
            source: A source of the ``media`` key.
            file: For a ``files`` source: a file relative to its ``path``.
            duration: Seconds; see :meth:`start`.
            takeover_id: The id the caller chose.
            owner: The instance that starts it.

        Implements:
            CORE-411: Core spec 0.54, §9.7 Media service.
        """
        self._enqueue(lambda: self._start_dropping(source, file, duration, takeover_id, owner))

    def submit_stop(self, takeover_id: str | None, owner: str | None) -> None:
        """Stop like ``MEDIA_STOP``, queued after the commands before it (MOD-101)."""
        self._enqueue(lambda: self._stop_ignoring(takeover_id, owner))

    async def _stop_ignoring(self, takeover_id: str | None, owner: str | None) -> None:
        await self.stop(takeover_id, owner=owner)

    async def _start_dropping(
        self,
        source: str,
        file: str | None,
        duration: float | None,
        takeover_id: str | None,
        owner: str,
    ) -> None:
        try:
            await self.start(
                source, file=file, duration=duration, takeover_id=takeover_id, owner=owner
            )
        except TakeoverRefused as exc:
            _log.warning("media_takeover_dropped", source=source, origin=owner, reason=exc.reason)

    # --- the bus (CORE-411) -------------------------------------------------------------

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        """Bus endpoint: ``MEDIA_TAKEOVER``, ``MEDIA_STOP`` and ``CORE_INTERACTION``.

        Args:
            n: The notification.
            exclude_instance: Unused.
        """
        if n.expects_reply or n.is_reply:
            return
        if n.name == "CORE_INTERACTION":
            if self.current is not None and n.payload.get("kind") == "touch":
                self._spawn(self._end(self.current, "touch"))
        elif n.name in ("MEDIA_TAKEOVER", "MEDIA_STOP"):
            self._enqueue(lambda: self._command(n))

    async def _command(self, n: Notification) -> None:
        owner, instance = self._origin(n.source)
        p = n.payload
        if n.name == "MEDIA_TAKEOVER":
            source = p.get("source")
            if not isinstance(source, str):
                return
            if instance is not None and not self._allowed(instance, source):
                return
            duration = p.get("duration")
            await self._start_dropping(
                source,
                p.get("file") if isinstance(p.get("file"), str) else None,
                float(duration) if isinstance(duration, int | float) else None,
                p.get("id") if isinstance(p.get("id"), str) else None,
                owner,
            )
            return
        tid = p.get("id") if isinstance(p.get("id"), str) else None
        if instance is not None:
            t = self.current
            if t is None or not self._allowed(instance, t.source):
                return
        await self.stop(tid, owner=instance)

    @staticmethod
    def _origin(source: str) -> tuple[str, str | None]:
        """``(owner, instance id or None)`` of a notification's source."""
        if "/" in source:
            iid = source.rpartition("/")[2]
            return iid, iid
        if source == "remote":
            return "rest", None
        return source, None

    def _allowed(self, instance: str, source: str) -> bool:
        """A module needs ``media.takeover`` and a ``media`` grant for the source (CORE-411)."""
        sup = self.supervisor
        if sup is None:
            return False
        try:
            inst = sup.get(instance)
        except KeyError:
            return False
        entry = sup.registry.get(inst.spec.module)
        caps = entry.cls.capabilities if entry is not None and entry.cls is not None else ()
        if CAPABILITY not in caps:
            _log.warning("media_takeover_denied", instance=instance, reason="capability")
            return False
        if not inst.spec.grants.media_granted(source):
            _log.warning("media_takeover_denied", instance=instance, reason="grant", source=source)
            return False
        return True
