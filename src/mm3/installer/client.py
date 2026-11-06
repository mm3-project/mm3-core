"""A client for the installer socket, on the standard library.

``mm3 modules`` and ``mm3 installer`` talk to the installer through it; the
engine forwards the admin's requests the same way (CORE-353, CORE-356). It
never writes a venv itself.

Implements:
    CORE-353: Core spec 0.33, §10.6 Module catalog.
    CORE-355: Core spec 0.35, §10.7 Installer service.
"""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mm3.installer.paths import Layout

__all__ = ["DEFAULT_SOCKET", "InstallerClient", "InstallerUnavailable", "Reply"]

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

    def request(self, method: str, target: str, body: dict[str, Any] | None = None) -> Reply:
        """Send one request.

        Raises:
            InstallerUnavailable: The socket does not answer.
        """
        conn = _UnixConnection(self.path, self.timeout)
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
