"""Validation records and the ``CORE_CFG_*`` codes.

Every problem found while loading or validating a configuration becomes a
:class:`ValidationRecord`. Pydantic errors are turned into records by
:func:`records_from_pydantic`, which maps Pydantic error types to codes and
turns error locations into JSON Pointers into the resolved configuration.

The English messages below are the source of the engine catalog entries
(core spec 8.3); the i18n service of milestone 3 compiles them into the
engine catalog with the other ``CORE_*`` keys.

Implements:
    CORE-206: Core spec 0.8, §6.2 Rules.
    CORE-243: Core spec 0.8, §6.2 Rules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError
from pydantic_core import ErrorDetails

__all__ = [
    "CODES",
    "MESSAGES",
    "WARNING_CODES",
    "Severity",
    "SourceMap",
    "ValidationRecord",
    "code_for_pydantic",
    "make_record",
    "pointer",
    "records_from_pydantic",
]

Severity = Literal["error", "warning"]

# Implements CORE-243 (Core spec 0.8, §6.2 Rules): the code table, with the
# English message of each code; placeholders name the code's params.
MESSAGES: dict[str, str] = {
    "CORE_CFG_YAML_SYNTAX": "Not valid YAML: {reason}",
    "CORE_CFG_YAML_TAG": "Tag {tag} is not allowed; use !env or !include",
    "CORE_CFG_DUPLICATE_KEY": "Key {key} appears more than once",
    "CORE_CFG_FILE_UNREADABLE": "Cannot read {file}",
    "CORE_CFG_INCLUDE_OUTSIDE": "Included file {path} is outside the configuration directory",
    "CORE_CFG_INCLUDE_CYCLE": "Including {path} creates a cycle",
    "CORE_CFG_INCLUDE_DEPTH": "More than {limit} nested !include levels",
    "CORE_CFG_ENV_MISSING": "Environment variable {name} is not set and has no default",
    "CORE_CFG_ENV_FILE_SYNTAX": "This line of the env file is not KEY=VALUE",
    "CORE_CFG_ENV_FILE_MODE": "The env file is readable by other users (mode {mode})",
    "CORE_CFG_VERSION": "config_version {value} is newer than this engine supports ({max})",
    "CORE_CFG_UNKNOWN_KEY": "Unknown key {key}",
    "CORE_CFG_REQUIRED": "This value is required",
    "CORE_CFG_TYPE": "Expected {expected}",
    "CORE_CFG_RANGE": "Value out of range ({bound})",
    "CORE_CFG_LENGTH": "Length out of range (min {min}, max {max})",
    "CORE_CFG_PATTERN": "Value does not match {pattern}",
    "CORE_CFG_CHOICE": "Expected one of {allowed}",
    "CORE_CFG_DURATION": "{value} is not a duration (seconds, ISO 8601 or 1h30m)",
    "CORE_CFG_TIMEZONE": "{value} is not an IANA time zone",
    "CORE_CFG_LOCALE": "{value} is not a valid BCP 47 language tag",
    "CORE_CFG_CRON": "Invalid cron expression: {reason}",
    "CORE_CFG_LOCALIZED_NO_EN": "This text has no English (en) entry",
    "CORE_CFG_ID_DUPLICATE": "Id {id} is used more than once",
    "CORE_CFG_ID_REQUIRED": "Module {module} is used by more than one instance; give each an id",
    "CORE_CFG_MODULE_MISSING": "Module {module} is not installed",
    "CORE_CFG_MODULE_UNAVAILABLE": "Module {module} cannot be used: {reason}",
    "CORE_CFG_POSITION_REQUIRED": "Module {module} has a front-end and needs a position",
    "CORE_CFG_POSITION_FORBIDDEN": "{key} is not allowed for a backend-only module",
    "CORE_CFG_OUT_OF_GRID": "The position does not fit the {columns} x {rows} grid",
    "CORE_CFG_PRESET_EMPTY": "Preset {preset} is empty on this grid",
    "CORE_CFG_OVERLAP": "Overlaps {other}",
    "CORE_CFG_CELL_TOO_SMALL": "Grid cells are smaller than 1 px ({width} x {height})",
    "CORE_CFG_MIN_SIZE": (
        "The area {width} x {height} px is smaller than the module's minimum "
        "{min_width} x {min_height} px"
    ),
    "CORE_CFG_CSS": "appearance.css: {reason}",
    "CORE_CFG_PAGE_UNKNOWN": "Page {page} is not in pages.list",
    "CORE_CFG_DISPLAY_MODE": "Give width and height together; refresh needs both",
    "CORE_CFG_SECRETS_UNENCRYPTED": "backup.parts includes secrets but encrypt_to is not set",
    "CORE_CFG_INVALID": "{reason}",
}
CODES = frozenset(MESSAGES)
WARNING_CODES = frozenset(
    {
        "CORE_CFG_ENV_FILE_MODE",
        "CORE_CFG_LOCALIZED_NO_EN",
        "CORE_CFG_MODULE_UNAVAILABLE",
        "CORE_CFG_MIN_SIZE",
    }
)

_RANGE_KEYS = ("gt", "ge", "lt", "le", "multiple_of")
_EXPECTED = {
    "int": "an integer",
    "float": "a number",
    "decimal": "a number",
    "bool": "true or false",
    "string": "a string",
    "str": "a string",
    "model": "a mapping",
    "model_attributes": "a mapping",
    "dict": "a mapping",
    "list": "a list",
    "tuple": "a list",
    "set": "a list",
    "frozen_set": "a list",
    "url": "a URL",
    "ip_any_network": "a CIDR network",
    "ip_v4_network": "a CIDR network",
    "ip_v6_network": "a CIDR network",
    "ip_any_address": "an IP address",
    "date": "a date",
    "datetime": "a date and time",
    "time": "a time",
    "none": "nothing",
}


@dataclass(frozen=True)
class ValidationRecord:
    """One problem found in a configuration.

    Attributes:
        file: File the problem is in; ``None`` when no file applies.
        line: 1-based line, 0 when unknown.
        col: 1-based column, 0 when unknown.
        severity: ``error`` or ``warning``; warnings never block loading.
        path: JSON Pointer into the resolved configuration.
        code: A ``CORE_CFG_*`` catalog key.
        params: Values for the message placeholders.
        message: English text.

    Implements:
        CORE-206: Core spec 0.8, §6.2 Rules.
    """

    file: str | None
    line: int
    col: int
    severity: Severity
    path: str
    code: str
    params: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the record as JSON-ready data.

        Returns:
            The eight fields of CORE-206.
        """
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "severity": self.severity,
            "path": self.path,
            "code": self.code,
            "params": dict(self.params),
            "message": self.message,
        }

    def format_line(self) -> str:
        """Format the record as ``file:line:col severity path message``.

        Returns:
            The line printed by ``mm3 check-config`` (CORE-207).
        """
        return (
            f"{self.file or '-'}:{self.line}:{self.col} "
            f"{self.severity} {self.path or '/'} {self.message}"
        )


