"""The catalog and installer routes of the admin REST API (CORE-356).

``GET /api/v1/catalog`` and ``/catalog/{name}`` read only the engine's copy of the index
(CORE-351), never the installer's copy and never the network, and work from a stale copy.
``POST /api/v1/catalog/refresh`` and the operation routes forward to the installer, which
alone verifies the index and changes venvs (CORE-353, CORE-355). The operation routes exist
only on the admin listener: the CLI reaches the installer on its own socket.

The passphrase and the TOTP code of an operation are passed on unchanged and are never
logged, stored or sent back; nothing here logs a request body.

Implements:
    CORE-356: Core spec 0.44, §10.7 Installer service.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mm3.core.catalog import (
    CachedCatalog,
    CatalogError,
    Entry,
    compatible,
    find_entry,
    newest_compatible,
    read_cached,
)
from mm3.core.logging import get_logger
from mm3.core.web.installer import (
    UNAVAILABLE,
    InstallerLink,
    Snapshot,
    catalog_object,
    parse_meta,
)
from mm3.core.web.problems import ApiError

__all__ = [
    "CATALOG_QUERY_KEYS",
    "CatalogRoutes",
    "OperationBody",
    "build_item",
    "build_items",
    "register_catalog_routes",
]

_log = get_logger(subsystem="api")

CATALOG_QUERY_KEYS = ("q", "category")
"""The query keys of ``GET /api/v1/catalog``; any other key is 422 (CORE-356)."""

MAX_SECRET = 1024
"""The longest passphrase or TOTP code the engine passes on."""


class OperationBody(BaseModel):
    """``POST /api/v1/installer/operations`` (CORE-356).

    ``name`` is for ``install``, ``update`` and ``remove``, ``version`` only for ``install``, and
    ``restore_previous`` takes neither. ``source``, ``path`` and ``client`` are refused: the source
    is always the catalog and the engine names the client itself (CORE-353, CORE-382).
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["install", "update", "remove", "restore_previous"]
    name: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=64)
    passphrase: str = Field(min_length=1, max_length=MAX_SECRET, repr=False)
    totp: str | None = Field(default=None, max_length=MAX_SECRET, repr=False)

    @model_validator(mode="after")
    def _shape(self) -> OperationBody:
        if self.action == "restore_previous":
            if self.name is not None or self.version is not None:
                raise ValueError("restore_previous takes no name or version")
        else:
            if not self.name:
                raise ValueError(f"{self.action} needs a name")
            if self.version is not None and self.action != "install":
                raise ValueError("a version is only for install")
        return self


def build_item(
    entry: Entry,
    installed: dict[str, str] | None,
    locale: str,
) -> dict[str, Any]:
    """Return one catalog item (CORE-356).

    Args:
        entry: The module as the index holds it.
        installed: The installer's record ``{name, version, origin}`` of the module, or ``None``
            when it is not installed or the installer cannot be reached.
        locale: The admin language for ``description``.

    Returns:
        ``{name, pypi, repository, badge, category, license, description, compatible,
        incompatible_reason, latest, installed, origin, update_available, unlisted}``. A module
        with no compatible release is listed with ``compatible`` false and the reason ``api``,
        never hidden (CORE-124 keeps it out of the CLI only).
    """
    newest = newest_compatible(entry)
    version = installed["version"] if installed else None
    return {
        "name": entry.pypi,
        "pypi": entry.pypi,
        "repository": entry.repository,
        "badge": entry.badge,
        "category": entry.category,
        "license": entry.license,
        "description": entry.text(locale),
        "compatible": newest is not None,
        "incompatible_reason": None if newest is not None else "api",
        "latest": newest.version if newest is not None else None,
        "installed": version,
        "origin": installed["origin"] if installed else None,
        "update_available": _newer(newest.version if newest else None, version),
        "unlisted": False,
    }


def _newer(latest: str | None, installed: str | None) -> bool:
    if latest is None or installed is None:
        return False
    try:
        return Version(latest) > Version(installed)
    except InvalidVersion:
        return False


