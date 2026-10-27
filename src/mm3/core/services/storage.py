"""Per-instance storage: data directory and JSON key-value store.

The data directory is ``<state>/modules/<instance>/``, where ``<state>`` is
``$STATE_DIRECTORY`` (set by systemd) or else ``$XDG_STATE_HOME/mm3``. The
key-value store is one JSON file per instance, written atomically, at most
1 MiB; it lives in ``<state>/kv/<instance>.json`` so the data directory
stays entirely the module's. Data is kept when the instance is removed.

Implements:
    CORE-198: Core spec 0.4, §3.1 Shared services offered to modules.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["KV_MAX_BYTES", "InstanceStorage", "resolve_state_dir"]

KV_MAX_BYTES = 1024 * 1024
"""Largest key-value file (CORE-198)."""


def resolve_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Find the engine's state directory.

    Args:
        env: Environment to read; defaults to ``os.environ``.

    Returns:
        ``$STATE_DIRECTORY`` (first entry), else ``$XDG_STATE_HOME/mm3``,
        else ``~/.local/state/mm3`` (the XDG default).

    Implements:
        CORE-198: Core spec 0.4, §3.1 Shared services offered to modules.
    """
    env = os.environ if env is None else env
    if state := env.get("STATE_DIRECTORY"):
        return Path(state.split(":")[0])
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "mm3"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


class InstanceStorage:
    """Storage of one instance.

    Args:
        state_dir: Engine state directory.
        instance_id: The instance.
        run_blocking: Runs a blocking function in the shared thread pool.

    Implements:
        CORE-198: Core spec 0.4, §3.1 Shared services offered to modules.
    """

    def __init__(
        self,
        state_dir: Path,
        instance_id: str,
        run_blocking: Callable[..., Any],
    ) -> None:
        self._dir = state_dir / "modules" / instance_id
        self._kv_path = state_dir / "kv" / f"{instance_id}.json"
        self._run = run_blocking
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        """Data directory, created on first access."""
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    async def _load(self) -> dict[str, Any]:
        if self._cache is None:
            data: dict[str, Any] = await self._run(_read, self._kv_path)
            self._cache = data
        return self._cache

    async def kv_get(self, key: str, default: Any = None) -> Any:
        """Return the value under ``key``, or ``default``.

        Args:
            key: The key.
            default: Returned when the key is absent.

        Returns:
            The stored JSON value.
        """
        async with self._lock:
            data = await self._load()
            return data.get(key, default)

    async def kv_set(self, key: str, value: Any) -> None:
        """Store a JSON value under ``key``.

        Args:
            key: The key.
            value: JSON-serialisable value.

        Raises:
            ValueError: Not JSON-serialisable, or the store would exceed 1 MiB.
        """
        async with self._lock:
            data = dict(await self._load())
            data[key] = value
            try:
                text = json.dumps(data, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"value is not JSON-serialisable: {exc}") from exc
            if len(text.encode()) > KV_MAX_BYTES:
                raise ValueError(f"key-value store would exceed {KV_MAX_BYTES} bytes")
            await self._run(_write_atomic, self._kv_path, text)
            self._cache = data

    async def kv_delete(self, key: str) -> None:
        """Remove ``key``; nothing happens if it is absent.

        Args:
            key: The key.
        """
        async with self._lock:
            data = dict(await self._load())
            if key not in data:
                return
            del data[key]
            await self._run(_write_atomic, self._kv_path, json.dumps(data, ensure_ascii=False))
            self._cache = data
