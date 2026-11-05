"""Errors of the admin REST API as ``application/problem+json`` (RFC 9457).

Every error response of the engine's own ``/api`` routes carries ``type``
(``about:blank``), ``title``, ``status``, ``detail`` and ``code``, one of
the stable names below. ``title`` and ``detail`` are English; the admin shows
the core catalog text of ``code``. Routes a module registers on
``ctx.router`` answer in their own format.

Implements:
    CORE-298: Core spec 0.25, §9.1 Remote-control API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.responses import JSONResponse

__all__ = ["CODES", "PROBLEM_JSON", "ApiError", "problem"]

PROBLEM_JSON = "application/problem+json"

CODES: dict[str, tuple[int, str]] = {
    "CORE_API_UNAUTHORIZED": (401, "Unauthorized"),
    "CORE_API_FORBIDDEN": (403, "Forbidden"),
    "CORE_API_NOT_FOUND": (404, "Not found"),
    "CORE_API_INVALID_REQUEST": (422, "Invalid request"),
    "CORE_API_TOO_LARGE": (413, "Payload too large"),
    "CORE_API_PRECONDITION_REQUIRED": (428, "Precondition required"),
    "CORE_API_CONFLICT": (409, "Conflict"),
    "CORE_API_DISK_INVALID": (409, "Configuration on disk is invalid"),
    "CORE_API_NO_KIOSK": (409, "No kiosk agent connected"),
    "CORE_API_CONFIG_INVALID": (422, "Configuration invalid"),
    "CORE_API_PATCH_FAILED": (422, "Patch failed"),
    "CORE_API_MASKED_UNKNOWN": (422, "Unknown masked value"),
    "CORE_API_RESERVED_NAME": (403, "Reserved name"),
    "CORE_API_PAYLOAD_INVALID": (422, "Payload invalid"),
    "CORE_API_UNAVAILABLE": (503, "Engine unavailable"),
    "CORE_API_REQUEST_TIMEOUT": (504, "Request timed out"),
    "CORE_API_NOTHING_TO_DO": (409, "Nothing to undo or redo"),
}
"""Every code of CORE-298 with its HTTP status and English title; a new code is a spec change."""


class ApiError(Exception):
    """An error the API answers with a problem document.

    Args:
        code: One of :data:`CODES`.
        detail: English explanation for this occurrence.
        errors: Validation records (CORE-35), when the failure is a validation.
        headers: Extra response headers.
        extra: Further members of the problem document (``etag`` and
            ``files`` of a conflict, CORE-300).

    Raises:
        KeyError: ``code`` is not a code of CORE-298.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        errors: Sequence[Mapping[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        CODES[code]  # unknown codes are a programming error
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = errors
        self.headers = headers
        self.extra = extra

    def response(self) -> JSONResponse:
        """Return the problem document."""
        return problem(
            self.code, self.detail, errors=self.errors, headers=self.headers, extra=self.extra
        )


def problem(
    code: str,
    detail: str,
    *,
    errors: Sequence[Mapping[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Build a problem response.

    Args:
        code: One of :data:`CODES`.
        detail: English explanation.
        errors: Validation records, added as ``errors``.
        headers: Extra response headers.
        extra: Further members, such as ``etag`` and ``files`` (CORE-300).

    Returns:
        The response with media type ``application/problem+json``.

    Implements:
        CORE-298: Core spec 0.25, §9.1 Remote-control API.
    """
    status, title = CODES[code]
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
    }
    if errors is not None:
        body["errors"] = [dict(e) for e in errors]
    for key, value in (extra or {}).items():
        body.setdefault(key, value)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)
