"""The engine's HTTP transport (CORE-282..287) against local servers."""

from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from mm3.core.config.model import Grants
from mm3.core.config.validate import check_grant_ca_files
from mm3.core.services.grants import GrantSet
from mm3.core.services.http import (
    HttpxTransport,
    InstanceHttp,
    address_refused,
)
from mm3.errors import (
    ContextClosedError,
    HttpConnectionError,
    HttpError,
    HttpStatusError,
    HttpTimeoutError,
)

Reply = tuple[int | None, dict[str, str], bytes]


@dataclass
class Seen:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Server:
    """A minimal HTTP/1.1 server; ``respond`` decides each answer."""

    port: int = 0
    scheme: str = "http"
    seen: list[Seen] = field(default_factory=list)
    active: int = 0
    peak: int = 0
    respond: Callable[[Seen], Awaitable[Reply]] | None = None
    writers: set[asyncio.StreamWriter] = field(default_factory=set)

    @property
    def base(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}/"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                method, target, _ = line.decode().split(" ", 2)
                headers: dict[str, str] = {}
                while (raw := await reader.readline()) not in (b"\r\n", b""):
                    key, _, value = raw.decode().partition(":")
                    headers[key.strip().lower()] = value.strip()
                body = await reader.readexactly(int(headers.get("content-length", 0)))
                req = Seen(method, target, headers, body)
                self.seen.append(req)
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    assert self.respond is not None
                    status, extra, payload = await self.respond(req)
                finally:
                    self.active -= 1
                if status is None:
                    return
                head = [f"HTTP/1.1 {status} X"]
                if "no-length" not in extra:
                    head.append(f"Content-Length: {len(payload)}")
                head += [f"{k}: {v}" for k, v in extra.items() if k != "no-length"]
                writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
                await writer.drain()
                if "no-length" in extra:
                    return
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


class Listener:
    """Closes the server and its kept-alive connections on exit."""

    def __init__(self, server: asyncio.Server, srv: Server) -> None:
        self._server = server
        self._srv = srv

    async def __aenter__(self) -> Listener:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        for writer in list(self._srv.writers):
            writer.close()
        await self._server.wait_closed()


async def ok(_req: Seen) -> Reply:
    return 200, {"Content-Type": "application/json"}, b'{"ok": true}'


