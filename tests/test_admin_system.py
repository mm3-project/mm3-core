"""Tests of the engine's restart and reboot routes and ``GET /api/v1/installer``.

Implements:
- CORE-382 (Core spec 0.42, §9.1): the three routes forward to the installer, answer 202, map
  its refusals to 409 and 503, and are not on the admin socket.
- CORE-356 (Core spec 0.33, §10.7): ``GET /api/v1/installer`` and the installer problem codes.
"""

# ruff: noqa: F811 - the shared `api` fixture is imported, then named by the tests
from __future__ import annotations

from typing import Any

import httpx
import pytest

from mm3.core.web.apps import socket_app
from mm3.core.web.installer import SYSTEM_ACTIONS, InstallerLink
from mm3.core.web.problems import CODES
from mm3.core.web.sessions import ADMIN_COOKIE
from mm3.installer.client import InstallerUnavailable, Reply
from test_admin_api import (
    Api,
    api,  # noqa: F401 - the shared admin API fixture
    is_problem,
)

LOOPBACK = ("127.0.0.1", 50000)


class FakeInstaller:
    """A stand-in for the installer socket client with scripted replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.replies: dict[tuple[str, str], Reply] = {}
        self.down = False

    def request(self, method: str, target: str, body: dict[str, Any] | None = None) -> Reply:
        self.calls.append((method, target, body))
        if self.down:
            raise InstallerUnavailable("installer socket /run/mm3-install/installer.sock: gone")
        if (method, target) in self.replies:
            return self.replies[(method, target)]
        if method == "GET":
            return Reply(200, {"busy": False})
        action = target.rsplit("/", 1)[1]
        return Reply(202, {"action": action, "requested_at": "2026-11-14T10:00:00+00:00"})


@pytest.fixture
def installer(api: Api) -> FakeInstaller:
    fake = FakeInstaller()
    api.services.installer = InstallerLink(fake)  # type: ignore[arg-type]
    return fake


def test_core_382_the_new_codes_are_in_the_list() -> None:
    assert CODES["CORE_API_INSTALLER_UNAVAILABLE"][0] == 503
    assert CODES["CORE_API_INSTALLER_BUSY"][0] == 409


@pytest.mark.parametrize("action", SYSTEM_ACTIONS)
async def test_core_382_each_route_forwards_and_answers_202(
    api: Api,
    installer: FakeInstaller,
    action: str,
) -> None:
    r = await api.post(f"/api/v1/system/{action}")
    assert r.status_code == 202, r.text
    assert r.json() == {"action": action, "requested_at": "2026-11-14T10:00:00+00:00"}
    method, target, body = installer.calls[-1]
    assert (method, target) == ("POST", f"/v1/system/{action}")
    assert body is not None
    assert body["client"] == "test (bearer) 127.0.0.1"  # the token's name and the address


async def test_core_382_a_browser_session_is_named_with_its_token_and_origin(
    api: Api,
    installer: FakeInstaller,
) -> None:
    hub = api.services.hub
    assert hub is not None
    rec = hub.auth.tokens.verify(api.token, "admin")
    assert rec is not None
    sid = hub.auth.sessions.create(rec)
    api.client.cookies.set(ADMIN_COOKIE, sid)
    # A session needs a matching Origin for an unsafe method (CORE-253).
    refused = await api.client.post("/api/v1/system/reboot")
    is_problem(refused, 403, "CORE_API_FORBIDDEN")
    r = await api.client.post("/api/v1/system/reboot", headers={"origin": "http://127.0.0.1:8081"})
    assert r.status_code == 202, r.text
    assert installer.calls[-1][2] == {"client": "test (session) 127.0.0.1"}


async def test_core_382_the_routes_need_authentication(
    api: Api,
    installer: FakeInstaller,
) -> None:
    for action in SYSTEM_ACTIONS:
        r = await api.client.post(f"/api/v1/system/{action}")
        is_problem(r, 401, "CORE_API_UNAUTHORIZED")
    assert installer.calls == []


async def test_core_382_a_busy_installer_is_409_installer_busy(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.replies[("POST", "/v1/system/reboot")] = Reply(
        409, {"error": "operation 1234 is running", "code": "busy"}
    )
    body = is_problem(await api.post("/api/v1/system/reboot"), 409, "CORE_API_INSTALLER_BUSY")
    assert "operation 1234" in body["detail"]


async def test_core_382_no_kiosk_unit_is_409_no_kiosk(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.replies[("POST", "/v1/system/restart-kiosk")] = Reply(
        409, {"error": "mm3-kiosk.service is not installed", "code": "no_kiosk"}
    )
    r = await api.post("/api/v1/system/restart-kiosk")
    is_problem(r, 409, "CORE_API_NO_KIOSK")
    # the other actions do not need a kiosk
    assert (await api.post("/api/v1/system/restart-engine")).status_code == 202


async def test_core_382_without_an_installer_the_routes_are_503(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.down = True
    for action in SYSTEM_ACTIONS:
        r = await api.post(f"/api/v1/system/{action}")
        is_problem(r, 503, "CORE_API_INSTALLER_UNAVAILABLE")
    api.services.installer = None  # a system without the installer
    is_problem(await api.post("/api/v1/system/reboot"), 503, "CORE_API_INSTALLER_UNAVAILABLE")


async def test_core_382_an_unexpected_answer_is_503(
    api: Api,
    installer: FakeInstaller,
) -> None:
    installer.replies[("POST", "/v1/system/reboot")] = Reply(404, {"error": "no route"})
    body = is_problem(
        await api.post("/api/v1/system/reboot"), 503, "CORE_API_INSTALLER_UNAVAILABLE"
    )
    assert "404" in body["detail"]


async def test_core_382_the_routes_take_no_body_and_no_passphrase(
    api: Api,
    installer: FakeInstaller,
) -> None:
    r = await api.client.post("/api/v1/system/restart-engine", headers=api.bearer)
    assert r.status_code == 202
    assert installer.calls[-1][2] == {"client": "test (bearer) 127.0.0.1"}


async def test_core_356_installer_status_reports_available_and_busy(
    api: Api,
    installer: FakeInstaller,
) -> None:
    assert (await api.get("/api/v1/installer")).json() == {"available": True, "busy": False}
    installer.replies[("GET", "/v1/status")] = Reply(200, {"busy": True})
    assert (await api.get("/api/v1/installer")).json() == {"available": True, "busy": True}
    installer.replies[("GET", "/v1/status")] = Reply(500, {"error": "boom"})
    assert (await api.get("/api/v1/installer")).json() == {"available": False, "busy": False}
    installer.down = True
    assert (await api.get("/api/v1/installer")).json() == {"available": False, "busy": False}
    api.services.installer = None
    assert (await api.get("/api/v1/installer")).json() == {"available": False, "busy": False}
    is_problem(await api.client.get("/api/v1/installer"), 401, "CORE_API_UNAUTHORIZED")


async def test_core_382_not_on_the_admin_socket(
    api: Api,
    installer: FakeInstaller,
) -> None:
    app = socket_app(api.services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://socket") as client:
        for action in SYSTEM_ACTIONS:
            r = await client.post(f"/api/v1/system/{action}")
            is_problem(r, 404, "CORE_API_NOT_FOUND")
        assert (await client.get("/api/v1/installer")).status_code == 200
        paths = (await client.get("/api/openapi.json")).json()["paths"]
    assert not any("/system/" in p for p in paths)
    assert installer.calls == [("GET", "/v1/status", None)]
    schema = (await api.get("/api/openapi.json")).json()["paths"]
    assert all(f"/api/v1/system/{a}" in schema for a in SYSTEM_ACTIONS)
