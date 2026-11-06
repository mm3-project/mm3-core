"""Display drivers: how the module turns the display on and off (PRS-06).

A driver is built with the module context and ``display_options`` and offers
``name``, ``supports_brightness``, ``async set_power(on)``,
``async set_brightness(percent)`` and ``async check()``, which returns ``None``
when the driver can work or a reason. A driver that can read the brightness
may also offer ``async get_brightness()``; the module uses it to restore the
level before ``idle`` when ``brightness`` is not configured. Built in:
``kiosk`` (the engine hook), ``ddcutil`` (DDC/CI, VCP 0x10 and 0xD6) and ``cec``
(HDMI-CEC through ``cec-client``). Others are plug-ins on the entry point group
``mm3_presence.display``.

Implements:
    PRS-06: Core spec 0.34, §9.2 Presence and power: module boundary.
    CORE-346: Core spec 0.34, §9.2 Presence and power: module boundary.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mm3 import ConfigurationError, ModuleContext

__all__ = [
    "CecDriver",
    "DdcutilDriver",
    "DisplayDriver",
    "KioskDriver",
    "builtin",
    "load_driver",
]

GROUP = "mm3_presence.display"
TIMEOUT_S = 10.0
"""Subprocess drivers give up after this long (PRS-06)."""
_POWER_ON, _POWER_OFF = 1, 4
"""VCP 0xD6 values: on and off (standby) (PRS-06)."""


class DisplayDriver(Protocol):
    """What a display driver offers (PRS-06)."""

    name: str
    supports_brightness: bool

    async def set_power(self, on: bool) -> None:
        """Turn the display on or off."""
        ...

    async def set_brightness(self, percent: int) -> None:
        """Set the brightness, 0 to 100; only called when ``supports_brightness``."""
        ...

    async def check(self) -> str | None:
        """Return ``None`` when the driver can work, else the reason it cannot."""
        ...


class _NoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DdcutilOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display: int = Field(1, ge=1)


class _CecOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str | None = None


class _Driver:
    """Base of the built-in drivers: option validation and subprocess helpers."""

    name: ClassVar[str]
    supports_brightness: ClassVar[bool] = False
    options_model: ClassVar[type[BaseModel]] = _NoOptions

    def __init__(self, ctx: ModuleContext, options: dict[str, Any]) -> None:
        self.ctx = ctx
        try:
            self.options = self.options_model.model_validate(options)
        except ValidationError as exc:
            raise ConfigurationError("display_options", "CFG_DISPLAY_OPTIONS_INVALID") from exc

    async def _run(self, command: list[str], stdin: str | None = None) -> tuple[int, str]:
        """Run a command off the loop with the 10 s limit; return its status and output."""

        def work() -> tuple[int, str]:
            done = subprocess.run(  # noqa: S603 - fixed program names, validated options
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                check=False,
            )
            return done.returncode, (done.stdout or "") + (done.stderr or "")

        try:
            return await self.ctx.run_blocking(work)
        except FileNotFoundError:
            return 127, f"{command[0]} was not found"
        except subprocess.TimeoutExpired:
            return 124, f"{command[0]} did not answer within {TIMEOUT_S:g} s"

    @staticmethod
    def _reason(output: str, fallback: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return lines[0] if lines else fallback


class KioskDriver(_Driver):
    """Turns the kiosk display on and off through the engine (CORE-346); no brightness."""

    name = "kiosk"

    async def set_power(self, on: bool) -> None:
        """Ask the kiosk agent to switch the output (raises ``TransientError`` without one)."""
        await self.ctx.system.set_display_power(on)

    async def set_brightness(self, percent: int) -> None:
        """Not supported; never called."""
        raise NotImplementedError

    async def check(self) -> str | None:
        """The kiosk agent may connect later, so there is nothing to check here."""
        return None


class DdcutilDriver(_Driver):
    """DDC/CI through ``ddcutil``: VCP 0x10 brightness, 0xD6 power (1 on, 4 off)."""

    name = "ddcutil"
    supports_brightness = True
    options_model = _DdcutilOptions
    options: _DdcutilOptions

    def _base(self) -> list[str]:
        return ["ddcutil", "--display", str(self.options.display)]

    async def _set(self, code: str, value: int) -> None:
        status, output = await self._run([*self._base(), "setvcp", code, str(value)])
        if status != 0:
            raise RuntimeError(self._reason(output, f"ddcutil exited with {status}"))

    async def set_power(self, on: bool) -> None:
        """Set VCP 0xD6 to 1 or 4."""
        await self._set("0xD6", _POWER_ON if on else _POWER_OFF)

    async def set_brightness(self, percent: int) -> None:
        """Set VCP 0x10 to the percentage."""
        await self._set("0x10", percent)

    async def get_brightness(self) -> int | None:
        """Read VCP 0x10 as a percentage of its maximum, or ``None`` when it cannot be read."""
        status, output = await self._run([*self._base(), "getvcp", "0x10", "--brief"])
        found = re.search(r"VCP\s+10\s+C\s+(\d+)\s+(\d+)", output)
        if status != 0 or found is None or int(found.group(2)) == 0:
            return None
        return round(100 * int(found.group(1)) / int(found.group(2)))

    async def check(self) -> str | None:
        """``None`` when the monitor answers a power query, else why not."""
        status, output = await self._run([*self._base(), "getvcp", "0xD6", "--brief"])
        if status == 0:
            return None
        return self._reason(output, f"ddcutil exited with {status}")


class CecDriver(_Driver):
    """HDMI-CEC through ``cec-client -s -d 1`` with ``on 0`` and ``standby 0``."""

    name = "cec"
    options_model = _CecOptions
    options: _CecOptions

    def _base(self) -> list[str]:
        command = ["cec-client", "-s", "-d", "1"]
        if self.options.adapter:
            command.append(self.options.adapter)
        return command

    async def set_power(self, on: bool) -> None:
        """Send ``on 0`` or ``standby 0`` to the TV."""
        status, output = await self._run(self._base(), "on 0\n" if on else "standby 0\n")
        if status != 0:
            raise RuntimeError(self._reason(output, f"cec-client exited with {status}"))

    async def set_brightness(self, percent: int) -> None:
        """Not supported; never called."""
        raise NotImplementedError

    async def check(self) -> str | None:
        """``None`` when ``cec-client`` lists an adapter, else why not."""
        if shutil.which("cec-client") is None:
            return "cec-client was not found"
        status, output = await self._run(["cec-client", "-l"])
        if status != 0:
            return self._reason(output, f"cec-client exited with {status}")
        found = re.search(r"found\s+(\d+)\s+CEC adapter", output, re.IGNORECASE)
        if found is not None and int(found.group(1)) == 0:
            return "no CEC adapter was found"
        return None


def builtin() -> dict[str, type[_Driver]]:
    """Return the built-in drivers by name (PRS-06)."""
    return {d.name: d for d in (KioskDriver, DdcutilDriver, CecDriver)}


def load_driver(name: str, ctx: ModuleContext, options: dict[str, Any]) -> DisplayDriver:
    """Build a driver by name: built in first, then the entry point group.

    Args:
        name: The ``display`` config value.
        ctx: The module context.
        options: The ``display_options`` mapping.

    Returns:
        The driver.

    Raises:
        ConfigurationError: ``CFG_DISPLAY_UNKNOWN`` for a name nobody provides,
            ``CFG_DISPLAY_OPTIONS_INVALID`` for options a built-in driver refuses.
    """
    found = builtin()
    if name in found:
        return found[name](ctx, options)  # type: ignore[return-value]
    for point in entry_points(group=GROUP):
        if point.name != name:
            continue
        try:
            factory = point.load()
        except ImportError as exc:
            ctx.log.warning("driver_import_failed", driver=name, error=str(exc))
            break
        driver: DisplayDriver = factory(ctx, options)
        return driver
    raise ConfigurationError("display", "CFG_DISPLAY_UNKNOWN")
