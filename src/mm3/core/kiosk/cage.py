"""The cage version check of ``mm3 kiosk session`` (CORE-143).

Implements:
    CORE-143: Core spec 0.16, §6.7 Display output.
"""

from __future__ import annotations

import re
import subprocess

__all__ = ["MIN_CAGE", "CageError", "cage_version", "parse_cage_version"]

MIN_CAGE = (0, 1, 5)
"""The oldest cage the kiosk supports: it has wlr-output-management."""

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class CageError(Exception):
    """cage is missing, its version cannot be read, or it is too old."""


def parse_cage_version(text: str) -> tuple[int, int, int]:
    """Read the leading ``MAJOR.MINOR.PATCH`` of ``cage -v`` output, ignoring suffixes.

    Args:
        text: What ``cage -v`` printed, e.g. ``cage version 0.1.5+20240127``.

    Returns:
        The version.

    Raises:
        CageError: No version in the text.
    """
    m = _VERSION.search(text)
    if m is None:
        raise CageError(f"cannot read the cage version from {text.strip()!r}")
    return int(m[1]), int(m[2]), int(m[3])


def cage_version(binary: str = "cage") -> tuple[int, int, int]:
    """Run ``cage -v`` and check the version against :data:`MIN_CAGE`.

    Args:
        binary: The cage executable.

    Returns:
        The version.

    Raises:
        CageError: cage cannot run, prints no version, or is older than 0.1.5.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed arguments
            [binary, "-v"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CageError(f"cannot run {binary} -v: {exc}") from exc
    version = parse_cage_version(done.stdout + done.stderr)
    if version < MIN_CAGE:
        have = ".".join(map(str, version))
        need = ".".join(map(str, MIN_CAGE))
        raise CageError(f"cage {have} is too old; the kiosk needs cage {need} or later")
    return version
