"""Clock module: digital and analog clock, world times, sun and moon.

A front-end-only module (Contract 1): the time comes from the browser's
``Date``, shown in the mirror's time zone; there is no backend hook.

Implements:
    CLK-01: Core spec 0.19, §9.4 Reference modules.
    CORE-289: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from typing import ClassVar

from mm3 import MirrorModule, ModuleConfig

from .config import ClockConfig

__all__ = ["ClockConfig", "ClockModule"]


class ClockModule(MirrorModule):
    """Shows the time; no backend, no capabilities, no notifications (CLK-01)."""

    name: ClassVar[str] = "clock"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = ClockConfig
    element: ClassVar[str | None] = "mm3-clock"
    scripts: ClassVar[list[str]] = ["clock.js"]
    styles: ClassVar[list[str]] = ["clock.css"]
    min_size_px: ClassVar[tuple[int, int] | None] = (180, 80)
