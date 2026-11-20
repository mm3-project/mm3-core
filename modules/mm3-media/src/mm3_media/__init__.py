"""Media module: video, image, slideshow, audio and YouTube through ``ctx.media``.

The backend resolves the instance's source to a URL of the engine's origin with
``ctx.media.url()`` and gives it to its element in the state; the element plays it
only through ``mm3-player`` and ``mm3-youtube`` and never builds a URL (MOD-102).
A slideshow reads the image files of a ``files`` source, picks the next one every
``interval`` and sends the new URL; a touch on the cell starts a takeover of what
is shown when ``on_touch`` is ``takeover``.

Implements:
    MED-01, MED-02, MED-03, MED-04, MED-05, MED-06, MED-07, MED-08: Core spec 0.57,
        §9.4 Reference modules.
    CORE-289: Core spec 0.57, §9.4 Reference modules.
    MOD-102: Module Contract 1.0 rev 16, §4.5 Media.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel

from mm3 import MirrorModule, ModuleConfig
from mm3.errors import TransientError
from mm3.media import MediaFile
from mm3.notifications import ConfigChanged

from .config import MediaConfig, MediaKind

__all__ = ["MediaConfig", "MediaKind", "MediaModule"]

LIST_REFRESH = 600.0
"""Seconds between two readings of a slideshow's folder (MED-03)."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def order_key(order: str, file: MediaFile) -> tuple[Any, ...]:
    """The sort key of a slideshow file for ``order`` ``name`` or ``date`` (MED-03).

    Args:
        order: ``name`` (path, ignoring case) or ``date`` (modification time, oldest
            first; a file without one comes first).
        file: The entry of the folder.

    Returns:
        A key; the path is the last part, so equal times keep a fixed order.
    """
    if order == "date":
        return (file.modified or _EPOCH, file.path.casefold(), file.path)
    return (file.path.casefold(), file.path)


