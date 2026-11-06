"""Assembling the installer service from its production parts.

Implements:
    CORE-130: Core spec 0.33, §10.7 Installer service.
    CORE-367, CORE-368: Core spec 0.35, §10.7 Installer service.
"""

from __future__ import annotations

import os

from mm3.installer.auth import Auth
from mm3.installer.fetch import UrllibFetcher
from mm3.installer.files import owner_of
from mm3.installer.operations import Installer
from mm3.installer.paths import BUILD_USER, ENGINE_USER, Layout
from mm3.installer.runner import SetprivRunner
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
    return InstallerServer(installer, Auth(layout), socket_gid=group_id())
