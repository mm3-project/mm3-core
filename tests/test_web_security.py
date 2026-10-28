"""Milestone 3, part A: listeners, allow lists, token store, sessions, security headers."""

from __future__ import annotations

import argparse
import asyncio
import io
import ipaddress
import json
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mm3.cli.main import build_parser
from mm3.cli.token_cmd import pair_link, run_token
from mm3.core.settings import EngineSettings, ServerSettings
from mm3.core.web import security as security_mod
from mm3.core.web.apps import admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.gateway import ListenerError, WebGateway
from mm3.core.web.pages import TEXT, admin_home_page, login_page, message_page, pair_page
from mm3.core.web.security import CSP, CSP_PREVIEW, AllowList, Network, is_loopback
from mm3.core.web.sessions import (
    ADMIN_COOKIE,
    DISPLAY_COOKIE,
    LoginLimiter,
    SessionStore,
)
from mm3.core.web.tokens import TokenError, TokenStore

LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.168.1.20", 50000)
ADMIN = "http://127.0.0.1:8081"
MIRROR = "http://127.0.0.1:8080"


class Now:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def now() -> Now:
    return Now()


@pytest.fixture
def auth(tmp_path: Path, now: Now) -> WebAuth:
    tokens = TokenStore(tmp_path, now=now)
    return WebAuth(tokens, SessionStore(tmp_path, tokens, now=now), LoginLimiter(now=now))


def ports(server: int = 0, admin_port: int = 0) -> EngineSettings:
    """Settings with given ports; 0 (a free port) is below the config minimum."""
    from mm3.core.settings import AdminSettings

    return EngineSettings().model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=server, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=admin_port, allow=[]),
        }
    )


def nets(*cidrs: str) -> list[Network]:
    return [ipaddress.ip_network(c) for c in cidrs]


def client(app: Any, base: str, peer: tuple[str, int] = LOOPBACK) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer), base_url=base)


def admin(auth: WebAuth, allow: list[Network] | None = None) -> Any:
    return protect(admin_app(auth), lambda: allow or [], "admin")


def mirror(auth: WebAuth, allow: list[Network] | None = None) -> Any:
    return protect(mirror_app(auth), lambda: allow or [], "mirror")


async def login(c: httpx.AsyncClient, token: str, origin: str = ADMIN) -> httpx.Response:
    return await c.post(
        "/admin/login",
        content=f"token={token}",
        headers={"content-type": "application/x-www-form-urlencoded", "origin": origin},
    )


# --- CORE-60: defaults and the running gateway ----------------------------------------


def test_core_60_listeners_bind_loopback_by_default() -> None:
    s = EngineSettings()
    assert (s.server.host, s.admin.host) == ("127.0.0.1", "127.0.0.1")
    assert (s.server.allow, s.admin.allow) == ([], [])


async def test_core_60_gateway_serves_both_listeners(tmp_path: Path) -> None:
    gw = WebGateway(ports(), tmp_path)
    await gw.start()
    try:
        async with httpx.AsyncClient() as c:
            m = f"http://127.0.0.1:{gw.mirror.port}"
            a = f"http://127.0.0.1:{gw.admin.port}"
            assert (await c.get(f"{m}/pair")).status_code == 200
            assert (await c.get(f"{m}/api/v1/status")).status_code == 404
            assert (await c.get(f"{m}/admin")).status_code == 404
            assert (await c.get(f"{a}/api/v1/status")).status_code == 401
            assert (await c.get(f"{a}/pair")).status_code == 404
    finally:
        await gw.stop()


async def test_core_60_gateway_reports_a_port_in_use(tmp_path: Path) -> None:
    first = WebGateway(ports(), tmp_path)
    await first.start()
    try:
        taken = first.mirror.port
        second = WebGateway(ports(server=taken), tmp_path)
        with pytest.raises(ListenerError):
            await second.start()
    finally:
        await first.stop()


