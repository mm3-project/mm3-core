"""Entry point of the ``mm3`` command.

Implemented so far: ``--version``, ``mm3 check-config``, ``mm3 run``,
``mm3 config`` (history, diff, undo, redo, restore), ``mm3 token``
(new, list, revoke), ``mm3 i18n`` (compile, check), ``mm3 kiosk``
(session, unit), ``mm3 module check`` and ``mm3 new-module``.
``mm3 run`` loads and validates the configuration, refuses to start with
exit code 2 when it has errors, runs the module instances and the web
gateway (mirror and admin listeners) until SIGTERM or SIGINT, and reloads
the configuration when its files change or on SIGHUP.
The other subcommands (``modules``, ``migrate``,
``diag``, ``installer``) are added with their milestones.

Implements:
    -: Core spec 0.8, §10.2 Packaging and deployment.
    CORE-30: Core spec 0.8, §6 Configuration.
    CORE-207: Core spec 0.8, §6.2 Rules.
    -: Core spec 0.8, §7.2 Error reporting.
    CORE-208: Core spec 0.11, §6.3 Hot reload.
    CORE-60: Core spec 0.12, §10 Security, packaging, deployment, quality.
    CORE-62: Core spec 0.12, §10.1 Security.
    CORE-273: Core spec 0.13, §5.6 WebSocket protocol.
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    CORE-290, CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
    CORE-292: Core spec 0.20, §10.2 Packaging and deployment.
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
from typing import TYPE_CHECKING, Any, TextIO

from mm3.__about__ import MODULE_API_VERSION, __version__
from mm3.cli.config_cmd import add_config_parser, run_config
from mm3.cli.i18n_cmd import add_i18n_parser, run_i18n
from mm3.cli.kiosk_cmd import add_kiosk_parser, run_kiosk
from mm3.cli.module_cmd import add_module_parser, run_module
from mm3.cli.new_module_cmd import add_new_module_parser, run_new_module
from mm3.cli.service_cmd import add_service_parser, run_service
from mm3.cli.token_cmd import add_token_parser, run_token
from mm3.core.config import DEFAULT_CONFIG, DEFAULT_ENV_FILE, ConfigResult, load_config
from mm3.core.registry import Registry

if TYPE_CHECKING:
    from mm3.core.config.diff import ReloadPlan
    from mm3.core.config.manager import ConfigManager
    from mm3.core.engine import Engine
    from mm3.core.web.kiosk import KioskAgents
    from mm3.core.web.ws import ClientHub

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
    add_config_parser(sub, _config_args)
    add_token_parser(sub, _config_args)
    add_i18n_parser(sub)
    add_kiosk_parser(sub)
    add_service_parser(sub)
    add_module_parser(sub)
    add_new_module_parser(sub)
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


def _kiosk_agents(hub: ClientHub, manager: ConfigManager, engine: Engine) -> KioskAgents | None:
    """Write the agent key and build ``/ws/kiosk-agent``; ``None`` when the key cannot be written.

    Implements:
        CORE-268: Core spec 0.20, §6.7 Display output.
    """
    from mm3.core.logging import get_logger
    from mm3.core.web.kiosk import AgentKey, KioskAgents, runtime_dir

    log = get_logger(subsystem="kiosk")
    key = AgentKey(runtime_dir())
    try:
        path = key.write()
    except OSError as exc:
        log.warning("kiosk_agent_key_unwritable", error=str(exc))
        return None
    log.info("kiosk_agent_key_written", path=str(path))

    def display() -> dict[str, Any]:
        config = manager.current.config
        return config.display.model_dump(mode="json") if config is not None else {}

    agents = KioskAgents(key, hub, display, manager.set_applied, engine.clock)

    async def on_reload(new: ConfigResult, plan: ReloadPlan) -> None:
        if plan.display:
            await agents.display_changed()

    manager.layout_listeners.append(on_reload)
    return agents


async def _serve(result: ConfigResult, registry: Registry, env_file: Path) -> None:
    from mm3.core.config.history import ConfigHistory, HistoryRecorder
    from mm3.core.config.manager import ConfigManager
    from mm3.core.engine import Engine
    from mm3.core.pages import PageService
    from mm3.core.services.storage import resolve_state_dir
    from mm3.core.web.auth import WebAuth
    from mm3.core.web.gateway import WebGateway
    from mm3.core.web.shell import STATIC_DIR, ShellSources
    from mm3.core.web.view import cached_view
    from mm3.core.web.ws import ClientHub, engine_build

    assert result.config is not None  # noqa: S101 - checked by the caller
    state_dir = resolve_state_dir()
    engine = Engine(result.config.engine, registry=registry, state_dir=state_dir)
    engine.start_watchdog()  # CORE-291: lives as long as the process
    history = ConfigHistory(result.path, env_file=env_file, registry=registry, state_dir=state_dir)
    manager = ConfigManager(
        engine, result, registry=registry, env_file=env_file, recorder=HistoryRecorder(history)
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
        loop.add_signal_handler(signal.SIGHUP, manager.request_reload, "sighup")

    def server_allow() -> list[Any]:
        # server.allow applies live, to connections made afterwards (CORE-201).
        current = manager.current.config
        return list(current.engine.server.allow) if current is not None else []

    view = cached_view(lambda: manager.current, registry, i18n=engine.i18n)
    hub = ClientHub(
        view,
        WebAuth.open(state_dir),
        engine_build(STATIC_DIR),
    )
    engine.attach_clients(hub)
    pages = PageService(result, engine.supervisor, engine.clock)
    engine.supervisor.pages = pages
    engine.bus.add_endpoint(pages)
    hub.attach_pages(pages, engine.supervisor)
    manager.layout_listeners.append(hub.reloaded)
    agents = _kiosk_agents(hub, manager, engine)
    gateway = WebGateway(
        result.config.engine,
        state_dir,
        server_allow=server_allow,
        hub=hub,
        sources=ShellSources(view, lambda: manager.current, engine.i18n),
        agents=agents,
    )
    await engine.start(result.instance_specs())
    await manager.start()
    await pages.start()
    watchdog = asyncio.create_task(agents.watchdog(), name="mm3-kiosk-watchdog") if agents else None
    try:
        await gateway.start()
        engine.notify_ready()  # CORE-290: READY=1 once the listeners are up
        await stop.wait()
    finally:
        engine.notify_stopping()  # CORE-290: STOPPING=1 when shutdown begins
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        await gateway.stop()
        await pages.close()
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
    if args.command == "config":
        return run_config(args, registry=_discover())
    if args.command == "token":
        return run_token(args)
    if args.command == "i18n":
        return run_i18n(args)
    if args.command == "kiosk":
        return run_kiosk(args)
    if args.command == "service":
        return run_service(args)
    if args.command == "module":
        return run_module(args)
    if args.command == "new-module":
        return run_new_module(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