def _unlisted_item(record: dict[str, str]) -> dict[str, Any]:
    """An installed module that is not in the index: only ``name``, ``installed`` and ``origin``."""
    return {
        "name": record["name"],
        "pypi": None,
        "repository": None,
        "badge": None,
        "category": None,
        "license": None,
        "description": None,
        "compatible": None,
        "incompatible_reason": None,
        "latest": None,
        "installed": record["version"],
        "origin": record["origin"],
        "update_available": False,
        "unlisted": True,
    }


def _records(snapshot: Snapshot) -> dict[str, dict[str, str]]:
    """The installer's installed modules by normalized name; empty when it is unavailable."""
    if not snapshot.available:
        return {}
    out: dict[str, dict[str, str]] = {}
    for rec in snapshot.installed:
        name, version, origin = rec.get("name"), rec.get("version"), rec.get("origin")
        if isinstance(name, str) and isinstance(version, str) and isinstance(origin, str):
            out[canonicalize_name(name)] = {"name": name, "version": version, "origin": origin}
    return out


def build_items(
    cached: CachedCatalog,
    snapshot: Snapshot,
    locale: str,
    *,
    q: str = "",
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Return the items of ``GET /api/v1/catalog``, sorted by name (CORE-356).

    Args:
        cached: The engine's copy of the index.
        snapshot: What the installer reports as installed.
        locale: The admin language.
        q: Keeps the items in whose name or description it occurs, ignoring case; an unlisted
            module matches on its name only.
        category: Keeps the items whose category equals it.

    Returns:
        Every index entry that matches, then the installed modules the index does not list
        (``unlisted`` true), as one list sorted by normalized name.
    """
    installed = _records(snapshot)
    needle = q.strip().lower()
    items: list[dict[str, Any]] = []
    listed: set[str] = set()
    for entry in cached.index.modules:
        key = canonicalize_name(entry.pypi)
        listed.add(key)
        if category is not None and entry.category != category:
            continue
        if needle and needle not in entry.pypi.lower() and needle not in entry.text(locale).lower():
            continue
        items.append(build_item(entry, installed.get(key), locale))
    for unlisted_key, record in installed.items():
        if unlisted_key in listed or category is not None:
            continue
        if needle and needle not in record["name"].lower():
            continue
        items.append(_unlisted_item(record))
    return sorted(items, key=lambda i: canonicalize_name(str(i["name"])))


def build_detail(
    cached: CachedCatalog,
    snapshot: Snapshot,
    locale: str,
    name: str,
) -> dict[str, Any] | None:
    """Return ``GET /api/v1/catalog/{name}``: the item, its publisher and releases .

    Args:
        cached: The engine's copy of the index.
        snapshot: What the installer reports as installed.
        locale: The admin language.
        name: The module's name, as the index spells it or normalized.

    Returns:
        The item plus ``publisher`` and ``releases`` (newest first, each ``{version, api,
        compatible, capabilities, grants, check}``; ``files`` and hashes are never sent), or
        ``None`` for a name that is neither in the index nor installed. An installed module the
        index does not list has ``publisher`` null and no releases.
    """
    installed = _records(snapshot)
    entry = find_entry(cached.index, name)
    if entry is None:
        record = installed.get(canonicalize_name(name))
        if record is None:
            return None
        return {**_unlisted_item(record), "publisher": None, "releases": []}
    releases = sorted(entry.releases, key=lambda r: Version(r.version), reverse=True)
    return {
        **build_item(entry, installed.get(canonicalize_name(entry.pypi)), locale),
        "publisher": entry.publisher.model_dump(),
        "releases": [
            {
                "version": r.version,
                "api": r.api,
                "compatible": compatible(r),
                "capabilities": list(r.capabilities),
                "grants": dict(r.grants),
                "check": r.check.model_dump(),
            }
            for r in releases
        ],
    }


class CatalogRoutes:
    """What the catalog and installer routes read.

    Args:
        installer: The link to the installer, or ``None`` on a system without it.
        state_dir: The engine's state directory (``<state>/catalog/`` holds the index copy).
        now: The engine clock's wall time.
        language: The admin language of a request (CORE-309).
    """

    def __init__(
        self,
        installer: Callable[[], InstallerLink | None],
        state_dir: Callable[[], Any],
        now: Callable[[], datetime],
        language: Callable[[Request], Awaitable[str]],
    ) -> None:
        self.installer = installer
        self.state_dir = state_dir
        self.now = now
        self.language = language

    def link(self) -> InstallerLink:
        """The installer link.

        Raises:
            ApiError: 503 ``CORE_API_INSTALLER_UNAVAILABLE`` when none is configured.
        """
        link = self.installer()
        if link is None:
            raise ApiError("CORE_API_INSTALLER_UNAVAILABLE", "No installer is configured")
        return link

    async def snapshot(self) -> Snapshot:
        """What the installer reports; unavailable without a link."""
        link = self.installer()
        return UNAVAILABLE if link is None else await link.snapshot()

    async def cached(self) -> CachedCatalog:
        """Read the engine's copy of the index.

        Raises:
            ApiError: 503 ``CORE_API_CATALOG_UNAVAILABLE`` with no copy, or one that cannot be
                read.
        """
        try:
            return await asyncio.to_thread(read_cached, self.state_dir(), self.now())
        except CatalogError as exc:
            raise ApiError("CORE_API_CATALOG_UNAVAILABLE", str(exc)) from exc


def register_catalog_routes(
    router: APIRouter, routes: CatalogRoutes, *, operations: bool = True
) -> None:
    """Add the routes of CORE-356 to the ``/api/v1`` router.

    Args:
        router: The router of the REST API.
        routes: What the routes read.
        operations: Whether to add the operation routes: false on the admin socket, where the
            catalog and ``GET /api/v1/installer`` are served but operations are not (CORE-311).
    """

    @router.get("/catalog")
    async def list_catalog(request: Request) -> dict[str, Any]:
        """The catalog from the engine's copy of the index (CORE-356)."""
        query = dict(request.query_params)
        unknown = set(query) - set(CATALOG_QUERY_KEYS)
        if unknown:
            raise ApiError("CORE_API_INVALID_REQUEST", f"Unknown parameter {sorted(unknown)[0]!r}")
        cached = await routes.cached()
        snapshot = await routes.snapshot()
        locale = await routes.language(request)
        return {
            "catalog": catalog_object(cached.meta, routes.now()),
            "installer_available": snapshot.available,
            "items": build_items(
                cached, snapshot, locale, q=query.get("q", ""), category=query.get("category")
            ),
        }

    @router.post("/catalog/refresh")
    async def refresh_catalog() -> dict[str, Any]:
        """Download and verify the index now, through the installer (CORE-351, CORE-356)."""
        meta = parse_meta(await routes.link().refresh())
        return {"catalog": catalog_object(meta, routes.now())}

    @router.get("/catalog/{name}")
    async def get_catalog_module(name: str, request: Request) -> dict[str, Any]:
        """One module with its publisher and releases (CORE-356)."""
        cached = await routes.cached()
        snapshot = await routes.snapshot()
        detail = build_detail(cached, snapshot, await routes.language(request), name)
        if detail is None:
            raise ApiError("CORE_API_NOT_FOUND", f"Unknown module {name!r}")
        return detail

    if not operations:
        return

    @router.post("/installer/operations", status_code=202, response_model=None)
    async def start_operation(body: OperationBody, request: Request) -> Response:
        """Forward an install, update, removal or restore-previous to the installer (CORE-356)."""
        link = routes.link()
        client = str(request.state.caller)
        op_id = await link.submit(body.model_dump(exclude_none=True), client)
        _log.warning("api_installer_operation", id=op_id, action=body.action, client=client)
        return Response(
            json.dumps({"id": op_id}),
            status_code=202,
            media_type="application/json",
            headers={"Location": f"/api/v1/installer/operations/{op_id}"},
        )

    @router.get("/installer/operations")
    async def list_operations() -> list[dict[str, Any]]:
        """The installer's last 100 operations, newest first (CORE-356)."""
        return await routes.link().operations()

    @router.get("/installer/operations/{op_id}")
    async def get_operation(op_id: str) -> dict[str, Any]:
        """One operation; a 404 for an unknown id (CORE-356)."""
        return await routes.link().operation(op_id)
