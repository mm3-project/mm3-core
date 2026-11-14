"""Tests of the engine's catalog and installer API and the ``installer`` topic of ``/admin/ws``.

Implements:
- CORE-356 (Core spec 0.44, §10.7): the catalog routes read only the engine's copy of the index,
  the operation routes forward to the installer with the codes of CORE-298, and ``/admin/ws``
  has the topic ``installer``.
  The module detail (publisher, releases, check result) and the ``unlisted`` marking of an
  installed module the index does not list are part of it.
"""

# ruff: noqa: F811 - the shared `api` and `rig` fixtures are imported, then named by the tests
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer_fakes import entry, index_bytes, make_wheel, release
from mm3.core.catalog import CatalogMeta, read_cached
from mm3.core.logging import numbered_records
from mm3.core.web.apps import socket_app
from mm3.core.web.catalogapi import build_items
from mm3.core.web.installer import (
    OPERATION_FIELDS,
    UNAVAILABLE,
    InstallerLink,
    Snapshot,
    project_operation,
)
from mm3.core.web.problems import CODES
from mm3.installer.client import InstallerUnavailable, Reply
from test_admin_api import (
    Api,
    api,  # noqa: F401 - the shared admin API fixture
    is_problem,
)
from test_admin_diag import (
    Rig,
    recv,
    recv_kind,
    rig,  # noqa: F401 - the shared admin channel fixture
    send,
)

PASSPHRASE = "correct horse battery staple"  # noqa: S105 - test data
TOTP = "123456"
OP_A = "0123456789abcdef"
OP_B = "fedcba9876543210"


class ScriptedInstaller:
    """A stand-in for the installer socket with scripted state and replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, float | None]] = []
        self.down = False
        self.status: dict[str, Any] = {
            "busy": False,
            "locked_until": None,
            "installed": [],
        }
        self.operations: list[dict[str, Any]] = []
        self.replies: dict[tuple[str, str], Reply] = {}

    def request(
        self,
        method: str,
        target: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Reply:
        self.calls.append((method, target, body, timeout))
        if self.down:
            raise InstallerUnavailable("installer socket /run/mm3-install/installer.sock: gone")
        if (method, target) in self.replies:
            return self.replies[(method, target)]
        if (method, target) == ("GET", "/v1/status"):
            return Reply(200, self.status)
        if (method, target) == ("GET", "/v1/operations"):
            return Reply(200, {"operations": self.operations})
        if method == "GET" and target.startswith("/v1/operations/"):
            op_id = target.rsplit("/", 1)[1]
            found = next((o for o in self.operations if o["id"] == op_id), None)
            return Reply(200, found) if found else Reply(404, {"error": "no such operation"})
        if (method, target) == ("POST", "/v1/operations"):
            return Reply(202, {"id": OP_A})
        raise AssertionError(f"unscripted request {method} {target}")

    def count(self, target: str) -> int:
        return sum(1 for _, t, _, _ in self.calls if t == target)


def operation(
    op_id: str = OP_A,
    state: str = "queued",
    *,
    created: str = "2026-11-14T10:00:00+00:00",
    steps: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """An operation as the installer sends it (CORE-355), with its private fields."""
    return {
        "id": op_id,
        "action": "install",
        "name": "mm3-alpha",
        "client": "test (bearer) 127.0.0.1",
        "peer_uid": 996,
        "source": "catalog",
        "created_at": created,
        "state": state,
        "from": None,
        "to": "1.1.0",
        "steps": [{"at": created, "state": "queued", "detail": f"step {n}"} for n in range(steps)],
        "error": None,
        "finished_at": None,
        **extra,
    }


@pytest.fixture
def installer(api: Api) -> ScriptedInstaller:
    fake = ScriptedInstaller()
    api.services.installer = InstallerLink(fake)  # type: ignore[arg-type]
    return fake


def write_catalog(
    api: Api,
    modules: list[dict[str, Any]] | None = None,
    *,
    lifetime: timedelta = timedelta(days=7),
    generated: datetime | None = None,
) -> datetime:
    """Put a copy of the index in the engine's state directory, as the installer does."""
    now = api.clock.wall()
    generated = generated or now - timedelta(hours=1)
    mods = modules if modules is not None else default_modules()
    root = api.engine.supervisor.state_dir / "catalog"
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_bytes(
        index_bytes(mods, serial=12, generated=generated, lifetime=lifetime)
    )
    meta = CatalogMeta(12, now - timedelta(minutes=30), generated + lifetime)
    (root / "meta.json").write_text(json.dumps(meta.to_json()), encoding="utf-8")
    return now


