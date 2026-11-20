"""The local camera feed: a Unix socket that carries JPEG frames (core spec 9.5).

The owner of a device (CORE-389) may offer its picture to other local
services on ``/run/mm3/feed/<device>.sock`` (CORE-394), so that the device
is opened and decoded once. On connect the owner sends one JSON line, the
header, then frames: a 4-byte big-endian length and that many bytes of one
JPEG (CORE-395). A slow reader gets the newest frame; older ones are dropped,
never queued. The owner sends frames only while a reader is connected.

Both sides live here, on the standard library only:

- :class:`FeedServer` is the owner's side. ``mm3-vision`` uses it, or
  speaks the same bytes; the engine itself offers no feed (Media only reads
  one, decided on 2026-11-20).
- :class:`FeedReader` is a consumer's side; the media service reads the
  feed of a camera owned by ``vision`` with it (CORE-402).

No frame is ever written to disk, and nothing here opens a network socket.

Implements:
    CORE-394: Core spec 0.47, §9.5 Local camera feed.
    CORE-395: Core spec 0.47, §9.5 Local camera feed.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mm3.core.logging import get_logger

__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "FEED_GROUP",
    "FEED_MODE",
    "MAX_FPS",
    "MAX_FRAME_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_HEIGHT",
    "MAX_WIDTH",
    "FeedClosed",
    "FeedError",
    "FeedHeader",
    "FeedReader",
    "FeedServer",
    "encode_frame",
]

MAX_WIDTH = 640
MAX_HEIGHT = 480
MAX_FPS = 30
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
DEFAULT_FPS = 15
"""Size and rate limits and defaults of a feed (CORE-395)."""
FORMAT = "jpeg"
FEED_MODE = 0o660
FEED_GROUP = "mm3"
"""The socket's mode and group (CORE-394)."""
MAX_HEADER_BYTES = 1024
"""A header line longer than this breaks CORE-395."""
MAX_FRAME_BYTES = 2 * 1024 * 1024
"""A frame longer than this breaks CORE-395: a 640 x 480 JPEG is far smaller."""
_LENGTH = 4
_SOI = b"\xff\xd8"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_log = get_logger(subsystem="feed")


class FeedError(Exception):
    """A feed that breaks CORE-395, or that cannot be reached."""


class FeedClosed(FeedError):
    """The other side closed the feed."""


