"""Tests of the installer's staged backup uploads (CORE-361).

Implements:
- CORE-361 (Core spec 0.45, §10.8): ``POST /v1/backups/upload`` streams an archive into
  ``uploads/`` (mode 0600, temporary file and rename), at most two files and 24 h, 2 GiB at
  most, and a restore or verify names the file as ``{upload}``; a restore deletes it when it ends.
"""

# ruff: noqa: F811 - the shared `mirror` and `served` fixtures are imported, then named by tests
from __future__ import annotations

import asyncio
import os
import socket
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mm3.installer.client import InstallerClient, InstallerUnavailable, _UnixConnection
from mm3.installer.uploads import MAX_AGE_S, MAX_STAGED, UPLOAD_ID, Uploads
from test_backup import (
    Mirror,
    mirror,  # noqa: F401 - the shared backup fixture
    served,  # noqa: F401 - the shared socket fixture
)
from test_installer_operations import PASS, Served, World, _wait_done

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


def pieces(data: bytes, size: int = 65536) -> Iterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def upload(served: Served, data: bytes) -> Any:
    client = InstallerClient(served.path, timeout=10)
    return await asyncio.to_thread(client.upload, "/v1/backups/upload", pieces(data), len(data))


def staged(mirror: Mirror) -> list[Path]:
    folder = mirror.runner.uploads.directory
    return sorted(folder.iterdir()) if folder.is_dir() else []


# --- the store -------------------------------------------------------------------------------


def test_core_361_a_staged_file_is_private_and_named_by_a_random_id(mirror: Mirror) -> None:
    uploads = mirror.runner.uploads
    staging = uploads.begin()
    staging.write(b"abc")
    staging.write(b"def")
    upload_id = staging.commit()
    assert UPLOAD_ID.match(upload_id)
    path = uploads.path(upload_id)
    assert path is not None
    assert path.read_bytes() == b"abcdef"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(uploads.directory.stat().st_mode) == 0o700
    assert staging.size == 6


def test_core_361_an_aborted_upload_leaves_nothing(mirror: Mirror) -> None:
    staging = mirror.runner.uploads.begin()
    staging.write(b"abc")
    staging.abort()
    assert staged(mirror) == []


def test_core_361_a_malformed_or_unknown_id_has_no_path(mirror: Mirror) -> None:
    uploads = mirror.runner.uploads
    for bad in ("../x", "A" * 32, "0" * 31, "0" * 33, "", "0" * 32):
        assert uploads.path(bad) is None
    uploads.discard("../../etc/passwd")  # nothing happens, and nothing outside is touched


def test_core_361_at_most_two_files_are_kept_and_the_oldest_goes(mirror: Mirror) -> None:
    uploads = mirror.runner.uploads
    ids = []
    for n in range(3):
        staging = uploads.begin()
        staging.write(bytes([n]))
        ids.append(staging.commit())
        stamp = time.time() - 100 + n  # an order the clock can't tie
        os.utime(uploads.path(ids[-1]) or "", (stamp, stamp))
    assert len(staged(mirror)) == MAX_STAGED
    assert uploads.path(ids[0]) is None
    assert uploads.path(ids[1]) is not None
    assert uploads.path(ids[2]) is not None


def test_core_361_a_file_older_than_24_hours_is_deleted_at_the_next_upload(tmp_path: Path) -> None:
    mirror = Mirror(World(tmp_path), tmp_path)
    clock = {"now": 10_000_000.0}
    uploads = Uploads(mirror.layout, now=lambda: clock["now"])
    staging = uploads.begin()
    staging.write(b"old")
    old = staging.commit()
    os.utime(uploads.path(old) or "", (clock["now"], clock["now"]))
    clock["now"] += MAX_AGE_S - 1
    uploads.begin().abort()
    assert uploads.path(old) is not None  # not yet 24 h
    clock["now"] += 2
    uploads.begin().abort()
    assert uploads.path(old) is None


def test_core_361_an_upload_in_progress_survives_another_one_starting(mirror: Mirror) -> None:
    uploads = mirror.runner.uploads
    first = uploads.begin()
    first.write(b"abc")
    second = uploads.begin()
    second.abort()
    first.write(b"def")
    upload_id = first.commit()
    path = uploads.path(upload_id)
    assert path is not None
    assert path.read_bytes() == b"abcdef"


def test_core_361_leftovers_of_a_crash_are_deleted_at_start(mirror: Mirror) -> None:
    uploads = mirror.runner.uploads
    uploads.begin().write(b"half")  # never committed: the installer "crashed"
    assert len(staged(mirror)) == 1
    uploads.cleanup()
    assert len(staged(mirror)) == 1  # a plain cleanup keeps what may still be written
    uploads.cleanup(crashed=True)
    assert staged(mirror) == []


# --- the route -------------------------------------------------------------------------------