def _escape(token: str | int) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def pointer(parts: Iterable[str | int]) -> str:
    """Build a JSON Pointer (RFC 6901).

    Args:
        parts: Keys and list indices from the root.

    Returns:
        The pointer, ``""`` for the root.
    """
    return "".join("/" + _escape(p) for p in parts)


class SourceMap:
    """Positions in the source files, keyed by JSON Pointer.

    ``values`` holds where each value starts; ``keys`` where a mapping key
    is written. Lookups fall back to the nearest parent that is known.
    """

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int, int]] = {}
        self.keys: dict[str, tuple[str, int, int]] = {}

    def add(self, ptr: str, file: str, line: int, col: int, *, key: bool = False) -> None:
        """Record a position.

        Args:
            ptr: JSON Pointer.
            file: Source file.
            line: 1-based line.
            col: 1-based column.
            key: Whether this is the position of the mapping key.
        """
        (self.keys if key else self.values)[ptr] = (file, line, col)

    def lookup(self, ptr: str, *, prefer_key: bool = False) -> tuple[str | None, int, int]:
        """Find the position of a value, or of its nearest known parent.

        Args:
            ptr: JSON Pointer.
            prefer_key: Use the key's position when the pointer names a key.

        Returns:
            ``(file, line, col)``; ``(None, 0, 0)`` when nothing is known.
        """
        probe = ptr
        while True:
            if prefer_key and probe in self.keys:
                return self.keys[probe]
            if probe in self.values:
                return self.values[probe]
            if probe in self.keys:
                return self.keys[probe]
            if not probe:
                return (None, 0, 0)
            probe = probe.rsplit("/", 1)[0]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return str(value)


def make_record(
    code: str,
    path: str,
    params: Mapping[str, Any] | None = None,
    *,
    sources: SourceMap | None = None,
    file: str | None = None,
    line: int = 0,
    col: int = 0,
    prefer_key: bool = False,
) -> ValidationRecord:
    """Build a record, taking severity and message from the code table.

    Args:
        code: A ``CORE_CFG_*`` code.
        path: JSON Pointer.
        params: Message parameters.
        sources: Used to find file, line and column when ``file`` is not given.
        file: Explicit file.
        line: Explicit line.
        col: Explicit column.
        prefer_key: Point at the mapping key rather than its value.

    Returns:
        The record.

    Raises:
        KeyError: ``code`` is not in the table (CORE-243).

    Implements:
        CORE-243: Core spec 0.8, §6.2 Rules.
    """
    template = MESSAGES[code]
    clean = {k: _json_safe(v) for k, v in (params or {}).items()}
    if file is None and sources is not None:
        file, line, col = sources.lookup(path, prefer_key=prefer_key)
    shown = dict(clean)
    if code == "CORE_CFG_RANGE":
        shown["bound"] = ", ".join(f"{k} {clean[k]}" for k in _RANGE_KEYS if k in clean)
    try:
        message = template.format(**shown)
    except (KeyError, IndexError):
        message = template
    severity: Severity = "warning" if code in WARNING_CODES else "error"
    return ValidationRecord(file, line, col, severity, path, code, clean, message)


