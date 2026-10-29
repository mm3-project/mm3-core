"""Pages and visibility: the current page, rotation, touch and hide reasons.

The engine keeps one current page for all displays. An instance is visible
when no hide reason applies: ``page`` (not on the current page), ``manual``,
``self`` (its element called ``this.hide()``) or ``module:<id>`` (set by a
module with ``layout.control``). When an instance becomes hidden its
backend is suspended; when it becomes visible again it is resumed. Every
change is applied in order by one worker task, emitted on the bus as
``CORE_PAGE_CHANGED`` and pushed to the shells.

Implements:
    CORE-233, CORE-234, CORE-235, CORE-237: Core spec 0.14, §6.9 Pages and visibility.
    CORE-238, CORE-239, CORE-241: Core spec 0.14, §6.9 Pages and visibility.
    CORE-260: Core spec 0.14, §6.9 Pages and visibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

from mm3.context import PageInfo
from mm3.core.clock import Clock
from mm3.core.config.validate import ConfigResult
from mm3.core.context import IMPLICIT_PAGE
from mm3.core.logging import get_logger
from mm3.core.supervisor import State, Supervisor
from mm3.messages import Notification

__all__ = [
    "DEFAULT_FADE_MS",
    "PAGE_FADE_MS",
    "Gesture",
    "PageReason",
    "PageService",
    "ShellControl",
]

DEFAULT_FADE_MS = 300
"""Fade of ``this.hide()``, ``this.show()`` and visibility changes (CORE-259)."""
PAGE_FADE_MS = 300
"""Cross-fade of a page change with ``transition: fade`` (CORE-237)."""

PageReason = Literal["rotate", "notification", "touch", "rest", "ha", "resume", "reload", "start"]
Gesture = Literal["swipe_left", "swipe_right", "tap"]
_NOT_INTERACTION = frozenset({"rotate", "start", "reload", "resume"})
"""Page changes that do not pause rotation (CORE-234)."""
_STOPPED = (State.STOPPED, State.FAILED, State.DISABLED)

_log = get_logger(subsystem="pages")


class ShellControl(Protocol):
    """Where page and visibility changes go: the connected shells (CORE-275)."""

    def page_changed(self, page: str, index: int, count: int, transition: str) -> None:
        """Send ``control`` ``page`` to every shell."""
        ...

    def visibility_changed(self, instance_id: str, hidden: bool, fade: int) -> None:
        """Send ``control`` ``hide`` or ``show`` for one instance."""
        ...


@dataclass(frozen=True)
class _Page:
    id: str
    title: str
    rotate: timedelta | None


class PageService:
    """The current page and every instance's hide reasons.

    Args:
        result: The running configuration.
        supervisor: Instances to suspend and resume, and the bus to emit on.
        clock: Time source for rotation.

    Implements:
        CORE-233, CORE-234, CORE-237: Core spec 0.14, §6.9 Pages and visibility.
        CORE-238, CORE-239, CORE-241: Core spec 0.14, §6.9 Pages and visibility.
    """

    def __init__(self, result: ConfigResult, supervisor: Supervisor, clock: Clock) -> None:
        self.supervisor = supervisor
        self.clock = clock
        self.shells: ShellControl | None = None
        self._reasons: dict[str, set[str]] = {}
        self._hidden: dict[str, bool] = {}
        self._pause_until = 0.0
        self._changed = asyncio.Event()
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._configure(result)
        self.page = self.default
        supervisor.state_listeners.append(self._state_changed)

    # --- configuration --------------------------------------------------------------

    def _configure(self, result: ConfigResult) -> None:
        config = result.config
        if config is None:
            raise ValueError("the configuration is not valid")
        self.result = result
        locale = config.engine.locale
        if config.pages is None:
            self._pages = [_Page(IMPLICIT_PAGE, IMPLICIT_PAGE, None)]
            self.default = IMPLICIT_PAGE
            self.rotate = timedelta(0)
            self.pause = timedelta(minutes=2)
            self.reset_on_resume = True
            self.transition = "fade"
        else:
            pc = config.pages
            self._pages = [_Page(p.id, p.resolved_title(locale), p.rotate) for p in pc.pages]
            self.default = pc.default or self._pages[0].id
            self.rotate = pc.rotate
            self.pause = pc.pause_on_interaction
            self.reset_on_resume = pc.reset_on_resume
            self.transition = pc.transition
        self._on: dict[str, frozenset[str] | None] = {}
        for inst in result.instances:
            if not inst.enabled:
                continue
            pages = inst.spec.pages
            self._on[inst.id] = None if pages == "all" else frozenset(pages)
        for iid in list(self._reasons):
            if iid not in self._on:
                del self._reasons[iid]
                self._hidden.pop(iid, None)

    @property
    def ids(self) -> list[str]:
        """Page ids in order."""
        return [p.id for p in self._pages]

    def page_infos(self) -> list[PageInfo]:
        """Return the pages with titles resolved for the engine locale (CORE-237).

        Returns:
            One entry per page, in order.
        """
        return [PageInfo(p.id, p.title) for p in self._pages]

    # --- visibility -------------------------------------------------------------------

    def reasons(self, instance_id: str) -> set[str]:
        """Return the hide reasons of an instance (CORE-238).

        Args:
            instance_id: The instance.

        Returns:
            The reasons; empty when visible.
        """
        out = set(self._reasons.get(instance_id, set()))
        on = self._on.get(instance_id)
        if on is not None and self.page not in on:
            out.add("page")
        return out

    def visible(self, instance_id: str) -> bool:
        """Return True when no hide reason applies (CORE-238)."""
        return not self.reasons(instance_id)

    def visible_ids(self) -> list[str]:
        """Return the visible instances in configuration order."""
        return [iid for iid in self._on if self.visible(iid)]

    async def _apply_visibility(self, fade: int) -> list[str]:
        """Suspend newly hidden and resume newly visible instances (CORE-239).

        Returns:
            Ids whose visibility changed.
        """
        changed: list[str] = []
        for iid in self._on:
            hidden = not self.visible(iid)
            if self._hidden.get(iid, False) == hidden:
                continue
            self._hidden[iid] = hidden
            changed.append(iid)
            with contextlib.suppress(KeyError):
                await self.supervisor.get(iid).set_hidden(hidden)
        return changed

    # --- the worker -------------------------------------------------------------------

    def _submit(self, op: Callable[[], Awaitable[None]]) -> None:
        self._queue.put_nowait(op)

    async def _worker(self) -> None:
        while True:
            op = await self._queue.get()
            try:
                await op()
            except Exception as exc:  # one bad change never stops page handling
                _log.exception("pages_change_failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    async def idle(self) -> None:
        """Wait until every submitted change is applied (tests, shutdown)."""
        await self._queue.join()

    async def start(self) -> None:
        """Apply the start page and start the worker and rotation (reason ``start``)."""
        self._tasks = [
            asyncio.create_task(self._worker(), name="mm3-pages"),
            asyncio.create_task(self._rotation(), name="mm3-pages-rotation"),
        ]
        self._submit(lambda: self._set_page(self.default, "start", force=True))
        await self.idle()

    async def close(self) -> None:
        """Stop the worker and rotation."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        with contextlib.suppress(ValueError):
            self.supervisor.state_listeners.remove(self._state_changed)

    # --- page changes -----------------------------------------------------------------

    async def _set_page(self, page: str, reason: PageReason, *, force: bool = False) -> None:
        """Change the current page, apply visibility, emit and push (CORE-233, CORE-237)."""
        if page not in self.ids:
            _log.warning("page_unknown", page=page, reason=reason)
            return
        if page == self.page and not force:
            return
        self.page = page
        if reason not in _NOT_INTERACTION:
            self.pause_rotation()
        self._changed.set()
        await self._apply_visibility(PAGE_FADE_MS)
        index = self.ids.index(page)
        self.supervisor.emit(
            "CORE_PAGE_CHANGED",
            {
                "page": page,
                "index": index,
                "count": len(self._pages),
                "pages": [{"id": p.id, "title": p.title} for p in self._pages],
                "reason": reason,
            },
        )
        if self.shells is not None:
            self.shells.page_changed(page, index, len(self._pages), self.transition)
        _log.info("page_changed", page=page, reason=reason)

    def _step(self, delta: int) -> str:
        ids = self.ids
        return ids[(ids.index(self.page) + delta) % len(ids)]

    def set_page(self, page: str, reason: PageReason) -> None:
        """Ask for a page change; applied in order by the worker (CORE-233).

        Args:
            page: Page id; an unknown id is ignored with a warning.
            reason: Why (``CORE_PAGE_CHANGED.reason``).
        """
        self._submit(lambda: self._set_page(page, reason))

    def next(self, reason: PageReason) -> None:
        """Go to the next page, cyclically (``PAGE_NEXT``, swipe left, rotation)."""
        self._submit(lambda: self._set_page(self._step(1), reason))

    def prev(self, reason: PageReason) -> None:
        """Go to the previous page, cyclically (``PAGE_PREV``, swipe right)."""
        self._submit(lambda: self._set_page(self._step(-1), reason))

    # --- hide reasons -----------------------------------------------------------------

    async def _set_reason(self, instance_id: str, reason: str, hidden: bool, fade: int) -> None:
        if instance_id not in self._on:
            _log.debug("visibility_unknown_instance", instance=instance_id, reason=reason)
            return
        reasons = self._reasons.setdefault(instance_id, set())
        if hidden:
            reasons.add(reason)
        else:
            reasons.discard(reason)
        for iid in await self._apply_visibility(fade):
            if self.shells is not None:
                self.shells.visibility_changed(iid, not self.visible(iid), fade)

    def set_self(self, instance_id: str, hidden: bool, fade: int = DEFAULT_FADE_MS) -> None:
        """``this.hide()`` / ``this.show()`` from an interactive shell (CORE-238, CORE-276)."""
        self._submit(lambda: self._set_reason(instance_id, "self", hidden, fade))

    def set_manual(self, instance_id: str, hidden: bool) -> None:
        """Hide or show from the admin, REST or Home Assistant (reason ``manual``)."""
        self._submit(lambda: self._set_reason(instance_id, "manual", hidden, DEFAULT_FADE_MS))

    def set_module_reason(self, by: str, target: str, hidden: bool) -> None:
        """``ctx.layout.hide`` / ``show``: the reason ``module:<by>`` (CORE-241).

        Args:
            by: The instance that uses ``ctx.layout``.
            target: The instance to hide or show.
            hidden: Set or clear the reason.
        """
        reason = f"module:{by}"
        self._submit(lambda: self._set_reason(target, reason, hidden, DEFAULT_FADE_MS))

    def _state_changed(self, instance_id: str, old: State, new: State) -> None:
        """Clear the hide reasons a module set when it stops (CORE-241)."""
        if new not in _STOPPED:
            return
        reason = f"module:{instance_id}"
        targets = [iid for iid, rs in self._reasons.items() if reason in rs]
        for target in targets:
            self._submit(
                functools.partial(self._set_reason, target, reason, False, DEFAULT_FADE_MS)
            )

    # --- input ------------------------------------------------------------------------

    def pause_rotation(self) -> None:
        """Pause rotation for ``pause_on_interaction`` (CORE-234)."""
        self._pause_until = self.clock.boottime() + self.pause.total_seconds()
        self._changed.set()

    def touch(self, gesture: Gesture) -> None:
        """A touch from an interactive client (CORE-235, CORE-260, CORE-276).

        Args:
            gesture: ``swipe_left`` (next page), ``swipe_right`` (previous) or
                ``tap`` (only pauses rotation).
        """
        self.pause_rotation()
        if gesture == "swipe_left":
            self.next("touch")
        elif gesture == "swipe_right":
            self.prev("touch")

    # --- bus endpoint -----------------------------------------------------------------

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        """React to page commands and suspend/resume on the bus (CORE-233).

        Args:
            n: The notification.
            exclude_instance: Unused.
        """
        if n.expects_reply or n.is_reply:
            return
        if n.name == "PAGE_NEXT":
            self.next("notification")
        elif n.name == "PAGE_PREV":
            self.prev("notification")
        elif n.name == "PAGE_SET":
            page = n.payload.get("page")
            if isinstance(page, str):
                self.set_page(page, "notification")
        elif n.name == "CORE_RESUME":
            self._changed.set()
            if self.reset_on_resume:
                self.set_page(self.default, "resume")
        elif n.name == "CORE_SUSPEND":
            self._changed.set()

    # --- rotation ---------------------------------------------------------------------

    def _rotate_after(self) -> float:
        page = next(p for p in self._pages if p.id == self.page)
        rotate = page.rotate if page.rotate is not None else self.rotate
        return rotate.total_seconds()

    async def _rotation(self) -> None:
        """Move to the next page after ``rotate`` (CORE-234)."""
        while True:
            self._changed.clear()
            seconds = self._rotate_after()
            if seconds <= 0 or self.supervisor.suspended or len(self._pages) < 2:
                await self._changed.wait()
                continue
            wait = max(seconds, self._pause_until - self.clock.boottime())
            if await self.clock.wait(self._changed, wait):
                continue  # something changed: plan again
            if self.clock.boottime() < self._pause_until:
                continue
            self.next("rotate")
            await self.idle()

    # --- reload -----------------------------------------------------------------------

    def reload(self, result: ConfigResult) -> None:
        """Use a reloaded configuration: keep the page if it still exists (CORE-233).

        Args:
            result: The new configuration.
        """

        async def apply() -> None:
            self._configure(result)
            self._changed.set()
            if self.page in self.ids:
                await self._apply_visibility(PAGE_FADE_MS)
            else:
                await self._set_page(self.default, "reload")

        self._submit(apply)
