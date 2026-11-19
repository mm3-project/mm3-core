"""The device registry file and the device list of the admin (core spec 6.11).

At start and after every hot reload the engine resolves the owner of each
device (CORE-389) and writes ``/run/mm3/devices.json``, which the services
that own devices read (CORE-391). The same resolution feeds
``GET /api/v1/devices`` and the debug overview (CORE-393) and the warning
about camera images (CORE-392).

Implements:
    CORE-391: Core spec 0.47, §6.11 Camera devices.
    CORE-392: Core spec 0.47, §6.11 Camera devices.
    CORE-393: Core spec 0.47, §6.11 Camera devices.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mm3.core.config.devices import ResolvedDevice, resolve_devices
from mm3.core.config.diff import ReloadPlan
from mm3.core.config.validate import ConfigResult
from mm3.core.logging import get_logger

__all__ = [
    "REGISTRY_FILE",
    "DeviceRegistry",
    "device_list",
    "feed_path",
    "images_enabled",
    "registry_document",
    "resolved",
]

REGISTRY_FILE = "devices.json"
FEED_DIR = "feed"

_log = get_logger(subsystem="devices")


def resolved(result: ConfigResult) -> list[ResolvedDevice]:
    """Resolve the devices of a valid configuration, sorted by name.

    Args:
        result: The running configuration.

    Returns:
        The devices with their owners; empty when the configuration has errors.

    Implements:
        CORE-389: Core spec 0.47, §6.11 Camera devices.
    """
    if result.config is None:
        return []
    return resolve_devices(result.config.devices, result.enabled_modules)


def feed_path(runtime: Path, name: str) -> Path:
    """The local feed socket of a device (CORE-394), as named in the registry file."""
    return runtime / FEED_DIR / f"{name}.sock"


def registry_document(devices: list[ResolvedDevice], runtime: Path) -> dict[str, Any]:
    """Build the content of ``devices.json`` (CORE-391).

    Args:
        devices: The resolved devices.
        runtime: The engine's runtime directory.

    Returns:
        ``{devices: {<name>: {kind, path?, serial?, owner, images, feed}}}``
        with ``owner`` resolved; ``path`` only for a camera and ``serial``
        only for a Kinect that names one.

    Implements:
        CORE-391: Core spec 0.47, §6.11 Camera devices.
    """
    out: dict[str, Any] = {}
    for dev in devices:
        entry: dict[str, Any] = {"kind": dev.config.kind}
        if dev.config.path is not None:
            entry["path"] = dev.config.path
        if dev.config.serial is not None:
            entry["serial"] = dev.config.serial
        entry["owner"] = dev.owner
        entry["images"] = dev.config.images
        entry["feed"] = str(feed_path(runtime, dev.name))
        out[dev.name] = entry
    return {"devices": out}


def device_list(devices: list[ResolvedDevice]) -> list[dict[str, Any]]:
    """Build the body of ``GET /api/v1/devices`` (CORE-393).

    Args:
        devices: The resolved devices, sorted by name.

    Returns:
        ``[{name, kind, path, serial, owner, reason, present, images}]``;
        ``present`` says whether a camera's path exists, null for a Kinect.

    Implements:
        CORE-393: Core spec 0.47, §6.11 Camera devices.
    """
    return [
        {
            "name": d.name,
            "kind": d.config.kind,
            "path": d.config.path,
            "serial": d.config.serial,
            "owner": d.owner,
            "reason": d.reason,
            "present": os.path.exists(d.config.path) if d.config.path is not None else None,
            "images": d.config.images,
        }
        for d in devices
    ]


def images_enabled(result: ConfigResult) -> bool:
    """True while any device has ``images: true`` (the warning of CORE-392)."""
    return result.config is not None and any(d.images for d in result.config.devices.values())


class DeviceRegistry:
    """Writes ``devices.json`` at start and after every hot reload (CORE-391).

    Args:
        runtime: The engine's runtime directory (``/run/mm3``).

    Implements:
        CORE-391: Core spec 0.47, §6.11 Camera devices.
    """

    def __init__(self, runtime: Path) -> None:
        self.runtime = runtime
        self.path = runtime / REGISTRY_FILE

    def write(self, result: ConfigResult) -> bool:
        """Write the file atomically with mode 0644: a temporary file, then a rename.

        Args:
            result: The configuration now running.

        Returns:
            True when written; a failure is logged as a warning and the
            engine keeps running.
        """
        if result.config is None:
            return False
        doc = registry_document(resolved(result), self.runtime)
        data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
        tmp: str | None = None
        try:
            self.runtime.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".devices.", suffix=".tmp", dir=self.runtime)
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o644)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            tmp = None
        except OSError as exc:
            _log.warning("devices_file_unwritable", path=str(self.path), error=str(exc))
            return False
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        _log.info("devices_file_written", path=str(self.path), devices=len(doc["devices"]))
        return True

    async def reloaded(self, new: ConfigResult, plan: ReloadPlan) -> None:
        """Layout listener: write the file again after every applied reload."""
        self.write(new)
