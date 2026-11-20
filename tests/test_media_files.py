"""Milestone 8, step 6a: files in the admin and the upload limit (CORE-404, CORE-405).

The routes run on the admin listener and on the admin socket over
``httpx.ASGITransport``, with a real media folder in ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from mm3.core.config import load_config
from mm3.core.engine import Engine
from mm3.core.media import MediaService
from mm3.core.media import files as media_files
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings
from mm3.core.web.api import ApiServices
from mm3.core.web.apps import admin_app, protect, socket_app
from mm3.core.web.auth import WebAuth
from mm3.testing import VirtualClock

ADMIN = "http://127.0.0.1:8081"


class Admin:
    def __init__(self, tmp: Path) -> None:
        self.clock = VirtualClock()
        self.engine = Engine(
            EngineSettings(), registry=Registry(), clock=self.clock, state_dir=tmp / "e"
        )
        self.media = MediaService(tmp / "run", tmp / "state", self.clock)
        self.root = self.media.root
        self.auth = WebAuth.open(tmp / "web")
        self.services = ApiServices(self.engine, media=self.media)
        self.app = protect(admin_app(self.auth, api=self.services), lambda: [], "admin")

    def configure(self, text: str, tmp: Path) -> None:
        path = tmp / "config.yaml"
        path.write_text(text, encoding="utf-8")
        result = load_config(path, registry=Registry(), env_file=tmp / "x.env", environ={})
        assert result.ok, result.records
        self.media.configure(result)


@pytest.fixture
async def admin(tmp_path: Path) -> AsyncIterator[tuple[Admin, httpx.AsyncClient]]:
    a = Admin(tmp_path)
    a.configure("media:\n  limits: {upload_max_mb: 1}\n", tmp_path)
    await a.engine.start([])
    token = a.auth.tokens.new("t")
    transport = httpx.ASGITransport(app=a.app, client=("127.0.0.1", 5))
    headers = {"authorization": f"Bearer {token}", "origin": ADMIN}
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ADMIN, headers=headers) as c:
            yield a, c
    finally:
        await a.engine.stop()
        await a.media.close()


# --- CORE-405: listing -------------------------------------------------------------------


async def test_core_405_lists_kinds_sizes_and_times_without_links(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin
    files = a.root / "files"
    (files / "trip").mkdir()
    (files / "a.mp4").write_bytes(b"v" * 10)
    (files / "b.flac").write_bytes(b"a" * 3)
    (files / "c.JPG").write_bytes(b"i")
    (files / "notes.txt").write_text("no")
    (files / "link.mp4").symlink_to(files / "a.mp4")
    os.utime(files / "a.mp4", (0, 86400))
    r = await c.get("/api/v1/media/files", params={"path": "files"})
    assert r.status_code == 200
    got = r.json()
    assert [(e["name"], e["kind"], e["size"]) for e in got] == [
        ("a.mp4", "video", 10),
        ("b.flac", "audio", 3),
        ("c.JPG", "image", 1),
        ("trip", "folder", None),
    ]
    assert got[0]["modified"] == "1970-01-02T00:00:00Z"
    # The default path is files/; shares lists the shares.
    assert (await c.get("/api/v1/media/files")).json() == got
    (a.root / "shares" / "nas").mkdir()
    r = await c.get("/api/v1/media/files", params={"path": "shares"})
    assert [e["name"] for e in r.json()] == ["nas"]


async def test_core_405_missing_and_invalid_paths(admin: tuple[Admin, httpx.AsyncClient]) -> None:
    _, c = admin
    r = await c.get("/api/v1/media/files", params={"path": "files/nope"})
    assert (r.status_code, r.json()["code"]) == (404, "CORE_API_NOT_FOUND")
    for bad in ("files/../x", "/files", "other", "files//a"):
        r = await c.get("/api/v1/media/files", params={"path": bad})
        assert (r.status_code, r.json()["code"]) == (422, "CORE_API_INVALID_REQUEST"), bad


async def test_core_405_a_link_on_the_way_is_never_followed(
    admin: tuple[Admin, httpx.AsyncClient], tmp_path: Path
) -> None:
    a, c = admin
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"s")
    (a.root / "files" / "away").symlink_to(outside)
    r = await c.get("/api/v1/media/files", params={"path": "files/away"})
    assert r.status_code == 404
    r = await c.post(
        "/api/v1/media/files", params={"path": "files/away", "name": "x.mp4"}, content=b"x"
    )
    assert r.status_code == 404
    r = await c.delete("/api/v1/media/files", params={"path": "files/away/secret.mp4"})
    assert r.status_code == 404
    assert (outside / "secret.mp4").exists()


async def test_core_405_a_hung_folder_answers_503_and_the_loop_stays_free(
    admin: tuple[Admin, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, c = admin
    monkeypatch.setattr(media_files, "FOLDER_TIMEOUT_S", 0.2)

    def hung(root: Path, rel: str) -> list[object]:
        time.sleep(1.0)
        return []

    monkeypatch.setattr("mm3.core.web.mediafiles.list_folder", hung)
    started = time.monotonic()
    r = await c.get("/api/v1/media/files", params={"path": "shares/nas"})
    assert (r.status_code, r.json()["code"]) == (503, "CORE_API_UNAVAILABLE")
    assert time.monotonic() - started < 0.9
    # Other routes answer at once meanwhile.
    assert (await c.get("/api/v1/media")).status_code == 200


# --- CORE-405: uploads ------------------------------------------------------------------


async def test_core_405_upload_answers_201_with_the_entry(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin
    (a.root / "files" / "trip").mkdir()
    r = await c.post(
        "/api/v1/media/files", params={"path": "files/trip", "name": "clip.mp4"}, content=b"abc"
    )
    assert r.status_code == 201
    assert {k: r.json()[k] for k in ("name", "kind", "size")} == {
        "name": "clip.mp4",
        "kind": "video",
        "size": 3,
    }
    assert (a.root / "files" / "trip" / "clip.mp4").read_bytes() == b"abc"
    assert [p.name for p in (a.root / "files" / "trip").iterdir()] == ["clip.mp4"]


async def test_core_405_an_existing_name_is_409_unless_overwrite(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin
    q = {"path": "files", "name": "a.png"}
    assert (await c.post("/api/v1/media/files", params=q, content=b"1")).status_code == 201
    r = await c.post("/api/v1/media/files", params=q, content=b"22")
    assert (r.status_code, r.json()["code"]) == (409, "CORE_API_CONFLICT")
    assert (a.root / "files" / "a.png").read_bytes() == b"1"
    r = await c.post("/api/v1/media/files", params={**q, "overwrite": "true"}, content=b"22")
    assert r.status_code == 201
    assert (a.root / "files" / "a.png").read_bytes() == b"22"
    (a.root / "files" / "dir.mp4").mkdir()
    r = await c.post(
        "/api/v1/media/files",
        params={"path": "files", "name": "dir.mp4", "overwrite": "true"},
        content=b"x",
    )
    assert r.status_code == 409


async def test_core_405_names_extensions_and_folders_are_checked(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin
    for name in (".hidden.mp4", "a/b.mp4", "notes.txt", "x" * 252 + ".mp4", ""):
        r = await c.post(
            "/api/v1/media/files", params={"path": "files", "name": name}, content=b"x"
        )
        assert (r.status_code, r.json()["code"]) == (422, "CORE_API_INVALID_REQUEST"), name
    r = await c.post(
        "/api/v1/media/files", params={"path": "files/nope", "name": "a.mp4"}, content=b"x"
    )
    assert (r.status_code, r.json()["code"]) == (404, "CORE_API_NOT_FOUND")
    (a.root / "shares" / "nas").mkdir()
    r = await c.post(
        "/api/v1/media/files", params={"path": "shares/nas", "name": "a.mp4"}, content=b"x"
    )
    assert (r.status_code, r.json()["code"]) == (403, "CORE_API_FORBIDDEN")
    assert list((a.root / "shares" / "nas").iterdir()) == []


async def test_core_405_too_large_is_refused_from_the_header_before_reading(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin
    read: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        read.append(1)
        yield b"x" * 10

    size = 1024 * 1024 + 1  # upload_max_mb is 1
    r = await c.post(
        "/api/v1/media/files",
        params={"path": "files", "name": "big.mp4"},
        content=body(),
        headers={"content-length": str(size)},
    )
    assert (r.status_code, r.json()["code"]) == (413, "CORE_API_UPLOAD_TOO_LARGE")
    assert read == []
    assert list((a.root / "files").iterdir()) == []
    ok = await c.post(
        "/api/v1/media/files", params={"path": "files", "name": "max.mp4"}, content=b"x" * 2**20
    )
    assert ok.status_code == 201


async def test_core_405_without_content_length_or_a_body_of_another_length_no_file_appears(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin

    async def chunks(n: int) -> AsyncIterator[bytes]:
        for _ in range(n):
            yield b"y" * 100

    q = {"path": "files", "name": "a.mp3"}
    r = await c.post("/api/v1/media/files", params=q, content=chunks(2))  # chunked, no length
    assert (r.status_code, r.json()["code"]) == (422, "CORE_API_INVALID_REQUEST")
    for sent, declared in ((2, 300), (3, 150)):
        r = await c.post(
            "/api/v1/media/files",
            params=q,
            content=chunks(sent),
            headers={"content-length": str(declared)},
        )
        assert (r.status_code, r.json()["code"]) == (422, "CORE_API_INVALID_REQUEST")
    assert list((a.root / "files").iterdir()) == []


async def test_core_405_a_failed_write_removes_the_temporary_file(
    admin: tuple[Admin, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, c = admin

    def full(self: media_files.Upload, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(media_files.Upload, "write", full)
    r = await c.post(
        "/api/v1/media/files", params={"path": "files", "name": "a.mp4"}, content=b"abc"
    )
    assert r.status_code == 503
    assert list((a.root / "files").iterdir()) == []


# --- CORE-405: folders and deletes --------------------------------------------------------


async def test_core_405_new_folder_needs_a_parent_and_a_free_name(
    admin: tuple[Admin, httpx.AsyncClient],
) -> None:
    a, c = admin
    r = await c.post("/api/v1/media/folders", json={"path": "files/trip"})
    assert (r.status_code, r.json()["kind"], r.json()["name"]) == (201, "folder", "trip")
    assert (a.root / "files" / "trip").is_dir()
    r = await c.post("/api/v1/media/folders", json={"path": "files/trip"})
    assert (r.status_code, r.json()["code"]) == (409, "CORE_API_CONFLICT")
    r = await c.post("/api/v1/media/folders", json={"path": "files/a/b"})
    assert r.status_code == 404
    r = await c.post("/api/v1/media/folders", json={"path": "shares/nas"})
    assert r.status_code == 403
    r = await c.post("/api/v1/media/folders", json={"path": "files"})
    assert r.status_code == 422
    r = await c.post("/api/v1/media/folders", json={"path": "files/x", "more": 1})
    assert r.status_code == 422


async def test_core_405_delete_files_and_folders(admin: tuple[Admin, httpx.AsyncClient]) -> None:
    a, c = admin
    files = a.root / "files"
    (files / "trip" / "day1").mkdir(parents=True)
    (files / "trip" / "day1" / "a.jpg").write_bytes(b"1")
    (files / "empty").mkdir()
    (files / "one.mp4").write_bytes(b"1")
    assert (
        await c.delete("/api/v1/media/files", params={"path": "files/one.mp4"})
    ).status_code == 204
    assert not (files / "one.mp4").exists()
    assert (
        await c.delete("/api/v1/media/files", params={"path": "files/empty"})
    ).status_code == 204
    r = await c.delete("/api/v1/media/files", params={"path": "files/trip"})
    assert (r.status_code, r.json()["code"]) == (409, "CORE_API_CONFLICT")
    r = await c.delete("/api/v1/media/files", params={"path": "files/trip", "recursive": "true"})
    assert r.status_code == 204
    assert list(files.iterdir()) == []
    r = await c.delete("/api/v1/media/files", params={"path": "files", "recursive": "true"})
    assert (r.status_code, r.json()["code"]) == (422, "CORE_API_INVALID_REQUEST")
    assert files.is_dir()
    r = await c.delete("/api/v1/media/files", params={"path": "files/gone.mp4"})
    assert r.status_code == 404
    (a.root / "shares" / "nas").mkdir()
    (a.root / "shares" / "nas" / "a.mp4").write_bytes(b"1")
    r = await c.delete("/api/v1/media/files", params={"path": "shares/nas/a.mp4"})
    assert (r.status_code, r.json()["code"]) == (403, "CORE_API_FORBIDDEN")
    assert (a.root / "shares" / "nas" / "a.mp4").exists()


async def test_core_405_routes_need_admin_auth_and_are_on_the_admin_socket(
    tmp_path: Path,
) -> None:
    a = Admin(tmp_path)
    a.configure("media: {}\n", tmp_path)
    await a.engine.start([])
    try:
        transport = httpx.ASGITransport(app=a.app, client=("127.0.0.1", 5))
        async with httpx.AsyncClient(transport=transport, base_url=ADMIN) as c:
            assert (await c.get("/api/v1/media/files")).status_code == 401
        sock = httpx.ASGITransport(app=socket_app(a.services))
        async with httpx.AsyncClient(transport=sock, base_url="http://mm3") as c:
            r = await c.post(
                "/api/v1/media/files", params={"path": "files", "name": "s.png"}, content=b"p"
            )
            assert r.status_code == 201
            assert [e["name"] for e in (await c.get("/api/v1/media/files")).json()] == ["s.png"]
    finally:
        await a.engine.stop()
        await a.media.close()


def test_core_405_kinds_follow_the_extensions_of_core_404() -> None:
    from mm3.core.media.folder import MEDIA_EXTENSIONS

    kinds = {ext: media_files.entry_kind("x" + ext) for ext in MEDIA_EXTENSIONS}
    assert None not in kinds.values()
    assert {kinds[e] for e in (".mp4", ".m4v", ".webm")} == {"video"}
    assert {kinds[e] for e in (".gif", ".avif", ".png")} == {"image"}
    assert media_files.entry_kind("x.txt") is None


async def test_core_407_file_delivery_runs_on_the_folder_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from fastapi import FastAPI

    from mm3.core.web import media as web_media
    from mm3.core.web.media import add_media_routes

    media = MediaService(tmp_path / "run", tmp_path / "state", VirtualClock())
    a = Admin(tmp_path / "x")
    a.media = media
    a.configure("media: {}\n", tmp_path)
    (media.root / "files" / "a.mp4").write_bytes(b"abcdef")
    seen: list[str] = []
    real = web_media.open_file

    def spy(root: Path, rel: str) -> object:
        seen.append(threading.current_thread().name)
        return real(root, rel)

    monkeypatch.setattr(web_media, "open_file", spy)
    app = FastAPI()
    add_media_routes(app, media, "/media")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5))
    async with httpx.AsyncClient(transport=transport, base_url="http://m") as c:
        r = await c.get("/media/files/files/a.mp4")
        assert (r.status_code, r.content) == (200, b"abcdef")
        assert seen[0].startswith("mm3-media-folder")
        monkeypatch.setattr(media_files, "FOLDER_TIMEOUT_S", 0.2)

        def hung(root: Path, rel: str) -> object:
            time.sleep(1.0)
            return None

        monkeypatch.setattr(web_media, "open_file", hung)
        started = time.monotonic()
        r = await c.get("/media/files/files/a.mp4")
        assert r.status_code == 503
        assert time.monotonic() - started < 0.9
    await media.close()


def test_core_408_the_backups_page_offers_the_media_part() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "mm3" / "core"
    script = (root / "web" / "adminui" / "backups.js").read_text(encoding="utf-8")
    assert 'export const PARTS = ["config", "data", "modules", "secrets", "media"];' in script
    assert 'parts: ["config", "data", "modules"]' in script  # never a default
    for lang in ("en", "it", "fr", "es", "de"):
        catalog = json.loads((root / "locales" / lang / "messages.json").read_text("utf-8"))
        assert catalog.get("CORE_ADMIN_BACKUPS_PART_MEDIA"), lang
