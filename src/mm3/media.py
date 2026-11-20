"""``ctx.media``: the media sources of the engine and the full-screen takeover.

A module lists the sources it may play, gets URLs on the engine's origin to
give to its front-end (which plays them only through ``mm3-player`` and
``mm3-youtube``), lists the files of a ``files`` source and starts or stops a
takeover. Sources are configured by the user in the top-level ``media`` key,
never by a module. ``sources()``, ``url()`` and ``files()`` need the
capability ``media.play``, ``takeover()`` and ``stop()`` need
``media.takeover``, and every call needs a ``media`` grant that covers the
source (``media: [names]`` or ``["*"]``); otherwise it raises
``PermissionError`` (Contract section 8).

Implements:
    MOD-100: Module Contract 1.0 rev 16, §4.5 Media.
    MOD-101: Module Contract 1.0 rev 16, §4.5 Media.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

__all__ = [
    "Media",
    "MediaFile",
    "MediaFileKind",
    "MediaKind",
    "MediaSource",
    "MediaState",
    "MediaUrl",
    "MediaUrlKind",
]

MediaKind = Literal["rtsp", "stream", "camera", "files"]
"""The kind of a source (core CORE-398)."""

MediaState = Literal["idle", "running", "error", "unavailable"]
"""The state of a source (core CORE-412)."""

MediaUrlKind = Literal["mjpeg", "hls", "file"]
"""How a URL is delivered (core CORE-400): the value for ``mm3-player`` to expect."""

MediaFileKind = Literal["folder", "video", "audio", "image"]
"""The kind of an entry of a ``files`` source, by its extension (core CORE-404)."""


@dataclass(frozen=True)
class MediaSource:
    """A source the instance may play.

    Attributes:
        name: The source name in the ``media`` key.
        kind: ``rtsp``, ``stream``, ``camera`` or ``files``.
        delivery: ``mjpeg``, ``hls`` or ``file``.
        audio: Whether the source carries sound (never for a camera).
        state: ``idle``, ``running``, ``error`` or ``unavailable`` when it was read.
    """

    name: str
    kind: MediaKind
    delivery: MediaUrlKind
    audio: bool
    state: MediaState


@dataclass(frozen=True)
class MediaUrl:
    """A URL on the engine's origin, valid while the source exists (MOD-100).

    Attributes:
        kind: ``mjpeg``, ``hls`` or ``file``.
        url: The path, such as ``/media/doorbell/stream.mjpg``.
    """

    kind: MediaUrlKind
    url: str


@dataclass(frozen=True)
class MediaFile:
    """An entry of a ``files`` source.

    Attributes:
        name: The file or folder name.
        path: Its path under the source's ``path``, to pass as ``file`` to
            ``url()`` and ``takeover()``, or as ``path`` to ``files()`` for a folder.
        kind: ``folder``, ``video``, ``audio`` or ``image``.
        size: Size in bytes, ``None`` for a folder.
        modified: Modification time (aware, UTC), or ``None`` when unknown.
    """

    name: str
    path: str
    kind: MediaFileKind
    size: int | None
    modified: datetime | None


class Media(Protocol):
    """The media sources of the engine (Contract section 4.5).

    Implements:
        MOD-100: Module Contract 1.0 rev 16, §4.5 Media.
        MOD-101: Module Contract 1.0 rev 16, §4.5 Media.
    """

    def sources(self) -> Sequence[MediaSource]:
        """The granted sources, with their state when read (capability ``media.play``)."""
        ...

    def url(self, source: str, *, file: str | None = None) -> MediaUrl:
        """A URL on the engine's origin for a source, or for one file of a ``files`` source.

        Args:
            source: A granted source.
            file: For a ``files`` source: a file under its ``path``. Without it a
                ``files`` source whose ``path`` is one file gives that file.

        Raises:
            PermissionError: ``media.play`` is not declared, or the grant does
                not cover the source.
            ValueError: An unknown source, a ``file`` on a source that is not of
                kind ``files``, a missing or unsafe ``file``, or a file of
                ``files/`` that does not exist. A file under a share is not
                looked up here (a share may be a network mount and this call
                never blocks); the player reports it as unavailable.
        """
        ...

    async def files(self, source: str, path: str = "") -> Sequence[MediaFile]:
        """The entries of a folder of a ``files`` source, in name order.

        Args:
            source: A granted source of kind ``files`` whose ``path`` is a folder.
            path: A subfolder of the source's ``path``; default the folder itself.

        Raises:
            PermissionError: ``media.play`` is not declared, or the grant does
                not cover the source.
            ValueError: An unknown source, a source that is not a folder of
                files, or a folder that does not exist.
            TransientError: The folder did not answer in 5 s (a hung mount).
        """
        ...

    async def takeover(
        self, source: str, *, file: str | None = None, duration: float | None = None
    ) -> str:
        """Show a source or file full screen, replacing a running takeover.

        It starts as ``MEDIA_TAKEOVER`` does (core CORE-411); the call returns
        the id at once, and a takeover the engine cannot start is logged and
        dropped like a command.

        Args:
            source: A granted source.
            file: For a ``files`` source: a file under its ``path``.
            duration: Seconds; default ``media.takeover.duration``, shortened to
                ``max_duration``.

        Returns:
            The takeover id.

        Raises:
            PermissionError: ``media.takeover`` is not declared, or the grant
                does not cover the source.
            ValueError: An unknown source, a ``duration`` of 0 or less, or a
                missing or unsafe ``file`` (checked as in ``url()``).
        """
        ...

    async def stop(self, takeover_id: str | None = None) -> None:
        """Stop a takeover (``MEDIA_STOP``, core CORE-411).

        Without an id it stops the takeover this instance started last, and never
        one started by another instance, REST or MQTT; a takeover already
        replaced is over.

        Raises:
            PermissionError: ``media.takeover`` is not declared, or the instance
                has no ``media`` grant.
        """
        ...
