"""The engine's link to the installer service (CORE-356, CORE-382).

The installer holds the only rights to restart the engine or the kiosk and to
reboot (CORE-359), so the engine forwards those requests to its socket and
turns the installer's answers into the codes of CORE-298. ``GET
/api/v1/installer`` reports whether the installer can be reached and whether
it is busy, which the admin shows as disabled buttons.

Implements:
    CORE-382: Core spec 0.42, §9.1 Remote-control API.
    CORE-356: Core spec 0.33, §10.7 Installer service.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mm3.core.web.problems import ApiError
from mm3.installer.client import DEFAULT_SOCKET, InstallerClient, InstallerUnavailable

__all__ = ["SYSTEM_ACTIONS", "InstallerLink"]

SYSTEM_ACTIONS = ("restart-engine", "restart-kiosk", "reboot")
"""The routes ``POST /api/v1/system/<action>`` (CORE-382)."""


class InstallerLink:
    """Asks the installer and forwards system actions to it.

    Args:
        client: The installer socket client; the default socket when ``None``.
    """

    def __init__(self, client: InstallerClient | None = None) -> None:
        self._client = client or InstallerClient(DEFAULT_SOCKET, timeout=10.0)

    async def status(self) -> dict[str, bool]:
        """Return ``{available, busy}`` for ``GET /api/v1/installer`` (CORE-356, CORE-383).

        ``available`` is false when the socket does not answer; ``busy`` is true while
        an operation, a restore or a system action runs or waits (CORE-382).
        """
        try:
            reply = await asyncio.to_thread(self._client.request, "GET", "/v1/status")
        except InstallerUnavailable:
            return {"available": False, "busy": False}
        if reply.status != 200 or not isinstance(reply.body, dict):
            return {"available": False, "busy": False}
        return {"available": True, "busy": bool(reply.body.get("busy"))}

    async def system(self, action: str, client: str) -> dict[str, Any]:
        """Forward ``action`` to ``POST /v1/system/<action>`` (CORE-382).

        Args:
            action: One of :data:`SYSTEM_ACTIONS`.
            client: The caller as the installer logs it: token or session name and address.

        Returns:
            ``{action, requested_at}`` once the installer has accepted it.

        Raises:
            ApiError: 409 ``CORE_API_INSTALLER_BUSY``, 409 ``CORE_API_NO_KIOSK`` or 503
                ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        try:
            reply = await asyncio.to_thread(
                self._client.request, "POST", f"/v1/system/{action}", {"client": client}
            )
        except InstallerUnavailable as exc:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", str(exc)) from exc
        body = reply.body if isinstance(reply.body, dict) else {}
        if reply.status == 202:
            return {"action": body.get("action", action), "requested_at": body.get("requested_at")}
        if reply.status == 409:
            if body.get("code") == "no_kiosk":
                raise ApiError("CORE_API_NO_KIOSK", reply.error or "No kiosk unit is installed")
            raise ApiError("CORE_API_INSTALLER_BUSY", reply.error or "The installer is busy")
        raise ApiError(
            "CORE_API_INSTALLER_UNAVAILABLE",
            f"The installer answered {reply.status}: {reply.error}".rstrip(": "),
        )
