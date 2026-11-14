"""Restarting the engine or the kiosk and rebooting the machine (CORE-382).

The installer holds the only rights for these, through its polkit rule
(CORE-359): ``systemctl restart mm3.service``, ``systemctl restart
mm3-kiosk.service`` and ``systemctl reboot`` (logind). Nothing else is run
here. The server answers the request first and calls :meth:`SystemActions.run`
after a short delay (:data:`DELAY_S`), so the answer reaches the engine and
the caller before the engine stops.

Implements:
    CORE-382: Core spec 0.42, §9.1 Remote-control API.
    CORE-359: Core spec 0.42, §10.8 Backup and restore.
"""

from __future__ import annotations

import subprocess
from typing import Protocol

from mm3.installer.service import ENGINE_UNIT, SYSTEMCTL

__all__ = ["ACTIONS", "DELAY_S", "KIOSK_UNIT", "SystemActions", "SystemdActions"]

KIOSK_UNIT = "mm3-kiosk.service"
DELAY_S = 2.0
"""Seconds between the 202 answer and the command (CORE-382)."""
ACTIONS = ("restart-engine", "restart-kiosk", "reboot")
"""The routes ``POST /v1/system/<action>`` (CORE-382)."""
_ARGV = {
    "restart-engine": ("restart", ENGINE_UNIT),
    "restart-kiosk": ("restart", KIOSK_UNIT),
    "reboot": ("reboot",),
}
_TIMEOUT_S = 120


class SystemActions(Protocol):
    """What the installer may do to the machine (a fake in tests)."""

    def kiosk_installed(self) -> bool:
        """Whether ``mm3-kiosk.service`` exists on this machine."""

    def run(self, action: str) -> bool:
        """Run one of :data:`ACTIONS`; whether the command succeeded."""


class SystemdActions:
    """``systemctl`` through the installer's polkit rule (production)."""

    def kiosk_installed(self) -> bool:
        """Whether the kiosk unit file is known to systemd."""
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                [SYSTEMCTL, "list-unit-files", "--no-legend", KIOSK_UNIT],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0 and KIOSK_UNIT in done.stdout

    def run(self, action: str) -> bool:
        """Run ``systemctl`` for ``action``.

        Args:
            action: One of :data:`ACTIONS`.

        Returns:
            Whether ``systemctl`` exited with 0 within two minutes.

        Raises:
            KeyError: ``action`` is not one of :data:`ACTIONS`.
        """
        argv = _ARGV[action]
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                [SYSTEMCTL, "--no-ask-password", *argv],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0
