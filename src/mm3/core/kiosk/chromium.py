"""The Chromium command line of the kiosk (CORE-270).

Implements:
    CORE-270: Core spec 0.16, §6.7 Display output.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["CHROMIUM_FLAGS", "chromium_command"]

CHROMIUM_FLAGS: tuple[str, ...] = (
    "--kiosk",
    "--ozone-platform=wayland",
    "--no-first-run",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--disable-translate",
    "--disable-pinch",
    "--overscroll-history-navigation=0",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=Translate,MediaRouter",
    "--no-default-browser-check",
    "--password-store=basic",
    "--hide-scrollbars",
)
"""Every flag Chromium gets; the cursor is hidden by the shell's CSS."""


def chromium_command(url: str, profile: Path, binary: str = "chromium") -> list[str]:
    """Return the command that starts Chromium on the shell.

    Args:
        url: The start URL, with ``#kiosk=<key>``.
        profile: The profile directory, in the kiosk user's state directory.
        binary: The Chromium executable.

    Returns:
        The argument list.
    """
    return [binary, *CHROMIUM_FLAGS, f"--user-data-dir={profile}", url]
