"""Pages and visibility: the current page, rotation, touch and hide reasons.

The engine keeps one current page for all displays. An instance is visible
when no hide reason applies: ``page`` (not on the current page), ``manual``,
``self`` (its element called ``this.hide()``) or ``module:<id>`` (set by a
module with ``layout.control``). When an instance becomes hidden its
backend is suspended; when it becomes visible again it is resumed. Every
change is applied in order by one worker task, emitted on the bus as
``CORE_PAGE_CHANGED`` and pushed to the shells.

Automatic page changes (rotation, timed jumps, idle return) share one planner
task. Pause holds (``PAGE_PAUSE``, REST, Home Assistant) stop all three and
restart their counts from zero when the last hold ends.

Implements:
    CORE-233, CORE-234, CORE-235, CORE-237: Core spec 0.15, §6.9 Pages and visibility.
    CORE-238, CORE-239, CORE-241: Core spec 0.14, §6.9 Pages and visibility.
    CORE-260: Core spec 0.14, §6.9 Pages and visibility.
    CORE-333, CORE-334, CORE-335, CORE-336, CORE-337: Core spec 0.34, §6.9 Pages and visibility.
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
    "HOLDER_MQTT",
    "HOLDER_REST",
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

PageReason = Literal[
    "rotate", "notification", "touch", "rest", "ha", "resume", "reload", "start", "after", "idle"
]
Gesture = Literal["swipe_left", "swipe_right", "tap"]
_NOT_INTERACTION = frozenset({"rotate", "start", "reload", "resume", "after", "idle"})
"""Page changes that do not pause rotation (CORE-234, CORE-337)."""
_AUTOMATIC = frozenset({"rotate", "after", "idle"})
"""Page changes that are not an interaction for the idle count (CORE-335)."""
HOLDER_REST = "rest"
"""Holder name of pause holds set through REST (CORE-336)."""
HOLDER_MQTT = "mqtt"
"""Holder name of the pause hold of the Home Assistant switch (CORE-343)."""
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
    cycle: bool = True
    after: timedelta | None = None
    after_page: str | None = None


class PageService:
    """The current page and every instance's hide reasons.

    Args:
        result: The running configuration.
        supervisor: Instances to suspend and resume, and the bus to emit on.
        clock: Time source for rotation, timed jumps and idle return.

    Implements:
        CORE-233, CORE-234, CORE-237: Core spec 0.14, §6.9 Pages and visibility.
        CORE-238, CORE-239, CORE-241: Core spec 0.14, §6.9 Pages and visibility.
        CORE-333, CORE-334, CORE-335, CORE-336: Core spec 0.34, §6.9 Pages and visibility.
        CORE-343: Core spec 0.34, §9.3 MQTT bridge.
    """

    def __init__(self, result: ConfigResult, supervisor: Supervisor, clock: Clock) -> None:
        self.supervisor = supervisor
        self.clock = clock
        self.shells: ShellControl | None = None
        self.listeners: list[Callable[[], None]] = []
        self._reasons: dict[str, set[str]] = {}
        self._hidden: dict[str, bool] = {}
        self._pause_until = 0.0
        self._holds: dict[str, float | None] = {}
        self._anchor = clock.boottime()
        self._interaction_at = self._anchor
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
            self.return_after = timedelta(0)
            self.transition = "fade"
        else:
            pc = config.pages
            self.default = pc.default or pc.pages[0].id
            self._pages = [
                _Page(
                    p.id,
                    p.resolved_title(locale),
                    p.rotate,
                    p.cycle,
                    p.after.time if p.after is not None else None,
                    (p.after.page or self.default) if p.after is not None else None,
                )
                for p in pc.pages
            ]
            self.rotate = pc.rotate
            self.pause = pc.pause_on_interaction
            self.reset_on_resume = pc.reset_on_resume
            self.return_after = pc.return_after
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

    @property
    def cycle_ids(self) -> list[str]:
        """Ids of the pages in the cycle, in list order (CORE-333)."""
        return [p.id for p in self._pages if p.cycle]

    def is_in_cycle(self, page: str) -> bool:
        """Return True when a page is in the cycle (CORE-333)."""
        return page in self.cycle_ids

    @property
    def paused(self) -> list[str]:
        """Holders of the active pause holds, sorted (CORE-336)."""
        self._expire_holds()
        return sorted(self._holds)

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
        previous = None if reason == "start" else self.page
        self.page = page
        now = self.clock.boottime()
        self._anchor = now
        if reason not in _AUTOMATIC:
            self._interaction_at = now
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
                "previous": previous,
                "reason": reason,
            },
        )
        if self.shells is not None:
            self.shells.page_changed(page, index, len(self._pages), self.transition)
        _log.info("page_changed", page=page, reason=reason)
        self._notify()

    def _step(self, delta: int) -> str | None:
        """Return the next (+1) or previous (-1) page in the cycle (CORE-333).

        From a page out of the cycle the step starts at that page's place in
        the list. Returns ``None`` when no page is in the cycle.
        """
        cycle = self.cycle_ids
        if not cycle:
            return None
        if self.page in cycle:
            return cycle[(cycle.index(self.page) + delta) % len(cycle)]
        ids = self.ids
        here = ids.index(self.page)
        if delta > 0:
            ordered = ids[here + 1 :] + ids[:here]
        else:
            ordered = list(reversed(ids[:here])) + list(reversed(ids[here + 1 :]))
        return next(i for i in ordered if i in cycle)

    async def _go(self, delta: int, reason: PageReason) -> None:
        target = self._step(delta)
        if target is not None:
            await self._set_page(target, reason)

    def set_page(self, page: str, reason: PageReason) -> None:
        """Ask for a page change; applied in order by the worker (CORE-233).

        Args:
            page: Page id; an unknown id is ignored with a warning.
            reason: Why (``CORE_PAGE_CHANGED.reason``).
        """
        self._submit(lambda: self._set_page(page, reason))

    def next(self, reason: PageReason) -> None:
        """Go to the next page in the cycle (``PAGE_NEXT``, swipe left, rotation)."""
        self._submit(lambda: self._go(1, reason))

    def prev(self, reason: PageReason) -> None:
        """Go to the previous page in the cycle (``PAGE_PREV``, swipe right)."""
        self._submit(lambda: self._go(-1, reason))

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
        self._notify()

    def _notify(self) -> None:
        """Tell the listeners (the MQTT bridge) that page or visibility changed (CORE-343)."""
        for listener in self.listeners:
            listener()

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
        """Clear the hide reasons and the pause hold of a stopped module (CORE-241, CORE-336)."""
        if new not in _STOPPED:
            return
        self.resume(instance_id)
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
        self._interaction_at = self.clock.boottime()
        self._anchor = self._interaction_at
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
        elif n.name == "PAGE_PAUSE":
            seconds = n.payload.get("for")
            holder = _holder(n.source)
            self.pause_hold(holder, float(seconds) if isinstance(seconds, int | float) else None)
        elif n.name == "PAGE_RESUME":
            self.resume(_holder(n.source))
        elif n.name == "CORE_RESUME":
            self._restart_counts()
            self._changed.set()
            if self.reset_on_resume:
                self.set_page(self.default, "resume")
        elif n.name == "CORE_SUSPEND":
            self._changed.set()

    # --- pause holds ------------------------------------------------------------------

    def pause_hold(self, holder: str, seconds: float | None = None) -> None:
        """Set or replace the pause hold of a holder (CORE-336).

        While any hold is active rotation, timed jumps and idle return make
        no change and their counts stop; every other page change still works.

        Args:
            holder: The sending instance, ``rest`` or ``mqtt``.
            seconds: Duration (1 to 3600); ``None`` holds until a resume from
                the same holder or until the holding instance stops.
        """
        self._expire_holds()
        until = None if seconds is None else self.clock.boottime() + seconds
        self._holds[holder] = until
        _log.info("pages_pause", holder=holder, seconds=seconds)
        self._changed.set()
        self._notify()

    def resume(self, holder: str) -> None:
        """Clear the hold of a holder; without a hold nothing changes (CORE-336).

        Args:
            holder: The holder whose hold ends.
        """
        self._expire_holds()
        if holder not in self._holds:
            return
        del self._holds[holder]
        _log.info("pages_resume", holder=holder)
        if not self._holds:
            self._restart_counts()
        self._changed.set()
        self._notify()

    def _expire_holds(self) -> bool:
        """Drop holds whose ``for`` has passed; True when any ended (CORE-336)."""
        now = self.clock.boottime()
        ended = [h for h, until in self._holds.items() if until is not None and until <= now]
        for holder in ended:
            del self._holds[holder]
        if ended and not self._holds:
            self._restart_counts()
        if ended:
            self._notify()
        return bool(ended)

    def _restart_counts(self) -> None:
        """Start the timed-jump and idle counts again from zero (CORE-334, CORE-335)."""
        self._anchor = self._interaction_at = self.clock.boottime()

    # --- automatic changes ------------------------------------------------------------

    def _current(self) -> _Page:
        return next(p for p in self._pages if p.id == self.page)

    def _rotation_seconds(self) -> float:
        """Rotation time on the current page; 0 when rotation does not run (CORE-333, CORE-334)."""
        page = self._current()
        if not page.cycle or page.after is not None or len(self.cycle_ids) < 2:
            return 0.0
        rotate = page.rotate if page.rotate is not None else self.rotate
        return rotate.total_seconds()

    def _plan(self) -> tuple[float, PageReason] | None:
        """Return the next automatic change and its delay, or None (CORE-334, CORE-335)."""
        if self.supervisor.suspended or self._holds:
            return None
        now = self.clock.boottime()
        page = self._current()
        if page.after is not None:
            if page.after_page is None or page.after_page == page.id:
                return None
            return max(self._anchor + page.after.total_seconds() - now, 0.0), "after"
        seconds = self._rotation_seconds()
        if seconds > 0:
            return max(seconds, self._pause_until - now), "rotate"
        if self.return_after.total_seconds() > 0 and page.id != self.default:
            return max(self._interaction_at + self.return_after.total_seconds() - now, 0.0), "idle"
        return None

    def _next_expiry(self) -> float | None:
        times = [u for u in self._holds.values() if u is not None]
        return min(times) - self.clock.boottime() if times else None

    async def _rotation(self) -> None:
        """Plan and run rotation, timed jumps and idle return (CORE-234, CORE-334, CORE-335)."""
        while True:
            self._changed.clear()
            self._expire_holds()
            plan = self._plan()
            expiry = self._next_expiry()
            if plan is None and expiry is None:
                await self._changed.wait()
                continue
            delay = plan[0] if plan is not None else None
            if expiry is not None and (delay is None or expiry < delay):
                await self.clock.wait(self._changed, max(expiry, 0.0))
                continue  # a change, or a hold ended: plan again
            if delay is None or plan is None:
                continue
            if await self.clock.wait(self._changed, delay):
                continue  # something changed: plan again
            reason = plan[1]
            if reason == "rotate":
                if self.clock.boottime() < self._pause_until:
                    continue
                self.next("rotate")
            elif reason == "after":
                target = self._current().after_page
                if target is not None:
                    self.set_page(target, "after")
            else:
                self.set_page(self.default, "idle")
            await self.idle()

    # --- reload -----------------------------------------------------------------------

    def reload(self, result: ConfigResult) -> None:
        """Use a reloaded configuration: keep the page if it still exists (CORE-233).

        Args:
            result: The new configuration.
        """

        async def apply() -> None:
            self._configure(result)
            self._restart_counts()
            self._changed.set()
            if self.page in self.ids:
                await self._apply_visibility(PAGE_FADE_MS)
            else:
                await self._set_page(self.default, "reload")

        self._submit(apply)


def _holder(source: str) -> str:
    """Name the holder of a pause hold from a notification's source (CORE-336).

    Args:
        source: ``<module>/<instance>``, ``remote`` (REST), ``mqtt`` or ``core``.

    Returns:
        The instance id, ``rest`` for REST or ``mqtt``.
    """
    if source == "remote":
        return HOLDER_REST
    return source.rpartition("/")[2]
