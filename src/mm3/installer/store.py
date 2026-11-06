"""The installer's catalog index: refresh, rollback guard and the engine's copy.

Only the installer verifies the index (CORE-351). It keeps the verified index,
its bundle and the highest ``serial`` it has ever accepted in
``/var/lib/mm3-install/catalog/``; a downloaded index replaces it only when the
signature verifies, the serial is not lower and it has not expired. It then
writes a copy with ``meta.json`` to the engine's ``/var/lib/mm3/catalog/``
(owner ``mm3``) for search, info and list. Before every install the installer
verifies its own copy again; the engine's copy is never read back.

Implements:
    CORE-350: Core spec 0.33, §10.6 Module catalog.
    CORE-351: Core spec 0.35, §10.6 Module catalog.
    CORE-131: Core spec 0.33, §10.7 Installer service.
    CORE-373: Core spec 0.37, §10.7 Installer service.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mm3.core.catalog import (
    BUNDLE_FILE,
    BUNDLE_URL,
    INDEX_FILE,
    INDEX_URL,
    META_FILE,
    REFRESH_AFTER,
    CatalogError,
    CatalogMeta,
    Index,
    parse_index,
    validate_lifetime,
)
from mm3.core.logging import get_logger
from mm3.installer.fetch import LIMIT_BUNDLE, LIMIT_INDEX, Fetcher, FetchError
from mm3.installer.files import Owner, atomic_write, ensure_dir, read_json, write_json
from mm3.installer.paths import Layout
from mm3.installer.verify import Verification, VerifyError

__all__ = ["ACCEPTED_FILE", "CatalogStore"]

ACCEPTED_FILE = "accepted.json"
_log = get_logger("installer")
"""The highest accepted serial and when the index was fetched."""


def _now() -> datetime:
    return datetime.now(UTC)


class CatalogStore:
    """The installer's verified index.

    Args:
        layout: The installer's paths.
        fetcher: Downloads (CORE-370).
        verification: Signature checks (CORE-349).
        engine_owner: Owner of the engine's copy (``mm3``), or None to keep ours.
        now: Clock, for tests.
    """

    def __init__(
        self,
        layout: Layout,
        fetcher: Fetcher,
        verification: Verification,
        *,
        engine_owner: Owner | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self._layout = layout
        self._fetcher = fetcher
        self._verification = verification
        self._engine_owner = engine_owner
        self._now = now
        self._mutex = threading.Lock()

    def _accepted(self) -> dict[str, Any]:
        data = read_json(self._layout.catalog / ACCEPTED_FILE, {})
        return data if isinstance(data, dict) else {}

    def meta(self) -> CatalogMeta | None:
        """The ``serial``, ``fetched_at`` and ``expires`` of the kept index, or None."""
        data = self._accepted()
        if "index_serial" not in data:
            return None
        return CatalogMeta(
            serial=int(data["index_serial"]),
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            expires=datetime.fromisoformat(data["expires"]),
        )

    def due(self) -> bool:
        """Whether the kept index is missing or more than a day old (CORE-351)."""
        meta = self.meta()
        return meta is None or self._now() - meta.fetched_at > REFRESH_AFTER

    def refresh(self) -> CatalogMeta:
        """Download, verify and keep the index; write the engine's copy.

        Returns:
            The meta of the index now kept.

        Raises:
            CatalogError: The index cannot be fetched, does not verify, is
                malformed, has expired or has a lower serial than one already
                accepted. The kept index is unchanged.
        """
        try:
            raw = self._fetcher.get(INDEX_URL, LIMIT_INDEX, accept="application/json")
            bundle = self._fetcher.get(BUNDLE_URL, LIMIT_BUNDLE, accept="application/json")
        except FetchError as exc:
            raise CatalogError(str(exc)) from exc
        try:
            self._verification.verify_index(raw, bundle)
        except VerifyError as exc:
            raise CatalogError(str(exc)) from exc
        index = parse_index(raw)
        now = self._now()
        validate_lifetime(index, now)
        with self._mutex:
            accepted = self._accepted()
            highest = int(accepted.get("highest_serial", -1))
            if index.serial < highest:
                raise CatalogError(
                    f"index serial {index.serial} is lower than {highest}, already accepted"
                )
            root = self._layout.catalog
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write(root / INDEX_FILE, raw, mode=0o600)
            atomic_write(root / BUNDLE_FILE, bundle, mode=0o600)
            write_json(
                root / ACCEPTED_FILE,
                {
                    "highest_serial": max(highest, index.serial),
                    "index_serial": index.serial,
                    "fetched_at": now.isoformat(),
                    "expires": index.expires.isoformat(),
                },
            )
            meta = CatalogMeta(index.serial, now, index.expires)
            try:
                self._publish(raw, bundle, meta)
            except OSError as exc:
                # The installer's own copy is kept; only search, info and list
                # in the engine miss the update.
                _log.warning("catalog_engine_copy_failed", reason=str(exc))
        return meta

    def _publish(self, raw: bytes, bundle: bytes, meta: CatalogMeta) -> None:
        root = self._layout.engine_catalog
        # The engine's user owns its state directory and may have replaced
        # catalog/ with a symlink: refused, never followed (CORE-373).
        self._layout.engine_state.mkdir(mode=0o755, parents=True, exist_ok=True)
        ensure_dir(root, 0o755)
        owner = self._engine_owner
        atomic_write(root / INDEX_FILE, raw, mode=0o644, owner=owner)
        atomic_write(root / BUNDLE_FILE, bundle, mode=0o644, owner=owner)
        write_json(root / META_FILE, meta.to_json(), mode=0o644, owner=owner)

    def kept(self) -> Index | None:
        """The kept index without a new signature check, for display only."""
        try:
            return parse_index((self._layout.catalog / INDEX_FILE).read_bytes())
        except (FileNotFoundError, CatalogError):
            return None

    def verified(self) -> Index:
        """The kept index, verified again, for an install or update (CORE-351).

        Raises:
            CatalogError: No kept index, or it no longer verifies, has a serial
                below the highest accepted one or has expired.
        """
        root = self._layout.catalog
        with self._mutex:
            try:
                raw = (root / INDEX_FILE).read_bytes()
                bundle = (root / BUNDLE_FILE).read_bytes()
            except FileNotFoundError as exc:
                raise CatalogError(
                    "no verified catalog index yet; run: mm3 modules refresh"
                ) from exc
            accepted = self._accepted()
        try:
            self._verification.verify_index(raw, bundle)
        except VerifyError as exc:
            raise CatalogError(str(exc)) from exc
        index = parse_index(raw)
        if index.serial < int(accepted.get("highest_serial", -1)):
            raise CatalogError("the kept index is older than one already accepted")
        validate_lifetime(index, self._now())
        return index
