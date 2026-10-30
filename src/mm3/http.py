"""``ctx.http``: the MM3 HTTP client modules use for outbound requests.

The client is MM3-owned and stable for the whole API 1.x line; the library
behind it can change without affecting modules (MOD-23). Every request needs
the capability ``http.outbound`` and a URL that starts with one of the
instance's ``grants.http`` prefixes; otherwise it raises ``PermissionError``
(Contract section 8).

Implements:
    -: Module Contract 1.0 rev 9, §4.3 HttpClient.
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from mm3.errors import HttpStatusError

__all__ = ["MAX_BODY_BYTES", "HttpClient", "HttpResponse", "Response"]

MAX_BODY_BYTES = 10 * 1024 * 1024
"""Response bodies above this size are refused (Contract 4.3)."""


class HttpResponse(Protocol):
    """A received response."""

    @property
    def status(self) -> int:
        """HTTP status code."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Response headers, names in lower case."""
        ...

    @property
    def url(self) -> str:
        """Final URL, after granted redirects."""
        ...

    def json(self) -> Any:
        """Decode the body as JSON."""
        ...

    def text(self) -> str:
        """Decode the body as text (UTF-8 unless the headers say otherwise)."""
        ...

    def bytes(self) -> bytes:
        """The raw body."""
        ...

    def raise_for_status(self) -> None:
        """Raise ``HttpStatusError`` for a 4xx or 5xx status."""
        ...


class HttpClient(Protocol):
    """``ctx.http`` (Contract 4.3)."""

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
        """Send a request and return the response.

        Raises:
            PermissionError: ``http.outbound`` not declared or URL not granted.
            mm3.errors.HttpError: Network failure or timeout.
        """
        ...

    async def get(self, url: str, **kw: Any) -> HttpResponse:
        """``request("GET", url, **kw)``."""
        ...

    async def post(self, url: str, **kw: Any) -> HttpResponse:
        """``request("POST", url, **kw)``."""
        ...


@dataclass(frozen=True)
class Response:
    """A complete response held in memory; the engine's and the test kit's.

    Attributes:
        status: HTTP status code.
        body: Raw body.
        url: Final URL.
        headers: Headers, names in lower case.
    """

    status: int
    body: bytes = b""
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Decode the body as JSON."""
        return _json.loads(self.body)

    def text(self) -> str:
        """Decode the body (charset from ``content-type``, else UTF-8)."""
        charset = "utf-8"
        for part in self.headers.get("content-type", "").split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.lower() == "charset" and value:
                charset = value.strip('"')
        return self.body.decode(charset, errors="replace")

    def bytes(self) -> bytes:
        """The raw body."""
        return self.body

    def raise_for_status(self) -> None:
        """Raise ``HttpStatusError`` for a 4xx or 5xx status.

        Raises:
            HttpStatusError: The status is 400 or above.
        """
        if self.status >= 400:
            raise HttpStatusError(self.status, self.url)
