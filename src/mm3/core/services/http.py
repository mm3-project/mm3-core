"""``ctx.http`` on the engine side: capability and grant checks over a transport.

The checks are the same on a mirror and in the test kit: the capability
``http.outbound`` must be declared and the URL must start with one of the
instance's ``grants.http`` prefixes (Contract section 8). The transport sends
the request; the httpx transport with redirect and body limits (Contract 4.3)
arrives with milestone 5, until then the engine's transport reports every
request as a connection error. The test kit supplies its fake.

Implements:
    -: Module Contract 1.0 rev 9, §4.3 HttpClient.
    -: Module Contract 1.0 rev 9, §8 Capabilities and grants.
    MOD-70: Module Contract 1.0 rev 9, §8 Capabilities and grants.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from mm3.errors import HttpConnectionError
from mm3.http import HttpResponse

__all__ = ["HttpTransport", "InstanceHttp", "UnavailableTransport", "url_granted"]

CAPABILITY = "http.outbound"


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
    ) -> HttpResponse:
        """Send the request; ``grants`` bound the redirects it may follow."""
        ...


class UnavailableTransport:
    """The engine's transport until the httpx adapter of milestone 5."""

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
    ) -> HttpResponse:
        """Refuse the request.

        Raises:
            HttpConnectionError: Always.
        """
        raise HttpConnectionError("outbound HTTP is not available in this engine yet", url)


def url_granted(url: str, prefixes: Sequence[str]) -> bool:
    """Return whether a URL starts with one of the granted prefixes.

    Args:
        url: Request URL.
        prefixes: ``grants.http``.

    Returns:
        True when granted.
    """
    return any(url.startswith(p) for p in prefixes if p)


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

    Implements:
        -: Module Contract 1.0 rev 9, §4.3 HttpClient.
    """

    def __init__(
        self,
        instance_id: str,
        transport: HttpTransport,
        capabilities: frozenset[str],
        grants: Sequence[str],
        guard: Callable[[], None],
        on_denied: Callable[[str], None] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._transport = transport
        self._capabilities = capabilities
        self._grants = tuple(grants)
        self._guard = guard
        self._on_denied = on_denied

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
        return await self._transport.send(
            self._instance_id,
            method.upper(),
            url,
            params=params,
            headers=headers,
            json=json,
            data=data,
            timeout=timeout,
            grants=self._grants,
        )

    async def get(self, url: str, **kw: Any) -> HttpResponse:
        """``request("GET", url, **kw)``."""
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> HttpResponse:
        """``request("POST", url, **kw)``."""
        return await self.request("POST", url, **kw)