@dataclass(frozen=True)
class FeedHeader:
    """The JSON line an owner sends on connect (CORE-395).

    Attributes:
        device: The device name, as in ``devices`` (6.11).
        width: Frame width in pixels, at most 640.
        height: Frame height in pixels, at most 480.
        fps: Frames a second, at most 30.
        format: Always ``jpeg``.
    """

    device: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    format: str = FORMAT

    def __post_init__(self) -> None:
        if not isinstance(self.device, str) or not _NAME.match(self.device):
            raise FeedError("header: device is not a device name")
        for key, top in (("width", MAX_WIDTH), ("height", MAX_HEIGHT), ("fps", MAX_FPS)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
                raise FeedError(f"header: {key} must be an integer from 1 to {top}")
        if self.format != FORMAT:
            raise FeedError("header: format must be jpeg")

    def encode(self) -> bytes:
        """The header as one JSON line."""
        doc = {
            "device": self.device,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "format": self.format,
        }
        return json.dumps(doc, separators=(",", ":")).encode("utf-8") + b"\n"

    @classmethod
    def decode(cls, line: bytes) -> FeedHeader:
        """Read a header line.

        Raises:
            FeedError: The line is not a JSON object with the five keys, or
                a value is outside the limits of CORE-395.
        """
        try:
            doc: Any = json.loads(line)
        except ValueError as exc:
            raise FeedError("header: not JSON") from exc
        if not isinstance(doc, dict) or set(doc) != {"device", "width", "height", "fps", "format"}:
            raise FeedError("header: expected device, width, height, fps and format")
        return cls(doc["device"], doc["width"], doc["height"], doc["fps"], doc["format"])


def encode_frame(jpeg: bytes) -> bytes:
    """One frame on the wire: a 4-byte big-endian length, then the JPEG (CORE-395).

    Raises:
        FeedError: Not a JPEG, empty or larger than :data:`MAX_FRAME_BYTES`.
    """
    if not jpeg.startswith(_SOI) or len(jpeg) > MAX_FRAME_BYTES:
        raise FeedError("frame: not a JPEG of an allowed size")
    return len(jpeg).to_bytes(_LENGTH, "big") + jpeg


class _Client:
    """One connected reader: a slot for the newest frame and a writer task."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.frame: bytes | None = None
        self.ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def offer(self, wire: bytes) -> None:
        self.frame = wire  # a frame not yet sent is replaced: the newest wins
        self.ready.set()


class FeedServer:
    """The owner's side of a feed (CORE-394, CORE-395).

    The owner calls :meth:`publish` for every frame it captures; the server
    keeps only the newest unsent frame per reader. ``on_readers`` is called
    with the number of readers whenever it changes, so that the owner
    captures (and sends) only while at least one reader is connected.

    Args:
        path: The socket path, ``/run/mm3/feed/<device>.sock``.
        header: The header sent on connect.
        on_readers: Called with the new number of readers.
        group: The socket's group; a failure to set it is logged.

    Implements:
        CORE-394: Core spec 0.47, §9.5 Local camera feed.
        CORE-395: Core spec 0.47, §9.5 Local camera feed.
    """

    def __init__(
        self,
        path: Path,
        header: FeedHeader,
        *,
        on_readers: Callable[[int], None] | None = None,
        group: str | None = FEED_GROUP,
    ) -> None:
        self.path = path
        self.header = header
        self.on_readers = on_readers
        self.group = group
        self._clients: set[_Client] = set()
        self._server: asyncio.Server | None = None

    @property
    def readers(self) -> int:
        """The number of connected readers."""
        return len(self._clients)

    async def start(self) -> None:
        """Create the socket with mode 0660 and group ``mm3`` (CORE-394).

        A stale socket left by an earlier run is removed; any other file at
        the path is left alone and the start fails.

        Raises:
            FeedError: The path holds something that is not a socket.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(os.lstat(self.path).st_mode):
                raise FeedError(f"{self.path} exists and is not a socket")
            os.unlink(self.path)
        self._server = await asyncio.start_unix_server(self._accept, path=str(self.path))
        os.chmod(self.path, FEED_MODE)
        if self.group is not None:
            try:
                os.chown(self.path, -1, grp.getgrnam(self.group).gr_gid)
            except (KeyError, OSError) as exc:
                _log.warning("feed_group", path=str(self.path), group=self.group, error=str(exc))

    def publish(self, jpeg: bytes) -> None:
        """Offer a frame to every reader; slow readers skip to it (CORE-395).

        Raises:
            FeedError: Not a JPEG of an allowed size.
        """
        if not self._clients:
            return
        wire = encode_frame(jpeg)
        for client in self._clients:
            client.offer(wire)

    async def close(self) -> None:
        """Close the socket and every reader, and remove the socket file."""
        if self._server is not None:
            self._server.close()
        for client in list(self._clients):
            await self._drop(client)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        try:
            writer.write(self.header.encode())
            await writer.drain()
        except (ConnectionError, OSError):
            writer.close()
            return
        self._clients.add(client)
        self._readers_changed()
        client.task = asyncio.create_task(self._send(client), name="mm3-feed-send")
        with contextlib.suppress(ConnectionError, OSError):
            await reader.read()  # a reader sends nothing; EOF means it left
        await self._drop(client)

    async def _send(self, client: _Client) -> None:
        try:
            while True:
                await client.ready.wait()
                client.ready.clear()
                wire, client.frame = client.frame, None
                if wire is None:
                    continue
                client.writer.write(wire)
                await client.writer.drain()
        except (ConnectionError, OSError):
            client.writer.close()

    async def _drop(self, client: _Client) -> None:
        if client not in self._clients:
            return
        self._clients.discard(client)
        self._readers_changed()  # at once, so two readers leaving report 1, then 0
        if client.task is not None and client.task is not asyncio.current_task():
            client.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await client.task
        client.writer.close()
        with contextlib.suppress(Exception):
            await client.writer.wait_closed()

    def _readers_changed(self) -> None:
        if self.on_readers is not None:
            self.on_readers(len(self._clients))


class FeedReader:
    """A consumer's side of a feed (CORE-395).

    Use :meth:`connect`; then :meth:`frame` returns one JPEG at a time.
    The reader never buffers more than one frame: the owner drops what a
    slow reader has not yet taken.

    Implements:
        CORE-395: Core spec 0.47, §9.5 Local camera feed.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, header: FeedHeader
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.header = header

    @classmethod
    async def connect(
        cls,
        path: Path,
        device: str,
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - one budget for two steps
    ) -> FeedReader:
        """Connect and read the header.

        Args:
            path: The socket path from the registry file (CORE-391).
            device: The device the feed must be for.
            timeout: Seconds for the connection and the header.

        Returns:
            A connected reader.

        Raises:
            FeedError: The socket is missing or refuses the connection, the
                header breaks CORE-395, names another device or does not
                come within ``timeout``.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path), limit=MAX_HEADER_BYTES), timeout
            )
        except (OSError, TimeoutError) as exc:
            raise FeedError(f"cannot connect: {exc}") from exc
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout)
            header = FeedHeader.decode(line)
            if header.device != device:
                raise FeedError("header: another device")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as exc:
            writer.close()
            raise FeedError("header: missing or too long") from exc
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer, header)

    async def frame(self) -> bytes:
        """Read the next JPEG.

        Raises:
            FeedClosed: The owner closed the feed.
            FeedError: A length of 0 or above :data:`MAX_FRAME_BYTES`, or a
                frame that is not a JPEG.
        """
        try:
            size = int.from_bytes(await self._reader.readexactly(_LENGTH), "big")
            if not 0 < size <= MAX_FRAME_BYTES:
                raise FeedError("frame: bad length")
            data = await self._reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise FeedClosed("closed") from exc
        except (ConnectionError, OSError) as exc:
            raise FeedClosed(str(exc)) from exc
        if not data.startswith(_SOI):
            raise FeedError("frame: not a JPEG")
        return data

    async def close(self) -> None:
        """Leave the feed."""
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
