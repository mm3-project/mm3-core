"""A client for the installer socket, on the standard library.

``mm3 modules`` and ``mm3 installer`` talk to the installer through it; the
engine forwards the admin's requests the same way (CORE-353, CORE-356). It
never writes a venv itself.

Implements:
    CORE-353: Core spec 0.33, §10.6 Module catalog.
    CORE-355: Core spec 0.35, §10.7 Installer service.
    CORE-361: Core spec 0.45, §10.8 Backup and restore.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from mm3.installer.paths import Layout

__all__ = ["DEFAULT_SOCKET", "InstallerClient", "InstallerUnavailable", "Reply", "Stream"]

DEFAULT_SOCKET = Layout().socket


class InstallerUnavailable(OSError):
    """The installer socket does not answer."""


class Reply:
    """A status code and a decoded JSON body (``{}`` when empty)."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body

    @property
    def error(self) -> str:
        """The ``error`` message of an error reply."""
        if isinstance(self.body, dict):
            return str(self.body.get("error", ""))
        return ""


class Stream:
    """An open answer whose body is read in pieces (a backup download, CORE-361).

    Attributes:
        status: The HTTP status.
        headers: The answer's headers.
        body: For an error status, the decoded JSON body (``{}`` when empty); ``None`` for 200.
    """

    def __init__(
        self,
        conn: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self._conn = conn
        self._response = response
        self.status = response.status
        self.headers = response.headers
        self.body: Any = None
        if response.status != 200:
            raw = response.read()
            try:
                self.body = json.loads(raw) if raw else {}
            except ValueError:
                self.body = {"error": raw.decode(errors="replace")}
            self.close()

    @property
    def error(self) -> str:
        """The ``error`` message of an error answer."""
        return str(self.body.get("error", "")) if isinstance(self.body, dict) else ""

    def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes; ``b""`` at the end.

        Raises:
            InstallerUnavailable: The socket failed in the middle of the body.
        """
        try:
            return self._response.read(size)
        except (OSError, http.client.HTTPException) as exc:
            raise InstallerUnavailable(f"installer socket: {exc}") from exc

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except OSError:
            sock.close()
            raise
        self.sock = sock


class InstallerClient:
    """HTTP/JSON on the installer socket.

    Args:
        path: The socket.
        timeout: Seconds per request (a refresh may take a while).
    """

    def __init__(self, path: Path = DEFAULT_SOCKET, timeout: float = 120.0) -> None:
        self.path = path
        self.timeout = timeout

    def request(
        self,
        method: str,
        target: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Reply:
        """Send one request.

        Args:
            method: The HTTP method.
            target: The path.
            body: A JSON object to send, if any.
            timeout: Seconds for this request instead of the client's (a refresh waits longer
                than a status read, CORE-356).

        Raises:
            InstallerUnavailable: The socket does not answer.
        """
        conn = _UnixConnection(self.path, timeout if timeout is not None else self.timeout)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        try:
            conn.request(method, target, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise InstallerUnavailable(f"installer socket {self.path}: {exc}") from exc
        finally:
            conn.close()
        try:
            decoded = json.loads(raw) if raw else {}
        except ValueError:
            decoded = {"error": raw.decode(errors="replace")}
        return Reply(response.status, decoded)

    def stream(self, method: str, target: str, *, timeout: float = 60.0) -> Stream:
        """Send a request and return the answer to be read in pieces.

        Args:
            method: The HTTP method.
            target: The path.
            timeout: Seconds a read may wait for the next piece.

        Raises:
            InstallerUnavailable: The socket does not answer.
        """
        conn = _UnixConnection(self.path, timeout)
        try:
            conn.request(method, target)
            return Stream(conn, conn.getresponse())
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise InstallerUnavailable(f"installer socket {self.path}: {exc}") from exc

    def upload(
        self,
        target: str,
        chunks: Iterable[bytes],
        length: int,
        *,
        timeout: float = 60.0,
    ) -> Reply:
        """Send a raw body of ``length`` bytes taken from ``chunks`` (a backup upload, CORE-361).

        Args:
            target: The path.
            chunks: The body, in pieces; read once, as they are sent.
            length: The size of the body, sent as ``Content-Length``.
            timeout: Seconds a send or the answer may wait.

        Returns:
            The status and the decoded JSON body. When the installer answers and closes before
            the whole body is sent, its answer is returned, not a broken pipe.

        Raises:
            InstallerUnavailable: The socket does not answer.
        """
        conn = _UnixConnection(self.path, timeout)
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
        try:
            # The installer may refuse early; its answer is read below.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                conn.request("POST", target, body=chunks, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise InstallerUnavailable(f"installer socket {self.path}: {exc}") from exc
        finally:
            conn.close()
        try:
            decoded = json.loads(raw) if raw else {}
        except ValueError:
            decoded = {"error": raw.decode(errors="replace")}
        return Reply(response.status, decoded)

    def events(self, op_id: str) -> Iterator[dict[str, Any]]:
        """Follow ``GET /v1/operations/{id}/events`` until the operation ends.

        Raises:
            InstallerUnavailable: The socket does not answer.
        """
        conn = _UnixConnection(self.path, None)  # type: ignore[arg-type]
        try:
            conn.request("GET", f"/v1/operations/{op_id}/events")
            response = conn.getresponse()
            if response.status != 200:
                return
            while line := response.readline():
                if line.strip():
                    yield json.loads(line)
        except (OSError, http.client.HTTPException) as exc:
            raise InstallerUnavailable(f"installer socket {self.path}: {exc}") from exc
        finally:
            conn.close()
