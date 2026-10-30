"""``mm3 module check``: prove a module meets the Module Contract.

Builds the module's wheel with ``uv build``, installs it with the running
engine in a temporary venv and runs the static checks, the test kit and the
browser harness
(Module Contract 12.1). The report goes to ``.mm3/check-report.json`` in the
module's directory. Exit code 0 means compliant, 1 a MUST failed, 2 the tool
could not run the check.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 10, §12.1 mm3 module check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

from mm3.core.modcheck.project import ToolError
from mm3.core.modcheck.report import details, summary, to_junit
from mm3.core.modcheck.runner import REPORT_PATH, Stage, run_check

__all__ = ["EXIT_FAILED", "EXIT_TOOL_ERROR", "add_module_parser", "run_module"]

EXIT_FAILED = 1
EXIT_TOOL_ERROR = 2


def add_module_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 module check`` to the top-level parser.

    Args:
        sub: The top-level subparsers.
    """
    module = sub.add_parser("module", help="check modules against the Module Contract")
    cmds = module.add_subparsers(dest="module_command", required=True)
    check = cmds.add_parser("check", help="check a module's source directory")
    check.add_argument("path", type=Path, nargs="?", default=Path(), help="project directory")
    check.add_argument("--only", choices=["static", "kit", "harness"], help="run one stage")
    check.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")
    check.add_argument("--junit", type=Path, metavar="FILE", help="also write JUnit XML")


def run_module(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """Run ``mm3 module check``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.

    Returns:
        0 when compliant, 1 when a MUST failed, 2 on a tool error.
    """
    stream = out or sys.stdout
    stage: Stage | None = args.only
    try:
        report = run_check(args.path, stage)
        target = args.path.resolve() / REPORT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
        if args.junit is not None:
            args.junit.write_text(to_junit(report), encoding="utf-8")
    except (ToolError, OSError) as exc:
        print(f"mm3 module check: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    if args.fmt == "json":
        print(json.dumps(report.as_dict(), indent=2), file=stream)
    else:
        text = details(report)
        if text:
            print(text, file=stream)
            print(file=stream)
        print(summary(report), file=stream)
        for note in report.notes:
            print(f"Note        {note}", file=stream)
        print(f"{'Report':<11} {target}", file=stream)
    return EXIT_FAILED if report.result == "non_compliant" else 0
