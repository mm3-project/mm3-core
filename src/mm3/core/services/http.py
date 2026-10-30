"""``ctx.http`` on the engine side: capability and grant checks over a transport.

The checks are the same on a mirror and in the test kit: the capability
``http.outbound`` must be declared and the URL must start with one of the
instance's ``grants.http`` prefixes (Contract section 8). The transport sends
the request. The engine's :class:`HttpxTransport` is the one place that
imports httpx (CORE-282); the test kit supplies its own fake.

Implements:
    -: Module Contract 1.0 rev 12, §4.3 HttpClient.
    -: Module Contract 1.0 rev 12, §8 Capabilities and grants.
    MOD-70: Module Contract 1.0 rev 12, §8 Capabilities and grants.
    CORE-282, CORE-283, CORE-284, CORE-285, CORE-286, CORE-287: Core spec 0.19,
        §3.4 HTTP transport.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import ipaddress
import socket
import ssl
import urllib.request
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from mm3.__about__ import __version__
from mm3.errors import HttpConnectionError, HttpError, HttpTimeoutError
from mm3.http import HttpResponse, Response

__all__ = [
    "HttpTransport",
    "HttpxTransport",
    "InstanceHttp",
    "UnavailableTransport",
    "url_granted",
]

CAPABILITY = "http.outbound"

MAX_BODY = 10 * 1024 * 1024
"""Largest response body in bytes (CORE-282)."""
MAX_TIMEOUT = 60.0
"""Longest request timeout in seconds (CORE-282)."""
MAX_REDIRECTS = 5
"""Most redirects followed per request (CORE-286)."""
MAX_IN_FLIGHT = 4
"""Most simultaneous requests of one instance (CORE-282)."""

_DROPPED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})
_CROSS_ORIGIN_DROPPED = frozenset({"authorization", "cookie", "proxy-authorization"})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


class HttpTransport(Protocol):
    """Sends one checked request."""

    async def send(
        self,
        instance_id: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        json: Any,
        data: bytes | None,
        timeout: float,  # noqa: ASYNC109 - Contract 4.3 signature
        grants: Sequence[str],
        ca_files: Mapping[str, str] | None = None,
        user_agent: str = "",
    ) -> HttpResponse:
        """Send the request; ``grants`` bound the redirects it may follow.

        ``ca_files`` maps a grant prefix to the PEM file that replaces the CA
        store for it (CORE-284); ``user_agent`` is the default User-Agent
        (CORE-283).
        """
        ...


class UnavailableTransport:
    """A transport that refuses every request (the supervisor's default)."""

    async def send(
        self,
        instance_id: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        json: Any,
        data: bytes | None,
        timeout: float,  # noqa: ASYNC109 - Contract 4.3 signature
        grants: Sequence[str],
        ca_files: Mapping[str, str] | None = None,
        user_agent: str = "",
    ) -> HttpResponse:
        """Refuse the request.

        Raises:
            HttpConnectionError: Always.
        """
        raise HttpConnectionError("outbound HTTP is not available", url)


def url_granted(url: str, prefixes: Sequence[str]) -> bool:
    """Return whether a URL starts with one of the granted prefixes.

    Args:
        url: Request URL.
        prefixes: ``grants.http``.

    Returns:
        True when granted.
    """
    return any(url.startswith(p) for p in prefixes if p)


# --- the httpx transport (CORE-282..287) -----------------------------------


class _LocalAddressRefused(Exception):
    """The connection target is a local address (CORE-285)."""


def address_refused(address: str) -> bool:
    """Return whether an address is loopback, link-local, unspecified or multicast.

    Args:
        address: IPv4 or IPv6 literal; anything else returns False.

    Returns:
        True for the ranges CORE-285 refuses.

    Implements:
        CORE-285: Core spec 0.19, §3.4 HTTP transport.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


class _GuardBackend(httpcore.AsyncNetworkBackend):
    """Network backend that checks the address actually connected to.

    Name resolution happens here, every resolved address is checked, and the
    connection goes to the checked address, so a name cannot change meaning
    between the check and the connect (CORE-285). TLS still uses the host name.

    Implements:
        CORE-285: Core spec 0.19, §3.4 HTTP transport.
    """

    def __init__(self, strict: bool, exempt: frozenset[tuple[str, int]] = frozenset()) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._strict = strict
        self._exempt = exempt

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend signature
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect, refusing local addresses unless the pool is lax or exempt."""
        targets = [host]
        if self._strict and (host, port) not in self._exempt:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
            targets = list(dict.fromkeys(str(i[4][0]) for i in infos))
            if any(address_refused(a) for a in targets):
                raise _LocalAddressRefused(host)
        last: Exception | None = None
        for target in targets:
            try:
                return await self._inner.connect_tcp(
                    target, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None  # noqa: S101 - getaddrinfo returns at least one address
        raise last

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        """Unix sockets are never used."""
        raise httpcore.UnsupportedProtocol("unix sockets are not supported")

    async def sleep(self, seconds: float) -> None:
        """Sleep (pool housekeeping)."""
        await self._inner.sleep(seconds)


class _NoCookies(http.cookiejar.CookiePolicy):
    """Cookie policy that stores and sends nothing (CORE-282: no cookie jar)."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = False

    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def domain_return_ok(self, domain: str, request: Any) -> bool:
        return False

    def path_return_ok(self, path: str, request: Any) -> bool:
        return False


@dataclass(frozen=True)
class _Route:
    """Which connection pool serves a request."""

    ca: str | None
    lax: bool
    proxy: str | None


class _Body(httpx.AsyncByteStream):
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._inner:
            yield part

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Router(httpx.AsyncBaseTransport):
    """Hands each request to the pool its route names (client extension ``mm3_route``)."""

    def __init__(self, owner: HttpxTransport) -> None:
        self._owner = owner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pool = self._owner.pool_for(request.extensions["mm3_route"])
        url = request.url
        out = await pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=url.raw_scheme,
                    host=url.raw_host,
                    port=url.port,
                    target=url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            status_code=out.status,
            headers=out.headers,
            stream=_Body(out.stream),
            extensions=out.extensions,
        )


class HttpxTransport:
    """The engine's transport: one pooled ``httpx.AsyncClient`` for all instances.

    Created at engine start (:meth:`start`) and closed at shutdown
    (:meth:`aclose`). Connections are pooled per route: system or grant CA
    store, address rule on or lifted, and proxy.

    Args:
        core_version: Engine version for diagnostics; defaults to the running one.

    Implements:
        CORE-282, CORE-283, CORE-284, CORE-285, CORE-286, CORE-287: Core spec 0.19,
            §3.4 HTTP transport.
    """

    def __init__(self, core_version: str = __version__) -> None:
        self._core_version = core_version
        self._client: httpx.AsyncClient | None = None
        self._pools: dict[_Route, httpcore.AsyncConnectionPool] = {}
        self._ssl: dict[str | None, ssl.SSLContext] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._proxies: dict[str, str] = {}

    async def start(self) -> None:
        """Create the client; the proxy settings are read from the environment once."""
        if self._client is not None:
            return
        self._proxies = dict(urllib.request.getproxies())
        client = httpx.AsyncClient(
            transport=_Router(self),
            follow_redirects=False,
            trust_env=False,
        )
        client.cookies.jar.set_policy(_NoCookies())
        self._client = client

    async def aclose(self) -> None:
        """Close the client and every pooled connection."""
        client, self._client = self._client, None
        pools, self._pools = list(self._pools.values()), {}
        if client is not None:
            await client.aclose()
        for pool in pools:
            await pool.aclose()

    # -- pools ---------------------------------------------------------------

    def _context(self, ca: str | None) -> ssl.SSLContext:
        ctx = self._ssl.get(ca)
        if ctx is None:
            ctx = ssl.create_default_context(cafile=ca)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self._ssl[ca] = ctx
        return ctx

    def pool_for(self, route: _Route) -> httpcore.AsyncConnectionPool:
        """Return (creating on first use) the pool of a route."""
        pool = self._pools.get(route)
        if pool is not None:
            return pool
        common: dict[str, Any] = {
            "ssl_context": self._context(route.ca),
            "max_connections": 100,
            "max_keepalive_connections": 20,
            "keepalive_expiry": 5.0,
            "http1": True,
            "http2": False,
            "retries": 0,
        }
        if route.proxy is None:
            pool = httpcore.AsyncConnectionPool(
                network_backend=_GuardBackend(strict=not route.lax), **common
            )
        else:
            parts = urlsplit(route.proxy)
            auth = (
                (parts.username.encode(), (parts.password or "").encode())
                if parts.username
                else None
            )
            host = parts.hostname or ""
            port = parts.port or (443 if parts.scheme == "https" else 80)
            netloc = f"{parts.scheme}://{host}:{port}"
            pool = httpcore.AsyncHTTPProxy(
                proxy_url=httpcore.URL(netloc),
                proxy_auth=auth,
                network_backend=_GuardBackend(
                    strict=not route.lax, exempt=frozenset({(host, port)})
                ),
                **common,
            )
        self._pools[route] = pool
        return pool

    def _proxy_for(self, url: httpx.URL) -> str | None:
        proxy = self._proxies.get(url.scheme)
        if not proxy:
            return None
        if urllib.request.proxy_bypass(url.host):
            return None
        return proxy

    def _route(self, url: str, grants: Sequence[str], ca_files: Mapping[str, str]) -> _Route:
        parsed = httpx.URL(url)
        ca = None
        for prefix in sorted(ca_files, key=len, reverse=True):
            if url.startswith(prefix):
                ca = ca_files[prefix]
                break
        return _Route(ca, _lifted(url, grants), self._proxy_for(parsed))

    # -- sending ---------------------------------------------------------------

    async def send(
        self,
        instance_id: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        json: Any,
        data: bytes | None,
        timeout: float,  # noqa: ASYNC109 - Contract 4.3 signature
        grants: Sequence[str],
        ca_files: Mapping[str, str] | None = None,
        user_agent: str = "",
    ) -> HttpResponse:
        """Send one request and follow the redirects the rules allow.

        Args:
            instance_id: Calling instance; at most 4 of its requests run at once.
            method: HTTP method.
            url: Granted URL.
            params: Query parameters added to the URL.
            headers: Module headers; Host, Content-Length, Connection and
                Transfer-Encoding are dropped, a User-Agent replaces the default.
            json: JSON body.
            data: Raw body (when ``json`` is None).
            timeout: Seconds for the whole call, at most 60.
            grants: URL prefixes of the instance (redirects must match one).
            ca_files: Grant prefix to PEM file (CORE-284).
            user_agent: Default User-Agent (CORE-283).

        Returns:
            The response with its body read.

        Raises:
            PermissionError: A local address was refused (CORE-285).
            HttpTimeoutError: No complete response in time.
            HttpConnectionError: Connection, TLS or protocol failure.
            HttpError: The body exceeds 10 MiB.
        """
        client = self._client
        if client is None:
            raise HttpConnectionError("outbound HTTP is not available", url)
        limit = min(max(float(timeout), 0.001), MAX_TIMEOUT)
        sem = self._sems.setdefault(instance_id, asyncio.Semaphore(MAX_IN_FLIGHT))
        out_headers = {
            k: v for k, v in (headers or {}).items() if k.lower() not in _DROPPED_HEADERS
        }
        if not any(k.lower() == "user-agent" for k in out_headers):
            out_headers["User-Agent"] = user_agent or f"mm3/{self._core_version}"
        try:
            async with sem, asyncio.timeout(limit):
                return await self._follow(
                    client, method, url, params, out_headers, json, data,
                    limit, grants, ca_files or {},
                )  # fmt: skip
        except TimeoutError:
            raise HttpTimeoutError("request timed out", url) from None
        except _LocalAddressRefused as exc:
            raise PermissionError(f"{exc} is a local address (not granted by name)") from None
        except HttpError:
            raise
        except (httpcore.TimeoutException, httpx.TimeoutException):
            raise HttpTimeoutError("request timed out", url) from None
        except (
            httpcore.NetworkError,
            httpcore.ProtocolError,
            httpcore.ProxyError,
            httpcore.UnsupportedProtocol,
            httpx.HTTPError,
            httpx.InvalidURL,
            ssl.SSLError,
            OSError,
            ValueError,
        ):
            raise HttpConnectionError("connection failed", url) from None

    async def _follow(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: dict[str, str],
        json: Any,
        data: bytes | None,
        limit: float,
        grants: Sequence[str],
        ca_files: Mapping[str, str],
    ) -> Response:
        previous: Response | None = None
        for hop in range(MAX_REDIRECTS + 1):
            try:
                response = await self._once(
                    client, method, url, params, headers, json, data, limit, grants, ca_files
                )
            except _LocalAddressRefused:
                if previous is None:
                    raise
                return previous  # CORE-286: a redirect that fails CORE-285 is not followed
            location = response.headers.get("location")
            if response.status not in _REDIRECT_STATUS or not location or hop == MAX_REDIRECTS:
                return response
            target = urljoin(url, location)
            scheme = urlsplit(target).scheme
            if (
                scheme not in ("http", "https")
                or not url_granted(target, grants)
                or (urlsplit(url).scheme == "https" and scheme == "http")
            ):
                return response
            previous = response
            if urlsplit(target)._replace(path="", query="", fragment="") != urlsplit(url)._replace(
                path="", query="", fragment=""
            ):
                headers = {
                    k: v for k, v in headers.items() if k.lower() not in _CROSS_ORIGIN_DROPPED
                }
            if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                if method != "HEAD":
                    method = "GET"
                json, data = None, None
                headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            url, params = target, None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _once(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        json: Any,
        data: bytes | None,
        limit: float,
        grants: Sequence[str],
        ca_files: Mapping[str, str],
    ) -> Response:
        request = client.build_request(
            method,
            url,
            params=params,
            headers=headers,
            json=json,
            content=data if json is None else None,
            timeout=limit,
            extensions={"mm3_route": self._route(url, grants, ca_files)},
        )
        response = await client.send(request, stream=True)
        try:
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BODY:
                raise HttpError("response body too large", url)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY:
                    raise HttpError("response body too large", url)
        finally:
            await response.aclose()
        return Response(
            response.status_code,
            bytes(body),
            str(response.url),
            {k.lower(): v for k, v in response.headers.items()},
        )


def _lifted(url: str, grants: Sequence[str]) -> bool:
    """Return whether a granted prefix names this URL's local host literally (CORE-285)."""
    host = (urlsplit(url).hostname or "").lower()
    if not host or not (host == "localhost" or address_refused(host)):
        return False
    return any(
        url.startswith(p) and (urlsplit(p).hostname or "").lower() == host for p in grants if p
    )


# --- ctx.http ----------------------------------------------------------------


class InstanceHttp:
    """One instance's ``ctx.http``.

    Args:
        instance_id: Owner instance.
        transport: Sends the checked requests.
        capabilities: The module's declared capabilities.
        grants: ``grants.http`` URL prefixes.
        guard: Raises ``ContextClosedError`` once the context is closed.
        on_denied: Called with the capability when a call is refused
            because it was not declared (the test kit records MOD-70).
        ca_files: Grant prefix to absolute PEM path (CORE-284).
        user_agent: Default User-Agent of the instance's requests (CORE-283).
        log: Logger for the ``http_request`` debug record (CORE-287).
        on_result: Called with True after a response and False after an
            ``HttpError``: the instance's request and failure counts (CORE-287).

    Implements:
        -: Module Contract 1.0 rev 12, §4.3 HttpClient.
        CORE-283, CORE-287: Core spec 0.19, §3.4 HTTP transport.
    """

    def __init__(
        self,
        instance_id: str,
        transport: HttpTransport,
        capabilities: frozenset[str],
        grants: Sequence[str],
        guard: Callable[[], None],
        on_denied: Callable[[str], None] | None = None,
        *,
        ca_files: Mapping[str, str] | None = None,
        user_agent: str = "",
        log: Any = None,
        on_result: Callable[[bool], None] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._transport = transport
        self._capabilities = capabilities
        self._grants = tuple(grants)
        self._guard = guard
        self._on_denied = on_denied
        self._ca_files = dict(ca_files or {})
        self._user_agent = user_agent
        self._log = log
        self._on_result = on_result

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: bytes | None = None,
        timeout: float = 10.0,  # noqa: ASYNC109 - Contract 4.3 signature
    ) -> HttpResponse:
        """Check the capability and the grant, then send.

        Raises:
            PermissionError: ``http.outbound`` not declared or URL not granted.
        """
        self._guard()
        if CAPABILITY not in self._capabilities:
            if self._on_denied is not None:
                self._on_denied(CAPABILITY)
            raise PermissionError(f"capability {CAPABILITY} is not declared")
        if not url_granted(url, self._grants):
            raise PermissionError(f"{url} is not granted to {self._instance_id} (grants.http)")
        method = method.upper()
        try:
            response = await self._transport.send(
                self._instance_id,
                method,
                url,
                params=params,
                headers=headers,
                json=json,
                data=data,
                timeout=timeout,
                grants=self._grants,
                ca_files=self._ca_files,
                user_agent=self._user_agent,
            )
        except HttpError as exc:
            self._record(method, url, None, type(exc).__name__)
            raise
        self._record(method, url, response.status, None)
        return response

    def _record(self, method: str, url: str, status: int | None, error: str | None) -> None:
        """Debug record without query string, headers or body, and the counters."""
        if self._log is not None:
            parts = urlsplit(url)
            fields: dict[str, Any] = {
                "method": method,
                "host": parts.netloc,
                "path": parts.path,
                "status": status,
            }
            if error is not None:
                fields["error"] = error
            self._log.debug("http_request", **fields)
        if self._on_result is not None:
            self._on_result(error is None)

    async def get(self, url: str, **kw: Any) -> HttpResponse:
        """``request("GET", url, **kw)``."""
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> HttpResponse:
        """``request("POST", url, **kw)``."""
        return await self.request("POST", url, **kw)