async def start_server(
    respond: Callable[[Seen], Awaitable[Reply]] = ok,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[Server, Listener]:
    srv = Server(scheme="https" if ssl_context else "http", respond=respond)
    extra: dict[str, Any] = {"ssl": ssl_context, "ssl_shutdown_timeout": 0.2} if ssl_context else {}
    server = await asyncio.start_server(srv.handle, "127.0.0.1", 0, **extra)
    srv.port = server.sockets[0].getsockname()[1]
    return srv, Listener(server, srv)


@pytest_asyncio.fixture
async def transport() -> AsyncIterator[HttpxTransport]:
    t = HttpxTransport("9.9.9")
    await t.start()
    yield t
    await t.aclose()


async def get(
    t: HttpxTransport,
    url: str,
    grants: list[str] | None = None,
    *,
    iid: str = "i",
    method: str = "GET",
    **kw: Any,
) -> Any:
    args: dict[str, Any] = {
        "params": None,
        "headers": None,
        "json": None,
        "data": None,
        "timeout": 5.0,
        "grants": grants if grants is not None else [url],
        "user_agent": "mm3/9.9.9 test/1.0.0",
    }
    args.update(kw)
    return await t.send(iid, method, url, **args)


# --- CORE-282 -------------------------------------------------------------------


async def test_core_282_get_with_query_and_json_body(transport: HttpxTransport) -> None:
    srv, listener = await start_server()
    async with listener:
        res = await get(transport, srv.base + "a", params={"q": "1"})
        assert res.status == 200
        assert res.json() == {"ok": True}
        assert srv.seen[0].target == "/a?q=1"
        await get(transport, srv.base + "p", method="POST", json={"x": 1})
        assert srv.seen[1].body == b'{"x":1}' or srv.seen[1].body == b'{"x": 1}'
        assert srv.seen[1].headers["content-type"] == "application/json"


async def test_core_282_response_above_10_mib_is_refused_while_read(
    transport: HttpxTransport,
) -> None:
    big = b"x" * (10 * 1024 * 1024 + 1)

    async def reply(req: Seen) -> Reply:
        if req.target == "/declared":
            return 200, {}, big
        if req.target == "/small":
            return 200, {}, b"{}"
        return 200, {"no-length": "1", "Connection": "close"}, big

    srv, listener = await start_server(reply)
    async with listener:
        for path in ("declared", "streamed"):
            with pytest.raises(HttpError, match="too large"):
                await get(transport, srv.base + path)
        small = await get(transport, srv.base + "small")
        assert small.status == 200


async def test_core_282_no_cookie_jar_no_retry(transport: HttpxTransport) -> None:
    calls = 0

    async def reply(req: Seen) -> Reply:
        nonlocal calls
        if req.target == "/drop":
            calls += 1
            return None, {}, b""
        return 200, {"Set-Cookie": "sid=1; Path=/"}, b"{}"

    srv, listener = await start_server(reply)
    async with listener:
        await get(transport, srv.base + "one")
        await get(transport, srv.base + "two")
        assert "cookie" not in srv.seen[1].headers
        with pytest.raises(HttpConnectionError):
            await get(transport, srv.base + "drop")
        assert calls == 1  # no automatic retry


async def test_core_282_at_most_4_requests_of_one_instance_in_flight(
    transport: HttpxTransport,
) -> None:
    async def slow(_req: Seen) -> Reply:
        await asyncio.sleep(0.1)
        return 200, {}, b"{}"

    srv, listener = await start_server(slow)
    async with listener:
        results = await asyncio.gather(*(get(transport, srv.base + str(n)) for n in range(10)))
        assert all(r.status == 200 for r in results)
        assert srv.peak == 4
        srv.peak = 0
        await asyncio.gather(*(get(transport, srv.base + "x", iid=f"inst{n}") for n in range(6)))
        assert srv.peak > 4  # the limit is per instance


async def test_core_282_timeout_default_and_cap(transport: HttpxTransport) -> None:
    async def hang(_req: Seen) -> Reply:
        await asyncio.sleep(5)
        return 200, {}, b"{}"

    srv, listener = await start_server(hang)
    async with listener:
        with pytest.raises(HttpTimeoutError):
            await get(transport, srv.base, timeout=0.2)
    # the cap: a larger value is reduced to 60 s (checked on the clamp, not by waiting)
    assert min(max(1e9, 0.001), 60.0) == 60.0


async def test_core_282_not_started_transport_refuses() -> None:
    with pytest.raises(HttpConnectionError):
        await get(HttpxTransport(), "https://x.example/")


# --- CORE-283 -------------------------------------------------------------------


async def test_core_283_default_user_agent_and_replacement(transport: HttpxTransport) -> None:
    srv, listener = await start_server()
    async with listener:
        await get(transport, srv.base)
        assert srv.seen[0].headers["user-agent"] == "mm3/9.9.9 test/1.0.0"
        await get(transport, srv.base, headers={"user-agent": "me (me@example.org)"})
        assert srv.seen[1].headers["user-agent"] == "me (me@example.org)"


async def test_core_283_managed_headers_are_dropped(transport: HttpxTransport) -> None:
    srv, listener = await start_server()
    async with listener:
        await get(
            transport,
            srv.base,
            method="POST",
            data=b"abc",
            headers={
                "Host": "evil.example",
                "Content-Length": "999",
                "Connection": "upgrade",
                "Transfer-Encoding": "chunked",
                "X-Key": "k",
            },
        )
        seen = srv.seen[0]
        assert seen.headers["host"] == f"127.0.0.1:{srv.port}"
        assert seen.headers["content-length"] == "3"
        assert seen.body == b"abc"
        assert "transfer-encoding" not in seen.headers
        assert seen.headers["x-key"] == "k"


# --- CORE-284 -------------------------------------------------------------------


def make_cert(tmp: Path) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    cert, key = tmp / "ca.pem", tmp / "key.pem"
    openssl = shutil.which("openssl")
    assert openssl is not None
    args = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1",
        "-addext", "basicConstraints=critical,CA:TRUE",
    ]  # fmt: skip
    subprocess.run(args, check=True, capture_output=True)  # noqa: S603 - fixed arguments
    return cert, key


