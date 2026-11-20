"""The file routes of the admin: list, upload, new folder and delete (CORE-405).

``GET /api/v1/media/files?path=`` lists a folder of ``files/`` or
``shares/``; ``POST /api/v1/media/files?path=&name=`` takes a file as the raw
body, refused from its ``Content-Length`` before any byte is read when it is
above ``media.limits.upload_max_mb``; ``POST /api/v1/media/folders`` creates a
folder and ``DELETE /api/v1/media/files?path=`` deletes. They live on the
``/api/v1`` router, so the admin listener and the admin socket both serve them
with the authentication of the other routes. Every folder call runs off the
event loop with a 5 s timeout (:func:`mm3.core.media.files.run_folder`).

Implements:
    CORE-405: Core spec 0.55, §9.7 Media service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from mm3.core.logging import get_logger
from mm3.core.media.files import (
    FileRefused,
    Upload,
    delete_path,
    list_folder,
    make_folder,
    run_folder,
    start_upload,
)
from mm3.core.web.problems import ApiError

if TYPE_CHECKING:
    from mm3.core.media import MediaService

__all__ = ["register_media_file_routes"]

_log = get_logger("mm3.core.web.mediafiles")

_CODES = {
    "invalid": "CORE_API_INVALID_REQUEST",
    "not_found": "CORE_API_NOT_FOUND",
    "forbidden": "CORE_API_FORBIDDEN",
    "conflict": "CORE_API_CONFLICT",
    "unavailable": "CORE_API_UNAVAILABLE",
}

WRITE_CHUNK = 1024 * 1024
"""Body bytes gathered before one write in the folder thread."""


class FolderBody(BaseModel):
    """``POST /api/v1/media/folders``: the new folder (CORE-405)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


def _api_error(exc: FileRefused) -> ApiError:
    return ApiError(_CODES[exc.reason], exc.detail)


def register_media_file_routes(router: APIRouter, media: MediaService | None) -> None:
    """Add the file routes of CORE-405 to the ``/api/v1`` router.

    Args:
        router: The REST router (admin listener and admin socket).
        media: The media service, for the media folder and ``upload_max_mb``;
            without it every path is 404.

    Implements:
        CORE-405: Core spec 0.55, §9.7 Media service.
    """

    def service() -> MediaService:
        if media is None:
            raise ApiError("CORE_API_NOT_FOUND", "There is no media folder")
        return media

    @router.get("/media/files")
    async def list_files(path: str = "files") -> list[dict[str, Any]]:
        """List a folder of ``files/`` or ``shares/`` (CORE-405)."""
        svc = service()
        try:
            return await run_folder(list_folder, svc.root, path)
        except FileRefused as exc:
            raise _api_error(exc) from None

    @router.post("/media/files", status_code=201)
    async def upload_file(
        request: Request, path: str, name: str, overwrite: bool = False
    ) -> dict[str, Any]:
        """Write one file from the raw body into a folder of ``files/`` (CORE-405)."""
        svc = service()
        raw = request.headers.get("content-length", "")
        if not raw.isdigit():
            raise ApiError("CORE_API_INVALID_REQUEST", "Send the file with a Content-Length")
        length = int(raw)
        limit = svc.upload_max_mb * 1024 * 1024
        if length > limit:
            # Refused from the header alone: no byte of the body is read.
            raise ApiError(
                "CORE_API_UPLOAD_TOO_LARGE", f"An upload is at most {svc.upload_max_mb} MB"
            )
        try:
            upload: Upload = await run_folder(start_upload, svc.root, path, name, overwrite)
        except FileRefused as exc:
            raise _api_error(exc) from None
        try:
            entry = await _receive(request, upload, length)
        finally:
            await run_folder(upload.close)
        _log.info("media_file_uploaded", path=path, name=name, size=length)
        return entry

    @router.post("/media/folders", status_code=201)
    async def new_folder(body: FolderBody) -> dict[str, Any]:
        """Create a folder under ``files/`` in an existing parent (CORE-405)."""
        svc = service()
        try:
            entry = await run_folder(make_folder, svc.root, body.path)
        except FileRefused as exc:
            raise _api_error(exc) from None
        _log.info("media_folder_created", path=body.path)
        return entry

    @router.delete("/media/files", status_code=204)
    async def delete_file(path: str, recursive: bool = False) -> Response:
        """Delete a file or a folder under ``files/``; 204 (CORE-405)."""
        svc = service()
        try:
            await run_folder(delete_path, svc.root, path, recursive)
        except FileRefused as exc:
            raise _api_error(exc) from None
        _log.info("media_file_deleted", path=path, recursive=recursive)
        return Response(status_code=204)


async def _receive(request: Request, upload: Upload, length: int) -> dict[str, Any]:
    """Stream the body into the upload; remove it when the body does not match ``length``."""
    pending = bytearray()
    try:
        async for chunk in request.stream():
            if upload.written + len(pending) + len(chunk) > length:
                raise ApiError("CORE_API_INVALID_REQUEST", "The body is longer than its length")
            pending += chunk
            if len(pending) >= WRITE_CHUNK:
                await run_folder(upload.write, bytes(pending))
                pending.clear()
        if pending:
            await run_folder(upload.write, bytes(pending))
        if upload.written != length:
            raise ApiError("CORE_API_INVALID_REQUEST", "The body is shorter than its length")
        return await run_folder(upload.finish)
    except FileRefused as exc:
        await run_folder(upload.abort)
        raise _api_error(exc) from None
    except OSError as exc:  # a full disk, or another write error
        await run_folder(upload.abort)
        _log.warning("media_upload_failed", error=exc.strerror or type(exc).__name__)
        raise ApiError("CORE_API_UNAVAILABLE", "The file could not be written") from None
    except BaseException:
        await run_folder(upload.abort)
        raise
