"""The engine's link to the installer service (CORE-356, CORE-382).

The installer holds the only rights to install, update and remove modules, to restart the
engine or the kiosk and to reboot (CORE-359), so the engine forwards those requests to its
socket and turns the installer's answers into the codes of CORE-298. ``GET /api/v1/installer``
reports whether the installer can be reached, whether it is busy and until when it is locked;
the admin shows that as disabled buttons.

The passphrase and the TOTP code of an operation pass through unchanged: they are never
logged, stored or put in an answer, a frame or an error detail (CORE-356).

Implements:
    CORE-382: Core spec 0.42, §9.1 Remote-control API.
    CORE-356: Core spec 0.44, §10.7 Installer service.
    CORE-358: Core spec 0.33, §10.7 Installer service.
    CORE-361: Core spec 0.45, §10.8 Backup and restore.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mm3.core.catalog import CatalogError, CatalogMeta
from mm3.core.web.problems import ApiError
from mm3.installer.client import (
    DEFAULT_SOCKET,
    InstallerClient,
    InstallerUnavailable,
    Reply,
    Stream,
)

__all__ = [
    "DOWNLOAD_CHUNK",
    "FINAL_STATES",
    "MAX_UPLOAD",
    "OPERATION_ACTIONS",
    "OPERATION_FIELDS",
    "OPERATION_ID",
    "SYSTEM_ACTIONS",
    "UNAVAILABLE",
    "InstallerLink",
    "Snapshot",
    "catalog_object",
    "parse_meta",
    "project_operation",
]

SYSTEM_ACTIONS = ("restart-engine", "restart-kiosk", "reboot")
"""The routes ``POST /api/v1/system/<action>`` (CORE-382)."""

OPERATION_ACTIONS = ("install", "update", "remove", "restore_previous")
"""The actions of ``POST /api/v1/installer/operations`` (CORE-356)."""

OPERATION_FIELDS = (
    "id",
    "action",
    "name",
    "from",
    "to",
    "state",
    "steps",
    "error",
    "client",
    "created_at",
    "finished_at",
)
"""What an operation shows: the installer's ``source`` and ``peer_uid`` are left out."""

OPERATION_ID = re.compile(r"^[0-9a-f]{16}$")
"""The form of an installer operation id."""

FINAL_STATES = ("done", "rolled_back", "failed")
"""States an operation does not leave (CORE-355)."""

MAX_UPLOAD = 2 * 1024**3
"""The largest backup upload, 2 GiB (CORE-361)."""

DOWNLOAD_CHUNK = 1 << 20
"""Bytes of an archive read from the installer at a time."""

SLOW_TIMEOUT = 120.0
"""Seconds a catalog refresh may take: it downloads and verifies the index (CORE-351)."""


@dataclass(frozen=True)
class Snapshot:
    """One reading of the installer's ``GET /v1/status`` (CORE-355).

    Attributes:
        available: False when the socket does not answer or answers an error.
        busy: An operation, a restore or a system action runs or waits (CORE-382).
        locked_until: End of a lock after failed re-authentication, ISO 8601, or ``None``.
        installed: ``[{name, version, origin}]`` of the installed modules; empty when
            the installer is not available.
    """

    available: bool
    busy: bool = False
    locked_until: str | None = None
    installed: list[dict[str, str]] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        """The object of ``GET /api/v1/installer``: ``{available, busy, locked_until}``."""
        return {"available": self.available, "busy": self.busy, "locked_until": self.locked_until}


UNAVAILABLE = Snapshot(available=False)


def project_operation(raw: dict[str, Any]) -> dict[str, Any]:
    """Return an installer operation as the engine shows it (CORE-356).

    Args:
        raw: The installer's operation object (CORE-355).

    Returns:
        The fields of :data:`OPERATION_FIELDS`; ``source``, ``peer_uid``, and anything the
        installer may add later are not passed on.
    """
    return {key: raw.get(key) for key in OPERATION_FIELDS}


def catalog_object(meta: CatalogMeta, now: datetime) -> dict[str, Any]:
    """Return ``{serial, fetched_at, expires, stale}`` (CORE-356).

    Args:
        meta: The serial and times of the index copy.
        now: The current time.

    Returns:
        ``stale`` is true once ``expires`` has passed (CORE-351).
    """
    return {
        "serial": meta.serial,
        "fetched_at": meta.fetched_at.isoformat(),
        "expires": meta.expires.isoformat(),
        "stale": meta.expires <= now,
    }