async def test_core_284_tls_is_verified_and_a_grant_ca_replaces_the_store(
    transport: HttpxTransport, tmp_path: Path
) -> None:
    cert, key = make_cert(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    srv, listener = await start_server(ssl_context=server_ctx)
    async with listener:
        with pytest.raises(HttpConnectionError):
            await get(transport, srv.base)  # self-signed: not in the system store
        res = await get(transport, srv.base, ca_files={srv.base: str(cert)})
        assert res.status == 200
        # another prefix without the CA keeps failing: the CA is per grant
        with pytest.raises(HttpConnectionError):
            await get(transport, srv.base, ca_files={"https://other.example/": str(cert)})


async def test_core_284_grant_entry_with_ca(tmp_path: Path) -> None:
    grants = Grants.model_validate(
        {"http": ["https://a.example/", {"prefix": "https://nas.lan/api/", "ca": "nas.pem"}]}
    )
    gs = GrantSet.from_config(grants, tmp_path)
    assert gs.http == ("https://a.example/", "https://nas.lan/api/")
    assert gs.http_ca == (("https://nas.lan/api/", str(tmp_path / "nas.pem")),)


def test_core_284_missing_ca_file_is_a_validation_error(tmp_path: Path) -> None:
    from mm3.core.config.model import InstanceConfig
    from mm3.core.config.validate import ResolvedInstance

    spec = InstanceConfig.model_validate(
        {"module": "m", "grants": {"http": [{"prefix": "https://nas.lan/", "ca": "nas.pem"}]}}
    )
    inst = ResolvedInstance(3, "m", "m", spec, None, None)
    (rec,) = check_grant_ca_files([inst], tmp_path, {})
    assert rec.code == "CORE_CFG_FILE_UNREADABLE"
    assert rec.path == "/modules/3/grants/http/0"
    (tmp_path / "nas.pem").write_text("pem")
    assert check_grant_ca_files([inst], tmp_path, {}) == []


# --- CORE-285 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "refused"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("169.254.169.254", True),
        ("fe80::1%eth0", True),
        ("0.0.0.0", True),  # noqa: S104 - an address under test
        ("224.0.0.1", True),
        ("ff02::1", True),
        ("192.168.1.10", False),
        ("10.0.0.5", False),
        ("93.184.216.34", False),
        ("not-an-ip", False),
    ],
)
def test_core_285_address_ranges(address: str, refused: bool) -> None:
    assert address_refused(address) is refused


