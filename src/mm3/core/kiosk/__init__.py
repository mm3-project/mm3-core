"""The kiosk: cage, the display and Chromium, driven by the kiosk agent.

``mm3 kiosk session`` runs inside cage (``mm3-kiosk.service``). It checks the
cage version, connects to the engine's ``/ws/kiosk-agent``, applies the
``display`` block with ``wlr-randr``, reports outputs and modes, and starts
and supervises Chromium on the mirror listener. It also applies the
``audio`` block with ``pactl`` and reports the outputs (``mm3.core.kiosk.audio``).

Implements:
    CORE-06: Core spec 0.16, §3 Engine components.
    CORE-143: Core spec 0.21, §6.7 Display output.
    CORE-295, CORE-296: Core spec 0.21, §6.7 Display output.
"""

from __future__ import annotations

from mm3.core.kiosk.audio import (
    Card,
    Profile,
    Resolution,
    Sink,
    parse_cards,
    parse_sinks,
    resolve_sink,
)
from mm3.core.kiosk.cage import MIN_CAGE, CageError, cage_version, parse_cage_version
from mm3.core.kiosk.chromium import CHROMIUM_FLAGS, chromium_command
from mm3.core.kiosk.randr import Mode, Output, apply_command, choose_mode, parse_outputs

__all__ = [
    "CHROMIUM_FLAGS",
    "MIN_CAGE",
    "CageError",
    "Card",
    "Mode",
    "Output",
    "Profile",
    "Resolution",
    "Sink",
    "apply_command",
    "cage_version",
    "choose_mode",
    "chromium_command",
    "parse_cage_version",
    "parse_cards",
    "parse_outputs",
    "parse_sinks",
    "resolve_sink",
]
