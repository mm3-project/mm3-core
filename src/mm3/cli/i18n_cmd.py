"""``mm3 i18n``: compile message catalogs and run the catalog quality gate.

``compile`` writes ``messages.mo`` and ``messages.json`` next to every
``.po`` under the given paths; ``check`` fails when a compiled file is
missing or does not match its ``.po``, when a shipped locale lacks a key of
``en`` or a placeholder set differs from ``en``, and when one of the five
required locales is missing. Without a path both use the engine catalog.

Implements:
    CORE-40: Core spec 0.15, §8 Internationalization.
    CORE-41, CORE-42: Core spec 0.15, §8.4 Quality gate.
    CORE-263: Core spec 0.15, §8.5 Catalogs at run time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from mm3.core.i18n import CORE_CATALOG, check_locales, compile_locales
from mm3.core.i18n.compiler import po_files

__all__ = ["add_i18n_parser", "catalog_roots", "run_i18n"]

EXIT_FAILED = 1


def add_i18n_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 i18n compile`` and ``mm3 i18n check`` to the top-level parser.

    Args:
        sub: The top-level subparsers.
    """
    i18n = sub.add_parser("i18n", help="compile and check message catalogs")
    cmds = i18n.add_subparsers(dest="i18n_command", required=True)
    for name, text in (
        ("compile", "write messages.mo and messages.json next to every .po"),
        ("check", "fail on missing or stale compiled files, keys or placeholders"),
    ):
        p = cmds.add_parser(name, help=text)
        p.add_argument(
            "paths",
            nargs="*",
            type=Path,
            help="catalog roots or folders to search (default: the engine catalog)",
        )


def catalog_roots(paths: list[Path]) -> list[Path]:
    """Find the catalog roots (``<root>/<lang>/LC_MESSAGES/messages.po``) under paths.

    Args:
        paths: Roots or folders to search; empty for the engine catalog.

    Returns:
        The roots, sorted, without duplicates.
    """
    if not paths:
        return [CORE_CATALOG]
    roots: set[Path] = set()
    for path in paths:
        if po_files(path):
            roots.add(path.resolve())
        for po in path.rglob("LC_MESSAGES/messages.po"):
            roots.add(po.parent.parent.parent.resolve())
    return sorted(roots)


def run_i18n(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """Run ``mm3 i18n compile`` or ``mm3 i18n check``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.

    Returns:
        0 on success, 1 when ``check`` finds problems or no catalog was found.
    """
    stream = out or sys.stdout
    roots = catalog_roots(list(args.paths))
    if not roots:
        print("no catalogs found (<root>/<lang>/LC_MESSAGES/messages.po)", file=stream)
        return EXIT_FAILED
    if args.i18n_command == "compile":
        for root in roots:
            written = compile_locales(root)
            print(f"{root}: {len(written)} file(s) written", file=stream)
        return 0
    failed = False
    for root in roots:
        problems = check_locales(root)
        for problem in problems:
            print(problem, file=stream)
        failed = failed or bool(problems)
        if not problems:
            print(f"{root}: ok", file=stream)
    return EXIT_FAILED if failed else 0
