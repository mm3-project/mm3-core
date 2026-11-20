"""``ctx.media`` of one instance: the capabilities, the grants and the calls (Contract §4.5).

:class:`InstanceMedia` checks what a module may do and turns its calls into
the engine's: the sources, the URLs of CORE-400, the folder listing of
CORE-405 and the takeover of CORE-411. What lies behind it is a
:class:`MediaBackend`: :class:`EngineMediaBackend` over the media service and
the takeover service in the engine, and the fake of the test kit under
``ModuleHarness``.

Implements:
    MOD-100: Module Contract 1.0 rev 16, §4.5 Media.
    MOD-101: Module Contract 1.0 rev 16, §4.5 Media.
    CORE-400: Core spec 0.57, §9.7 Media service.
    CORE-404: Core spec 0.57, §9.7 Media service.
    CORE-411: Core spec 0.57, §9.7 Media service.
"""

from __future__ import annotations

import math
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from mm3.core.media.files import FileRefused, entry_kind, list_folder, run_folder
from mm3.core.media.folder import MEDIA_EXTENSIONS, media_url, open_file, split_media_path
from mm3.errors import TransientError
from mm3.media import MediaFile, MediaFileKind, MediaKind, MediaSource, MediaState, MediaUrl

if TYPE_CHECKING:
    from mm3.core.media.service import MediaService
    from mm3.core.media.takeover import TakeoverService
    from mm3.core.services.grants import GrantSet

__all__ = [
    "PLAY",
    "TAKEOVER",
    "EngineMediaBackend",
    "InstanceMedia",
    "MediaBackend",
    "NoMedia",
    "SourceInfo",
]

PLAY = "media.play"
"""The capability for ``sources()``, ``url()`` and ``files()`` (Contract §8)."""
TAKEOVER = "media.takeover"
"""The capability for ``takeover()`` and ``stop()`` (Contract §8)."""


@dataclass(frozen=True)
class SourceInfo:
    """What the engine knows of a configured source.

    Attributes:
        name: The source name.
        kind: ``rtsp``, ``stream``, ``camera`` or ``files``.
        delivery: ``mjpeg``, ``hls`` or ``file``.
        audio: Whether the source carries sound.
        state: ``idle``, ``running``, ``error`` or ``unavailable``.
        path: For a ``files`` source: its ``path`` under the media folder.
    """

    name: str
    kind: MediaKind
    delivery: Any
    audio: bool
    state: MediaState
    path: str | None = None


class MediaBackend(Protocol):
    """The media of the engine behind ``ctx.media``: the engine's or the test kit's."""

    def sources(self) -> Mapping[str, SourceInfo]:
        """Every configured source by name."""
        ...

    def file_known(self, rel: str) -> bool | None:
        """Whether a file exists, or ``None`` when it is not looked up without blocking."""
        ...

    async def list_folder(self, rel: str) -> list[dict[str, Any]]:
        """A folder as ``{name, kind, size, modified}``.

        Raises:
            ValueError: It is not a folder.
            TransientError: It did not answer in time.
        """
        ...

    async def takeover(
        self,
        owner: str,
        source: str,
        *,
        file: str | None,
        duration: float | None,
        takeover_id: str,
    ) -> None:
        """Start a takeover for an instance, as ``MEDIA_TAKEOVER`` does."""
        ...

    async def stop(
        self, owner: str, takeover_id: str | None, granted: Callable[[str], bool]
    ) -> None:
        """Stop a takeover for an instance, as ``MEDIA_STOP`` does."""
        ...


class NoMedia:
    """The backend of a context without a media service: no source exists."""

    def sources(self) -> Mapping[str, SourceInfo]:
        """No source."""
        return {}

    def file_known(self, rel: str) -> bool | None:
        """No file."""
        return False

    async def list_folder(self, rel: str) -> list[dict[str, Any]]:
        """No folder."""
        raise ValueError("no media service")

    async def takeover(
        self,
        owner: str,
        source: str,
        *,
        file: str | None,
        duration: float | None,
        takeover_id: str,
    ) -> None:
        """Nothing starts."""

    async def stop(
        self, owner: str, takeover_id: str | None, granted: Callable[[str], bool]
    ) -> None:
        """Nothing stops."""


