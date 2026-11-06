"""Where the installer reads and writes.

Every path the installer touches comes from one :class:`Layout`, so tests run
the whole service inside a temporary directory. The defaults are the paths of
CORE-351, CORE-355, CORE-359 and CORE-368.

Implements:
    CORE-355, CORE-359: Core spec 0.35, §10.7 Installer service.
    CORE-368: Core spec 0.35, §10.7 Installer service.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["BUILD_USER", "ENGINE_USER", "INSTALLER_USER", "Layout"]

INSTALLER_USER = "mm3-install"
"""The installer's own user (CORE-130)."""
BUILD_USER = "mm3-build"
"""The user package code runs as, without capabilities (CORE-367)."""
ENGINE_USER = "mm3"
"""The engine's user; owner of the files the installer writes for it (CORE-359)."""


@dataclass(frozen=True)
class Layout:
    """The installer's paths.

    Args:
        opt: ``/opt/mm3``: holds ``venvs/``, the ``venv`` symlink and ``installer/``.
        state: The installer's state directory ``/var/lib/mm3-install``.
        engine_state: The engine's state directory ``/var/lib/mm3``.
        runtime: The installer's runtime directory ``/run/mm3-install``.
        engine_runtime: The engine's runtime directory ``/run/mm3`` (admin socket).
        tmp: Downloads and build homes, in the unit's private ``/tmp`` (CORE-367).
    """

    opt: Path = Path("/opt/mm3")
    state: Path = Path("/var/lib/mm3-install")
    engine_state: Path = Path("/var/lib/mm3")
    runtime: Path = Path("/run/mm3-install")
    engine_runtime: Path = Path("/run/mm3")
    tmp: Path = Path("/tmp/mm3-installer")  # noqa: S108 - PrivateTmp=yes (CORE-359)

    @classmethod
    def under(cls, root: Path) -> Layout:
        """A layout with every directory below ``root`` (for tests)."""
        return cls(
            opt=root / "opt",
            state=root / "state",
            engine_state=root / "engine-state",
            runtime=root / "run",
            engine_runtime=root / "engine-run",
            tmp=root / "tmp",
        )

    @property
    def venvs(self) -> Path:
        """Staged venvs, one per build (CORE-134)."""
        return self.opt / "venvs"

    @property
    def venv_link(self) -> Path:
        """The symlink the engine runs from (CORE-134)."""
        return self.opt / "venv"

    @property
    def socket(self) -> Path:
        """The installer socket (CORE-355)."""
        return self.runtime / "installer.sock"

    @property
    def admin_socket(self) -> Path:
        """The engine's admin socket (CORE-311)."""
        return self.engine_runtime / "admin.sock"

    @property
    def catalog(self) -> Path:
        """The installer's authoritative index copy (CORE-351)."""
        return self.state / "catalog"

    @property
    def engine_catalog(self) -> Path:
        """The engine's index copy (CORE-351)."""
        return self.engine_state / "catalog"

    @property
    def operations(self) -> Path:
        """Kept operations and their events (CORE-355)."""
        return self.state / "operations"

    @property
    def wheels(self) -> Path:
        """Wheels of installed modules, kept to rebuild venvs (CORE-354)."""
        return self.state / "wheels"

    @property
    def modules_file(self) -> Path:
        """Installed modules and their origin (CORE-355)."""
        return self.state / "modules.json"

    @property
    def auth_file(self) -> Path:
        """Passphrase hash and TOTP secret (CORE-369)."""
        return self.state / "auth.json"

    @property
    def lock_file(self) -> Path:
        """Failed re-authentications and the lock (CORE-132)."""
        return self.state / "lock.json"
