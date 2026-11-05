"""Show on mirror: a draft layout on the kiosk shell for a while (CORE-307).

``POST /api/v1/preview/mirror`` sends the admin's draft layout object
(CORE-274) to the kiosk shell only, as a ``control`` ``layout`` frame; LAN
browsers keep the running layout. The draft stays until
``DELETE /api/v1/preview/mirror``, a configuration reload (a save), 60 s
after the last push, or until the admin session that pushed it has no open
``/admin/ws`` channel; then the kiosk gets the running layout again. A push
from another admin replaces the draft shown and becomes its owner. A push
made without a session (a bearer token), or from a session with no open
channel, has no owner and ends only by the other three rules.

Implements:
    CORE-307: Core spec 0.28, §6.6 Live layout preview.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from mm3.core.logging import get_logger

__all__ = ["DRAFT_TTL_S", "MirrorPreview", "NoKioskError"]

DRAFT_TTL_S = 60.0

_log = get_logger("core", "web.mirror")


class NoKioskError(Exception):
    """No kiosk shell is connected (``CORE_API_NO_KIOSK``)."""


class _Shells(Protocol):
    def kiosk_connected(self) -> bool: ...

    def control_kiosk(self, action: str, data: Mapping[str, Any]) -> int: ...


class MirrorPreview:
    """The draft layout shown on the kiosk, if any.

    Args:
        shells: The client hub (kiosk-only control frames).
        running: Returns the running layout object (CORE-274).
        has_session: Whether an admin session has an open ``/admin/ws``.
        ttl: Seconds a draft stays after its last push.

    Implements:
        CORE-307: Core spec 0.28, §6.6 Live layout preview.
    """

    def __init__(
        self,
        shells: _Shells,
        running: Callable[[], Mapping[str, Any]],
        has_session: Callable[[str], bool] = lambda _sid: False,
        *,
        ttl: float = DRAFT_TTL_S,
    ) -> None:
        self.shells = shells
        self.running = running
        self.has_session = has_session
        self.ttl = ttl
        self.active = False
        self.owner: str | None = None
        self._timer: asyncio.TimerHandle | None = None

    def push(self, layout: Mapping[str, Any], session: str | None) -> None:
        """Show ``layout`` on the kiosk and (re)start the 60 s timer.

        Args:
            layout: The draft layout object.
            session: The admin session that pushed it, if any.

        Raises:
            NoKioskError: No kiosk shell is connected.
        """
        if not self.shells.kiosk_connected():
            raise NoKioskError("no kiosk shell is connected")
        self.shells.control_kiosk("layout", {"layout": dict(layout)})
        self.owner = session if session is not None and self.has_session(session) else None
        self.active = True
        self._restart()
        _log.info("mirror_preview_push", owned=self.owner is not None)

    def clear(self, reason: str = "delete") -> bool:
        """End the draft and send the running layout to the kiosk.

        Args:
            reason: Why, for the log: ``delete``, ``expired``, ``session`` or ``reload``.

        Returns:
            Whether a draft was shown.
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self.active:
            return False
        self.active = False
        self.owner = None
        # Without a valid running configuration the kiosk resyncs on the next reload.
        with contextlib.suppress(ValueError):
            self.shells.control_kiosk("layout", {"layout": dict(self.running())})
        _log.info("mirror_preview_end", reason=reason)
        return True

    def session_closed(self, sid: str) -> None:
        """An admin session closed its last ``/admin/ws`` channel."""
        if self.active and self.owner is not None and self.owner == sid:
            self.clear("session")

    async def reloaded(self, *_args: Any) -> None:
        """A configuration was applied (a save): the draft ends (layout listener)."""
        self.clear("reload")

    def _restart(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(self.ttl, self.clear, "expired")