# --- CORE-61: allow lists -------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("192.168.1.20", False),
        ("", False),
        (None, False),
        ("not-an-ip", False),
    ],
)
def test_core_61_is_loopback(host: str | None, expected: bool) -> None:
    assert is_loopback(host) is expected


async def test_core_61_lan_refused_without_allow_list(auth: WebAuth) -> None:
    async with client(mirror(auth), MIRROR, LAN) as c:
        r = await c.get("/pair")
    assert r.status_code == 403
    assert r.headers["content-security-policy"] == CSP
    async with client(mirror(auth), MIRROR, LOOPBACK) as c:
        assert (await c.get("/pair")).status_code == 200


async def test_core_61_allow_list_admits_its_networks(auth: WebAuth) -> None:
    app = mirror(auth, nets("192.168.1.0/24"))
    async with client(app, MIRROR, LAN) as c:
        assert (await c.get("/pair")).status_code == 200
    async with client(app, MIRROR, ("10.0.0.5", 1)) as c:
        assert (await c.get("/pair")).status_code == 403
    async with client(app, MIRROR, ("::ffff:192.168.1.9", 1)) as c:
        assert (await c.get("/pair")).status_code == 200


async def test_core_61_server_allow_applies_to_later_connections(auth: WebAuth) -> None:
    allow: list[Network] = []
    app = protect(mirror_app(auth), lambda: allow, "mirror")
    async with client(app, MIRROR, LAN) as c:
        assert (await c.get("/pair")).status_code == 403
        allow.extend(nets("192.168.1.0/24"))
        assert (await c.get("/pair")).status_code == 200


