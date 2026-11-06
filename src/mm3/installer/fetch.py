"""The installer's downloads, on the standard library only.

The index, its bundle, wheels and provenance are fetched here and nowhere else
(CORE-370): HTTPS only, TLS always verified against the system CA store, only
the hosts ``mm3-project.github.io``, ``pypi.org`` and ``files.pythonhosted.org``
(redirects too), 30 s per read and a size cap per kind. The module's own
dependencies are downloaded by ``pip`` in the build child (CORE-367). The
Sigstore libraries refresh their trust root themselves (CORE-349).

Implements:
    CORE-370: Core spec 0.35, §10.7 Installer service.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import urlsplit

from mm3.__about__ import __version__

__all__ = [
    "ALLOWED_HOSTS",
    "LIMIT_BUNDLE",
    "LIMIT_INDEX",
    "LIMIT_PROVENANCE",
    "LIMIT_WHEEL",
    "READ_TIMEOUT_S",
    "FetchError",
    "Fetcher",
    "UrllibFetcher",
]

ALLOWED_HOSTS = frozenset({"mm3-project.github.io", "pypi.org", "files.pythonhosted.org"})
READ_TIMEOUT_S = 30.0
MIB = 1024 * 1024
LIMIT_INDEX = 16 * MIB
LIMIT_BUNDLE = 1 * MIB
LIMIT_PROVENANCE = 1 * MIB
LIMIT_WHEEL = 256 * MIB
_CHUNK = 64 * 1024


class FetchError(OSError):
    """A download failed: refused URL, HTTP error, network error or too large."""


class Fetcher(Protocol):
    """What the installer needs from a downloader (a fake in tests)."""

    def get(self, url: str, limit: int, *, accept: str | None = None) -> bytes:
        """Download ``url`` into memory, at most ``limit`` bytes."""

    def download(self, url: str, dest: Path, limit: int) -> None:
        """Download ``url`` into the file ``dest``, at most ``limit`` bytes."""


def check_url(url: str) -> None:
    """Refuse a URL that is not HTTPS to an allowed host.

    Raises:
        FetchError: The URL is refused.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise FetchError(f"refused URL {url!r}: only HTTPS to {', '.join(sorted(ALLOWED_HOSTS))}")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise FetchError(f"refused URL {url!r}")


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()  # system CA store, hostname check on
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _Redirects())


class UrllibFetcher:
    """Downloads with :mod:`urllib.request` (CORE-370).

    Args:
        opener: The opener; the default verifies TLS and follows redirects only
            to allowed hosts.
        timeout: Seconds per read.
    """

    def __init__(
        self,
        opener: Callable[[], urllib.request.OpenerDirector] = _opener,
        timeout: float = READ_TIMEOUT_S,
    ) -> None:
        self._opener = opener
        self._timeout = timeout

    def _open(self, url: str, accept: str | None) -> Any:
        check_url(url)
        headers = {"User-Agent": f"mm3-installer/{__version__}"}
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 - checked above
        try:
            return self._opener().open(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"{url} answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FetchError(f"cannot fetch {url}: {reason}") from exc

    @staticmethod
    def _declared_too_large(response: Any, url: str, limit: int) -> None:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > limit:
            raise FetchError(f"{url} is larger than {limit} bytes")

    def get(self, url: str, limit: int, *, accept: str | None = None) -> bytes:
        """Download ``url`` into memory.

        Args:
            url: An HTTPS URL on an allowed host.
            limit: Maximum size in bytes.
            accept: An ``Accept`` header.

        Returns:
            The body.

        Raises:
            FetchError: Refused URL, HTTP or network error, or larger than ``limit``.
        """
        with self._open(url, accept) as response:
            self._declared_too_large(response, url, limit)
            try:
                body: bytes = response.read(limit + 1)
            except OSError as exc:
                raise FetchError(f"cannot read {url}: {exc}") from exc
        if len(body) > limit:
            raise FetchError(f"{url} is larger than {limit} bytes")
        return body

    def download(self, url: str, dest: Path, limit: int) -> None:
        """Download ``url`` into ``dest``, removing it again on any failure.

        Raises:
            FetchError: As for :meth:`get`.
        """
        try:
            with self._open(url, None) as response, dest.open("wb") as out:
                self._declared_too_large(response, url, limit)
                total = 0
                while chunk := response.read(_CHUNK):
                    total += len(chunk)
                    if total > limit:
                        raise FetchError(f"{url} is larger than {limit} bytes")
                    out.write(chunk)
        except FetchError:
            dest.unlink(missing_ok=True)
            raise
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise FetchError(f"cannot download {url}: {exc}") from exc