async def test_core_361_an_upload_is_staged_and_can_be_verified(
    served: Served, mirror: Mirror
) -> None:
    created = mirror.make("manual", ["config"])
    data = created.path.read_bytes()
    reply = await upload(served, data)
    assert reply.status == 201, reply.body
    assert reply.body["size"] == len(data)
    upload_id = reply.body["upload"]
    assert UPLOAD_ID.match(upload_id)
    assert [p.name for p in staged(mirror)] == [f"{upload_id}.mm3backup"]
    assert (staged(mirror)[0]).read_bytes() == data
    # An upload needs no passphrase and no root: it only waits for a restore.
    verified = await served.call("POST", "/v1/backups/verify", {"upload": upload_id})
    assert verified.status == 200, verified.body
    assert verified.body["ok"] is True
    preview = await served.call("POST", "/v1/backups/preview", {"upload": upload_id})
    assert preview.status == 200
    assert staged(mirror) != []  # verifying keeps the file


async def test_core_361_a_restore_from_an_upload_deletes_it_when_it_ends(
    served: Served, mirror: Mirror
) -> None:
    created = mirror.make("manual", ["config"])
    reply = await upload(served, created.path.read_bytes())
    upload_id = reply.body["upload"]
    mirror.damage()
    # A restore still re-authenticates, even from an upload.
    body = {"upload": upload_id, "parts": ["config"]}
    assert (await served.call("POST", "/v1/backups/restore", body)).status == 403
    assert staged(mirror) != []
    ok = await served.call("POST", "/v1/backups/restore", {**body, "passphrase": PASS})
    assert ok.status == 202, ok.body
    assert (await _wait_done(served, ok.body["id"]))["state"] == "done"
    assert "locale: en" in mirror.config_text()
    assert [p for p in staged(mirror) if p.name.endswith(".mm3backup")] == []


async def test_core_361_a_failed_restore_also_deletes_the_upload(
    served: Served, mirror: Mirror
) -> None:
    created = mirror.make("manual", ["config"])
    data = bytearray(created.path.read_bytes())
    data[len(data) // 2] ^= 0xFF  # a damaged archive
    reply = await upload(served, bytes(data))
    upload_id = reply.body["upload"]
    ok = await served.call("POST", "/v1/backups/restore", {"upload": upload_id, "passphrase": PASS})
    assert ok.status == 202
    assert (await _wait_done(served, ok.body["id"]))["state"] == "failed"
    assert staged(mirror) == []


async def test_core_361_an_unknown_upload_is_refused(served: Served) -> None:
    gone = "0" * 32
    for route in ("verify", "preview"):
        r = await served.call("POST", f"/v1/backups/{route}", {"upload": gone})
        assert r.status == 400
        assert "upload it again" in r.error


async def test_core_361_an_upload_without_a_body_is_refused(served: Served) -> None:
    r = await upload(served, b"")
    assert r.status == 400

    def bare() -> int:
        conn = _UnixConnection(served.path, 10)
        conn.putrequest("POST", "/v1/backups/upload")
        conn.endheaders()  # no Content-Length at all
        status = conn.getresponse().status
        conn.close()
        return status

    assert await asyncio.to_thread(bare) == 400


async def test_core_361_a_body_over_2_gib_is_refused_from_the_header(
    served: Served, mirror: Mirror
) -> None:
    def ask() -> tuple[int, bytes]:
        conn = _UnixConnection(served.path, 10)
        conn.putrequest("POST", "/v1/backups/upload")
        conn.putheader("Content-Length", str(2 * 1024**3 + 1))
        conn.endheaders()  # no byte of the body follows
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    status, body = await asyncio.to_thread(ask)
    assert status == 413
    assert b"too_large" in body
    assert staged(mirror) == []


async def test_core_361_a_body_that_ends_early_leaves_nothing(
    served: Served, mirror: Mirror
) -> None:
    def cut() -> bytes:
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(10)
        sock.connect(str(served.path))
        sock.sendall(
            b"POST /v1/backups/upload HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n"
            + b"x" * 10
        )
        sock.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := sock.recv(4096):
            answer += chunk
        sock.close()
        return answer

    answer = await asyncio.to_thread(cut)
    assert answer.startswith(b"HTTP/1.1 400")
    assert staged(mirror) == []


async def test_core_361_an_upload_is_not_allowed_by_other_methods(served: Served) -> None:
    for method in ("GET", "PUT", "DELETE"):
        r = await served.call(method, "/v1/backups/upload")
        assert r.status == 404, method


async def test_core_361_a_dead_installer_is_reported_by_the_client(tmp_path: Path) -> None:
    client = InstallerClient(tmp_path / "none.sock", timeout=1)
    with pytest.raises(InstallerUnavailable):
        await asyncio.to_thread(client.upload, "/v1/backups/upload", iter([b"a"]), 1)


def test_core_361_the_constants_are_the_spec_values() -> None:
    assert MAX_STAGED == 2
    assert MAX_AGE_S == 24 * 3600