async def test_core_61_refused_websocket_is_closed() -> None:
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("must not be reached")

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    mw = AllowList(app, lambda: [], listener="mirror")
    await mw({"type": "websocket", "client": LAN, "path": "/ws"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_core_61_refusals_logged_once_a_minute(
    auth: WebAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(security_mod._log, "info", lambda event, **kw: logged.append(kw))
    t = [1000.0]
    inner = mirror_app(auth)
    app = AllowList(inner, lambda: [], listener="mirror", clock=lambda: t[0])
    async with client(app, MIRROR, LAN) as c:
        await c.get("/pair")
        await c.get("/pair")
        t[0] += 61
        await c.get("/pair")
    assert len(logged) == 2
    assert logged[0]["address"] == "192.168.1.20"


# --- CORE-62: token store and CLI -----------------------------------------------------


def test_core_62_tokens_are_hashed_named_and_private(tmp_path: Path, now: Now) -> None:
    store = TokenStore(tmp_path, now=now)
    token = store.new("laptop")
    assert re.fullmatch(r"mm3_[0-9a-f]{12}_[A-Za-z0-9_-]{43}", token)
    raw = (tmp_path / "tokens.json").read_text()
    assert token not in raw
    assert token.split("_")[-1] not in raw
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600
    (rec,) = store.all()
    assert (rec.name, rec.kind, rec.created, rec.last_used) == (
        "laptop",
        "admin",
        "2026-10-28T12:00:00Z",
        None,
    )
    assert rec.hash.startswith("$argon2")
    assert "hash" not in rec.public()


def test_core_62_verify_checks_kind_and_records_use(tmp_path: Path, now: Now) -> None:
    store = TokenStore(tmp_path, now=now)
    adm = store.new("a")
    disp = store.new("d", "display")
    assert store.verify(adm, "admin") is not None
    assert store.verify(adm, "display") is None
    assert store.verify(disp, "admin") is None
    assert store.verify(disp, "display") is not None
    assert store.verify(adm[:-1] + ("A" if adm[-1] != "A" else "B")) is None
    assert store.verify("nonsense") is None
    assert {r.name: r.last_used for r in store.all()}["a"] == "2026-10-28T12:00:00Z"


def test_core_62_names_unique_and_valid(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    store.new("one")
    with pytest.raises(TokenError):
        store.new("one")
    with pytest.raises(TokenError):
        store.new("bad name")
    with pytest.raises(TokenError):
        store.revoke("missing")


def test_core_62_revoke_seen_by_another_store_at_once(tmp_path: Path) -> None:
    engine_side = TokenStore(tmp_path)
    cli_side = TokenStore(tmp_path)
    token = cli_side.new("tablet")
    assert engine_side.verify(token) is not None
    cli_side.revoke("tablet")
    assert engine_side.verify(token) is None


async def test_core_62_api_needs_an_admin_token(auth: WebAuth) -> None:
    token = auth.tokens.new("ci")
    display = auth.tokens.new("hall", "display")
    async with client(admin(auth), ADMIN) as c:
        r = await c.get("/api/v1/status")
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        bad = await c.get("/api/v1/status", headers={"authorization": "Bearer mm3_x"})
        assert bad.status_code == 401
        other = await c.get("/api/v1/status", headers={"authorization": f"Bearer {display}"})
        assert other.status_code == 401
        ok = await c.post("/api/v1/nothing", headers={"authorization": f"Bearer {token}"})
        assert ok.status_code == 404  # authenticated; the REST API arrives in milestone 6
        assert (await c.get("/admin")).status_code == 303


def cli(tmp_path: Path, *argv: str) -> tuple[int, str, str]:
    args: argparse.Namespace = build_parser().parse_args(["token", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = run_token(
        args,
        state_dir=tmp_path,
        server=ServerSettings(host="192.168.1.50", port=8080),
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_core_62_token_cli_new_list_revoke(tmp_path: Path) -> None:
    code, out, err = cli(tmp_path, "new", "laptop")
    assert code == 0
    token = out.strip()
    assert token.startswith("mm3_")
    assert "only once" in err
    code, out, _ = cli(tmp_path, "list")
    assert code == 0
    assert out.startswith("laptop\tadmin\t")
    assert "never used" in out
    assert token not in out
    _, out, _ = cli(tmp_path, "list", "--json")
    assert json.loads(out)[0]["name"] == "laptop"
    assert cli(tmp_path, "new", "laptop")[0] == 1
    assert cli(tmp_path, "revoke", "laptop")[:2] == (0, "revoked laptop\n")
    assert cli(tmp_path, "revoke", "laptop")[0] == 1


# --- CORE-63: security headers --------------------------------------------------------


async def test_core_63_headers_on_every_response(auth: WebAuth) -> None:
    async with client(admin(auth), ADMIN) as c:
        for path, status in (("/admin/login", 200), ("/nope", 404), ("/api/x", 401)):
            r = await c.get(path)
            assert r.status_code == status
            assert r.headers["content-security-policy"] == CSP
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["cache-control"] == "no-store"
            policy = r.headers["permissions-policy"]
            for feature in ("camera", "microphone", "geolocation", "usb", "serial"):
                assert f"{feature}=()" in policy
            assert "bluetooth=()" in policy
            assert "payment=()" in policy
        preview = await c.get("/admin/preview")
        assert preview.headers["content-security-policy"] == CSP_PREVIEW
    assert "frame-ancestors 'none'" in CSP
    assert "frame-ancestors 'self'" in CSP_PREVIEW


async def test_core_63_static_files_and_route_cache_control(auth: WebAuth) -> None:
    async with client(mirror(auth), MIRROR) as c:
        r = await c.get("/core/pair.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert r.headers["cache-control"] == "no-store"

    from starlette.responses import PlainTextResponse

    async def versioned(scope: Any, receive: Any, send: Any) -> None:
        resp = PlainTextResponse("x", headers={"cache-control": "public, max-age=31536000"})
        await resp(scope, receive, send)

    async with client(protect(versioned, lambda: [], "mirror"), MIRROR) as c:
        r = await c.get("/modules/x/1.0/a.js")
    assert r.headers["cache-control"] == "public, max-age=31536000"


@pytest.mark.parametrize(
    "html",
    [
        login_page(),
        login_page("CORE_WEB_LOGIN_FAILED"),
        admin_home_page(),
        pair_page(),
        message_page("CORE_WEB_PAIR_TITLE", "CORE_WEB_FORBIDDEN"),
    ],
)
def test_core_63_pages_have_no_inline_script_or_style(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)


def test_core_63_page_text_comes_from_keys() -> None:
    assert all(re.fullmatch(r"CORE_WEB_[A-Z_]+", k) for k in TEXT)
    for html in (login_page(), admin_home_page(), pair_page()):
        visible = re.sub(r"<[^>]+>", "", html)
        for word in re.findall(r"[A-Za-z]{4,}", visible):
            assert any(word in v for v in TEXT.values()), word


# --- CORE-253: admin sessions ---------------------------------------------------------


async def test_core_253_login_sets_a_strict_session_cookie(auth: WebAuth) -> None:
    token = auth.tokens.new("me")
    async with client(admin(auth), ADMIN) as c:
        r = await login(c, token)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin"
        cookie = r.headers["set-cookie"].lower()
        assert cookie.startswith(f"{ADMIN_COOKIE}=")
        for flag in ("httponly", "samesite=strict", "path=/", f"max-age={30 * 24 * 3600}"):
            assert flag in cookie
        home = await c.get("/admin")
        assert home.status_code == 200
        assert TEXT["CORE_WEB_ADMIN_PENDING"] in home.text
        api = await c.get("/api/v1/x")
        assert api.status_code == 404
    sid = r.cookies[ADMIN_COOKIE]
    stored = await asyncio.to_thread(auth.sessions.path.read_text)
    assert sid not in stored


async def test_core_253_unsafe_requests_need_a_matching_origin(auth: WebAuth) -> None:
    token = auth.tokens.new("me")
    async with client(admin(auth), ADMIN) as c:
        assert (await login(c, token, origin="http://evil.example")).status_code == 403
        assert (await c.post("/admin/login", content=f"token={token}")).status_code == 403
        assert (await login(c, token)).status_code == 303
        assert (await c.post("/api/v1/x")).status_code == 403
        assert (
            await c.post("/api/v1/x", headers={"origin": "http://evil.example"})
        ).status_code == 403
        assert (await c.post("/api/v1/x", headers={"origin": ADMIN})).status_code == 404
        assert (await c.get("/api/v1/x")).status_code == 404
        # Bearer requests from scripts need no Origin.
        bearer = {"authorization": f"Bearer {token}"}
        assert (await c.post("/api/v1/x", headers=bearer)).status_code == 404


async def test_core_253_five_failures_lock_for_15_minutes(auth: WebAuth, now: Now) -> None:
    token = auth.tokens.new("me")
    async with client(admin(auth), ADMIN) as c:
        for _ in range(4):
            assert (await login(c, "mm3_wrong")).status_code == 401
        locked = await login(c, "mm3_wrong")
        assert locked.status_code == 429
        assert (await login(c, token)).status_code == 429
        async with client(admin(auth), ADMIN, ("127.0.0.2", 1)) as other:
            assert (await login(other, token)).status_code == 303
        now.t += timedelta(minutes=15)
        assert (await login(c, token)).status_code == 303


async def test_core_253_session_lasts_30_days_from_last_use(auth: WebAuth, now: Now) -> None:
    token = auth.tokens.new("me")
    async with client(admin(auth), ADMIN) as c:
        await login(c, token)
        now.t += timedelta(days=29)
        assert (await c.get("/admin")).status_code == 200
        now.t += timedelta(days=29)
        assert (await c.get("/admin")).status_code == 200
        now.t += timedelta(days=30, seconds=1)
        assert (await c.get("/admin")).status_code == 303


async def test_core_253_logout_and_revoke_end_the_session(auth: WebAuth) -> None:
    token = auth.tokens.new("me")
    async with client(admin(auth), ADMIN) as c:
        await login(c, token)
        out = await c.post("/admin/logout", headers={"origin": ADMIN})
        assert out.status_code == 303
        assert (await c.get("/admin")).status_code == 303
        await login(c, token)
        assert (await c.get("/admin")).status_code == 200
        TokenStore(auth.tokens.path.parent).revoke("me")  # mm3 token revoke in another process
        assert (await c.get("/admin")).status_code == 303


def test_core_253_sessions_survive_a_restart(tmp_path: Path, now: Now) -> None:
    tokens = TokenStore(tmp_path, now=now)
    rec = tokens.verify(tokens.new("me"))
    assert rec is not None
    sid = SessionStore(tmp_path, tokens, now=now).create(rec)
    again = SessionStore(tmp_path, TokenStore(tmp_path, now=now), now=now)
    assert again.check(sid, "admin") is not None
    assert again.check(sid, "display") is None
    assert stat.S_IMODE((tmp_path / "sessions.json").stat().st_mode) == 0o600


# --- CORE-254: display pairing --------------------------------------------------------


def test_core_254_pair_link() -> None:
    server = ServerSettings(host="192.168.1.50", port=8080)
    assert pair_link(server, "mm3_t") == "http://192.168.1.50:8080/pair#mm3_t"
    wide = ServerSettings(host="0.0.0.0", port=8080)  # noqa: S104 - test value
    assert pair_link(wide, "mm3_t", hostname="mirror") == "http://mirror:8080/pair#mm3_t"
    assert pair_link(ServerSettings(host="fd00::5"), "x") == "http://[fd00::5]:8080/pair#x"


def test_core_254_token_new_display_prints_the_link(tmp_path: Path) -> None:
    code, out, _ = cli(tmp_path, "new", "hall", "--display")
    assert code == 0
    assert re.fullmatch(r"http://192\.168\.1\.50:8080/pair#mm3_\w{12}_[\w-]{43}\n", out)
    assert TokenStore(tmp_path).all()[0].kind == "display"


async def test_core_254_pairing_sets_a_display_session(auth: WebAuth) -> None:
    token = auth.tokens.new("hall", "display")
    app = mirror(auth, nets("192.168.1.0/24"))
    async with client(app, "http://192.168.1.50:8080", LAN) as c:
        page = await c.get("/pair")
        assert '<script src="/core/pair.js" defer></script>' in page.text
        form = {"content-type": "application/x-www-form-urlencoded"}
        origin = {"origin": "http://192.168.1.50:8080", **form}
        bad = await c.post("/pair", content="token=mm3_nope", headers=origin)
        assert bad.status_code == 403
        assert (await c.post("/pair", content=f"token={token}", headers=form)).status_code == 403
        r = await c.post("/pair", content=f"token={token}", headers=origin)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        cookie = r.headers["set-cookie"].lower()
        assert cookie.startswith(f"{DISPLAY_COOKIE}=")
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
    sid = r.cookies[DISPLAY_COOKIE]

    conn = SimpleNamespace(cookies={DISPLAY_COOKIE: sid})
    assert await auth.display_session(conn) is not None  # type: ignore[arg-type]
    auth.tokens.revoke("hall")
    assert await auth.display_session(conn) is None  # type: ignore[arg-type]


async def test_core_254_display_token_grants_nothing_on_admin(auth: WebAuth) -> None:
    token = auth.tokens.new("hall", "display")
    async with client(admin(auth), ADMIN) as c:
        assert (await login(c, token)).status_code == 401
        r = await c.get("/api/v1/x", headers={"authorization": f"Bearer {token}"})
        assert r.status_code == 401
    rec = auth.tokens.verify(token, "display")
    assert rec is not None
    sid = auth.sessions.create(rec)
    async with client(admin(auth), ADMIN) as c:
        c.cookies.set(ADMIN_COOKIE, sid)
        assert (await c.get("/admin")).status_code == 303
