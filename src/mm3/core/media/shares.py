"""The engine's view of the network shares: their states, read from the installer (CORE-406).

The installer keeps the state of every share (CORE-406, CORE-407). While
``media.shares`` is not empty the engine reads ``GET /v1/media/shares`` every
30 s and gives the media service the shares in ``error``, whose ``files``
sources are then ``unavailable`` with the reason ``share``. When the installer
does not answer, the last answer stands; with no shares configured nothing is
read and no share counts as failed.

Implements:
    CORE-406: Core spec 0.56, §9.7 Media service.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from mm3.core.logging import get_logger
from mm3.core.media.service import MediaService

__all__ = ["READ_EVERY_S", "ShareWatch"]

READ_EVERY_S = 30.0
"""The share states are read every 30 s while shares are configured (CORE-406)."""

_log = get_logger("mm3.core.media.shares")


class ShareWatch:
    """Reads the share states from the installer and hands the failed ones to Media.

    Args:
        media: The media service.
        read: Returns the installer's ``[{name, state, …}]``; raises when it does not answer.
        every: Seconds between reads.
    """

    def __init__(
        self,
        media: MediaService,
        read: Callable[[], Awaitable[list[dict[str, Any]]]],
        *,
        every: float = READ_EVERY_S,
    ) -> None:
        self.media = media
        self._read = read
        self._every = every
        self._task: asyncio.Task[None] | None = None

    async def check(self) -> None:
        """Read once (when shares are configured) and update the media service."""
        if not self.media.share_names:
            self.media.set_failed_shares(frozenset())
            return
        try:
            shares = await self._read()
        except Exception as exc:  # the installer is away: keep the last answer
            _log.debug("media_shares_unread", reason=type(exc).__name__)
            return
        failed = frozenset(
            str(s["name"])
            for s in shares
            if s.get("state") == "error" and str(s.get("name")) in self.media.share_names
        )
        self.media.set_failed_shares(failed)

    async def run(self) -> None:
        """Check every 30 s until cancelled."""
        while True:
            await self.check()
            await asyncio.sleep(self._every)

    def start(self) -> None:
        """Start :meth:`run` as a task."""
        self._task = asyncio.create_task(self.run(), name="mm3-media-shares")

    async def close(self) -> None:
        """Stop the task."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
