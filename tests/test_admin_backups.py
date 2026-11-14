"""Tests of the engine's backup routes (CORE-361).

Implements:
- CORE-361 (Core spec 0.45, §10.8): the engine forwards the backup routes to the installer on the
  admin listener only, streams an upload and a download, passes the caller as ``client``, takes
  ``source`` as ``{name, remote?}`` or ``{upload}``, refuses ``path`` and ``pre``, and maps the
  installer's refusals to the codes of CORE-356.
"""

# ruff: noqa: F811 - the shared `api` fixture is imported, then named by the tests
from __future__ import annotations

import io
import json
from collections.abc import Iterable
from typing import Any

import httpx
import pytest

from mm3.core.logging import numbered_records
from mm3.core.web.apps import socket_app
from mm3.core.web.installer import InstallerLink
from mm3.core.web.problems import CODES
from mm3.installer.client import InstallerUnavailable, Reply, Stream
from test_admin_api import (
    Api,
    api,  # noqa: F401 - the shared admin API fixture
    is_problem,
)

PASSPHRASE = "correct horse battery staple"  # noqa: S105 - test data
OP = "0123456789abcdef"
UPLOAD = "0123456789abcdef0123456789abcdef"
ARCHIVE = "mm3-2026-11-14T10-00-00-mirror.mm3backup"


class FakeStream:
    """A stand-in for ``Stream`` that serves ``data`` or an error status."""

    def __init__(self, status: int, data: bytes = b"", body: Any = None) -> None:
        self.status = status
        self.body = body
        self.headers = {"Content-Length": str(len(data))}
        self.closed = False
        self._data = io.BytesIO(data)

    @property
    def error(self) -> str:
        return str(self.body.get("error", "")) if isinstance(self.body, dict) else ""

    def read(self, size: int) -> bytes:
        return self._data.read(size)

    def close(self) -> None:
        self.closed = True


