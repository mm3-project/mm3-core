"""The kiosk: cage, the display and Chromium, driven by the kiosk agent.

``mm3 kiosk session`` runs inside cage (``mm3-kiosk.service``). It checks the
cage version, connects to the engine's ``/ws/kiosk-agent``, applies the
``display`` block with ``wlr-randr``, reports outputs and modes, and starts
and supervises Chromium on the mirror listener.

Implements:
    CORE-06: Core spec 0.16, §3 Engine components.
    CORE-143: Core spec 0.16, §6.7 Display output.
"""

from __future__ import annotations

from mm3.core.kiosk.cage import MIN_CAGE, CageError, cage_version, parse_cage_version
from mm3.core.kiosk.chromium import CHROMIUM_FLAGS, chromium_command
from mm3.core.kiosk.randr import Mode, Output, apply_command, choose_mode, parse_outputs

__all__ = [
    "CHROMIUM_FLAGS",
    "MIN_CAGE",
    "CageError",
    "Mode",
    "Output",
    "apply_command",
    "cage_version",
    "choose_mode",
    "chromium_command",
    "parse_cage_version",
    "parse_outputs",
]