def default_modules() -> list[dict[str, Any]]:
    alpha = make_wheel("mm3-alpha", "1.0.0")
    alpha2 = make_wheel("mm3-alpha", "1.1.0")
    beta = make_wheel("mm3-beta", "2.0.0")
    gamma = make_wheel("mm3-gamma", "0.9.0")
    return [
        entry(
            "mm3-gamma",
            [release("mm3-gamma", "0.9.0", gamma)],
            category="media",
            badge="official",
        ),
        entry(
            "mm3-alpha",
            [release("mm3-alpha", "1.0.0", alpha), release("mm3-alpha", "1.1.0", alpha2)],
            badge="verified",
        ),
        entry("mm3-beta", [release("mm3-beta", "2.0.0", beta, api=">=2,<3")]),
    ]


def by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {i["name"]: i for i in items}


# --- the codes ------------------------------------------------------------------------------


def test_core_356_the_new_codes() -> None:
    assert CODES["CORE_API_REAUTH_FAILED"][0] == 403
    assert CODES["CORE_API_INSTALLER_LOCKED"][0] == 423
    assert CODES["CORE_API_CATALOG_UNAVAILABLE"][0] == 503
    assert CODES["CORE_API_INSTALLER_UNAVAILABLE"][0] == 503
    assert CODES["CORE_API_INSTALLER_BUSY"][0] == 409


# --- GET /api/v1/catalog --------------------------------------------------------------------