class Scripted:
    """A stand-in for the installer socket with scripted replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.replies: dict[tuple[str, str], Reply] = {}
        self.uploaded: list[bytes] = []
        self.upload_reply = Reply(201, {"upload": UPLOAD, "size": 0})
        self.stream_reply = FakeStream(200, b"archive-bytes")
        self.down = False

    def request(
        self,
        method: str,
        target: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Reply:
        self.calls.append((method, target, body))
        if self.down:
            raise InstallerUnavailable("installer socket /run/mm3-install/installer.sock: gone")
        if (method, target) in self.replies:
            return self.replies[(method, target)]
        if method == "POST" and target in ("/v1/backups", "/v1/backups/restore"):
            return Reply(202, {"id": OP})
        if method in ("PUT", "DELETE") or target.endswith("/test"):
            return Reply(204, {})
        return Reply(200, {"backups": []})

    def upload(
        self, target: str, chunks: Iterable[bytes], length: int, *, timeout: float = 60.0
    ) -> Reply:
        self.calls.append(("POST", target, {"length": length}))
        data = b"".join(chunks)
        self.uploaded.append(data)
        if self.upload_reply.status == 201:
            return Reply(201, {"upload": UPLOAD, "size": len(data)})
        return self.upload_reply

    def stream(self, method: str, target: str, *, timeout: float = 60.0) -> Stream:
        self.calls.append((method, target, None))
        if self.down:
            raise InstallerUnavailable("installer socket /run/mm3-install/installer.sock: gone")
        return self.stream_reply  # type: ignore[return-value]


@pytest.fixture
def installer(api: Api) -> Scripted:
    fake = Scripted()
    api.services.installer = InstallerLink(fake)  # type: ignore[arg-type]
    return fake


def logged() -> str:
    """Everything the engine logged so far, as one string."""
    return json.dumps([record for _, record in numbered_records()], default=str)


def test_core_361_the_upload_code_is_413() -> None:
    assert CODES["CORE_API_UPLOAD_TOO_LARGE"][0] == 413


# --- reading ---------------------------------------------------------------------------------


async def test_core_361_the_lists_are_forwarded(api: Api, installer: Scripted) -> None:
    installer.replies[("GET", "/v1/backups")] = Reply(
        200, {"backups": [{"name": ARCHIVE}], "status": {}}
    )
    r = await api.get("/api/v1/backups")
    assert r.status_code == 200
    assert r.json() == {"backups": [{"name": ARCHIVE}], "status": {}}
    assert (await api.get("/api/v1/backups/remote")).status_code == 200
    assert (await api.get("/api/v1/backups/remotes")).status_code == 200
    targets = [c[1] for c in installer.calls]
    assert targets == ["/v1/backups", "/v1/backups/remote", "/v1/backups/remotes"]


async def test_core_361_the_installer_being_down_is_503(api: Api, installer: Scripted) -> None:
    installer.down = True
    is_problem(await api.get("/api/v1/backups"), 503, "CORE_API_INSTALLER_UNAVAILABLE")
    api.services.installer = None
    is_problem(await api.get("/api/v1/backups"), 503, "CORE_API_INSTALLER_UNAVAILABLE")


async def test_core_361_a_remote_refusal_is_422(api: Api, installer: Scripted) -> None:
    installer.replies[("GET", "/v1/backups/remote")] = Reply(400, {"error": "no remote is set"})
    body = is_problem(await api.get("/api/v1/backups/remote"), 422, "CORE_API_INVALID_REQUEST")
    assert body["detail"] == "no remote is set"


# --- creating --------------------------------------------------------------------------------


async def test_core_361_a_backup_is_202_with_the_caller(api: Api, installer: Scripted) -> None:
    r = await api.post("/api/v1/backups", {"parts": ["config", "data"], "use_configured_key": True})
    assert r.status_code == 202, r.text
    assert r.json() == {"id": OP}
    assert r.headers["location"] == f"/api/v1/installer/operations/{OP}"
    method, target, body = installer.calls[-1]
    assert (method, target) == ("POST", "/v1/backups")
    assert body == {
        "parts": ["config", "data"],
        "use_configured_key": True,
        "client": "test (bearer) 127.0.0.1",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"pre": True},
        {"parts": []},
        {"parts": ["config", "config"]},
        {"parts": ["logs"]},
        {"recipient": "age1abc", "archive_passphrase": "x"},
        {"unknown": 1},
    ],
)
async def test_core_361_a_bad_backup_body_is_422(api: Api, installer: Scripted, body: Any) -> None:
    is_problem(await api.post("/api/v1/backups", body), 422, "CORE_API_INVALID_REQUEST")
    assert installer.calls == []


async def test_core_361_the_passphrase_of_an_archive_is_never_logged(
    api: Api, installer: Scripted
) -> None:
    await api.post("/api/v1/backups", {"archive_passphrase": PASSPHRASE})
    assert "api_backup" in logged()
    assert PASSPHRASE not in logged()


# --- upload ----------------------------------------------------------------------------------


async def test_core_361_an_upload_is_streamed_to_the_installer(
    api: Api, installer: Scripted
) -> None:
    data = b"x" * 300_000
    r = await api.client.post(
        "/api/v1/backups/upload",
        headers={**api.bearer, "content-type": "application/octet-stream"},
        content=data,
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"upload": UPLOAD, "size": len(data)}
    assert installer.uploaded == [data]
    assert installer.calls[-1] == ("POST", "/v1/backups/upload", {"length": len(data)})


async def test_core_361_an_upload_needs_a_length(api: Api, installer: Scripted) -> None:
    async def chunks() -> Any:
        yield b"abc"

    r = await api.client.post("/api/v1/backups/upload", headers=api.bearer, content=chunks())
    is_problem(r, 422, "CORE_API_INVALID_REQUEST")
    r = await api.client.post("/api/v1/backups/upload", headers=api.bearer, content=b"")
    is_problem(r, 422, "CORE_API_INVALID_REQUEST")
    assert installer.uploaded == []


async def test_core_361_a_large_upload_is_refused_from_the_header(
    api: Api, installer: Scripted
) -> None:
    r = await api.client.post(
        "/api/v1/backups/upload",
        headers={**api.bearer, "content-length": str(2 * 1024**3 + 1)},
        content=b"x",
    )
    is_problem(r, 413, "CORE_API_UPLOAD_TOO_LARGE")
    assert installer.calls == []


async def test_core_361_an_installer_413_is_the_upload_code(api: Api, installer: Scripted) -> None:
    installer.upload_reply = Reply(413, {"error": "too large", "code": "too_large"})
    r = await api.client.post("/api/v1/backups/upload", headers=api.bearer, content=b"abc")
    is_problem(r, 413, "CORE_API_UPLOAD_TOO_LARGE")


async def test_core_361_an_installer_refusal_of_an_upload_is_422(
    api: Api, installer: Scripted
) -> None:
    installer.upload_reply = Reply(400, {"error": "the upload ended early"})
    r = await api.client.post("/api/v1/backups/upload", headers=api.bearer, content=b"abc")
    is_problem(r, 422, "CORE_API_INVALID_REQUEST")


# --- download --------------------------------------------------------------------------------


async def test_core_361_a_download_is_streamed(api: Api, installer: Scripted) -> None:
    r = await api.get(f"/api/v1/backups/{ARCHIVE}")
    assert r.status_code == 200
    assert r.content == b"archive-bytes"
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == f'attachment; filename="{ARCHIVE}"'
    assert r.headers["content-length"] == str(len(b"archive-bytes"))
    assert installer.stream_reply.closed
    assert installer.calls[-1] == ("GET", f"/v1/backups/{ARCHIVE}", None)


async def test_core_361_an_unknown_archive_is_404(api: Api, installer: Scripted) -> None:
    installer.stream_reply = FakeStream(404, body={"error": "no such backup"})
    is_problem(await api.get(f"/api/v1/backups/{ARCHIVE}"), 404, "CORE_API_NOT_FOUND")
    before = len(installer.calls)
    is_problem(await api.get("/api/v1/backups/notabackup.txt"), 404, "CORE_API_NOT_FOUND")
    is_problem(await api.get("/api/v1/backups/..%2Fx.mm3backup"), 404, "CORE_API_NOT_FOUND")
    assert len(installer.calls) == before  # a malformed name never reaches the installer


# --- verify, preview, keygen -----------------------------------------------------------------


async def test_core_361_verify_and_preview_take_a_source(api: Api, installer: Scripted) -> None:
    for what in ("verify", "preview"):
        r = await api.post(
            f"/api/v1/backups/{what}",
            {
                "source": {"name": ARCHIVE, "remote": True},
                "key": "AGE-SECRET-KEY-1X",
                "parts": ["config"],
            },
        )
        assert r.status_code == 200, r.text
        assert installer.calls[-1] == (
            "POST",
            f"/v1/backups/{what}",
            {"name": ARCHIVE, "remote": True, "key": "AGE-SECRET-KEY-1X", "parts": ["config"]},
        )
    r = await api.post("/api/v1/backups/verify", {"source": {"upload": UPLOAD}})
    assert r.status_code == 200
    assert installer.calls[-1][2] == {"upload": UPLOAD}


@pytest.mark.parametrize(
    "source",
    [
        {"path": "/etc/passwd"},
        {"name": ARCHIVE, "path": "/x"},
        {},
        {"name": ARCHIVE, "upload": UPLOAD},
        {"upload": "../x"},
        {"upload": UPLOAD, "remote": True},
    ],
)
async def test_core_361_a_bad_source_is_422(api: Api, installer: Scripted, source: Any) -> None:
    is_problem(
        await api.post("/api/v1/backups/verify", {"source": source}),
        422,
        "CORE_API_INVALID_REQUEST",
    )
    assert installer.calls == []


async def test_core_361_keygen_returns_the_identity_once(api: Api, installer: Scripted) -> None:
    installer.replies[("POST", "/v1/backups/keygen")] = Reply(
        200, {"public": "age1pub", "identity": "AGE-SECRET-KEY-1X", "kept": True}
    )
    r = await api.post("/api/v1/backups/keygen", {"keep": True})
    assert r.json() == {"public": "age1pub", "identity": "AGE-SECRET-KEY-1X", "kept": True}
    assert installer.calls[-1][2] == {"keep": True}


async def test_core_361_the_identity_is_not_logged(api: Api, installer: Scripted) -> None:
    installer.replies[("POST", "/v1/backups/keygen")] = Reply(
        200, {"public": "age1pub", "identity": "AGE-SECRET-KEY-1SECRET", "kept": False}
    )
    await api.post("/api/v1/backups/keygen", {})
    assert "api_backup_keygen" in logged()
    assert "AGE-SECRET-KEY-1SECRET" not in logged()


# --- restore ---------------------------------------------------------------------------------


async def test_core_361_a_restore_passes_the_reauthentication_on(
    api: Api, installer: Scripted
) -> None:
    r = await api.post(
        "/api/v1/backups/restore",
        {
            "source": {"upload": UPLOAD},
            "parts": ["config"],
            "instance": "clock",
            "passphrase": PASSPHRASE,
            "totp": "123456",
        },
    )
    assert r.status_code == 202, r.text
    assert r.json() == {"id": OP}
    assert installer.calls[-1] == (
        "POST",
        "/v1/backups/restore",
        {
            "upload": UPLOAD,
            "parts": ["config"],
            "instance": "clock",
            "passphrase": PASSPHRASE,
            "totp": "123456",
            "client": "test (bearer) 127.0.0.1",
        },
    )


async def test_core_361_a_restore_needs_a_passphrase_and_refuses_a_path(
    api: Api, installer: Scripted
) -> None:
    is_problem(
        await api.post("/api/v1/backups/restore", {"source": {"name": ARCHIVE}}),
        422,
        "CORE_API_INVALID_REQUEST",
    )
    is_problem(
        await api.post(
            "/api/v1/backups/restore",
            {"source": {"path": "/root/x.mm3backup"}, "passphrase": PASSPHRASE},
        ),
        422,
        "CORE_API_INVALID_REQUEST",
    )
    assert installer.calls == []


async def test_core_361_a_failed_reauthentication_is_403_without_a_reason(
    api: Api, installer: Scripted
) -> None:
    installer.replies[("POST", "/v1/backups/restore")] = Reply(403, {"error": "wrong totp"})
    r = await api.post(
        "/api/v1/backups/restore", {"source": {"name": ARCHIVE}, "passphrase": PASSPHRASE}
    )
    body = is_problem(r, 403, "CORE_API_REAUTH_FAILED")
    assert "totp" not in json.dumps(body)


async def test_core_361_a_restore_while_busy_is_409(api: Api, installer: Scripted) -> None:
    installer.replies[("POST", "/v1/backups/restore")] = Reply(409, {"error": "busy"})
    r = await api.post(
        "/api/v1/backups/restore", {"source": {"name": ARCHIVE}, "passphrase": PASSPHRASE}
    )
    is_problem(r, 409, "CORE_API_INSTALLER_BUSY")


async def test_core_361_the_passphrase_of_a_restore_is_never_logged(
    api: Api, installer: Scripted
) -> None:
    await api.post(
        "/api/v1/backups/restore", {"source": {"name": ARCHIVE}, "passphrase": PASSPHRASE}
    )
    assert "api_backup" in logged()
    assert PASSPHRASE not in logged()


# --- remotes ---------------------------------------------------------------------------------


async def test_core_361_a_remote_is_changed_after_reauthentication(
    api: Api, installer: Scripted
) -> None:
    put = {"type": "s3", "params": {"provider": "AWS"}, "passphrase": PASSPHRASE}
    r = await api.put("/api/v1/backups/remotes/nas", put)
    assert r.status_code == 204, r.text
    assert installer.calls[-1] == ("PUT", "/v1/backups/remotes/nas", put)
    r = await api.client.request(
        "DELETE", "/api/v1/backups/remotes/nas", headers=api.bearer, json={"passphrase": PASSPHRASE}
    )
    assert r.status_code == 204, r.text
    assert installer.calls[-1] == ("DELETE", "/v1/backups/remotes/nas", {"passphrase": PASSPHRASE})
    r = await api.post("/api/v1/backups/remotes/nas/test", {"path": "mm3"})
    assert r.status_code == 204
    assert installer.calls[-1] == ("POST", "/v1/backups/remotes/nas/test", {"path": "mm3"})


async def test_core_361_a_remote_needs_a_passphrase(api: Api, installer: Scripted) -> None:
    is_problem(
        await api.put("/api/v1/backups/remotes/nas", {"type": "s3", "params": {}}),
        422,
        "CORE_API_INVALID_REQUEST",
    )
    r = await api.client.request(
        "DELETE", "/api/v1/backups/remotes/nas", headers=api.bearer, json={}
    )
    is_problem(r, 422, "CORE_API_INVALID_REQUEST")
    assert installer.calls == []


async def test_core_361_an_unknown_remote_is_404(api: Api, installer: Scripted) -> None:
    installer.replies[("GET", "/v1/backups/remotes/gone")] = Reply(404, {"error": "no remote"})
    is_problem(await api.get("/api/v1/backups/remotes/gone"), 404, "CORE_API_NOT_FOUND")
    before = len(installer.calls)
    is_problem(await api.get("/api/v1/backups/remotes/-bad"), 404, "CORE_API_NOT_FOUND")
    assert len(installer.calls) == before


async def test_core_361_the_secret_of_a_remote_is_never_logged(
    api: Api, installer: Scripted
) -> None:
    await api.put(
        "/api/v1/backups/remotes/nas",
        {"type": "s3", "params": {"secret_access_key": "SECRETVALUE"}, "passphrase": PASSPHRASE},
    )
    assert "api_backup_remote_put" in logged()
    assert "SECRETVALUE" not in logged()
    assert PASSPHRASE not in logged()


# --- authentication and the admin socket -----------------------------------------------------


async def test_core_361_the_routes_need_authentication(api: Api, installer: Scripted) -> None:
    for method, path in (
        ("GET", "/api/v1/backups"),
        ("POST", "/api/v1/backups"),
        ("POST", "/api/v1/backups/upload"),
        ("GET", f"/api/v1/backups/{ARCHIVE}"),
        ("POST", "/api/v1/backups/restore"),
    ):
        r = await api.client.request(method, path)
        is_problem(r, 401, "CORE_API_UNAUTHORIZED")
    assert installer.calls == []


async def test_core_361_the_socket_does_not_serve_the_backups(
    api: Api, installer: Scripted
) -> None:
    app = socket_app(api.services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://socket") as client:
        for method, path in (
            ("GET", "/api/v1/backups"),
            ("POST", "/api/v1/backups"),
            ("POST", "/api/v1/backups/upload"),
            ("GET", f"/api/v1/backups/{ARCHIVE}"),
            ("POST", "/api/v1/backups/restore"),
            ("GET", "/api/v1/backups/remotes"),
        ):
            r = await client.request(method, path, json={} if method == "POST" else None)
            is_problem(r, 404, "CORE_API_NOT_FOUND")
        paths = (await client.get("/api/openapi.json")).json()["paths"]
    assert not [p for p in paths if p.startswith("/api/v1/backups")]
    assert installer.calls == []
