"""The backup routes of the admin REST API (CORE-361).

The engine forwards every backup route to the installer, which alone reads and writes the
archives, the age keys and the rclone remotes (CORE-359, CORE-360). The routes exist only on the
admin listener: the CLI reaches the installer on its own socket and the admin socket has no token
to name a caller by (CORE-311).

An archive is uploaded as a raw body, streamed to the installer as it arrives and never held in
memory; a download is streamed the other way. A restore names its source as ``{name, remote?}``
or ``{upload}``; a ``path`` is refused here, it is for a root peer of the installer socket only.
Passphrases and TOTP codes are passed on unchanged and are never logged, stored or sent back;
nothing here logs a request body.

Implements:
    CORE-361: Core spec 0.45, §10.8 Backup and restore.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mm3.core.logging import get_logger
from mm3.core.web.installer import DOWNLOAD_CHUNK, MAX_UPLOAD, InstallerLink
from mm3.core.web.problems import ApiError

__all__ = [
    "ARCHIVE_NAME",
    "BackupBody",
    "RemoteBody",
    "RestoreBody",
    "Source",
    "SourceBody",
    "register_backup_routes",
]

_log = get_logger(subsystem="api")

ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.mm3backup$")
"""A backup archive's file name, as the installer accepts it."""

REMOTE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
"""An rclone remote's name, as the installer accepts it."""

MAX_SECRET = 1024
"""The longest passphrase, key or TOTP code the engine passes on."""

