"""``mm3 config``: configuration history, diff, undo, redo and restore.

The commands work whether the engine runs or not: they validate, write the
config files and record the version under the history lock, and a running
engine applies the result through its file watcher.

Implements:
    CORE-219: Core spec 0.10, §6.8 Configuration history, undo and redo.
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from mm3.core.config.history import ConfigHistory, OpResult
from mm3.core.registry import Registry
from mm3.core.services.storage import resolve_state_dir

__all__ = ["add_config_parser", "run_config"]


def add_config_parser(
    sub: argparse._SubParsersAction,  # type: ignore[type-arg]
    config_args: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Add ``mm3 config`` and its subcommands to the top-level parser.

    Args:
        sub: The top-level subparsers.
        config_args: Adds ``--config`` and ``--env-file`` to a parser.
    """
    cfg = sub.add_parser("config", help="configuration history, undo, redo and restore")
    cmds = cfg.add_subparsers(dest="config_command", required=True)
    add = config_args

    p = cmds.add_parser("history", help="list saved versions")
    add(p)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true")

    p = cmds.add_parser("diff", help="show the difference between two versions")
    add(p)
    p.add_argument("a", type=int)
    p.add_argument("b", type=int)
    p.add_argument("--section", default=None)
    p.add_argument("--json", action="store_true")

    for name, text in (("undo", "undo the last save"), ("redo", "redo the last undo")):
        p = cmds.add_parser(name, help=text)
        add(p)
        p.add_argument("--force", action="store_true", help="overwrite invalid files on disk")

    p = cmds.add_parser("restore", help="restore a version or one section of it")
    add(p)
    p.add_argument("version", type=int)
    p.add_argument("--section", default=None)
    p.add_argument("--force", action="store_true", help="overwrite invalid files on disk")


def _report(result: OpResult, out: TextIO, err: TextIO) -> int:
    if result.code == 0:
        print(result.version, file=out)
    else:
        print(f"mm3 config: {result.message}", file=err)
        for rec in result.records:
            print(rec.format_line(), file=err)
    return result.code


def run_config(
    args: argparse.Namespace,
    *,
    registry: Registry,
    state_dir: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run an ``mm3 config`` subcommand.

    Args:
        args: Parsed arguments.
        registry: Installed modules, used to validate.
        state_dir: Engine state directory; from the environment by default.
        out: Output stream.
        err: Error stream.

    Returns:
        Exit code: 0 done, 1 nothing to undo or redo, 2 validation failed and
        nothing applied, 3 refused because the files on disk differ from the
        current version and are invalid (CORE-219).

    Implements:
        CORE-219: Core spec 0.10, §6.8 Configuration history, undo and redo.
    """
    stdout, stderr = out or sys.stdout, err or sys.stderr
    history = ConfigHistory(
        args.config,
        env_file=args.env_file,
        registry=registry,
        state_dir=state_dir or resolve_state_dir(),
    )
    author = f"cli:{getpass.getuser()}"
    cmd = args.config_command
    if cmd == "history":
        versions = history.history(args.limit)
        if args.json:
            print(json.dumps([asdict(v) for v in versions], indent=2), file=stdout)
        for v in versions if not args.json else []:
            sections = ", ".join(v.sections) or "(no sections)"
            parent = "-" if v.parent is None else str(v.parent)
            print(
                f"{v.version}\t{v.time}\t{v.origin}\t{v.author}\tparent {parent}\t{sections}",
                file=stdout,
            )
        return 0
    if cmd == "diff":
        try:
            changes = history.diff(args.a, args.b, section=args.section)
        except KeyError as exc:
            print(f"mm3 config: version {exc.args[0]} does not exist", file=stderr)
            return 2
        if args.json:
            doc = {
                "a": args.a,
                "b": args.b,
                "sections": {k: {"a": a, "b": b} for k, (a, b) in changes.items()},
            }
            print(json.dumps(doc, indent=2), file=stdout)
            return 0
        for name, (before, after) in changes.items():
            stdout.writelines(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"{args.a}:{name}",
                    tofile=f"{args.b}:{name}",
                )
            )
        return 0
    if cmd == "undo":
        return _report(history.undo(author, force=args.force), stdout, stderr)
    if cmd == "redo":
        return _report(history.redo(author, force=args.force), stdout, stderr)
    return _report(
        history.restore(args.version, author, section=args.section, force=args.force),
        stdout,
        stderr,
    )
