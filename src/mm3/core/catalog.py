"""The module catalog index: its format, compatibility and the engine's copy.

The index is the signed ``index.json`` of ``mm3-project/module-index``
(CORE-350). Only the installer service downloads and verifies it; it keeps the
authoritative copy in its own state and writes a copy with ``meta.json`` to the
engine's ``<state>/catalog/``, which the engine and ``mm3 modules search``,
``info`` and ``list`` read here (CORE-351). Nothing in this module verifies a
signature: the engine never imports the verification libraries (CORE-349).

Implements:
    CORE-350: Core spec 0.33, §10.6 Module catalog.
    CORE-351: Core spec 0.35, §10.6 Module catalog.
    CORE-122, CORE-124, CORE-125: Core spec 0.33, §10.6 Module catalog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mm3.__about__ import MODULE_API_VERSION

__all__ = [
    "BUNDLE_FILE",
    "BUNDLE_URL",
    "CACHE_DIR",
    "INDEX_FILE",
    "INDEX_URL",
    "MAX_LIFETIME",
    "META_FILE",
    "REFRESH_AFTER",
    "SIGNER_IDENTITY",
    "SIGNER_ISSUER",
    "CachedCatalog",
    "CatalogError",
    "CatalogMeta",
    "Entry",
    "Index",
    "Publisher",
    "Release",
    "ReleaseFile",
    "compatible",
    "find_entry",
    "newest_compatible",
    "parse_index",
    "read_cached",
    "search",
    "validate_lifetime",
]

INDEX_URL = "https://mm3-project.github.io/module-index/index.json"
"""Where the index is published (CORE-350)."""
BUNDLE_URL = INDEX_URL + ".sigstore.json"
"""The index's Sigstore bundle (CORE-350)."""
SIGNER_IDENTITY = (
    "https://github.com/mm3-project/module-index/.github/workflows/publish.yml@refs/heads/main"
)
"""The certificate identity that signs the index: the publish workflow on main."""
SIGNER_ISSUER = "https://token.actions.githubusercontent.com"
"""The GitHub Actions OIDC issuer."""
MAX_LIFETIME = timedelta(days=14)
"""``expires`` is at most this long after ``generated_at`` (CORE-350)."""
REFRESH_AFTER = timedelta(days=1)
"""A cached index older than this is refreshed and reported as stale (CORE-351)."""

CACHE_DIR = "catalog"
"""The catalog directory inside a state directory."""
INDEX_FILE = "index.json"
BUNDLE_FILE = "index.json.sigstore.json"
META_FILE = "meta.json"

Badge = Literal["official", "verified", "community"]


class CatalogError(ValueError):
    """The index (or its copy) is missing, malformed or out of date."""


class _Model(BaseModel):
    # Later index schemas may add fields; unknown ones are ignored, known
    # ones are validated.
    model_config = ConfigDict(extra="ignore", frozen=True)


