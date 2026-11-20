"""Configuration of the media module.

Implements:
    MED-02, MED-03, MED-05: Core spec 0.57, §9.4 Reference modules.
    MOD-45, MOD-46, MOD-48, MOD-49: Module Contract 1.0 rev 16, §6.2 Configuration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mm3 import ModuleConfig

__all__ = ["MediaConfig", "MediaKind"]

MediaKind = Literal["video", "image", "slideshow", "audio", "youtube"]
"""What an instance shows (MED-02)."""

VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
"""A YouTube video id: 11 characters (CORE-413)."""


class MediaConfig(ModuleConfig):
    """Configuration of :class:`mm3_media.MediaModule` (MED-02, MED-03, MED-05)."""

    kind: MediaKind = Field(title="CFG_KIND", description="CFG_KIND_HELP")
    source: str | None = Field(None, title="CFG_SOURCE", description="CFG_SOURCE_HELP")
    file: str | None = Field(None, title="CFG_FILE", description="CFG_FILE_HELP")
    video_id: str | None = Field(
        None, pattern=VIDEO_ID_PATTERN, title="CFG_VIDEO_ID", description="CFG_VIDEO_ID_HELP"
    )
    fit: Literal["contain", "cover"] = Field("contain", title="CFG_FIT", description="CFG_FIT_HELP")
    loop: bool = Field(True, title="CFG_LOOP", description="CFG_LOOP_HELP")
    muted: bool = Field(False, title="CFG_MUTED", description="CFG_MUTED_HELP")
    opacity: float = Field(1.0, ge=0, le=1, title="CFG_OPACITY", description="CFG_OPACITY_HELP")
    interval: float = Field(15.0, ge=3, title="CFG_INTERVAL", description="CFG_INTERVAL_HELP")
    order: Literal["name", "date", "random"] = Field(
        "random", title="CFG_ORDER", description="CFG_ORDER_HELP"
    )
    transition: Literal["fade", "none"] = Field(
        "fade", title="CFG_TRANSITION", description="CFG_TRANSITION_HELP"
    )
    recursive: bool = Field(True, title="CFG_RECURSIVE", description="CFG_RECURSIVE_HELP")
    on_touch: Literal["none", "takeover"] = Field(
        "none", title="CFG_ON_TOUCH", description="CFG_ON_TOUCH_HELP"
    )

    @model_validator(mode="after")
    def _check_kind(self) -> MediaConfig:
        """Each kind needs its own keys and refuses the keys of the others (MED-02)."""
        if self.kind == "youtube":
            if self.video_id is None:
                raise ValueError("CFG_YOUTUBE_NEEDS_VIDEO_ID")
            if self.source is not None or self.file is not None:
                raise ValueError("CFG_YOUTUBE_NO_SOURCE")
            if self.on_touch == "takeover":
                raise ValueError("CFG_YOUTUBE_NO_TAKEOVER")
            return self
        if self.source is None:
            raise ValueError("CFG_SOURCE_REQUIRED")
        if self.video_id is not None:
            raise ValueError("CFG_VIDEO_ID_YOUTUBE_ONLY")
        if self.kind == "slideshow" and self.file is not None:
            raise ValueError("CFG_SLIDESHOW_NO_FILE")
        return self

    @property
    def source_name(self) -> str:
        """The source, which the validator guarantees for every kind but ``youtube`` (MED-02)."""
        if self.source is None:
            raise ValueError("CFG_SOURCE_REQUIRED")
        return self.source