Part = Literal["config", "data", "modules", "secrets"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackupBody(_Body):
    """``POST /api/v1/backups`` (CORE-361).

    ``pre`` is refused: a pre-operation backup is made by the installer itself (CORE-364), the
    engine only ever asks for a manual one.
    """

    parts: list[Part] | None = Field(default=None, min_length=1, max_length=4)
    recipient: str | None = Field(default=None, max_length=100)
    archive_passphrase: str | None = Field(default=None, max_length=MAX_SECRET, repr=False)
    use_configured_key: bool = False

    @model_validator(mode="after")
    def _one_way(self) -> BackupBody:
        if len(set(self.parts or ())) != len(self.parts or ()):
            raise ValueError("parts must not repeat")
        if self.recipient and self.archive_passphrase:
            raise ValueError("encrypt to a key or with a passphrase, not both")
        return self


class Source(_Body):
    """The archive a verify, preview or restore reads: ``{name, remote?}`` or ``{upload}``.

    There is no ``path`` here: a file by path is for a root peer of the installer socket only,
    so ``path`` is refused like any unknown key (CORE-361).
    """

    name: str | None = Field(default=None, max_length=256)
    remote: bool = False
    upload: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def _one(self) -> Source:
        if (self.name is None) == (self.upload is None):
            raise ValueError("give either name or upload")
        if self.upload is not None and self.remote:
            raise ValueError("an upload is not on a remote")
        return self


class SourceBody(_Body):
    """``POST /api/v1/backups/verify`` and ``/preview`` (CORE-361)."""

    source: Source
    key: str | None = Field(default=None, max_length=MAX_SECRET, repr=False)
    parts: list[Part] | None = Field(default=None, min_length=1, max_length=4)
    instance: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _parts(self) -> SourceBody:
        if len(set(self.parts or ())) != len(self.parts or ()):
            raise ValueError("parts must not repeat")
        return self


class RestoreBody(SourceBody):
    """``POST /api/v1/backups/restore`` (CORE-361): a source and a re-authentication."""

    passphrase: str = Field(min_length=1, max_length=MAX_SECRET, repr=False)
    totp: str | None = Field(default=None, max_length=MAX_SECRET, repr=False)


class RemoteBody(_Body):
    """``PUT /api/v1/backups/remotes/{name}`` (CORE-361): the rclone type and its parameters."""

    type: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(max_length=100)
    passphrase: str = Field(min_length=1, max_length=MAX_SECRET, repr=False)
    totp: str | None = Field(default=None, max_length=MAX_SECRET, repr=False)


class _Reauth(_Body):
    passphrase: str = Field(min_length=1, max_length=MAX_SECRET, repr=False)
    totp: str | None = Field(default=None, max_length=MAX_SECRET, repr=False)


class _KeygenBody(_Body):
    keep: bool = False


class _TestBody(_Body):
    path: str | None = Field(default=None, max_length=1024)


def register_backup_routes(
    router: APIRouter, installer: Callable[[], InstallerLink | None]
) -> None:
    """Add the routes of CORE-361 to the ``/api/v1`` router.

    Only called for the admin listener; the admin socket serves none of them.

    Args:
        router: The router of the REST API.
        installer: The link to the installer, or ``None`` on a system without it.
    """

    def link() -> InstallerLink:
        found = installer()
        if found is None:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "No installer is configured")
        return found

    def operation(op_id: str) -> Response:
        return Response(
            json.dumps({"id": op_id}),
            status_code=202,
            media_type="application/json",
            headers={"Location": f"/api/v1/installer/operations/{op_id}"},
        )

    def remote_name(name: str) -> str:
        if not REMOTE_NAME.match(name):
            raise ApiError("CORE_API_NOT_FOUND", "No such remote")
        return name

    def source(body: SourceBody) -> dict[str, Any]:
        found = body.source
        out: dict[str, Any] = (
            {"upload": found.upload}
            if found.upload
            else {"name": found.name, "remote": found.remote}
        )
        for key in ("key", "parts", "instance"):
            if getattr(body, key) is not None:
                out[key] = getattr(body, key)
        return out

    # Fixed paths first: ``/backups/{name}`` would otherwise take ``remote`` and ``remotes``.

    @router.get("/backups")
    async def list_backups() -> Any:
        """``{backups, status}`` of the installer's archives (CORE-361)."""
        return await link().backup("GET", "/v1/backups")

    @router.get("/backups/remote")
    async def list_remote_backups() -> Any:
        """The archives on the configured remote (CORE-361)."""
        return await link().backup("GET", "/v1/backups/remote")

    @router.post("/backups", status_code=202, response_model=None)
    async def create_backup(body: BackupBody, request: Request) -> Response:
        """Start a manual backup; 202 with the operation id (CORE-361)."""
        client = str(request.state.caller)
        sent = body.model_dump(exclude_none=True)
        answer = await link().backup("POST", "/v1/backups", {**sent, "client": client})
        op_id = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(op_id, str):
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "The installer sent no operation id")
        _log.warning("api_backup", id=op_id, client=client, parts=body.parts)
        return operation(op_id)

    @router.post("/backups/upload", status_code=201)
    async def upload_backup(request: Request) -> dict[str, Any]:
        """Stage an uploaded archive for a restore; the body is the raw archive (CORE-361)."""
        raw = request.headers.get("content-length", "")
        if not raw.isdigit() or int(raw) == 0:
            raise ApiError("CORE_API_INVALID_REQUEST", "Send the archive with a Content-Length")
        length = int(raw)
        if length > MAX_UPLOAD:
            raise ApiError("CORE_API_UPLOAD_TOO_LARGE", "A backup upload is at most 2 GiB")
        staged = await link().upload(request.stream(), length)
        _log.warning("api_backup_upload", client=str(request.state.caller), size=staged["size"])
        return staged

    @router.post("/backups/verify")
    async def verify_backup(body: SourceBody) -> Any:
        """Check an archive's integrity and what it holds (CORE-361)."""
        return await link().backup("POST", "/v1/backups/verify", source(body))

    @router.post("/backups/preview")
    async def preview_backup(body: SourceBody) -> Any:
        """Show what a restore would change (CORE-361)."""
        return await link().backup("POST", "/v1/backups/preview", source(body))

    @router.post("/backups/keygen")
    async def keygen(body: _KeygenBody, request: Request) -> Any:
        """Make an age key pair, kept as the configured key when ``keep`` (CORE-361)."""
        made = await link().backup("POST", "/v1/backups/keygen", {"keep": body.keep})
        _log.warning("api_backup_keygen", client=str(request.state.caller), kept=body.keep)
        return made

    @router.post("/backups/restore", status_code=202, response_model=None)
    async def restore_backup(body: RestoreBody, request: Request) -> Response:
        """Start a restore after re-authentication; 202 with the operation id (CORE-361)."""
        client = str(request.state.caller)
        sent = {
            **source(body),
            "passphrase": body.passphrase,
            "totp": body.totp,
            "client": client,
        }
        answer = await link().backup(
            "POST", "/v1/backups/restore", {k: v for k, v in sent.items() if v is not None}
        )
        op_id = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(op_id, str):
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "The installer sent no operation id")
        _log.warning("api_backup_restore", id=op_id, client=client, parts=body.parts)
        return operation(op_id)

    @router.get("/backups/remotes")
    async def list_remotes() -> Any:
        """The configured rclone remotes, secrets masked by the installer (CORE-361)."""
        return await link().backup("GET", "/v1/backups/remotes")

    @router.get("/backups/remotes/{name}")
    async def get_remote(name: str) -> Any:
        """One remote (CORE-361)."""
        return await link().backup("GET", f"/v1/backups/remotes/{remote_name(name)}")

    @router.put("/backups/remotes/{name}", status_code=204)
    async def put_remote(name: str, body: RemoteBody, request: Request) -> Response:
        """Create or replace a remote after re-authentication (CORE-361)."""
        sent = {k: v for k, v in body.model_dump().items() if v is not None}
        await link().backup("PUT", f"/v1/backups/remotes/{remote_name(name)}", sent)
        _log.warning("api_backup_remote_put", name=name, client=str(request.state.caller))
        return Response(status_code=204)

    @router.delete("/backups/remotes/{name}", status_code=204)
    async def delete_remote(name: str, body: _Reauth, request: Request) -> Response:
        """Remove a remote after re-authentication (CORE-361)."""
        sent = {k: v for k, v in body.model_dump().items() if v is not None}
        await link().backup("DELETE", f"/v1/backups/remotes/{remote_name(name)}", sent)
        _log.warning("api_backup_remote_delete", name=name, client=str(request.state.caller))
        return Response(status_code=204)

    @router.post("/backups/remotes/{name}/test", status_code=204)
    async def test_remote(name: str, body: _TestBody) -> Response:
        """Check that a remote answers; no re-authentication (CORE-361)."""
        await link().backup(
            "POST",
            f"/v1/backups/remotes/{remote_name(name)}/test",
            {"path": body.path} if body.path else {},
        )
        return Response(status_code=204)

    @router.get("/backups/{name}", response_model=None)
    async def download_backup(name: str) -> StreamingResponse:
        """Stream an archive; a download needs no re-authentication (CORE-361)."""
        if not ARCHIVE_NAME.match(name):
            raise ApiError("CORE_API_NOT_FOUND", "No such backup")
        stream = await link().download(name)

        async def pieces() -> AsyncIterator[bytes]:
            try:
                while chunk := await asyncio.to_thread(stream.read, DOWNLOAD_CHUNK):
                    yield chunk
            finally:
                await asyncio.to_thread(stream.close)

        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
        size = stream.headers.get("Content-Length")
        if size is not None:
            headers["Content-Length"] = size
        return StreamingResponse(pieces(), media_type="application/octet-stream", headers=headers)