async def test_core_356_the_catalog_lists_the_index_with_its_state(
    api: Api, installer: ScriptedInstaller
) -> None:
    now = write_catalog(api)
    installer.status["installed"] = [{"name": "mm3-alpha", "version": "1.0.0", "origin": "catalog"}]
    r = await api.get("/api/v1/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["installer_available"] is True
    assert body["catalog"] == {
        "serial": 12,
        "fetched_at": (now - timedelta(minutes=30)).isoformat(),
        "expires": (now - timedelta(hours=1) + timedelta(days=7)).isoformat(),
        "stale": False,
    }
    assert [i["name"] for i in body["items"]] == ["mm3-alpha", "mm3-beta", "mm3-gamma"]
    alpha = by_name(body["items"])["mm3-alpha"]
    assert alpha == {
        "name": "mm3-alpha",
        "pypi": "mm3-alpha",
        "repository": "https://github.com/example/mm3-alpha",
        "badge": "verified",
        "category": "info",
        "license": "MIT",
        "description": "The mm3-alpha module",
        "compatible": True,
        "incompatible_reason": None,
        "latest": "1.1.0",
        "installed": "1.0.0",
        "origin": "catalog",
        "update_available": True,
        "unlisted": False,
    }
    gamma = by_name(body["items"])["mm3-gamma"]
    assert (gamma["installed"], gamma["origin"], gamma["update_available"]) == (None, None, False)
    assert installer.calls[-1][1] != "/v1/catalog/refresh"  # the list never asks the network


async def test_core_356_a_module_with_no_compatible_release_is_listed_with_the_reason(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    beta = by_name((await api.get("/api/v1/catalog")).json()["items"])["mm3-beta"]
    assert beta["compatible"] is False
    assert beta["incompatible_reason"] == "api"
    assert beta["latest"] is None
    assert beta["update_available"] is False


async def test_core_356_installed_and_origin_are_null_without_the_installer(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    installer.status["installed"] = [{"name": "mm3-alpha", "version": "1.0.0", "origin": "catalog"}]
    installer.down = True
    body = (await api.get("/api/v1/catalog")).json()
    assert body["installer_available"] is False
    assert all(i["installed"] is None and i["origin"] is None for i in body["items"])
    assert all(i["update_available"] is False for i in body["items"])
    api.services.installer = None  # a system without the installer
    assert (await api.get("/api/v1/catalog")).json()["installer_available"] is False


async def test_core_356_unlisted_an_installed_module_the_index_does_not_list_is_unlisted(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    installer.status["installed"] = [{"name": "mm3-local", "version": "0.1.0", "origin": "local"}]
    items = by_name((await api.get("/api/v1/catalog")).json()["items"])
    assert items["mm3-local"] == {
        "name": "mm3-local",
        "pypi": None,
        "repository": None,
        "badge": None,
        "category": None,
        "license": None,
        "description": None,
        "compatible": None,
        "incompatible_reason": None,
        "latest": None,
        "installed": "0.1.0",
        "origin": "local",
        "update_available": False,
        "unlisted": True,
    }
    assert [i["name"] for i in (await api.get("/api/v1/catalog")).json()["items"]] == [
        "mm3-alpha",
        "mm3-beta",
        "mm3-gamma",
        "mm3-local",
    ]


async def test_core_356_q_and_category_filter_the_items(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    installer.status["installed"] = [{"name": "mm3-local", "version": "0.1.0", "origin": "local"}]

    async def names(query: str) -> list[str]:
        r = await api.get(f"/api/v1/catalog{query}")
        assert r.status_code == 200, r.text
        return [i["name"] for i in r.json()["items"]]

    assert await names("?q=ALPHA") == ["mm3-alpha"]  # ignoring case, in the name
    assert await names("?q=the%20mm3-be") == ["mm3-beta"]  # in the description
    assert await names("?q=local") == ["mm3-local"]  # an unlisted module matches on its name
    assert await names("?category=media") == ["mm3-gamma"]
    assert await names("?category=media&q=alpha") == []
    assert await names("?q=nothing-like-it") == []
    assert await names("") == ["mm3-alpha", "mm3-beta", "mm3-gamma", "mm3-local"]


async def test_core_356_an_unknown_query_key_is_422(api: Api, installer: ScriptedInstaller) -> None:
    write_catalog(api)
    body = is_problem(await api.get("/api/v1/catalog?sort=name"), 422, "CORE_API_INVALID_REQUEST")
    assert "sort" in body["detail"]


def test_core_356_the_description_is_in_the_admin_language(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    now = datetime.fromisoformat("2026-11-06T12:00:00+00:00")
    (root / "index.json").write_bytes(
        index_bytes(default_modules(), generated=now - timedelta(hours=1))
    )
    meta = CatalogMeta(10, now, now + timedelta(days=6))
    (root / "meta.json").write_text(json.dumps(meta.to_json()), encoding="utf-8")
    cached = read_cached(tmp_path, now)
    snapshot = Snapshot(available=True)
    italian = by_name(build_items(cached, snapshot, "it"))
    assert italian["mm3-alpha"]["description"] == "Il modulo mm3-alpha"
    german = by_name(build_items(cached, snapshot, "de"))  # no German text: English
    assert german["mm3-alpha"]["description"] == "The mm3-alpha module"
    assert [i["name"] for i in build_items(cached, snapshot, "it", q="il modulo mm3-g")] == [
        "mm3-gamma"
    ]


async def test_core_356_an_expired_copy_is_served_as_stale(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api, lifetime=timedelta(days=14), generated=api.clock.wall() - timedelta(days=15))
    r = await api.get("/api/v1/catalog")
    assert r.status_code == 200, r.text
    assert r.json()["catalog"]["stale"] is True
    assert len(r.json()["items"]) == 3


async def test_core_356_no_copy_of_the_index_is_503_catalog_unavailable(
    api: Api, installer: ScriptedInstaller
) -> None:
    for path in ("/api/v1/catalog", "/api/v1/catalog/mm3-alpha"):
        body = is_problem(await api.get(path), 503, "CORE_API_CATALOG_UNAVAILABLE")
        assert "no cached catalog index" in body["detail"]
    root = api.engine.supervisor.state_dir / "catalog"
    root.mkdir(parents=True)
    (root / "index.json").write_text("{not json", encoding="utf-8")
    (root / "meta.json").write_text("{}", encoding="utf-8")
    is_problem(await api.get("/api/v1/catalog"), 503, "CORE_API_CATALOG_UNAVAILABLE")


async def test_core_356_the_catalog_routes_need_authentication(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    for method, path in (
        ("GET", "/api/v1/catalog"),
        ("GET", "/api/v1/catalog/mm3-alpha"),
        ("POST", "/api/v1/catalog/refresh"),
        ("POST", "/api/v1/installer/operations"),
        ("GET", "/api/v1/installer/operations"),
        ("GET", f"/api/v1/installer/operations/{OP_A}"),
    ):
        is_problem(await api.client.request(method, path), 401, "CORE_API_UNAUTHORIZED")
    assert installer.calls == []


# --- GET /api/v1/catalog/{name} -------------------------------------------------------------


async def test_core_356_detail_the_detail_has_publisher_releases_and_the_check_result(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    installer.status["installed"] = [{"name": "mm3-alpha", "version": "1.0.0", "origin": "catalog"}]
    r = await api.get("/api/v1/catalog/mm3-alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["installed"] == "1.0.0"
    assert body["latest"] == "1.1.0"
    assert body["publisher"] == {
        "repository": "example/mm3-alpha",
        "workflow": "release.yml",
        "environment": None,
    }
    assert [x["version"] for x in body["releases"]] == ["1.1.0", "1.0.0"]  # newest first
    newest = body["releases"][0]
    assert newest == {
        "version": "1.1.0",
        "api": ">=1.0,<2",
        "compatible": True,
        "capabilities": ["http.outbound"],
        "grants": {"http": ["https://api.example.com/"]},
        "check": {"result": "pass", "report_url": "https://example.com/report"},
    }
    assert "files" not in json.dumps(body)
    assert "sha256" not in json.dumps(body)


async def test_core_356_detail_the_detail_shows_an_incompatible_release_and_finds_a_normalized_name(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    body = (await api.get("/api/v1/catalog/MM3_Beta")).json()
    assert body["name"] == "mm3-beta"
    assert body["compatible"] is False
    assert [(r["version"], r["compatible"]) for r in body["releases"]] == [("2.0.0", False)]


async def test_core_356_detail_an_unknown_name_is_404_and_an_unlisted_one_is_found(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    is_problem(await api.get("/api/v1/catalog/nothing"), 404, "CORE_API_NOT_FOUND")
    installer.status["installed"] = [{"name": "mm3-local", "version": "0.1.0", "origin": "local"}]
    body = (await api.get("/api/v1/catalog/mm3-local")).json()
    assert body["unlisted"] is True
    assert body["publisher"] is None
    assert body["releases"] == []


# --- POST /api/v1/catalog/refresh -----------------------------------------------------------


async def test_core_356_refresh_forwards_to_the_installer_and_answers_the_catalog(
    api: Api, installer: ScriptedInstaller
) -> None:
    now = api.clock.wall()
    meta = CatalogMeta(13, now, now + timedelta(days=6)).to_json()
    installer.replies[("POST", "/v1/catalog/refresh")] = Reply(200, {"catalog": meta})
    r = await api.post("/api/v1/catalog/refresh")
    assert r.status_code == 200, r.text
    assert r.json() == {"catalog": {**meta, "stale": False}}
    method, target, body, timeout = installer.calls[-1]
    assert (method, target, body) == ("POST", "/v1/catalog/refresh", None)
    assert timeout is not None
    assert timeout > 10


async def test_core_356_a_refused_refresh_is_503_with_the_reason(
    api: Api, installer: ScriptedInstaller
) -> None:
    installer.replies[("POST", "/v1/catalog/refresh")] = Reply(
        502, {"error": "index signature does not verify: bad"}
    )
    body = is_problem(
        await api.post("/api/v1/catalog/refresh"), 503, "CORE_API_CATALOG_UNAVAILABLE"
    )
    assert body["detail"] == "index signature does not verify: bad"
    installer.down = True
    is_problem(await api.post("/api/v1/catalog/refresh"), 503, "CORE_API_INSTALLER_UNAVAILABLE")
    api.services.installer = None
    is_problem(await api.post("/api/v1/catalog/refresh"), 503, "CORE_API_INSTALLER_UNAVAILABLE")


# --- GET /api/v1/installer ------------------------------------------------------------------


async def test_core_356_installer_status_has_the_lock(
    api: Api, installer: ScriptedInstaller
) -> None:
    assert (await api.get("/api/v1/installer")).json() == {
        "available": True,
        "busy": False,
        "locked_until": None,
    }
    installer.status.update(busy=True, locked_until="2026-11-14T11:00:00+00:00")
    assert (await api.get("/api/v1/installer")).json() == {
        "available": True,
        "busy": True,
        "locked_until": "2026-11-14T11:00:00+00:00",
    }
    installer.down = True
    assert (await api.get("/api/v1/installer")).json() == UNAVAILABLE.status()
    assert UNAVAILABLE.status() == {"available": False, "busy": False, "locked_until": None}


# --- POST /api/v1/installer/operations ------------------------------------------------------


async def test_core_356_an_operation_is_forwarded_with_the_caller_and_answers_202(
    api: Api, installer: ScriptedInstaller
) -> None:
    r = await api.post(
        "/api/v1/installer/operations",
        {"action": "install", "name": "mm3-alpha", "version": "1.1.0", "passphrase": PASSPHRASE},
    )
    assert r.status_code == 202, r.text
    assert r.json() == {"id": OP_A}
    assert r.headers["location"] == f"/api/v1/installer/operations/{OP_A}"
    method, target, body, _ = installer.calls[-1]
    assert (method, target) == ("POST", "/v1/operations")
    assert body == {
        "action": "install",
        "name": "mm3-alpha",
        "version": "1.1.0",
        "passphrase": PASSPHRASE,
        "client": "test (bearer) 127.0.0.1",
    }
    assert PASSPHRASE not in r.text


async def test_core_356_every_action_passes_the_passphrase_and_the_code_on(
    api: Api, installer: ScriptedInstaller
) -> None:
    for payload in (
        {"action": "install", "name": "mm3-alpha"},
        {"action": "update", "name": "mm3-alpha"},
        {"action": "remove", "name": "mm3-alpha"},
        {"action": "restore_previous"},
    ):
        r = await api.post(
            "/api/v1/installer/operations", {**payload, "passphrase": PASSPHRASE, "totp": TOTP}
        )
        assert r.status_code == 202, r.text
        sent = installer.calls[-1][2]
        assert sent is not None
        assert sent["passphrase"] == PASSPHRASE
        assert sent["totp"] == TOTP
        assert sent["action"] == payload["action"]


async def test_core_356_the_passphrase_and_the_code_are_never_logged_or_answered(
    api: Api, installer: ScriptedInstaller
) -> None:
    secret = "never-in-a-log-9f2c"  # noqa: S105 - test data
    r = await api.post(
        "/api/v1/installer/operations",
        {"action": "remove", "name": "mm3-alpha", "passphrase": secret, "totp": "654321"},
    )
    assert r.status_code == 202
    for status, reply in (
        (403, {"error": "wrong"}),
        (423, {"error": "locked"}),
        (400, {"error": "x"}),
    ):
        installer.replies[("POST", "/v1/operations")] = Reply(status, reply)
        again = await api.post(
            "/api/v1/installer/operations",
            {"action": "remove", "name": "mm3-alpha", "passphrase": secret, "totp": "654321"},
        )
        assert secret not in again.text
        assert "654321" not in again.text
    invalid = await api.post(
        "/api/v1/installer/operations", {"action": "nope", "passphrase": secret, "totp": "654321"}
    )
    assert invalid.status_code == 422
    assert secret not in invalid.text
    assert "654321" not in invalid.text
    dumped = json.dumps([record for _, record in numbered_records()], default=str)
    assert secret not in dumped
    assert "654321" not in dumped


@pytest.mark.parametrize(
    "body",
    [
        {"action": "install", "name": "mm3-alpha", "passphrase": PASSPHRASE, "source": "pypi"},
        {"action": "install", "name": "mm3-alpha", "passphrase": PASSPHRASE, "path": "/x.whl"},
        {"action": "install", "name": "mm3-alpha", "passphrase": PASSPHRASE, "client": "root"},
        {"action": "install", "name": "mm3-alpha"},  # no passphrase
        {"action": "install", "name": "mm3-alpha", "passphrase": ""},
        {"action": "install", "passphrase": PASSPHRASE},  # no name
        {"action": "update", "name": "mm3-alpha", "version": "1.0", "passphrase": PASSPHRASE},
        {"action": "remove", "name": "mm3-alpha", "version": "1.0", "passphrase": PASSPHRASE},
        {"action": "restore_previous", "name": "mm3-alpha", "passphrase": PASSPHRASE},
        {"action": "restore_previous", "version": "1.0", "passphrase": PASSPHRASE},
        {"action": "reboot", "passphrase": PASSPHRASE},
        {"name": "mm3-alpha", "passphrase": PASSPHRASE},
    ],
)
async def test_core_356_a_malformed_operation_is_422_and_never_reaches_the_installer(
    api: Api, installer: ScriptedInstaller, body: dict[str, Any]
) -> None:
    is_problem(
        await api.post("/api/v1/installer/operations", body), 422, "CORE_API_INVALID_REQUEST"
    )
    assert installer.calls == []


@pytest.mark.parametrize(
    ("reply", "status", "code"),
    [
        (Reply(403, {"error": "wrong passphrase"}), 403, "CORE_API_REAUTH_FAILED"),
        (Reply(403, {"error": "wrong TOTP code"}), 403, "CORE_API_REAUTH_FAILED"),
        (Reply(423, {"error": "installer locked until x"}), 423, "CORE_API_INSTALLER_LOCKED"),
        (Reply(409, {"error": "another operation is running"}), 409, "CORE_API_INSTALLER_BUSY"),
        (Reply(400, {"error": "name must be a package name"}), 422, "CORE_API_INVALID_REQUEST"),
        (Reply(500, {"error": "internal error"}), 503, "CORE_API_INSTALLER_UNAVAILABLE"),
        (Reply(502, {"error": "bad gateway"}), 503, "CORE_API_INSTALLER_UNAVAILABLE"),
    ],
)
async def test_core_356_the_installers_answers_become_the_codes_of_core_298(
    api: Api, installer: ScriptedInstaller, reply: Reply, status: int, code: str
) -> None:
    installer.replies[("POST", "/v1/operations")] = reply
    r = await api.post(
        "/api/v1/installer/operations",
        {"action": "remove", "name": "mm3-alpha", "passphrase": PASSPHRASE},
    )
    body = is_problem(r, status, code)
    if code == "CORE_API_REAUTH_FAILED":
        # never says whether the passphrase or the code was wrong
        assert "passphrase" not in body["detail"].lower()
        assert "totp" not in body["detail"].lower()
    if code == "CORE_API_INVALID_REQUEST":
        assert body["detail"] == "name must be a package name"


async def test_core_356_a_lock_carries_locked_until(api: Api, installer: ScriptedInstaller) -> None:
    installer.replies[("POST", "/v1/operations")] = Reply(423, {"error": "locked"})
    installer.status["locked_until"] = "2026-11-14T11:30:00+00:00"
    body = is_problem(
        await api.post(
            "/api/v1/installer/operations",
            {"action": "update", "name": "mm3-alpha", "passphrase": PASSPHRASE},
        ),
        423,
        "CORE_API_INSTALLER_LOCKED",
    )
    assert body["locked_until"] == "2026-11-14T11:30:00+00:00"
    installer.status["locked_until"] = None
    assert (
        is_problem(
            await api.post(
                "/api/v1/installer/operations",
                {"action": "update", "name": "mm3-alpha", "passphrase": PASSPHRASE},
            ),
            423,
            "CORE_API_INSTALLER_LOCKED",
        )["locked_until"]
        is None
    )


async def test_core_356_without_an_installer_an_operation_is_503(
    api: Api, installer: ScriptedInstaller
) -> None:
    payload = {"action": "install", "name": "mm3-alpha", "passphrase": PASSPHRASE}
    installer.down = True
    is_problem(
        await api.post("/api/v1/installer/operations", payload),
        503,
        "CORE_API_INSTALLER_UNAVAILABLE",
    )
    api.services.installer = None
    is_problem(
        await api.post("/api/v1/installer/operations", payload),
        503,
        "CORE_API_INSTALLER_UNAVAILABLE",
    )
    is_problem(await api.get("/api/v1/installer/operations"), 503, "CORE_API_INSTALLER_UNAVAILABLE")


async def test_core_356_a_package_the_installer_refuses_is_a_failed_operation_not_an_error(
    api: Api, installer: ScriptedInstaller
) -> None:
    failed = operation(
        state="failed",
        steps=2,
        error="mm3-nothing is not in the catalog",
        finished_at="2026-11-14T10:00:02+00:00",
    )
    installer.operations = [failed]
    r = await api.post(
        "/api/v1/installer/operations",
        {"action": "install", "name": "mm3-nothing", "passphrase": PASSPHRASE},
    )
    assert r.status_code == 202
    got = await api.get(f"/api/v1/installer/operations/{OP_A}")
    assert got.status_code == 200
    assert got.json()["state"] == "failed"
    assert got.json()["error"] == "mm3-nothing is not in the catalog"


# --- GET /api/v1/installer/operations -------------------------------------------------------


async def test_core_356_operations_are_listed_newest_first_without_private_fields(
    api: Api, installer: ScriptedInstaller
) -> None:
    older = operation(OP_A, "done", created="2026-11-14T09:00:00+00:00", steps=3)
    newer = operation(OP_B, "building", created="2026-11-14T10:00:00+00:00", steps=2)
    installer.operations = [older, newer]  # whatever order the installer uses
    r = await api.get("/api/v1/installer/operations")
    assert r.status_code == 200, r.text
    ops = r.json()
    assert [o["id"] for o in ops] == [OP_B, OP_A]
    for op in ops:
        assert tuple(op) == OPERATION_FIELDS
        assert "source" not in op
        assert "peer_uid" not in op
    assert ops[0]["steps"][0] == {
        "at": "2026-11-14T10:00:00+00:00",
        "state": "queued",
        "detail": "step 0",
    }
    assert project_operation({"id": "x", "source": "pypi", "peer_uid": 0})["id"] == "x"


async def test_core_356_one_operation_by_id_and_unknown_ids_are_404(
    api: Api, installer: ScriptedInstaller
) -> None:
    installer.operations = [operation(OP_A, "done")]
    got = await api.get(f"/api/v1/installer/operations/{OP_A}")
    assert got.status_code == 200
    assert got.json()["id"] == OP_A
    assert "peer_uid" not in got.json()
    is_problem(await api.get(f"/api/v1/installer/operations/{OP_B}"), 404, "CORE_API_NOT_FOUND")
    before = len(installer.calls)
    for bad in ("..%2Fstatus", "short", "0123456789ABCDEF", "x" * 40):
        is_problem(await api.get(f"/api/v1/installer/operations/{bad}"), 404, "CORE_API_NOT_FOUND")
    assert len(installer.calls) == before  # a malformed id is never put into the installer's path


# --- the admin socket -----------------------------------------------------------------------


async def test_core_356_the_socket_serves_the_catalog_but_not_the_operations(
    api: Api, installer: ScriptedInstaller
) -> None:
    write_catalog(api)
    installer.replies[("POST", "/v1/catalog/refresh")] = Reply(
        200, {"catalog": CatalogMeta(13, api.clock.wall(), api.clock.wall()).to_json()}
    )
    app = socket_app(api.services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://socket") as client:
        assert (await client.get("/api/v1/catalog")).status_code == 200
        assert (await client.get("/api/v1/catalog/mm3-alpha")).status_code == 200
        assert (await client.post("/api/v1/catalog/refresh")).status_code == 200
        assert (await client.get("/api/v1/installer")).status_code == 200
        for method, path in (
            ("POST", "/api/v1/installer/operations"),
            ("GET", "/api/v1/installer/operations"),
            ("GET", f"/api/v1/installer/operations/{OP_A}"),
        ):
            r = await client.request(method, path, json={} if method == "POST" else None)
            is_problem(r, 404, "CORE_API_NOT_FOUND")
        paths = (await client.get("/api/openapi.json")).json()["paths"]
    assert "/api/v1/catalog" in paths
    assert "/api/v1/installer" in paths
    assert not any("/installer/operations" in p for p in paths)
    schema = (await api.get("/api/openapi.json")).json()["paths"]
    assert "/api/v1/installer/operations" in schema


# --- the topic `installer` of /admin/ws -----------------------------------------------------


@pytest.fixture
def feed(rig: Rig) -> ScriptedInstaller:
    fake = ScriptedInstaller()
    rig.channel.installer = InstallerLink(fake)  # type: ignore[arg-type]
    rig.channel.installer_interval = 0.05
    rig.channel.installer_away_interval = 0.15
    return fake


async def installer_frame(ws: Any, wait: float = 3.0) -> dict[str, Any]:
    frame = await recv_kind(ws, "installer", wait)
    data: dict[str, Any] = frame["data"]
    assert set(data) == {"status", "operation"}
    return data


async def quiet(ws: Any, wait: float = 0.4) -> None:
    with pytest.raises(TimeoutError):
        await recv(ws, wait=wait)


async def test_core_356_at_subscribe_one_frame_then_each_operation_not_yet_final(
    rig: Rig, feed: ScriptedInstaller
) -> None:
    feed.operations = [
        operation("00000000000000c3", "done", created="2026-11-14T08:00:00+00:00"),
        operation("00000000000000b2", "building", created="2026-11-14T10:00:00+00:00"),
        operation("00000000000000a1", "queued", created="2026-11-14T09:00:00+00:00"),
        operation("00000000000000d4", "rolled_back", created="2026-11-14T07:00:00+00:00"),
        operation("00000000000000e5", "failed", created="2026-11-14T06:00:00+00:00"),
    ]
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer"})
    first = await installer_frame(ws)
    assert first == {
        "status": {"available": True, "busy": False, "locked_until": None},
        "operation": None,
    }
    second = await installer_frame(ws)
    third = await installer_frame(ws)
    assert [second["operation"]["id"], third["operation"]["id"]] == [
        "00000000000000a1",  # oldest first
        "00000000000000b2",
    ]
    assert "peer_uid" not in second["operation"]
    assert "source" not in second["operation"]
    await quiet(ws)  # nothing changed, nothing is sent
    await ws.close()


async def test_core_356_a_changed_operation_is_one_frame_with_its_newest_state(
    rig: Rig, feed: ScriptedInstaller
) -> None:
    feed.operations = [operation(OP_A, "queued", steps=1)]
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer"})
    await installer_frame(ws)
    await installer_frame(ws)  # the queued operation
    await quiet(ws, 0.3)
    feed.operations = [operation(OP_A, "building", steps=4)]  # several steps within one reading
    changed = await installer_frame(ws)
    assert changed["operation"]["state"] == "building"
    assert len(changed["operation"]["steps"]) == 4
    await quiet(ws, 0.3)
    feed.operations = [operation(OP_A, "done", steps=5, finished_at="2026-11-14T10:05:00+00:00")]
    done = await installer_frame(ws)
    assert done["operation"]["state"] == "done"
    await quiet(ws, 0.3)
    feed.operations = [
        operation(OP_A, "done", steps=5, finished_at="2026-11-14T10:05:00+00:00"),
        operation(OP_B, "queued", created="2026-11-14T10:10:00+00:00"),  # a new one
    ]
    assert (await installer_frame(ws))["operation"]["id"] == OP_B
    await ws.close()


async def test_core_356_a_change_of_status_alone_is_a_frame_with_no_operation(
    rig: Rig, feed: ScriptedInstaller
) -> None:
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer"})
    assert (await installer_frame(ws))["status"]["busy"] is False
    feed.status["busy"] = True
    busy = await installer_frame(ws)
    assert busy == {
        "status": {"available": True, "busy": True, "locked_until": None},
        "operation": None,
    }
    feed.status["locked_until"] = "2026-11-14T12:00:00+00:00"
    locked = await installer_frame(ws)
    assert locked["status"]["locked_until"] == "2026-11-14T12:00:00+00:00"
    await quiet(ws)
    await ws.close()


async def test_core_356_an_unreachable_installer_is_read_less_often_and_comes_back(
    rig: Rig, feed: ScriptedInstaller
) -> None:
    feed.down = True
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer"})
    away = await installer_frame(ws)
    assert away == {
        "status": {"available": False, "busy": False, "locked_until": None},
        "operation": None,
    }
    before = feed.count("/v1/status")
    await asyncio.sleep(0.6)
    slow = feed.count("/v1/status") - before
    feed.down = False
    back = await installer_frame(ws)
    assert back["status"]["available"] is True
    assert back["operation"] is None
    before = feed.count("/v1/status")
    await asyncio.sleep(0.6)
    fast = feed.count("/v1/status") - before
    assert slow <= 5 < fast  # 0.15 s against 0.05 s
    await ws.close()


async def test_core_356_the_installer_is_never_read_without_a_subscriber(
    rig: Rig, feed: ScriptedInstaller
) -> None:
    await asyncio.sleep(0.3)
    assert feed.calls == []  # nobody subscribed
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer"})
    await installer_frame(ws)
    assert feed.calls
    await send(ws, "unsubscribe", {"topic": "installer"})
    await asyncio.sleep(0.15)
    settled = len(feed.calls)
    await asyncio.sleep(0.4)
    assert len(feed.calls) == settled
    await send(ws, "subscribe", {"topic": "installer"})  # and again
    await installer_frame(ws)
    await ws.close()
    await asyncio.sleep(0.15)
    settled = len(feed.calls)
    await asyncio.sleep(0.4)
    assert len(feed.calls) == settled  # the channel closed: the reading ended with it


async def test_core_356_the_topic_takes_no_filter(rig: Rig, feed: ScriptedInstaller) -> None:
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer", "filter": {"state": "done"}})
    await quiet(ws)  # ignored and logged, like any invalid subscribe (CORE-316)
    assert feed.calls == []
    await ws.close()


async def test_core_356_without_a_link_the_topic_says_the_installer_is_unavailable(
    rig: Rig,
) -> None:
    rig.channel.installer = None
    rig.channel.installer_away_interval = 0.05
    ws = await rig.admin()
    await send(ws, "subscribe", {"topic": "installer"})
    frame = await installer_frame(ws)
    assert frame["status"] == {"available": False, "busy": False, "locked_until": None}
    await quiet(ws)
    await ws.close()


async def test_core_356_the_frames_of_one_subscriber_do_not_depend_on_another(
    rig: Rig, feed: ScriptedInstaller
) -> None:
    feed.operations = [operation(OP_A, "building", steps=2)]
    first = await rig.admin()
    second = await rig.admin()
    for ws in (first, second):
        await send(ws, "subscribe", {"topic": "installer"})
    for ws in (first, second):
        assert (await installer_frame(ws))["operation"] is None
        assert (await installer_frame(ws))["operation"]["id"] == OP_A
    feed.operations = [operation(OP_A, "done", steps=3)]
    for ws in (first, second):
        assert (await installer_frame(ws))["operation"]["state"] == "done"
    await first.close()
    await second.close()
