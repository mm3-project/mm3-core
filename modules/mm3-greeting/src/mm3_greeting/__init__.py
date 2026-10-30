"""Greeting module: shows a time-of-day greeting.

Implements:
    CORE-289: Core spec 0.19, §9.4 Reference modules.
    -: Module Contract 1.0 rev 12, §13 Minimal reference module.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from mm3 import LocalizedText, MirrorModule, ModuleConfig


class GreetingConfig(ModuleConfig):
    """Configuration for :class:`GreetingModule`."""

    name: LocalizedText | None = Field(None, title="CFG_NAME", description="CFG_NAME_HELP")


class GreetingModule(MirrorModule):
    """Publishes the current part of the day to its front-end."""

    name: ClassVar[str] = "greeting"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = GreetingConfig
    element: ClassVar[str | None] = "mm3-greeting"
    scripts: ClassVar[list[str]] = ["greeting.js"]
    styles: ClassVar[list[str]] = ["greeting.css"]
    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)

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
