"""``mm3 service unit``: print the shipped ``mm3.service`` (CORE-292).

The unit ships inside the ``mm3-core`` package, as the kiosk's does (CORE-143);
the installer puts it in ``/etc/systemd/system``.

Implements:
    CORE-292: Core spec 0.20, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from typing import TextIO

__all__ = ["add_service_parser", "run_service", "unit_text"]


def add_service_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 service unit``.

    Args:
        sub: The top-level subparsers.
    """
    service = sub.add_parser("service", help="the engine's systemd unit")
    cmds = service.add_subparsers(dest="service_command", required=True)
    cmds.add_parser("unit", help="print mm3.service")


def unit_text() -> str:
    """Return the shipped ``mm3.service``."""
    return (resources.files("mm3.core.systemd") / "mm3.service").read_text()


def run_service(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """Run ``mm3 service unit``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.

    Returns:
        The exit code.
    """
    if args.service_command == "unit":
        (out or sys.stdout).write(unit_text())
    return 0
