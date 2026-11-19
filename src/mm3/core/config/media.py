"""The ``media`` key: sources, shares, limits and takeover defaults, and its checks.

Only the configuration is defined here (core spec 9.7, CORE-398, CORE-399):
nothing in this module starts ``ffmpeg`` or reads the media folder. Checks of
one field live on the models; checks that relate a source to ``devices`` or
to ``media.shares`` are :func:`check_media`. The rclone remote a share names
is not checked here: it lives in the installer's ``rclone.conf``, and the
installer checks it when it runs the share (CORE-406, CORE-407).

Implements:
    CORE-398: Core spec 0.48, §9.7 Media service.
    CORE-399: Core spec 0.49, §9.7 Media service.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from mm3.config import ModuleConfig
from mm3.core.config.devices import DEVICE_NAME_PATTERN, DeviceConfig, DeviceName
from mm3.core.config.records import SourceMap, ValidationRecord, make_record, pointer

__all__ = [
    "MediaConfig",
    "MediaLimits",
    "MediaShare",
    "MediaSource",
    "MediaTakeover",
    "MountShare",
    "SyncShare",
    "check_media",
]

MediaName = Annotated[str, StringConstraints(pattern=DEVICE_NAME_PATTERN)]
SourceKind = Literal["rtsp", "stream", "camera", "files"]

_SCHEMES: dict[str, tuple[str, ...]] = {"rtsp": ("rtsp", "rtsps"), "stream": ("https", "http")}
_USES: dict[str, str] = {"rtsp": "url", "stream": "url", "camera": "device", "files": "path"}


class _Model(ModuleConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _not_used(field: str, kind: object) -> PydanticCustomError:
    return PydanticCustomError(
        "CORE_CFG_INVALID", "field not used", {"reason": f"{field} is not used by kind {kind}"}
    )


def _media_path_ok(value: str) -> bool:
    """Shape of a ``files`` path (CORE-399); the share name is checked by :func:`check_media`."""
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or "\\" in value:
        return False
    if value.startswith("files/"):
        return True
    parts = value.split("/")
    return len(parts) >= 3 and parts[0] == "shares" and bool(parts[1])


class MediaSource(_Model):
    """One entry of ``media.sources`` (CORE-398).

    Attributes:
        kind: ``rtsp``, ``stream``, ``camera`` or ``files``.
        url: For ``rtsp`` (scheme ``rtsp`` or ``rtsps``) and ``stream``
            (``https`` or ``http``).
        device: For ``camera``: a ``devices`` entry of kind ``camera``.
        path: For ``files``: a folder or file under the media folder,
            starting with ``files/`` or ``shares/<share>/``.
        delivery: ``auto``, ``mjpeg`` or ``hls``.
        transcode: Encode to H.264 instead of copying the codecs.
        audio: Whether the source has sound; always false for ``camera``.
        fps: MJPEG frame rate, 1 to 30.
        width: Output width, at most 1920; the source's width when unset.

    Implements:
        CORE-398: Core spec 0.48, §9.7 Media service.
        CORE-399: Core spec 0.49, §9.7 Media service.
    """

    kind: SourceKind = Field(
        title="CFG_MEDIA_SOURCE_KIND", description="CFG_MEDIA_SOURCE_KIND_HELP"
    )
    url: str | None = Field(
        default=None,
        validate_default=True,
        title="CFG_MEDIA_SOURCE_URL",
        description="CFG_MEDIA_SOURCE_URL_HELP",
    )
    device: DeviceName | None = Field(
        default=None,
        validate_default=True,
        title="CFG_MEDIA_SOURCE_DEVICE",
        description="CFG_MEDIA_SOURCE_DEVICE_HELP",
    )
    path: str | None = Field(
        default=None,
        validate_default=True,
        title="CFG_MEDIA_SOURCE_PATH",
        description="CFG_MEDIA_SOURCE_PATH_HELP",
    )
    delivery: Literal["auto", "mjpeg", "hls"] = Field(
        default="auto",
        title="CFG_MEDIA_SOURCE_DELIVERY",
        description="CFG_MEDIA_SOURCE_DELIVERY_HELP",
    )
    transcode: bool = Field(
        default=False,
        strict=True,
        title="CFG_MEDIA_SOURCE_TRANSCODE",
        description="CFG_MEDIA_SOURCE_TRANSCODE_HELP",
    )
    audio: bool | None = Field(
        default=None,
        strict=True,
        title="CFG_MEDIA_SOURCE_AUDIO",
        description="CFG_MEDIA_SOURCE_AUDIO_HELP",
    )
    fps: int = Field(
        default=15,
        ge=1,
        le=30,
        title="CFG_MEDIA_SOURCE_FPS",
        description="CFG_MEDIA_SOURCE_FPS_HELP",
    )
    width: int | None = Field(
        default=None,
        ge=1,
        le=1920,
        title="CFG_MEDIA_SOURCE_WIDTH",
        description="CFG_MEDIA_SOURCE_WIDTH_HELP",
    )

    @field_validator("url", mode="after")
    @classmethod
    def _check_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        kind = info.data.get("kind")
        if kind is None:
            return value
        if _USES[kind] != "url":
            if value is not None:
                raise _not_used("url", kind)
            return None
        if value is None:
            raise PydanticCustomError("CORE_CFG_REQUIRED", "url required", {})
        try:
            scheme = urlsplit(value).scheme.lower()
            host = urlsplit(value).hostname
        except ValueError:
            scheme, host = "", None
        if scheme not in _SCHEMES[kind] or not host:
            raise PydanticCustomError(
                "CORE_CFG_MEDIA_URL",
                "scheme does not fit",
                {"kind": kind, "allowed": " or ".join(_SCHEMES[kind])},
            )
        return value

    @field_validator("device", mode="after")
    @classmethod
    def _check_device(cls, value: str | None, info: ValidationInfo) -> str | None:
        kind = info.data.get("kind")
        if kind is None:
            return value
        if kind != "camera":
            if value is not None:
                raise _not_used("device", kind)
            return None
        if value is None:
            raise PydanticCustomError("CORE_CFG_REQUIRED", "device required", {})
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _check_path(cls, value: str | None, info: ValidationInfo) -> str | None:
        kind = info.data.get("kind")
        if kind is None:
            return value
        if kind != "files":
            if value is not None:
                raise _not_used("path", kind)
            return None
        if value is None:
            raise PydanticCustomError("CORE_CFG_REQUIRED", "path required", {})
        if not _media_path_ok(value):
            raise PydanticCustomError("CORE_CFG_MEDIA_PATH", "bad media path", {"path": value})
        return value

    @property
    def has_audio(self) -> bool:
        """Effective ``audio``: true by default, always false for ``camera`` (CORE-398)."""
        if self.kind == "camera":
            return False
        return True if self.audio is None else self.audio


class SyncShare(_Model):
    """A share copied by ``rclone sync`` (CORE-406; settings only here).

    Implements:
        CORE-398: Core spec 0.48, §9.7 Media service.
    """

    method: Literal["sync"] = Field(
        title="CFG_MEDIA_SHARE_METHOD", description="CFG_MEDIA_SHARE_METHOD_HELP"
    )
    remote: str = Field(
        pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$",
        title="CFG_MEDIA_SHARE_REMOTE",
        description="CFG_MEDIA_SHARE_REMOTE_HELP",
    )
    path: str = Field(
        default="", title="CFG_MEDIA_SHARE_PATH", description="CFG_MEDIA_SHARE_PATH_HELP"
    )
    interval: int = Field(
        default=60,
        ge=5,
        title="CFG_MEDIA_SHARE_INTERVAL",
        description="CFG_MEDIA_SHARE_INTERVAL_HELP",
    )
    max_size_gb: float = Field(
        default=20,
        gt=0,
        title="CFG_MEDIA_SHARE_MAX_SIZE_GB",
        description="CFG_MEDIA_SHARE_MAX_SIZE_GB_HELP",
    )


class MountShare(_Model):
    """A share mounted read-only by a systemd mount unit (CORE-407; settings only here).

    Implements:
        CORE-398: Core spec 0.48, §9.7 Media service.
    """

    method: Literal["mount"] = Field(
        title="CFG_MEDIA_SHARE_METHOD", description="CFG_MEDIA_SHARE_METHOD_HELP"
    )
    type: Literal["smb", "nfs"] = Field(
        title="CFG_MEDIA_SHARE_TYPE", description="CFG_MEDIA_SHARE_TYPE_HELP"
    )
    host: str = Field(
        pattern=r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$",
        title="CFG_MEDIA_SHARE_HOST",
        description="CFG_MEDIA_SHARE_HOST_HELP",
    )
    path: str = Field(
        min_length=1, title="CFG_MEDIA_SHARE_PATH", description="CFG_MEDIA_SHARE_PATH_HELP"
    )
    remote: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$",
        title="CFG_MEDIA_SHARE_REMOTE",
        description="CFG_MEDIA_SHARE_REMOTE_HELP",
    )

    @model_validator(mode="after")
    def _remote_for_smb(self) -> MountShare:
        if self.type == "smb" and self.remote is None:
            raise PydanticCustomError(
                "CORE_CFG_INVALID", "smb needs remote", {"reason": "an smb share needs remote"}
            )
        if self.type == "nfs" and self.remote is not None:
            raise _not_used("remote", "nfs")
        return self


def _share_method(value: object) -> str:
    method = value.get("method") if isinstance(value, Mapping) else getattr(value, "method", None)
    # Anything but ``mount`` is checked as ``sync``, so a wrong method is a choice error.
    return "mount" if method == "mount" else "sync"


MediaShare = Annotated[
    Annotated[SyncShare, Tag("sync")] | Annotated[MountShare, Tag("mount")],
    Discriminator(_share_method),
]


class MediaLimits(_Model):
    """``media.limits`` (CORE-397, CORE-401, CORE-405; settings only here).

    Implements:
        CORE-398: Core spec 0.48, §9.7 Media service.
    """

    processes: int = Field(
        default=4,
        ge=1,
        le=8,
        title="CFG_MEDIA_LIMITS_PROCESSES",
        description="CFG_MEDIA_LIMITS_PROCESSES_HELP",
    )
    transcode: int = Field(
        default=1,
        ge=0,
        le=4,
        title="CFG_MEDIA_LIMITS_TRANSCODE",
        description="CFG_MEDIA_LIMITS_TRANSCODE_HELP",
    )
    upload_max_mb: int = Field(
        default=2048,
        ge=1,
        le=8192,
        title="CFG_MEDIA_LIMITS_UPLOAD_MAX_MB",
        description="CFG_MEDIA_LIMITS_UPLOAD_MAX_MB_HELP",
    )


class MediaTakeover(_Model):
    """``media.takeover``: durations in seconds (CORE-410; settings only here).

    Implements:
        CORE-398: Core spec 0.48, §9.7 Media service.
    """

    duration: int = Field(
        default=60,
        ge=1,
        title="CFG_MEDIA_TAKEOVER_DURATION",
        description="CFG_MEDIA_TAKEOVER_DURATION_HELP",
    )
    max_duration: int = Field(
        default=3600,
        ge=1,
        title="CFG_MEDIA_TAKEOVER_MAX_DURATION",
        description="CFG_MEDIA_TAKEOVER_MAX_DURATION_HELP",
    )

    @field_validator("max_duration", mode="after")
    @classmethod
    def _at_least_duration(cls, value: int, info: ValidationInfo) -> int:
        duration = info.data.get("duration")
        if isinstance(duration, int) and value < duration:
            raise PydanticCustomError(
                "CORE_CFG_INVALID",
                "max below duration",
                {"reason": "max_duration is shorter than duration"},
            )
        return value


class MediaConfig(_Model):
    """The ``media`` key (CORE-398).

    Implements:
        CORE-398: Core spec 0.48, §9.7 Media service.
    """

    sources: dict[MediaName, MediaSource] = Field(
        default_factory=dict, title="CFG_MEDIA_SOURCES", description="CFG_MEDIA_SOURCES_HELP"
    )
    shares: dict[MediaName, MediaShare] = Field(
        default_factory=dict, title="CFG_MEDIA_SHARES", description="CFG_MEDIA_SHARES_HELP"
    )
    limits: MediaLimits = Field(
        default=MediaLimits(), title="CFG_MEDIA_LIMITS", description="CFG_MEDIA_LIMITS_HELP"
    )
    takeover: MediaTakeover = Field(
        default=MediaTakeover(), title="CFG_MEDIA_TAKEOVER", description="CFG_MEDIA_TAKEOVER_HELP"
    )
    browser_audio: bool = Field(
        default=False,
        strict=True,
        title="CFG_MEDIA_BROWSER_AUDIO",
        description="CFG_MEDIA_BROWSER_AUDIO_HELP",
    )


def check_media(
    media: MediaConfig,
    devices: Mapping[str, DeviceConfig],
    sources: SourceMap | None = None,
) -> list[ValidationRecord]:
    """Check sources against ``devices`` and ``media.shares`` (CORE-399).

    Args:
        media: The validated ``media`` key.
        devices: The validated ``devices`` key.
        sources: Source positions for the records.

    Returns:
        ``CORE_CFG_MEDIA_DEVICE`` for a camera source whose device is not in
        ``devices`` or is a Kinect, ``CORE_CFG_MEDIA_SHARE`` for a path under
        ``shares/<name>/`` of a share that is not in ``media.shares``, and the
        warning ``CORE_CFG_MEDIA_DELIVERY`` for ``delivery: hls`` on a camera.

    Implements:
        CORE-399: Core spec 0.49, §9.7 Media service.
    """
    records: list[ValidationRecord] = []
    for name, src in media.sources.items():
        base = ("media", "sources", name)
        if src.kind == "camera" and src.device is not None:
            dev = devices.get(src.device)
            if dev is None or dev.kind != "camera":
                records.append(
                    make_record(
                        "CORE_CFG_MEDIA_DEVICE",
                        pointer((*base, "device")),
                        {"source": name, "device": src.device},
                        sources=sources,
                    )
                )
            if src.delivery == "hls":
                records.append(
                    make_record(
                        "CORE_CFG_MEDIA_DELIVERY",
                        pointer((*base, "delivery")),
                        {"source": name},
                        sources=sources,
                    )
                )
        if src.kind == "files" and src.path is not None and src.path.startswith("shares/"):
            share = src.path.split("/")[1]
            if share not in media.shares:
                records.append(
                    make_record(
                        "CORE_CFG_MEDIA_SHARE",
                        pointer((*base, "path")),
                        {"source": name, "share": share},
                        sources=sources,
                    )
                )
    return records