class EngineMediaBackend:
    """The backend over the engine's media service and takeover service.

    Args:
        media: The media service: sources, states and the media folder.
        takeover: The takeover service, or ``None`` when there is none.
    """

    def __init__(self, media: MediaService, takeover: TakeoverService | None = None) -> None:
        self._media = media
        self._takeover = takeover

    def sources(self) -> Mapping[str, SourceInfo]:
        """Every configured source by name (CORE-412 states)."""
        return {
            name: SourceInfo(
                name=name,
                kind=src.config.kind,
                delivery=src.how,
                audio=src.config.has_audio and src.config.kind != "camera",
                state=src.state,
                path=src.config.path,
            )
            for name, src in self._media.sources.items()
        }

    def file_known(self, rel: str) -> bool | None:
        """Look a file of ``files/`` up; a path under a share is not (it may be a mount)."""
        if not rel.startswith("files/"):
            return None
        try:
            one = open_file(self._media.root, rel)
        except OSError:
            return None
        if one is None:
            return False
        os.close(one.fd)
        return True

    async def list_folder(self, rel: str) -> list[dict[str, Any]]:
        """A folder on the media folder threads, with their 5 s timeout (CORE-405, CORE-407)."""
        try:
            return await run_folder(list_folder, self._media.root, rel)
        except FileRefused as exc:
            if exc.reason == "unavailable":
                raise TransientError(f"the folder {rel!r} did not answer in 5 s") from None
            raise ValueError(f"no such folder: {rel}") from None

    async def takeover(
        self,
        owner: str,
        source: str,
        *,
        file: str | None,
        duration: float | None,
        takeover_id: str,
    ) -> None:
        """Queue the takeover like ``MEDIA_TAKEOVER`` (CORE-411)."""
        if self._takeover is None:
            return
        self._takeover.submit(
            source, file=file, duration=duration, takeover_id=takeover_id, owner=owner
        )

    async def stop(
        self, owner: str, takeover_id: str | None, granted: Callable[[str], bool]
    ) -> None:
        """Queue the stop like ``MEDIA_STOP``.

        An id of a takeover another instance started needs the grant of its source.
        """
        service = self._takeover
        if service is None:
            return
        current = service.current
        if (
            takeover_id is not None
            and current is not None
            and current.id == takeover_id
            and current.owner != owner
            and not granted(current.source)
        ):
            raise PermissionError(f"the media grant does not cover {current.source!r}")
        service.submit_stop(takeover_id, owner)