class ReleaseFile(_Model):
    """One wheel of a release."""

    filename: str = Field(pattern=r"^[A-Za-z0-9._+-]+\.whl$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckResult(_Model):
    """The index CI's ``mm3 module check`` result for a release (CORE-121)."""

    result: str
    report_url: str | None = None


class Release(_Model):
    """One release of a catalog module."""

    version: str
    api: str
    capabilities: list[str] = Field(default_factory=list)
    grants: dict[str, Any] = Field(default_factory=dict)
    check: CheckResult
    files: list[ReleaseFile] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"invalid version {value!r}") from exc
        return value

    @field_validator("api")
    @classmethod
    def _api(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid API range {value!r}") from exc
        return value


class Publisher(_Model):
    """The Trusted Publisher a release must come from (CORE-123)."""

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    workflow: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.ya?ml$")
    environment: str | None = None


class Entry(_Model):
    """One module of the catalog."""

    pypi: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    repository: str
    badge: Badge
    publisher: Publisher
    category: str = ""
    license: str = ""
    description: str | dict[str, str] = ""
    releases: list[Release] = Field(default_factory=list)

    def text(self, locale: str = "en") -> str:
        """The description in ``locale``, falling back to English, then any."""
        if isinstance(self.description, str):
            return self.description
        lang = locale.split("-")[0]
        for key in (locale, lang, "en"):
            if key in self.description:
                return self.description[key]
        return next(iter(self.description.values()), "")

    def release(self, version: str) -> Release | None:
        """The release ``version``, or None."""
        want = Version(version)
        return next((r for r in self.releases if Version(r.version) == want), None)


class Index(_Model):
    """The catalog index (schema 1, CORE-350)."""

    schema_: Literal[1] = Field(alias="schema")
    serial: int = Field(ge=0)
    generated_at: datetime
    expires: datetime
    modules: list[Entry] = Field(default_factory=list)

    @field_validator("generated_at", "expires")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps need a time zone")
        return value


def parse_index(raw: bytes) -> Index:
    """Parse and validate index bytes.

    Args:
        raw: The bytes of ``index.json``.

    Returns:
        The index.

    Raises:
        CatalogError: The bytes are not a valid schema 1 index.
    """
    try:
        return Index.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise CatalogError(f"invalid index at {where or 'top'}: {first.get('msg')}") from exc


def validate_lifetime(index: Index, now: datetime) -> None:
    """Check that an index has not expired and has a valid lifetime.

    Raises:
        CatalogError: Expired, or ``expires`` too far after ``generated_at``.
    """
    if index.expires - index.generated_at > MAX_LIFETIME:
        raise CatalogError("index expiry is more than 14 days after its generation")
    if index.expires <= now:
        raise CatalogError(f"index expired at {index.expires.isoformat()}")


def compatible(release: Release, api_version: str = MODULE_API_VERSION) -> bool:
    """Whether a release supports the running Module API (CORE-124).

    A release whose range does not contain the engine's Module API version is
    never compatible; there is no override.
    """
    return Version(api_version) in SpecifierSet(release.api)


def newest_compatible(entry: Entry, api_version: str = MODULE_API_VERSION) -> Release | None:
    """The newest release of ``entry`` compatible with the running API."""
    good = [r for r in entry.releases if compatible(r, api_version)]
    return max(good, key=lambda r: Version(r.version), default=None)


def find_entry(index: Index, name: str) -> Entry | None:
    """The entry whose PyPI name matches ``name`` (normalized), or None."""
    want = canonicalize_name(name)
    return next((e for e in index.modules if canonicalize_name(e.pypi) == want), None)


def search(index: Index, text: str = "", category: str | None = None) -> list[Entry]:
    """Entries whose name, category or description contain ``text``.

    Args:
        index: The index.
        text: Words to look for, case-insensitive; empty matches all.
        category: Only this category, when given.

    Returns:
        The matching entries, by name.
    """
    words = text.lower().split()
    out = []
    for entry in index.modules:
        if category and entry.category != category:
            continue
        hay = " ".join(
            [entry.pypi, entry.category]
            + (
                [entry.description]
                if isinstance(entry.description, str)
                else list(entry.description.values())
            )
        ).lower()
        if all(w in hay for w in words):
            out.append(entry)
    return sorted(out, key=lambda e: canonicalize_name(e.pypi))


@dataclass(frozen=True)
class CatalogMeta:
    """``meta.json`` next to a cached index."""

    serial: int
    fetched_at: datetime
    expires: datetime

    def to_json(self) -> dict[str, Any]:
        """The JSON form."""
        return {
            "serial": self.serial,
            "fetched_at": self.fetched_at.isoformat(),
            "expires": self.expires.isoformat(),
        }

    @classmethod
    def from_json(cls, data: Any) -> CatalogMeta:
        """Read the JSON form.

        Raises:
            CatalogError: Malformed.
        """
        try:
            return cls(
                serial=int(data["serial"]),
                fetched_at=datetime.fromisoformat(data["fetched_at"]),
                expires=datetime.fromisoformat(data["expires"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"invalid catalog meta: {exc}") from exc


@dataclass(frozen=True)
class CachedCatalog:
    """A cached index read from a state directory."""

    index: Index
    meta: CatalogMeta
    stale: bool


def read_cached(state_dir: Path, now: datetime | None = None) -> CachedCatalog:
    """Read the engine's copy of the index (CORE-351).

    The copy was verified by the installer before it was written; it is used
    only for search, info and list, never to install.

    Args:
        state_dir: The engine's state directory.
        now: The current time; defaults to the system clock.

    Returns:
        The index with its meta; ``stale`` when it has expired or was fetched
        more than a day ago.

    Raises:
        CatalogError: No copy, or it cannot be read.
    """
    now = now or datetime.now(UTC)
    root = state_dir / CACHE_DIR
    try:
        raw = (root / INDEX_FILE).read_bytes()
        meta = CatalogMeta.from_json(json.loads((root / META_FILE).read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise CatalogError("no cached catalog index") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read the cached catalog index: {exc}") from exc
    index = parse_index(raw)
    stale = index.expires <= now or now - meta.fetched_at > REFRESH_AFTER
    return CachedCatalog(index, meta, stale)