class InstallerLink:
    """Asks the installer and forwards operations and system actions to it.

    Args:
        client: The installer socket client; the default socket (10 s per request) when ``None``.
    """

    def __init__(self, client: InstallerClient | None = None) -> None:
        self._client = client or InstallerClient(DEFAULT_SOCKET, timeout=10.0)

    # --- reading ----------------------------------------------------------------------

    def read(self) -> Snapshot:
        """Read ``GET /v1/status`` (blocking; use :meth:`snapshot` from the event loop).

        Returns:
            What the installer reports, or :data:`UNAVAILABLE` when it does not answer.
        """
        try:
            reply = self._client.request("GET", "/v1/status")
        except InstallerUnavailable:
            return UNAVAILABLE
        body = reply.body
        if reply.status != 200 or not isinstance(body, dict):
            return UNAVAILABLE
        until = body.get("locked_until")
        installed = body.get("installed")
        return Snapshot(
            available=True,
            busy=bool(body.get("busy")),
            locked_until=until if isinstance(until, str) else None,
            installed=[i for i in installed if isinstance(i, dict)]
            if isinstance(installed, list)
            else [],
        )

    async def snapshot(self) -> Snapshot:
        """Read the installer's status without blocking the loop (CORE-356)."""
        return await asyncio.to_thread(self.read)

    async def status(self) -> dict[str, Any]:
        """Return ``{available, busy, locked_until}`` for ``GET /api/v1/installer`` (CORE-356).

        ``available`` is false when the socket does not answer within 10 s (then ``busy`` is
        false and ``locked_until`` null); ``busy`` is true while an operation, a restore or a
        system action runs or waits (CORE-382).
        """
        return (await self.snapshot()).status()

    async def operations(self) -> list[dict[str, Any]]:
        """Return the installer's last operations, newest first (CORE-356).

        Raises:
            ApiError: 503 ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        reply = await self._call("GET", "/v1/operations")
        body = reply.body if isinstance(reply.body, dict) else {}
        raw = body.get("operations")
        ops = (
            [project_operation(o) for o in raw if isinstance(o, dict)]
            if isinstance(raw, list)
            else []
        )
        ops.sort(key=lambda o: str(o["created_at"]), reverse=True)
        return ops

    async def operation(self, op_id: str) -> dict[str, Any]:
        """Return one operation (CORE-356).

        Raises:
            ApiError: 404 ``CORE_API_NOT_FOUND`` when the installer does not know it, or 503
                ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        if not OPERATION_ID.match(op_id):
            raise ApiError("CORE_API_NOT_FOUND", "Unknown operation")
        reply = await self._call("GET", f"/v1/operations/{op_id}", accept=(404,))
        if reply.status == 404 or not isinstance(reply.body, dict):
            raise ApiError("CORE_API_NOT_FOUND", "Unknown operation")
        return project_operation(reply.body)

    # --- acting -----------------------------------------------------------------------

    async def refresh(self) -> dict[str, Any]:
        """Ask the installer to download and verify the index now (CORE-351).

        Returns:
            The installer's ``catalog`` (``{serial, fetched_at, expires}``).

        Raises:
            ApiError: 503 ``CORE_API_CATALOG_UNAVAILABLE`` with the reason when the index
                cannot be fetched or verified, or 503 ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        reply = await self._call("POST", "/v1/catalog/refresh", seconds=SLOW_TIMEOUT, accept=(502,))
        body = reply.body if isinstance(reply.body, dict) else {}
        if reply.status == 502:
            raise ApiError("CORE_API_CATALOG_UNAVAILABLE", reply.error or "The index was refused")
        catalog = body.get("catalog")
        if not isinstance(catalog, dict):
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "The installer sent no catalog")
        return catalog

    async def require_free(self) -> None:
        """Refuse when the installer cannot take an operation now (CORE-358).

        A removal saves a configuration version before it is forwarded; when the installer is
        unreachable, busy or locked the request would be refused anyway, so the engine says so
        first and writes nothing. A refusal that only the installer can give (a wrong
        passphrase or code) still comes after the version is saved.

        Raises:
            ApiError: 503 ``CORE_API_INSTALLER_UNAVAILABLE``, 423 ``CORE_API_INSTALLER_LOCKED``
                (with ``locked_until``) or 409 ``CORE_API_INSTALLER_BUSY``.

        Implements:
            CORE-358: Core spec 0.33, §10.7 Installer service.
        """
        snap = await self.snapshot()
        if not snap.available:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "The installer does not answer")
        if snap.locked_until:
            raise ApiError(
                "CORE_API_INSTALLER_LOCKED",
                "The installer is locked after failed re-authentication",
                extra={"locked_until": snap.locked_until},
            )
        if snap.busy:
            raise ApiError("CORE_API_INSTALLER_BUSY", "The installer is busy")

    async def submit(self, body: dict[str, Any], client: str) -> str:
        """Forward an operation to ``POST /v1/operations`` (CORE-356).

        Args:
            body: ``{action, name?, version?, passphrase, totp?}``, already checked.
            client: The caller as the installer logs it (CORE-382).

        Returns:
            The operation id.

        Raises:
            ApiError: ``CORE_API_REAUTH_FAILED``, ``CORE_API_INSTALLER_LOCKED``,
                ``CORE_API_INSTALLER_BUSY``, ``CORE_API_INVALID_REQUEST`` or
                ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        reply = await self._call("POST", "/v1/operations", {**body, "client": client})
        op_id = reply.body.get("id") if isinstance(reply.body, dict) else None
        if reply.status != 202 or not isinstance(op_id, str):
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "The installer sent no operation id")
        return op_id

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
        reply = await self._call("POST", f"/v1/system/{action}", {"client": client}, accept=(409,))
        body = reply.body if isinstance(reply.body, dict) else {}
        if reply.status == 202:
            return {"action": body.get("action", action), "requested_at": body.get("requested_at")}
        if body.get("code") == "no_kiosk":
            raise ApiError("CORE_API_NO_KIOSK", reply.error or "No kiosk unit is installed")
        raise await self._problem(reply)

    # --- backups (CORE-361) -----------------------------------------------------------

    async def backup(self, method: str, target: str, body: dict[str, Any] | None = None) -> Any:
        """Forward a backup request and return the installer's JSON answer (CORE-361).

        Args:
            method: ``GET``, ``POST``, ``PUT`` or ``DELETE``.
            target: The installer path, ``/v1/backups...``.
            body: The JSON body, already checked; ``None`` for no body.

        Returns:
            The decoded body, ``None`` for a 204.

        Raises:
            ApiError: The installer's refusal as in CORE-356, 404 ``CORE_API_NOT_FOUND`` for
                an unknown archive or remote, or 503 ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        reply = await self._call(method, target, body, seconds=SLOW_TIMEOUT, accept=(404, 413))
        if reply.status in (404, 413):
            raise await self._backup_problem(reply)
        return None if reply.status == 204 else reply.body

    async def _backup_problem(self, reply: Reply) -> ApiError:
        """Map an installer answer to a problem, with the 404 and 413 of the backup routes."""
        if reply.status == 404:
            return ApiError("CORE_API_NOT_FOUND", reply.error or "The installer has no such item")
        if reply.status == 413:
            return ApiError("CORE_API_UPLOAD_TOO_LARGE", reply.error or "The upload is too large")
        return await self._problem(reply)

    async def upload(self, chunks: AsyncIterator[bytes], length: int) -> dict[str, Any]:
        """Stream a backup archive to ``POST /v1/backups/upload`` (CORE-361).

        Args:
            chunks: The request body as it arrives; read once.
            length: Its size from ``Content-Length``, at most :data:`MAX_UPLOAD`.

        Returns:
            ``{upload, size}``.

        Raises:
            ApiError: 413 ``CORE_API_UPLOAD_TOO_LARGE`` or 503 ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        if length > MAX_UPLOAD:
            raise ApiError("CORE_API_UPLOAD_TOO_LARGE", "A backup upload is at most 2 GiB")
        pipe: queue.Queue[bytes | None] = queue.Queue(maxsize=8)

        def pieces() -> Iterator[bytes]:
            while (item := pipe.get()) is not None:
                yield item

        sender = asyncio.create_task(
            asyncio.to_thread(self._client.upload, "/v1/backups/upload", pieces(), length)
        )
        try:
            async for chunk in chunks:
                await self._feed(pipe, chunk, sender)
                if sender.done():
                    break
            else:
                await self._feed(pipe, None, sender)
        except BaseException:
            sender.cancel()
            self._end(pipe)
            raise
        try:
            reply = await sender
        except InstallerUnavailable as exc:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", str(exc)) from exc
        if reply.status != 201 or not isinstance(reply.body, dict):
            raise await self._backup_problem(reply)
        return {"upload": reply.body.get("upload"), "size": reply.body.get("size")}

    @staticmethod
    async def _feed(
        pipe: queue.Queue[bytes | None], item: bytes | None, sender: asyncio.Task[Reply]
    ) -> None:
        """Put ``item`` on the pipe without blocking the loop; give up when the sender ended."""
        while not sender.done():
            try:
                pipe.put_nowait(item)
                return
            except queue.Full:
                await asyncio.sleep(0.01)

    @staticmethod
    def _end(pipe: queue.Queue[bytes | None]) -> None:
        """Unblock the sending thread after an abort."""
        with contextlib.suppress(queue.Full):
            pipe.put_nowait(None)

    async def download(self, name: str) -> Stream:
        """Open ``GET /v1/backups/{name}`` for streaming (CORE-361).

        Returns:
            The open answer; the caller reads it with ``Stream.read`` and closes it.

        Raises:
            ApiError: 404 ``CORE_API_NOT_FOUND`` or 503 ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        try:
            stream = await asyncio.to_thread(self._client.stream, "GET", f"/v1/backups/{name}")
        except InstallerUnavailable as exc:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", str(exc)) from exc
        if stream.status != 200:
            raise await self._backup_problem(Reply(stream.status, stream.body))
        return stream

    # --- the installer's answers ------------------------------------------------------

    async def _call(
        self,
        method: str,
        target: str,
        body: dict[str, Any] | None = None,
        *,
        seconds: float | None = None,
        accept: tuple[int, ...] = (),
    ) -> Reply:
        """Send a request and raise the problem for any answer but 2xx or ``accept``."""
        try:
            if seconds is None:
                reply = await asyncio.to_thread(self._client.request, method, target, body)
            else:
                reply = await asyncio.to_thread(
                    lambda: self._client.request(method, target, body, timeout=seconds)
                )
        except InstallerUnavailable as exc:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", str(exc)) from exc
        if 200 <= reply.status < 300 or reply.status in accept:
            return reply
        raise await self._problem(reply)

    async def _problem(self, reply: Reply) -> ApiError:
        """Map an installer answer to a problem (CORE-356).

        403 is ``CORE_API_REAUTH_FAILED`` and never says whether the passphrase or the code was
        wrong; 423 is ``CORE_API_INSTALLER_LOCKED`` with ``locked_until``; 409 is
        ``CORE_API_INSTALLER_BUSY``; 400 is 422 ``CORE_API_INVALID_REQUEST`` with the installer's
        message; every other answer is ``CORE_API_INSTALLER_UNAVAILABLE``.
        """
        status = reply.status
        if status == 403:
            return ApiError("CORE_API_REAUTH_FAILED", "Re-authentication failed")
        if status == 423:
            until = (await self.snapshot()).locked_until
            return ApiError(
                "CORE_API_INSTALLER_LOCKED",
                "The installer is locked after failed re-authentication",
                extra={"locked_until": until},
            )
        if status == 409:
            return ApiError("CORE_API_INSTALLER_BUSY", reply.error or "The installer is busy")
        if status == 400:
            return ApiError("CORE_API_INVALID_REQUEST", reply.error or "The installer refused it")

        return ApiError(
            "CORE_API_INSTALLER_UNAVAILABLE",
            f"The installer answered {status}: {reply.error}".rstrip(": "),
        )


def parse_meta(raw: dict[str, Any]) -> CatalogMeta:
    """Read the installer's ``catalog`` object.

    Raises:
        ApiError: 503 ``CORE_API_INSTALLER_UNAVAILABLE`` when it is malformed.
    """
    try:
        return CatalogMeta.from_json(raw)
    except CatalogError as exc:
        raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", str(exc)) from exc
