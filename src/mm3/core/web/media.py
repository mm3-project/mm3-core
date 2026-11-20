"""The delivery routes of the media service (CORE-400).

The mirror listener serves, for any client admitted by ``server.allow``:

- ``/media/<source>/stream.mjpg``: ``multipart/x-mixed-replace`` of JPEG frames;
- ``/media/<source>/hls/index.m3u8`` with ``init.mp4`` and its segments;
- ``/media/files/<path>``: a file under the media folder, with range requests.

The admin listener serves the same under ``/admin/preview/media/``, behind an
admin session or token. ``<path>`` always starts with ``files/`` or
``shares/<name>/``, so ``/media/files/stream.mjpg`` and ``/media/files/hls/…``
still name a source called ``files``. Every stream comes from the engine's
own origin, so the CSP of CORE-63 does not change.

Implements:
    CORE-400: Core spec 0.51, §9.7 Media service.
    CORE-407: Core spec 0.57, §9.7 Media service.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from mm3.core.media.files import FOLDER_TIMEOUT_S, FileRefused, run_folder
from mm3.core.media.folder import MediaFile, open_file, split_media_path
from mm3.core.media.service import MediaRefused, MediaService

__all__ = ["BOUNDARY", "OPEN_TIMEOUT_S", "add_media_routes"]

BOUNDARY = "mm3frame"
OPEN_TIMEOUT_S = FOLDER_TIMEOUT_S
"""A file of the media folder that cannot be opened in 5 s (a hung mount) is 503."""
CHUNK = 256 * 1024

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_HLS_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
}

Guard = Callable[[Request], Awaitable[bool]]


def _refused(exc: MediaRefused) -> Response:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return Response(exc.reason, status_code=exc.status, media_type="text/plain", headers=headers)


def _byte_range(header: str | None, size: int) -> tuple[int, int] | bool | None:
    """Parse one ``Range`` header: ``None`` for the whole file, ``False`` when unsatisfiable."""
    if header is None:
        return None
    m = _RANGE.match(header.strip())
    if m is None or (not m[1] and not m[2]):
        return None  # several ranges or another unit: send the whole file
    if not m[1]:
        length = int(m[2])
        if length == 0:
            return False
        return max(0, size - length), size - 1
    start = int(m[1])
    end = int(m[2]) if m[2] else size - 1
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)


async def _chunks(fd: int, start: int, length: int) -> AsyncIterator[bytes]:
    try:
        pos, left = start, length
        while left > 0:
            try:
                # The folder threads: a read from a mount that hangs ends the response
                # after 5 s and never takes the engine's default threads (CORE-407).
                data = await run_folder(os.pread, fd, min(CHUNK, left), pos)
            except FileRefused:
                return
            if not data:
                return
            pos += len(data)
            left -= len(data)
            yield data
    finally:
        os.close(fd)


async def _file_response(media: MediaService, rel: str, request: Request) -> Response:
    """Send a file of the media folder, honouring one byte range (CORE-400, CORE-404)."""
    try:
        found: MediaFile | None = await run_folder(open_file, media.root, rel)
    except FileRefused:
        return Response("unavailable", status_code=503, media_type="text/plain")
    if found is None:
        return Response(status_code=404)
    size = found.size
    picked = _byte_range(request.headers.get("range"), size)
    headers = {"Accept-Ranges": "bytes"}
    if picked is False:
        os.close(found.fd)
        headers["Content-Range"] = f"bytes */{size}"
        return Response(status_code=416, headers=headers)
    if request.method == "HEAD":
        os.close(found.fd)
        headers["Content-Length"] = str(size)
        return Response(status_code=200, media_type=found.content_type, headers=headers)
    if isinstance(picked, tuple):
        start, end = picked
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            _chunks(found.fd, start, end - start + 1),
            status_code=206,
            media_type=found.content_type,
            headers=headers,
        )
    headers["Content-Length"] = str(size)
    return StreamingResponse(
        _chunks(found.fd, 0, size), media_type=found.content_type, headers=headers
    )


async def _mjpeg(media: MediaService, source: str) -> Response:
    try:
        viewer = await media.open_mjpeg(source)
    except MediaRefused as exc:
        return _refused(exc)

    async def parts() -> AsyncIterator[bytes]:
        try:
            async for frame in viewer.frames():
                yield (
                    (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode("ascii")
                    + frame
                    + b"\r\n"
                )
        finally:
            viewer.close()

    return StreamingResponse(parts(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


async def _hls(media: MediaService, source: str, file: str, request: Request) -> Response:
    client = request.client.host if request.client is not None else "unknown"
    try:
        path = await media.hls_file(source, client, file)
    except MediaRefused as exc:
        return _refused(exc)
    return FileResponse(path, media_type=_HLS_TYPES[path.suffix])


def add_media_routes(
    app: FastAPI, media: MediaService, prefix: str, guard: Guard | None = None
) -> None:
    """Add the delivery routes under ``prefix`` (CORE-400).

    Args:
        app: The listener's application.
        media: The media service.
        prefix: ``/media`` on the mirror listener, ``/admin/preview/media`` on the admin one.
        guard: Returns whether the request may continue (the admin session);
            a refused request is 401.

    Implements:
        CORE-400: Core spec 0.51, §9.7 Media service.
    """

    async def allowed(request: Request) -> bool:
        return guard is None or await guard(request)

    @app.api_route(f"{prefix}/files/{{path:path}}", methods=["GET", "HEAD"])
    async def media_file(path: str, request: Request) -> Response:
        if not await allowed(request):
            return Response(status_code=401)
        if split_media_path(path) is not None:
            return await _file_response(media, path, request)
        # Not a media folder path: the routes of a source named ``files``.
        if path == "stream.mjpg":
            return await _mjpeg(media, "files")
        if path.startswith("hls/") and "/" not in path[4:]:
            return await _hls(media, "files", path[4:], request)
        return Response(status_code=404)

    @app.get(f"{prefix}/{{source}}/stream.mjpg")
    async def media_mjpeg(source: str, request: Request) -> Response:
        if not await allowed(request):
            return Response(status_code=401)
        return await _mjpeg(media, source)

    @app.get(f"{prefix}/{{source}}/hls/{{file}}")
    async def media_hls(source: str, file: str, request: Request) -> Response:
        if not await allowed(request):
            return Response(status_code=401)
        return await _hls(media, source, file, request)
