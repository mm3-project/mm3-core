"""``mm3 kiosk``: the kiosk session that runs inside cage, and its unit file.

``mm3 kiosk session`` checks the cage version first and refuses to start
when it cannot be read or is older than 0.1.5; ``--print-command`` shows
the Chromium command line instead. ``mm3 kiosk unit`` prints
``mm3-kiosk.service``.

Implements:
    CORE-143: Core spec 0.16, §6.7 Display output.
    CORE-270: Core spec 0.16, §6.7 Display output.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from importlib import resources
from pathlib import Path
from typing import TextIO

from mm3.core.kiosk import CageError, cage_version
from mm3.core.kiosk.agent import KioskSession
from mm3.core.web.kiosk import AGENT_KEY_FILE

__all__ = ["add_kiosk_parser", "kiosk_profile", "run_kiosk", "unit_text"]

EXIT_FAILED = 1
DEFAULT_URL = "http://127.0.0.1:8080"


def add_kiosk_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 kiosk session`` and ``mm3 kiosk unit``.

    Args:
        sub: The top-level subparsers.
    """
    kiosk = sub.add_parser("kiosk", help="the kiosk session (inside cage) and its unit file")
    cmds = kiosk.add_subparsers(dest="kiosk_command", required=True)
    p = cmds.add_parser("session", help="run the kiosk agent: display, Chromium (inside cage)")
    p.add_argument("--url", default=DEFAULT_URL, help=f"mirror listener (default {DEFAULT_URL})")
    p.add_argument(
        "--key-file",
        type=Path,
        default=Path("/run/mm3") / AGENT_KEY_FILE,
        help="agent key written by the engine",
    )
    p.add_argument("--profile", type=Path, default=None, help="Chromium profile directory")
    p.add_argument("--chromium", default="chromium", help="Chromium executable")
    p.add_argument(
        "--print-command", action="store_true", help="print the Chromium command and exit"
    )
    cmds.add_parser("unit", help="print mm3-kiosk.service")


def kiosk_profile() -> Path:
    """Chromium's profile: ``$STATE_DIRECTORY/chromium`` or ``~/.local/state/mm3-kiosk``."""
    state = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
    base = Path(state) if state else Path.home() / ".local" / "state" / "mm3-kiosk"
    return base / "chromium"


def unit_text() -> str:
    """Return the shipped ``mm3-kiosk.service``."""
    return (resources.files("mm3.core.kiosk") / "mm3-kiosk.service").read_text()


def run_kiosk(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """Run ``mm3 kiosk session`` or ``mm3 kiosk unit``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.

    Returns:
        The exit code.
    """
    stream = out or sys.stdout
    if args.kiosk_command == "unit":
        stream.write(unit_text())
        return 0
    profile = args.profile or kiosk_profile()
    if args.print_command:
        session = KioskSession(args.url, "", "", profile, chromium=args.chromium)
        print(" ".join(session.command("<key>")), file=stream)
        return 0
    try:
        version = cage_version()
    except CageError as exc:
        print(f"mm3 kiosk session: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        key = args.key_file.read_text().strip()
    except OSError as exc:
        print(f"mm3 kiosk session: cannot read the agent key: {exc}", file=sys.stderr)
        return EXIT_FAILED
    session = KioskSession(
        args.url, key, ".".join(map(str, version)), profile, chromium=args.chromium
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(session.run())
    return 0
