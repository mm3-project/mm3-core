"""Greeting module: shows a time-of-day greeting.

For trying the media elements on the mirror before ``ctx.media`` exists, an
instance may also play one media URL or one YouTube video under the greeting
(``media_demo``). The URL is typed by the user and passed to the front-end
in the public config: this breaks MOD-102 for this test module only, which is
never published (CORE-289), and goes away when ``ctx.media`` (MOD-100) lands.

Implements:
    CORE-289: Core spec 0.19, §9.4 Reference modules.
    -: Module Contract 1.0 rev 12, §13 Minimal reference module.
    CORE-409, CORE-413: Core spec 0.53, §9.7 Media service.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from mm3 import LocalizedText, MirrorModule, ModuleConfig


class MediaDemo(BaseModel):
    """One media URL or YouTube video played under the greeting (milestone 8 test)."""

    model_config = ConfigDict(extra="forbid")

    src: str | None = Field(
        None,
        pattern=r"^/media/[^\s]+$",
        title="CFG_MEDIA_DEMO_SRC",
        description="CFG_MEDIA_DEMO_SRC_HELP",
    )
    youtube: str | None = Field(
        None,
        pattern=r"^[A-Za-z0-9_-]{11}$",
        title="CFG_MEDIA_DEMO_YOUTUBE",
        description="CFG_MEDIA_DEMO_YOUTUBE_HELP",
    )
    fit: Literal["contain", "cover"] = Field(
        "contain", title="CFG_MEDIA_DEMO_FIT", description="CFG_MEDIA_DEMO_FIT_HELP"
    )
    loop: bool = Field(True, title="CFG_MEDIA_DEMO_LOOP", description="CFG_MEDIA_DEMO_LOOP_HELP")
    muted: bool = Field(
        False, title="CFG_MEDIA_DEMO_MUTED", description="CFG_MEDIA_DEMO_MUTED_HELP"
    )


class GreetingConfig(ModuleConfig):
    """Configuration for :class:`GreetingModule`."""

    name: LocalizedText | None = Field(None, title="CFG_NAME", description="CFG_NAME_HELP")
    media_demo: MediaDemo | None = Field(
        None, title="CFG_MEDIA_DEMO", description="CFG_MEDIA_DEMO_HELP"
    )


class GreetingModule(MirrorModule):
    """Publishes the current part of the day to its front-end."""

    name: ClassVar[str] = "greeting"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = GreetingConfig
    element: ClassVar[str | None] = "mm3-greeting"
    scripts: ClassVar[list[str]] = ["greeting.js"]
    styles: ClassVar[list[str]] = ["greeting.css"]
    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)
    # media_demo plays through mm3-player and mm3-youtube in greeting.js (MOD-103);
    # media_demo.youtube needs the grant youtube: true as well (CORE-413).
    capabilities: ClassVar[set[str]] = {"media.play", "media.youtube"}

    async def start(self) -> None:
        """Publish once, then every minute."""
        await self._publish()
        self.ctx.scheduler.every(60, self._publish)

    async def _publish(self) -> None:
        hour = self.ctx.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        self.ctx.set_state({"part": part})
        self.ctx.log.debug("part_of_day", part=part)

    def snapshot(self) -> dict[str, str]:
        """Latest state for newly connected clients."""
        return dict(self.ctx.state)