class MediaModule(MirrorModule):
    """Shows one video, image, slideshow, audio source or YouTube video (MED-01)."""

    name: ClassVar[str] = "media"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = MediaConfig
    element: ClassVar[str | None] = "mm3-media"
    scripts: ClassVar[list[str]] = ["media.js"]
    styles: ClassVar[list[str]] = ["media.css"]
    # media.takeover only serves on_touch; media.youtube only kind youtube; input.touch
    # only on_touch (MED-01, MED-05). Each needs its grant to do anything.
    capabilities: ClassVar[set[str]] = {
        "media.play",
        "media.takeover",
        "media.youtube",
        "input.touch",
    }
    min_size_px: ClassVar[tuple[int, int] | None] = (160, 90)
    subscribes: ClassVar[set[str] | None] = {"CORE_CONFIG_CHANGED"}
    consumes: ClassVar[dict[str, type[BaseModel]]] = {"CORE_CONFIG_CHANGED": ConfigChanged}  # type: ignore[assignment]

    config: MediaConfig  # type: ignore[assignment]

    async def start(self) -> None:
        """Resolve what to show and, for a slideshow, start its two jobs (MED-02, MED-03)."""
        self._lock = asyncio.Lock()
        self._rng = random.Random()  # noqa: S311  (the order of images, not security)
        self._files: list[MediaFile] = []
        self._queue: list[str] = []
        self._shown: set[str] = set()
        self._current: MediaFile | None = None
        self._seq = 0
        cfg = self.config
        if cfg.kind == "slideshow":
            await self._reload()
            self.ctx.scheduler.every(
                cfg.interval, self._advance, pause_on_suspend=True, name="advance"
            )
            self.ctx.scheduler.every(
                LIST_REFRESH, self._reload, pause_on_suspend=True, name="reload"
            )
        else:
            await self._prepare()

    # --- one source or file (MED-02) ----------------------------------------------------

    async def _prepare(self) -> None:
        """Set the state of every kind but the slideshow."""
        cfg = self.config
        if cfg.kind == "youtube":
            self.ctx.set_state({"kind": "youtube"})
            return
        try:
            url = self.ctx.media.url(cfg.source_name, file=cfg.file)
        except PermissionError:
            self.ctx.log.warning("media_grant_missing", source=cfg.source_name)
            self._error("grant", cfg.source_name)
        except ValueError as exc:
            self.ctx.log.warning("media_unavailable", source=cfg.source_name, reason=str(exc))
            self._error("unavailable")
        else:
            self.ctx.set_state({"kind": cfg.kind, "src": url.url, "delivery": url.kind})

    def _error(self, error: str, source: str | None = None) -> None:
        """Show a message instead of the media (MED-04, MOD-72)."""
        state: dict[str, Any] = {"kind": self.config.kind, "error": error}
        if source is not None:
            state["source"] = source
        self.ctx.set_state(state)

    # --- the slideshow (MED-03) ---------------------------------------------------------

    async def _images(self) -> list[MediaFile]:
        """The image files of the source's folder, with its subfolders when ``recursive``."""
        cfg = self.config
        found: list[MediaFile] = []
        todo = [""]
        seen: set[str] = set()
        while todo:
            folder = todo.pop()
            if folder in seen:
                continue
            seen.add(folder)
            try:
                entries = await self.ctx.media.files(cfg.source_name, folder)
            except ValueError:
                if folder == "":
                    raise
                continue  # a subfolder removed since its parent was read
            for entry in entries:
                if entry.kind == "image":
                    found.append(entry)
                elif entry.kind == "folder" and cfg.recursive:
                    todo.append(entry.path)
        return found

    async def _reload(self) -> None:
        """Read the folder again, keep what is shown and fill an empty slideshow (MED-03)."""
        async with self._lock:
            try:
                images = await self._images()
            except PermissionError:
                self._files = []
                self.ctx.log.warning("media_grant_missing", source=self.config.source_name)
                self._error("grant", self.config.source_name)
                return
            except ValueError as exc:
                self._files = []
                self.ctx.log.warning(
                    "media_unavailable", source=self.config.source_name, reason=str(exc)
                )
                self._error("unavailable")
                return
            except TransientError:
                self.ctx.log.warning("media_folder_slow", source=self.config.source_name)
                if not self.ctx.state:
                    self._error("unavailable")
                return
            self._set_files(images)
            if "src" not in self.ctx.state:
                self._show_next()

    def _set_files(self, images: list[MediaFile]) -> None:
        """Replace the list; a random round goes on with the files it has not shown."""
        order = self.config.order
        self._files = sorted(
            images, key=lambda f: order_key("name" if order == "random" else order, f)
        )
        known = {f.path for f in self._files}
        self._shown &= known
        self._queue = [p for p in self._queue if p in known]
        waiting = set(self._queue)
        added = [p for p in known if p not in self._shown and p not in waiting]
        self._rng.shuffle(added)
        self._queue.extend(added)

    async def _advance(self) -> None:
        """Show the next image (MED-03)."""
        async with self._lock:
            self._show_next()

    def _show_next(self) -> None:
        """Pick the next file and send its URL; skip files that vanished."""
        cfg = self.config
        for _ in range(len(self._files) + 1):
            file = self._next()
            if file is None:
                self.ctx.set_state({"kind": "slideshow", "error": "empty"})
                self._current = None
                return
            try:
                url = self.ctx.media.url(cfg.source_name, file=file.path)
            except ValueError:
                self._files = [f for f in self._files if f.path != file.path]
                continue
            except PermissionError:
                self.ctx.log.warning("media_grant_missing", source=cfg.source_name)
                self._error("grant", cfg.source_name)
                return
            previous = self.ctx.state.get("src") if cfg.transition == "fade" else None
            self._seq += 1
            self._current = file
            self.ctx.set_state(
                {"kind": "slideshow", "src": url.url, "previous": previous, "seq": self._seq}
            )
            return
        self.ctx.set_state({"kind": "slideshow", "error": "empty"})
        self._current = None

    def _next(self) -> MediaFile | None:
        """The next file by ``order``; random shows every file once before any repeats."""
        if not self._files:
            return None
        cfg = self.config
        if cfg.order == "random":
            if not self._queue:
                self._shown.clear()
                self._queue = [f.path for f in self._files]
                self._rng.shuffle(self._queue)
                last = self._current.path if self._current else None
                if len(self._queue) > 1 and self._queue[0] == last:
                    self._queue.append(self._queue.pop(0))  # not the same one twice running
            path = self._queue.pop(0)
            self._shown.add(path)
            return next((f for f in self._files if f.path == path), None)
        if self._current is None:
            return self._files[0]
        after = order_key(cfg.order, self._current)
        return next((f for f in self._files if order_key(cfg.order, f) > after), self._files[0])

    # --- notifications and touch (MED-03, MED-05) ---------------------------------------

    async def on_notification(self, n: Any) -> None:
        """Read the folder again, or look for a source that was missing, on a new configuration."""
        if n.name != "CORE_CONFIG_CHANGED":
            return
        if self.config.kind == "slideshow":
            await self._reload()
        elif "error" in self.ctx.state:
            async with self._lock:
                await self._prepare()

    async def on_client_message(self, msg: Any) -> None:
        """A touch on the cell starts a takeover of the same source or file (MED-05)."""
        if msg.type != "touch" or self.config.on_touch != "takeover":
            return
        cfg = self.config
        file = self._current.path if cfg.kind == "slideshow" and self._current else cfg.file
        try:
            await self.ctx.media.takeover(cfg.source_name, file=file)
        except (PermissionError, ValueError) as exc:
            self.ctx.log.warning("media_takeover_refused", source=cfg.source_name, reason=str(exc))