class InstanceMedia:
    """``ctx.media`` (Contract §4.5, MOD-100, MOD-101).

    Args:
        instance_id: The instance.
        backend: The media behind it.
        capabilities: The module's declared capabilities.
        grants: The instance's grants; ``media`` names the sources.
        guard: Raises when the context is closed.
        on_denied: Called with a capability used but not declared (MOD-70).
    """

    def __init__(
        self,
        instance_id: str,
        backend: MediaBackend,
        capabilities: frozenset[str],
        grants: GrantSet,
        guard: Callable[[], None],
        on_denied: Callable[[str], None] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._backend = backend
        self._capabilities = capabilities
        self._grants = grants
        self._guard = guard
        self._on_denied = on_denied

    def declared(self) -> bool:
        """Whether the module declares ``media.play`` or ``media.takeover``."""
        return PLAY in self._capabilities or TAKEOVER in self._capabilities

    def _need(self, capability: str) -> None:
        self._guard()
        if capability not in self._capabilities:
            if self._on_denied is not None:
                self._on_denied(capability)
            raise PermissionError(f"capability {capability} is not declared")

    def _grant(self, source: str) -> SourceInfo:
        if not self._grants.media_granted(source):
            raise PermissionError(f"the media grant does not cover {source!r}")
        info = self._backend.sources().get(source)
        if info is None:
            raise ValueError(f"unknown media source {source!r}")
        return info

    def sources(self) -> Sequence[MediaSource]:
        """The granted sources (MOD-100)."""
        self._need(PLAY)
        return [
            MediaSource(i.name, i.kind, i.delivery, i.audio, i.state)
            for i in self._backend.sources().values()
            if self._grants.media_granted(i.name)
        ]

    def _rel(self, info: SourceInfo, file: str | None) -> str | None:
        """The path under the media folder of a file, checked; ``None`` for a stream."""
        if info.kind != "files":
            if file is not None:
                raise ValueError(f"{info.name!r} is not a files source: file is not allowed")
            return None
        base = (info.path or "").rstrip("/")
        single = PurePosixPath(base).suffix.lower() in MEDIA_EXTENSIONS
        if file is None:
            if not single:
                raise ValueError(f"{info.name!r} is a folder: a file is required")
            rel = base
        else:
            sub = file.strip("/")
            if single or not sub:
                raise ValueError(f"{file!r} is not a file of {info.name!r}")
            rel = f"{base}/{sub}"
        if split_media_path(rel) is None:
            raise ValueError(f"{file!r} is not a safe path under {info.name!r}")
        if entry_kind(rel.rpartition("/")[2]) is None:
            raise ValueError(f"{rel!r} has an extension the media folder does not serve")
        if self._backend.file_known(rel) is False:
            raise ValueError(f"no such file: {rel}")
        return rel

    def url(self, source: str, *, file: str | None = None) -> MediaUrl:
        """A URL on the engine's origin (MOD-100, CORE-400)."""
        self._need(PLAY)
        info = self._grant(source)
        rel = self._rel(info, file)
        return MediaUrl(info.delivery, media_url(info.name, info.delivery, rel))

    async def files(self, source: str, path: str = "") -> Sequence[MediaFile]:
        """The entries of a folder of a ``files`` source (MOD-100, CORE-405)."""
        self._need(PLAY)
        info = self._grant(source)
        base = (info.path or "").rstrip("/")
        if info.kind != "files" or PurePosixPath(base).suffix.lower() in MEDIA_EXTENSIONS:
            raise ValueError(f"{source!r} is not a folder of files")
        sub = path.strip("/")
        if sub and any(p in ("", ".", "..") or "\\" in p for p in sub.split("/")):
            raise ValueError(f"{path!r} is not a safe folder")
        rel = f"{base}/{sub}" if sub else base
        if split_media_path(rel) is None:
            raise ValueError(f"{path!r} is not a safe folder")
        out: list[MediaFile] = []
        for entry in await self._backend.list_folder(rel):
            name = str(entry["name"])
            kind: MediaFileKind = entry["kind"]
            size = entry.get("size")
            out.append(
                MediaFile(
                    name=name,
                    path=f"{sub}/{name}" if sub else name,
                    kind=kind,
                    size=int(size) if isinstance(size, int | float) else None,
                    modified=_when(entry.get("modified")),
                )
            )
        return out

    async def takeover(
        self, source: str, *, file: str | None = None, duration: float | None = None
    ) -> str:
        """Start a takeover (MOD-101, CORE-411)."""
        self._need(TAKEOVER)
        info = self._grant(source)
        if duration is not None and not (math.isfinite(duration) and duration > 0):
            raise ValueError("duration must be more than 0 seconds")
        self._rel(info, file)
        tid = secrets.token_hex(6)
        await self._backend.takeover(
            self._instance_id, source, file=file, duration=duration, takeover_id=tid
        )
        return tid

    async def stop(self, takeover_id: str | None = None) -> None:
        """Stop a takeover (MOD-101, CORE-411)."""
        self._need(TAKEOVER)
        if not self._grants.media:
            raise PermissionError("the instance has no media grant")
        await self._backend.stop(self._instance_id, takeover_id, self._grants.media_granted)


def _when(value: Any) -> datetime | None:
    """An ISO 8601 time of a folder entry, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