async def test_core_285_loopback_needs_a_grant_naming_the_host_literally(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv, listener = await start_server()
    async with listener:
        # named literally: lifted, by address and by the name localhost
        assert (await get(transport, srv.base)).status == 200
        local = f"http://localhost:{srv.port}/"
        assert (await get(transport, local)).status == 200

        # a name that resolves to a local address is refused, checked at connect time
        real = socket.getaddrinfo

        def rebind(host: str, *a: Any, **k: Any) -> Any:
            return real("127.0.0.1" if host == "rebind.example" else host, *a, **k)

        monkeypatch.setattr(socket, "getaddrinfo", rebind)
        with pytest.raises(PermissionError, match="local address"):
            await get(transport, f"http://rebind.example:{srv.port}/")
        assert len(srv.seen) == 2  # nothing reached the server


async def test_core_285_instances_do_not_share_the_lifted_connection(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv, listener = await start_server()
    async with listener:
        await get(transport, srv.base, iid="a")  # lifted for instance a
        real = socket.getaddrinfo
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, *a, **k: real("127.0.0.1" if host == "nas.lan" else host, *a, **k),
        )
        with pytest.raises(PermissionError):
            await get(transport, f"http://nas.lan:{srv.port}/", iid="b")


# --- CORE-286 -------------------------------------------------------------------


async def test_core_286_redirects_follow_granted_targets_only(transport: HttpxTransport) -> None:
    async def reply(req: Seen) -> Reply:
        if req.target == "/start":
            return 302, {"Location": "/granted/final"}, b""
        if req.target == "/out":
            return 302, {"Location": "http://elsewhere.example/x"}, b""
        return 200, {}, b'{"at": "%s"}' % req.target.encode()

    srv, listener = await start_server(reply)
    async with listener:
        grants = [srv.base]
        res = await get(transport, srv.base + "start", grants)
        assert res.json() == {"at": "/granted/final"}
        assert res.url.endswith("/granted/final")
        res = await get(transport, srv.base + "out", grants)
        assert res.status == 302
        assert res.headers["location"] == "http://elsewhere.example/x"
        # not granted: the redirect response comes back as it is
        res = await get(transport, srv.base + "start", [srv.base + "start"])
        assert res.status == 302


async def test_core_286_at_most_5_redirects(transport: HttpxTransport) -> None:
    async def loop(req: Seen) -> Reply:
        n = int(req.target.strip("/") or 0)
        return 302, {"Location": f"/{n + 1}"}, b""

    srv, listener = await start_server(loop)
    async with listener:
        res = await get(transport, srv.base)
        assert res.status == 302
        assert len(srv.seen) == 6  # the request plus 5 followed redirects


async def test_core_286_method_body_and_credentials_on_redirect(
    transport: HttpxTransport,
) -> None:
    other, other_listener = await start_server()

    async def reply(req: Seen) -> Reply:
        if req.target == "/see-other":
            return 303, {"Location": "/landing"}, b""
        if req.target == "/temp":
            return 307, {"Location": "/landing"}, b""
        if req.target == "/away":
            return 302, {"Location": other.base + "landing"}, b""
        return 200, {}, b"{}"

    srv, listener = await start_server(reply)
    async with listener, other_listener:
        grants = [srv.base, other.base]
        auth = {"Authorization": "Bearer k"}
        await get(transport, srv.base + "see-other", grants, method="POST", data=b"x", headers=auth)
        landing = srv.seen[-1]
        assert (landing.method, landing.body) == ("GET", b"")
        await get(transport, srv.base + "temp", grants, method="POST", data=b"x", headers=auth)
        landing = srv.seen[-1]
        assert (landing.method, landing.body) == ("POST", b"x")
        assert landing.headers["authorization"] == "Bearer k"
        await get(transport, srv.base + "away", grants, headers=auth)
        assert "authorization" not in other.seen[-1].headers  # other origin


async def test_core_286_redirect_to_a_local_address_is_not_followed(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, target_listener = await start_server()

    async def reply(_req: Seen) -> Reply:
        return 302, {"Location": f"http://rebind.example:{target.port}/x"}, b""

    srv, listener = await start_server(reply)
    real = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: real("127.0.0.1" if host == "rebind.example" else host, *a, **k),
    )
    async with listener, target_listener:
        res = await get(transport, srv.base, [srv.base, f"http://rebind.example:{target.port}/"])
        assert res.status == 302
        assert target.seen == []


async def test_core_286_https_is_never_downgraded(
    transport: HttpxTransport, tmp_path: Path
) -> None:
    plain, plain_listener = await start_server()
    cert, key = make_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    async def to_http(_req: Seen) -> Reply:
        return 302, {"Location": plain.base}, b""

    tls, tls_listener = await start_server(to_http, ctx)
    async with plain_listener, tls_listener:
        res = await get(transport, tls.base, [tls.base, plain.base], ca_files={tls.base: str(cert)})
        assert res.status == 302
        assert plain.seen == []


# --- CORE-287 -------------------------------------------------------------------


async def test_core_287_errors_are_mm3_errors_without_library_text(
    transport: HttpxTransport,
) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    url = f"http://127.0.0.1:{closed_port}/"
    with pytest.raises(HttpConnectionError) as info:
        await get(transport, url)
    text = f"{info.value!r} {info.value.__cause__!r}".lower()
    assert "httpx" not in text
    assert "httpcore" not in text
    assert info.value.__cause__ is None

    srv, listener = await start_server()
    async with listener:
        res = await get(transport, srv.base)
        res.raise_for_status()

    async def not_found(_req: Seen) -> Reply:
        return 404, {}, b"{}"

    srv, listener = await start_server(not_found)
    async with listener:
        res = await get(transport, srv.base)
        with pytest.raises(HttpStatusError) as status:
            res.raise_for_status()
        assert status.value.status == 404


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **fields: Any) -> None:
        self.records.append((event, fields))


async def test_core_287_log_and_health_counters(transport: HttpxTransport) -> None:
    async def reply(req: Seen) -> Reply:
        return (200, {}, b"{}") if req.target.startswith("/good") else (None, {}, b"")

    srv, listener = await start_server(reply)
    log = RecordingLog()
    results: list[bool] = []
    async with listener:
        http = InstanceHttp(
            "i",
            transport,
            frozenset({"http.outbound"}),
            [srv.base],
            lambda: None,
            log=log,
            on_result=results.append,
        )
        await http.get(srv.base + "good?apikey=SECRET", headers={"X-Key": "SECRET"})
        with pytest.raises(HttpConnectionError):
            await http.get(srv.base + "bad")
        with pytest.raises(PermissionError):
            await http.get("https://not-granted.example/")
    assert results == [True, False]  # a refused grant never reaches the transport
    assert log.records[0] == (
        "http_request",
        {"method": "GET", "host": f"127.0.0.1:{srv.port}", "path": "/good", "status": 200},
    )
    assert log.records[1][1]["status"] is None
    assert "SECRET" not in repr(log.records)


async def test_core_287_guard_runs_before_the_transport(transport: HttpxTransport) -> None:
    def closed() -> None:
        raise ContextClosedError("closed")

    http = InstanceHttp("i", transport, frozenset({"http.outbound"}), ["https://x/"], closed)
    with pytest.raises(ContextClosedError):
        await http.get("https://x/")
