"""Assembling the installer service from its production parts.

Implements:
    CORE-130: Core spec 0.33, §10.7 Installer service.
    CORE-367: Core spec 0.37, §10.7 Installer service.
    CORE-368: Core spec 0.35, §10.7 Installer service.
    CORE-360, CORE-365: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import os
import threading

from mm3.installer.age import Age
from mm3.installer.auth import Auth
from mm3.installer.backup import BackupService
from mm3.installer.backupops import BackupRunner
from mm3.installer.fetch import UrllibFetcher
from mm3.installer.files import owner_of
from mm3.installer.operations import BusyError, Installer, Request
from mm3.installer.paths import BUILD_USER, ENGINE_USER, Layout
from mm3.installer.remotes import Remotes, SubprocessRclone
from mm3.installer.restore import Restorer
from mm3.installer.runner import SetprivRunner
from mm3.installer.schedule import AdminSettings, BackupScheduler, StatusFile
from mm3.installer.server import InstallerServer, group_id
from mm3.installer.service import SystemdEngine
from mm3.installer.store import CatalogStore
from mm3.installer.venv import VenvBuilder
from mm3.installer.verify import SigstoreVerification

__all__ = ["build_server"]


def build_server(layout: Layout | None = None) -> InstallerServer:
    """The installer with ``setpriv`` children, ``systemctl`` and Sigstore.

    Raises:
        KeyError: The users ``mm3`` or ``mm3-build`` do not exist
            (``mm3-install.sh`` creates them, CORE-368).
    """
    layout = layout or Layout()
    for path in (layout.state, layout.tmp):
        path.mkdir(mode=0o700 if path == layout.state else 0o755, parents=True, exist_ok=True)
    os.chmod(layout.tmp, 0o755)  # noqa: S103 - build homes and work copies are below it
    fetcher = UrllibFetcher()
    verification = SigstoreVerification()
    runner = SetprivRunner()
    me = (os.getuid(), os.getgid())
    store = CatalogStore(layout, fetcher, verification, engine_owner=owner_of(ENGINE_USER))
    builder = VenvBuilder(layout, runner, build_owner=owner_of(BUILD_USER), final_owner=me)
    installer = Installer(
        layout, store, fetcher, verification, builder, SystemdEngine(layout, runner)
    )
    engine_owner = owner_of(ENGINE_USER)
    service = BackupService(layout, Age(), lambda: installer.installed(store.kept()))
    reader = AdminSettings(layout)
    runner_ = BackupRunner(
        layout,
        service,
        Restorer(layout, engine_owner),
        Remotes(layout, SubprocessRclone(layout)),
        reader,
        StatusFile(layout),
    )
    installer.backups = runner_

    def submit_scheduled() -> bool:
        """Start the scheduled backup in the background; False while another operation runs."""
        if installer.busy:
            return False
        request = Request(action="backup", client="scheduler", options={"scheduled": True})
        try:
            op = installer.submit(request)
        except BusyError:
            return False
        threading.Thread(target=installer.run, args=(op, request), daemon=True).start()
        return True

    scheduler = BackupScheduler(reader, runner_.last_scheduled, submit_scheduled)
    return InstallerServer(installer, Auth(layout), socket_gid=group_id(), scheduler=scheduler)