def _expected(err_type: str) -> str:
    base = err_type.removesuffix("_type").removesuffix("_parsing")
    return _EXPECTED.get(base, base.replace("_", " "))


def code_for_pydantic(err: ErrorDetails) -> tuple[str, dict[str, Any]]:
    """Map one Pydantic error to a code and its params.

    Args:
        err: An entry of ``ValidationError.errors()``.

    Returns:
        ``(code, params)``.

    Implements:
        CORE-243: Core spec 0.8, §6.2 Rules.
    """
    t = err["type"]
    ctx: dict[str, Any] = dict(err.get("ctx") or {})
    loc = err.get("loc", ())
    if t in CODES:
        return t, ctx
    if t == "extra_forbidden":
        return "CORE_CFG_UNKNOWN_KEY", {"key": loc[-1] if loc else ""}
    if t == "missing":
        return "CORE_CFG_REQUIRED", {}
    if t in ("greater_than", "greater_than_equal", "less_than", "less_than_equal", "multiple_of"):
        bound = {k: ctx[k] for k in _RANGE_KEYS if k in ctx}
        return "CORE_CFG_RANGE", bound
    if t in ("too_short", "too_long", "string_too_short", "string_too_long"):
        return "CORE_CFG_LENGTH", {"min": ctx.get("min_length"), "max": ctx.get("max_length")}
    if t == "string_pattern_mismatch":
        return "CORE_CFG_PATTERN", {"pattern": ctx.get("pattern")}
    if t in ("literal_error", "enum"):
        return "CORE_CFG_CHOICE", {"allowed": ctx.get("expected")}
    if t.startswith("time_delta"):
        return "CORE_CFG_DURATION", {"value": err.get("input")}
    if t.endswith("_type") or t.endswith("_parsing") or t in _EXPECTED:
        return "CORE_CFG_TYPE", {"expected": _expected(t)}
    if t in ("value_error", "assertion_error"):
        reason = str(ctx.get("error", err.get("msg", "")))
        return "CORE_CFG_INVALID", {"reason": reason.removeprefix("Value error, ")}
    return "CORE_CFG_INVALID", {"reason": err.get("msg", t)}


def _loc_to_parts(loc: Sequence[str | int], data: Any, err_type: str) -> list[str | int]:
    """Turn a Pydantic location into keys that exist in the input data.

    Union branch and tag labels in the location are dropped by walking the
    input: a step is kept when it names a key or index that exists; a last
    step that does not exist is kept only for missing and unknown keys.
    """
    parts: list[str | int] = []
    current: Any = data
    for i, step in enumerate(loc):
        last = i == len(loc) - 1
        in_map = isinstance(current, Mapping) and step in current
        in_list = isinstance(current, list) and isinstance(step, int) and 0 <= step < len(current)
        if in_map or in_list:
            parts.append(step)
            current = current[step]
        elif last and err_type in ("missing", "extra_forbidden"):
            parts.append(step)
    return parts


def records_from_pydantic(
    exc: ValidationError,
    data: Any,
    *,
    prefix: Sequence[str | int] = (),
    sources: SourceMap | None = None,
    skip: Callable[[str], bool] | None = None,
) -> list[ValidationRecord]:
    """Turn a Pydantic ``ValidationError`` into records.

    Args:
        exc: The error.
        data: The input that was validated, used to rebuild pointers.
        prefix: Location of ``data`` in the whole configuration.
        sources: Source positions.
        skip: Returns True for pointers whose errors are already reported
            (for example a missing ``!env`` value).

    Returns:
        One record per error, in Pydantic's order.

    Implements:
        CORE-206: Core spec 0.8, §6.2 Rules.
    """
    out: list[ValidationRecord] = []
    seen: set[tuple[str, str]] = set()
    for err in exc.errors(include_url=False):
        code, params = code_for_pydantic(err)
        ptr = pointer([*prefix, *_loc_to_parts(err["loc"], data, err["type"])])
        if skip is not None and skip(ptr):
            continue
        if (ptr, code) in seen:
            continue
        seen.add((ptr, code))
        out.append(
            make_record(
                code,
                ptr,
                params,
                sources=sources,
                prefer_key=code == "CORE_CFG_UNKNOWN_KEY",
            )
        )
    return out


_CODE_RE = re.compile(r"^CORE_CFG_[A-Z0-9_]+$")
assert all(_CODE_RE.match(c) for c in CODES)  # noqa: S101 - table sanity
