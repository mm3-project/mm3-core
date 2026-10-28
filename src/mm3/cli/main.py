"""Entry point of the ``mm3`` command.

Implemented so far: ``--version``, ``mm3 check-config`` and ``mm3 run``.
``mm3 run`` loads and validates the configuration, refuses to start with
exit code 2 when it has errors, runs the module instances until SIGTERM or
SIGINT, and reloads the configuration when its files change or on SIGHUP;
the web gateway joins it in milestone 3.
The other subcommands (``modules``, ``module check``, ``migrate``, ``i18n``,
``token``, ``diag``, ``installer``, ``config``) are added with their
milestones.

Implements:
    -: Core spec 0.8, §10.2 Packaging and deployment.
    CORE-30: Core spec 0.8, §6 Configuration.
    CORE-207: Core spec 0.8, §6.2 Rules.
    -: Core spec 0.8, §7.2 Error reporting.
    CORE-208: Core spec 0.9, §6.3 Hot reload.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from mm3.__about__ import MODULE_API_VERSION, __version__
from mm3.core.config import DEFAULT_CONFIG, DEFAULT_ENV_FILE, ConfigResult, load_config
from mm3.core.registry import Registry

__all__ = ["EXIT_CONFIG_INVALID", "build_parser", "main"]

EXIT_CONFIG_INVALID = 2


def _config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="main configuration file")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="env file for !env")


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
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check-config", help="validate the configuration without starting")
    _config_args(check)
    check.add_argument("--strict", action="store_true", help="treat warnings as errors")
    check.add_argument("--json", action="store_true", help="print the records as JSON")
    run = sub.add_parser("run", help="run the engine")
    _config_args(run)
    return parser


def _discover() -> Registry:
    registry = Registry()
    registry.discover()
    return registry


def _print_records(result: ConfigResult, out: TextIO) -> None:
    for rec in result.records:
        print(rec.format_line(), file=out)


def check_config(
    config: Path,
    env_file: Path,
    *,
    strict: bool = False,
    as_json: bool = False,
    registry: Registry | None = None,
    out: TextIO | None = None,
) -> int:
    """Run ``mm3 check-config``.

    Args:
        config: Main configuration file.
        env_file: Env file.
        strict: Treat warnings as errors.
        as_json: Print JSON instead of one line per record.
        registry: Installed modules; discovered from entry points by default.
        out: Output stream; stdout by default.

    Returns:
        0 when there is no error, 2 when there are errors (or warnings with
        ``strict``).

    Implements:
        CORE-207: Core spec 0.8, §6.2 Rules.
    """
    stream = out or sys.stdout
    result = load_config(config, registry=registry or _discover(), env_file=env_file)
    failed = bool(result.errors) or (strict and bool(result.warnings))
    view = result.layout.viewport if result.layout is not None else None
    if as_json:
        doc: dict[str, object] = {
            "ok": not failed,
            "errors": len(result.errors),
            "warnings": len(result.warnings),
            "records": [r.as_dict() for r in result.records],
        }
        if view is not None:
            doc["viewport"] = {"width": view.width, "height": view.height, "assumed": True}
        print(json.dumps(doc, indent=2, ensure_ascii=False), file=stream)
    else:
        _print_records(result, stream)
        if view is not None:
            # Implements CORE-215 (Core spec 0.9, §6.7 Display output): say the viewport is assumed.
            print(
                f"viewport {view.width}x{view.height} CSS px (assumed, no kiosk report)",
                file=stream,
            )
    return EXIT_CONFIG_INVALID if failed else 0


async def _serve(result: ConfigResult, registry: Registry, env_file: Path) -> None:
    from mm3.core.config.manager import ConfigManager
    from mm3.core.engine import Engine

    assert result.config is not None  # noqa: S101 - checked by the caller
    engine = Engine(result.config.engine, registry=registry)
    manager = ConfigManager(engine, result, registry=registry, env_file=env_file)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
        loop.add_signal_handler(signal.SIGHUP, manager.request_reload, "sighup")
    await engine.start(result.instance_specs())
    await manager.start()
    try:
        await stop.wait()
    finally:
        await manager.close()
        await engine.stop()


def run(
    config: Path, env_file: Path, *, registry: Registry | None = None, err: TextIO | None = None
) -> int:
    """Run ``mm3 run``: validate, then run the engine until stopped.

    An invalid configuration aborts startup: every record is printed with
    its file and line, and the exit code is 2 (core spec 7.2).

    Args:
        config: Main configuration file.
        env_file: Env file.
        registry: Installed modules; discovered from entry points by default.
        err: Stream for startup errors; stderr by default.

    Returns:
        Process exit code.

    Implements:
        CORE-30: Core spec 0.8, §6 Configuration.
    """
    from mm3.core.logging import configure_logging, get_logger

    registry = registry or _discover()
    result = load_config(config, registry=registry, env_file=env_file)
    if not result.ok or result.config is None:
        _print_records(result, err or sys.stderr)
        return EXIT_CONFIG_INVALID
    configure_logging(result.config.engine.log_level)
    log = get_logger(subsystem="config")
    for rec in result.warnings:
        log.warning("config_warning", **rec.as_dict())
    asyncio.run(_serve(result, registry, env_file))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``mm3`` command.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check-config":
        return check_config(args.config, args.env_file, strict=args.strict, as_json=args.json)
    if args.command == "run":
        return run(args.config, args.env_file)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
