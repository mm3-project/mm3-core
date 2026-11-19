"""The ``devices`` key: camera devices, their owners and the checks on them.

A camera or a Kinect can be opened by one process at a time. The
configuration names the owner of each device; this module validates the
entries, resolves ``owner: auto`` and checks the entries against each other
(core spec 6.11). The registry file that publishes the result is written by
:mod:`mm3.core.devices`.

Implements:
    CORE-388: Core spec 0.50, §6.11 Camera devices.
    CORE-389: Core spec 0.47, §6.11 Camera devices.
    CORE-390: Core spec 0.49, §6.11 Camera devices.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from mm3.config import ModuleConfig
from mm3.core.config.records import SourceMap, ValidationRecord, make_record, pointer

__all__ = [
    "DEVICE_NAME_PATTERN",
    "VISION_MODULE",
    "DeviceConfig",
    "DeviceName",
    "ResolvedDevice",
    "check_devices",
    "resolve_devices",
]

DEVICE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
"""Pattern of a device name (CORE-388); media source and share names use it too."""

VISION_MODULE = "vision"
"""The module whose enabled instance makes ``owner: auto`` resolve to ``vision`` (CORE-389)."""

_PATH_ROOTS = ("/dev/v4l/by-id/", "/dev/v4l/by-path/")

DeviceName = Annotated[str, StringConstraints(pattern=DEVICE_NAME_PATTERN)]
DeviceKind = Literal["camera", "kinect"]
Owner = Literal["auto", "vision", "media", "none"]
ResolvedOwner = Literal["vision", "media", "none"]


class DeviceConfig(ModuleConfig):
    """One entry of ``devices`` (CORE-388).

    Attributes:
        kind: ``camera`` (a V4L2 device) or ``kinect`` (a Kinect v1).
        path: For a camera: an absolute path under ``/dev/v4l/by-id/`` or
            ``/dev/v4l/by-path/``; required.
        serial: For a Kinect: its serial; without it the first one found is used.
        owner: ``auto``, ``vision``, ``media`` or ``none``.
        images: Whether camera images of this device may be shown in a
            browser (CORE-392).

    Implements:
        CORE-388: Core spec 0.50, §6.11 Camera devices.
        CORE-389: Core spec 0.47, §6.11 Camera devices.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DeviceKind = Field(title="CFG_DEVICES_KIND", description="CFG_DEVICES_KIND_HELP")
    path: str | None = Field(
        default=None,
        validate_default=True,
        title="CFG_DEVICES_PATH",
        description="CFG_DEVICES_PATH_HELP",
    )
    serial: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        title="CFG_DEVICES_SERIAL",
        description="CFG_DEVICES_SERIAL_HELP",
    )
    owner: Owner = Field(
        default="auto", title="CFG_DEVICES_OWNER", description="CFG_DEVICES_OWNER_HELP"
    )
    images: bool = Field(
        default=False,
        strict=True,
        title="CFG_DEVICES_IMAGES",
        description="CFG_DEVICES_IMAGES_HELP",
    )

    @field_validator("path", mode="after")
    @classmethod
    def _check_path(cls, value: str | None, info: ValidationInfo) -> str | None:
        kind = info.data.get("kind")
        if kind == "kinect":
            if value is not None:
                raise PydanticCustomError(
                    "CORE_CFG_INVALID", "path on a kinect", {"reason": "a kinect has no path"}
                )
            return None
        if value is None or not _camera_path_ok(value):
            raise PydanticCustomError(
                "CORE_CFG_DEVICE_PATH", "bad camera path", {"path": value or ""}
            )
        return value

    @field_validator("serial", mode="after")
    @classmethod
    def _check_serial(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and info.data.get("kind") == "camera":
            raise PydanticCustomError(
                "CORE_CFG_INVALID", "serial on a camera", {"reason": "a camera has no serial"}
            )
        return value

    @field_validator("owner", mode="after")
    @classmethod
    def _check_owner(cls, value: Owner, info: ValidationInfo) -> Owner:
        if value == "media" and info.data.get("kind") == "kinect":
            raise PydanticCustomError("CORE_CFG_DEVICE_OWNER", "media owns no kinect", {})
        return value


def _camera_path_ok(value: str) -> bool:
    if not value.startswith(_PATH_ROOTS) or ".." in PurePosixPath(value).parts:
        return False
    return any(len(value) > len(root) for root in _PATH_ROOTS if value.startswith(root))


@dataclass(frozen=True)
class ResolvedDevice:
    """A device with its owner resolved (CORE-389).

    Attributes:
        name: The device name.
        config: The entry as configured.
        owner: The resolved owner: ``vision``, ``media`` or ``none``.
        reason: ``explicit`` when the entry names the owner, ``auto`` when it
            was resolved from ``owner: auto``.
    """

    name: str
    config: DeviceConfig
    owner: ResolvedOwner
    reason: Literal["explicit", "auto"]


def resolve_devices(
    devices: Mapping[str, DeviceConfig], enabled_modules: Collection[str]
) -> list[ResolvedDevice]:
    """Resolve the owner of every device, sorted by name (CORE-389).

    Args:
        devices: The ``devices`` key.
        enabled_modules: Module names that have at least one enabled instance.

    Returns:
        One entry per device, sorted by name.

    Implements:
        CORE-389: Core spec 0.47, §6.11 Camera devices.
    """
    vision = VISION_MODULE in enabled_modules
    out: list[ResolvedDevice] = []
    for name in sorted(devices):
        cfg = devices[name]
        if cfg.owner != "auto":
            out.append(ResolvedDevice(name, cfg, cfg.owner, "explicit"))
            continue
        owner: ResolvedOwner = "vision" if vision else "media" if cfg.kind == "camera" else "none"
        out.append(ResolvedDevice(name, cfg, owner, "auto"))
    return out


def _identity(cfg: DeviceConfig) -> tuple[str, str | None]:
    """What makes two entries the same device (CORE-390)."""
    if cfg.kind == "kinect":
        return ("kinect", cfg.serial)
    path = cfg.path or ""
    if os.path.lexists(path):
        path = os.path.realpath(path)
    return ("camera", path)


def check_devices(
    devices: Mapping[str, DeviceConfig],
    enabled_modules: Collection[str],
    sources: SourceMap | None = None,
) -> list[ValidationRecord]:
    """Check the entries against each other and against the instances (CORE-390).

    Args:
        devices: The validated ``devices`` key.
        enabled_modules: Module names that have at least one enabled instance.
        sources: Source positions for the records.

    Returns:
        ``CORE_CFG_DEVICE_DUPLICATE`` errors, one per later entry naming the
        first one (the same path after resolving links where it exists, the
        same serial, or two Kinects without a serial), and
        ``CORE_CFG_DEVICE_OWNER_ABSENT`` warnings for ``owner: vision``
        without an enabled ``vision`` instance.

    Implements:
        CORE-390: Core spec 0.49, §6.11 Camera devices.
    """
    records: list[ValidationRecord] = []
    first: dict[tuple[str, str | None], str] = {}
    for name, cfg in devices.items():
        ident = _identity(cfg)
        if ident in first:
            records.append(
                make_record(
                    "CORE_CFG_DEVICE_DUPLICATE",
                    pointer(("devices", name)),
                    {"device": name, "other": first[ident]},
                    sources=sources,
                    prefer_key=True,
                )
            )
        else:
            first[ident] = name
        if cfg.owner == "vision" and VISION_MODULE not in enabled_modules:
            records.append(
                make_record(
                    "CORE_CFG_DEVICE_OWNER_ABSENT",
                    pointer(("devices", name, "owner")),
                    {"device": name},
                    sources=sources,
                )
            )
    return records
