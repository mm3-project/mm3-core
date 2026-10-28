"""File watching for hot reload: the single watchfiles adapter.

The watcher observes the directories that hold the configuration files and
reports a change when one of the watched files, or anything in the widget
template directory, changes. Watching directories rather than files keeps
working when editors replace a file by renaming a new one over it.

Implements:
    CORE-208: Core spec 0.9, §6.3 Hot reload.
    -: Core spec 0.9, §10.9 Dependency management.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from watchfiles import awatch

from mm3.core.logging import get_logger

__all__ = ["ConfigWatcher"]

_log = get_logger(subsystem="config")


class ConfigWatcher:
    """Watch configuration files and call back on every relevant change.

    Args:
        files: Returns the files to watch now (main file, includes, env file).
        directories: Returns directories whose whole content is watched
            (the widget template directory).
        on_change: Called for each batch of relevant file events.

    Implements:
        CORE-208: Core spec 0.9, §6.3 Hot reload.
    """

    def __init__(
        self,
        files: Callable[[], set[Path]],
        directories: Callable[[], set[Path]],
        on_change: Callable[[], None],
    ) -> None:
        self._files = files
        self._directories = directories
        self._on_change = on_change
        self._stop = asyncio.Event()
        self._refresh = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current: tuple[set[Path], set[Path], set[Path]] = (set(), set(), set())

    def start(self) -> None:
        """Start watching in a background task."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="mm3-config-watch")

    def refresh(self) -> None:
        """Re-read the watched set after a reload; restarts watching only if it changed."""
        if self._task is not None and self._targets() != self._current:
            self._refresh.set()

    async def close(self) -> None:
        """Stop watching."""
        self._stop.set()
        self._refresh.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _targets(self) -> tuple[set[Path], set[Path], set[Path]]:
        files = {Path(p).absolute() for p in self._files()}
        trees = {Path(d).absolute() for d in self._directories()}
        roots = {f.parent for f in files} | {t for t in trees if t.is_dir()}
        roots |= {t.parent for t in trees if not t.is_dir()}
        return files, trees, {r for r in roots if r.is_dir()}

    def _relevant(self, path: Path, files: set[Path], trees: set[Path]) -> bool:
        return path in files or any(path == t or path.is_relative_to(t) for t in trees)

    async def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            self._refresh.clear()
            self._current = await asyncio.to_thread(self._targets)
            files, trees, roots = self._current
            if not first:
                # A change may have landed while watching restarted; the
                # debounced reload that follows is a no-op if nothing changed.
                self._on_change()
            first = False
            if not roots:
                await self._refresh.wait()
                continue
            try:
                async for changes in awatch(
                    *roots,
                    stop_event=self._refresh,
                    debounce=50,
                    step=20,
                    recursive=bool(trees),
                    rust_timeout=200,
                    yield_on_timeout=False,
                ):
                    if any(self._relevant(Path(p), files, trees) for _, p in changes):
                        self._on_change()
            except Exception as exc:  # keep watching after a watcher error
                _log.warning("config_watch_error", error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(1)
