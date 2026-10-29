"""Outputs and modes through ``wlr-randr`` (CORE-144, CORE-145).

The agent reads ``wlr-randr --json``, picks the mode to apply and builds the
``wlr-randr`` command that sets output, mode, refresh, rotation and scale.

Implements:
    CORE-144, CORE-145: Core spec 0.16, §6.7 Display output.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TRANSFORMS",
    "Mode",
    "Output",
    "apply_command",
    "choose_mode",
    "parse_outputs",
    "pick_output",
]

TRANSFORMS = {0: "normal", 90: "90", 180: "180", 270: "270"}
"""``display.rotation`` (clockwise) to the ``wlr-randr --transform`` value."""


@dataclass(frozen=True, order=True)
class Mode:
    """A monitor mode.

    Attributes:
        width: Horizontal pixels, in the monitor's native orientation.
        height: Vertical pixels.
        refresh: Refresh rate in Hz.
    """

    width: int
    height: int
    refresh: float

    def as_dict(self) -> dict[str, Any]:
        """Return ``{width, height, refresh}`` for the ``outputs`` frame (CORE-277)."""
        return {"width": self.width, "height": self.height, "refresh": round(self.refresh, 3)}


@dataclass(frozen=True)
class Output:
    """A connector as ``wlr-randr`` lists it.

    Attributes:
        name: Connector name (``HDMI-A-1``).
        modes: Supported modes.
        current: The current mode, or ``None`` when the output is off.
        preferred: The monitor's preferred mode, if it names one.
        enabled: Whether the output is on.
    """

    name: str
    modes: tuple[Mode, ...] = field(default_factory=tuple)
    current: Mode | None = None
    preferred: Mode | None = None
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return ``{name, modes, current}`` for the ``outputs`` frame (CORE-277)."""
        return {
            "name": self.name,
            "modes": [m.as_dict() for m in self.modes],
            "current": self.current.as_dict() if self.current is not None else None,
        }


def parse_outputs(text: str) -> list[Output]:
    """Parse ``wlr-randr --json`` output.

    Args:
        text: The JSON array ``wlr-randr --json`` prints.

    Returns:
        The outputs, in the listed order.

    Raises:
        ValueError: Not the expected JSON.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("wlr-randr --json did not print a list")
    outputs: list[Output] = []
    for item in data:
        modes: list[Mode] = []
        current = preferred = None
        for raw in item.get("modes", []):
            mode = Mode(int(raw["width"]), int(raw["height"]), float(raw.get("refresh", 0.0)))
            modes.append(mode)
            if raw.get("current"):
                current = mode
            if raw.get("preferred"):
                preferred = mode
        enabled = bool(item.get("enabled", True))
        outputs.append(
            Output(
                name=str(item["name"]),
                modes=tuple(modes),
                current=current if enabled else None,
                preferred=preferred,
                enabled=enabled,
            )
        )
    return outputs


def _ratio(w: int, h: int) -> float:
    return round(w / h, 3)


def choose_mode(
    output: Output,
    width: int | None,
    height: int | None,
    refresh: int | None = None,
) -> Mode | None:
    """Pick the mode to apply for the ``display`` block (CORE-145).

    The requested mode when the output supports it; otherwise the closest:
    same aspect ratio first, then the largest not above the request (the
    smallest when every mode is above it). Among modes of that size the
    refresh closest to the request wins, or the highest without one.
    Without a requested size the preferred mode is used.

    Args:
        output: The output.
        width: Requested width, or ``None``.
        height: Requested height, or ``None``.
        refresh: Requested refresh in Hz, or ``None``.

    Returns:
        The mode, or ``None`` when the output lists no mode.
    """
    if not output.modes:
        return None
    if width is None or height is None:
        base = output.preferred or output.current or output.modes[0]
        width, height = base.width, base.height
    sizes = {(m.width, m.height) for m in output.modes}
    if (width, height) in sizes:
        size = (width, height)
    else:
        same = [s for s in sizes if _ratio(*s) == _ratio(width, height)]
        pool = same or list(sizes)
        below = [s for s in pool if s[0] <= width and s[1] <= height]
        size = (
            max(below, key=lambda s: s[0] * s[1]) if below else min(pool, key=lambda s: s[0] * s[1])
        )
    candidates = [m for m in output.modes if (m.width, m.height) == size]
    if refresh is None:
        return max(candidates, key=lambda m: m.refresh)
    return min(candidates, key=lambda m: (abs(m.refresh - refresh), -m.refresh))


def apply_command(
    output: Output, mode: Mode, rotation: int, scale: float, binary: str = "wlr-randr"
) -> list[str]:
    """Return the ``wlr-randr`` command that applies a mode, rotation and scale.

    Args:
        output: The output.
        mode: The mode from :func:`choose_mode`.
        rotation: ``display.rotation``.
        scale: ``display.scale``.
        binary: The wlr-randr executable.

    Returns:
        The argument list.
    """
    return [
        binary,
        "--output",
        output.name,
        "--on",
        "--mode",
        f"{mode.width}x{mode.height}@{mode.refresh:g}Hz",
        "--transform",
        TRANSFORMS[rotation],
        "--scale",
        f"{scale:g}",
    ]


def pick_output(outputs: Sequence[Output], name: str | None) -> Output | None:
    """Return the named output, or the first enabled one (``display.output``, 6.7).

    Args:
        outputs: Listed outputs.
        name: ``display.output``, if set.

    Returns:
        The output, or ``None`` when there is none.
    """
    if name is not None:
        return next((o for o in outputs if o.name == name), None)
    return next((o for o in outputs if o.enabled and o.modes), None) or next(
        (o for o in outputs if o.modes), None
    )
