"""Entry point of the ``mm3`` command.

Only ``--version`` is implemented so far. Subcommands (``run``, ``check-config``,
``modules``, ``module check``, ``migrate``, ``i18n``, ``token``, ``diag``,
``installer``) are added with their milestones.

Implements:
    -: Core spec 0.2, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from mm3.__about__ import MODULE_API_VERSION, __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(prog="mm3", description="MM3 smart-mirror engine")
    parser.add_argument(
        "--version",
        action="version",
        version=f"mm3 {__version__} (module API {MODULE_API_VERSION})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``mm3`` command.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code.
    """
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
