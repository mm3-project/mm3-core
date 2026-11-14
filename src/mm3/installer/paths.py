"""Where the installer reads and writes.

Every path the installer touches comes from one :class:`Layout`, so tests run
the whole service inside a temporary directory. The defaults are the paths of
CORE-351, CORE-355, CORE-359 and CORE-368.

Implements:
    CORE-355: Core spec 0.35, §10.7 Installer service.
    CORE-359: Core spec 0.42, §10.8 Backup and restore.
    CORE-368: Core spec 0.35, §10.7 Installer service.
    CORE-360, CORE-362, CORE-364, CORE-365, CORE-366: Core spec 0.37, §10.8 Backup and restore.
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
        engine_config: The engine's configuration directory ``/etc/mm3``.
    """

    opt: Path = Path("/opt/mm3")
    state: Path = Path("/var/lib/mm3-install")
    engine_state: Path = Path("/var/lib/mm3")
    runtime: Path = Path("/run/mm3-install")
    engine_runtime: Path = Path("/run/mm3")
    tmp: Path = Path("/tmp/mm3-installer")  # noqa: S108 - PrivateTmp=yes (CORE-359)
    engine_config: Path = Path("/etc/mm3")

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
            engine_config=root / "engine-config",
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

    @property
    def env_file(self) -> Path:
        """The engine's env file, a secret of the backup (CORE-200, CORE-362)."""
        return self.engine_config / "mm3.env"

    @property
    def tokens_file(self) -> Path:
        """The engine's token store, a secret of the backup (CORE-62, CORE-362)."""
        return self.engine_state / "tokens.json"

    @property
    def history_dir(self) -> Path:
        """The engine's configuration history (CORE-153)."""
        return self.engine_state / "config-history"

    @property
    def instance_data(self) -> Path:
        """Per-instance data directories ``<instance>/`` (core spec 3.1)."""
        return self.engine_state / "modules"

    @property
    def instance_kv(self) -> Path:
        """Per-instance key-value files ``<instance>.json`` (core spec 3.1)."""
        return self.engine_state / "kv"

    @property
    def backups(self) -> Path:
        """Local backups, scheduled and manual (CORE-360, CORE-362)."""
        return self.engine_state / "backups"

    @property
    def pre_backups(self) -> Path:
        """Pre-operation backups, the last 3 (CORE-366)."""
        return self.backups / "pre"

    @property
    def backup_key(self) -> Path:
        """The age identity kept on request (CORE-364)."""
        return self.state / "backup.key"

    @property
    def rclone_conf(self) -> Path:
        """The rclone remotes (CORE-365)."""
        return self.state / "rclone.conf"

    @property
    def backup_status(self) -> Path:
        """Last scheduled run and remote results (CORE-360, CORE-365)."""
        return self.state / "backup.json"

    @property
    def restore_marker(self) -> Path:
        """Tells the engine that the next config version is a restore (CORE-363)."""
        return self.engine_state / "restore.json"
