"""Restarting the engine and checking it (CORE-357).

The installer may stop, start and restart ``mm3.service`` and nothing else,
through its polkit rule (CORE-359). After a restart the engine counts as READY
when the unit is active within 60 s: the unit is ``Type=notify``, so it is
active only after ``READY=1`` (CORE-290). Then ``mm3 diag check`` of the new
venv runs over the admin socket, as the engine's user ``mm3``, and a non-zero
exit means rollback (CORE-135).

Implements:
    CORE-357: Core spec 0.42, §10.7 Installer service.
    CORE-359: Core spec 0.42, §10.8 Backup and restore.
    CORE-363: Core spec 0.37, §10.8 Backup and restore.
    CORE-135: Core spec 0.33, §10.7 Installer service.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from mm3.installer.paths import ENGINE_USER, Layout
from mm3.installer.runner import Completed, Runner

__all__ = ["ENGINE_UNIT", "READY_TIMEOUT_S", "SYSTEMCTL", "Engine", "SystemdEngine"]

ENGINE_UNIT = "mm3.service"
READY_TIMEOUT_S = 60.0
SYSTEMCTL = "/usr/bin/systemctl"


class Engine(Protocol):
    """Control of ``mm3.service`` (a fake in tests)."""

    def restart(self) -> bool:
        """Restart the engine; whether it is READY within 60 s of the restart."""

    def stop(self) -> bool:
        """Stop the engine; whether it is stopped."""

    def start(self) -> bool:
        """Start the engine; whether it is READY within 60 s."""

    def diag_check(self, venv: Path) -> Completed:
        """Run ``mm3 diag check`` from ``venv`` over the admin socket."""


class SystemdEngine:
    """``systemctl`` and the diag check (production).

    Args:
        layout: The installer's paths.
        runner: Starts ``mm3 diag check`` as ``mm3``.
        sleep: Sleep function, for tests.
    """

    def __init__(
        self,
        layout: Layout,
        runner: Runner,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._layout = layout
        self._runner = runner
        self._sleep = sleep

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv
            [SYSTEMCTL, *args, ENGINE_UNIT],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def restart(self) -> bool:
        """``systemctl restart mm3.service``, then check that it is active.

        The restart job waits for the new process's ``READY=1``; the unit's
        ``TimeoutStartSec=60s`` fails the job when READY takes longer, so a
        successful job followed by ``is-active`` = ``active`` is READY within
        60 s (CORE-357). A queued job never makes an old process count.

        Returns:
            Whether the engine is READY.
        """
        return self._activate("restart")

    def stop(self) -> bool:
        """``systemctl stop mm3.service`` (CORE-363).

        Returns:
            Whether the unit is no longer active.
        """
        try:
            done = self._systemctl("stop")
        except subprocess.TimeoutExpired:
            return False
        return done.returncode == 0

    def start(self) -> bool:
        """``systemctl start mm3.service``, then wait for READY (CORE-363).

        Returns:
            Whether the engine is READY.
        """
        return self._activate("start")

    def _activate(self, verb: str) -> bool:
        started = time.monotonic()
        try:
            done = self._systemctl(verb)
        except subprocess.TimeoutExpired:
            return False
        if done.returncode != 0:
            return False
        while time.monotonic() - started < READY_TIMEOUT_S + 30:
            state = self._systemctl("is-active").stdout.strip()
            if state == "active":
                return True
            if state not in ("activating", "reloading"):
                return False
            self._sleep(1.0)
        return False

    def diag_check(self, venv: Path) -> Completed:
        """``<venv>/bin/mm3 diag check --socket <admin socket>`` as ``mm3``."""
        home = Path("/")
        return self._runner.run(
            [
                str(venv / "bin" / "mm3"),
                "diag",
                "check",
                "--socket",
                str(self._layout.admin_socket),
            ],
            user=ENGINE_USER,
            home=home,
            timeout=120.0,
        )
