"""``mm3 diag bundle`` and ``mm3 diag check``: diagnostics from the command line.

Both reach the running engine only on the local admin socket
``/run/mm3/admin.sock`` (CORE-311). ``mm3 diag bundle`` saves the zip of
``GET /api/v1/debug/bundle``; when the socket does not answer it builds a
partial bundle from the configuration and the journal (CORE-318).
``mm3 diag check`` runs the self-test and prints one line per check:
``pass``, ``fail`` with a hint, or ``skip`` with the reason. Its exit code is
0 when no check failed, 1 when one failed and 2 on a tool error (CORE-312).

Implements:
    CORE-311: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-312: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-318: Core spec 0.24, §10.5 Diagnostics and debugging.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.metadata
import json
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

from mm3.core.config import load_config
from mm3.core.config.validate import ConfigResult
from mm3.core.diag.bundle import Bundle, bundle_name, package_list
from mm3.core.diag.secrets import Masker, config_secrets
from mm3.core.i18n import module_catalog_root
from mm3.core.i18n.catalogs import CORE_CATALOG
from mm3.core.registry import Registry
from mm3.core.web.kiosk import runtime_dir

__all__ = [
    "LANGUAGES",
    "Check",
    "SocketClient",
    "add_diag_parser",
    "run_checks",
    "run_diag",
]

LANGUAGES = ("en", "it", "fr", "es", "de")
"""Languages every user-facing catalog ships (core spec 8)."""
JOURNAL_LINES = 1000
EXIT_OK, EXIT_FAILED, EXIT_TOOL = 0, 1, 2

Outcome = Literal["pass", "fail", "skip"]


def add_diag_parser(
    sub: argparse._SubParsersAction,  # type: ignore[type-arg]
    config_args: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Add ``mm3 diag bundle`` and ``mm3 diag check``.

    Args:
        sub: The top-level subparsers.
        config_args: Adds ``--config`` and ``--env-file``.
    """
    diag = sub.add_parser("diag", help="diagnostics: bundle and self-test")
    cmds = diag.add_subparsers(dest="diag_command", required=True)
    bundle = cmds.add_parser("bundle", help="save a diagnostic bundle (zip)")
    check = cmds.add_parser("check", help="run the self-test")
    for p in (bundle, check):
        config_args(p)
        p.add_argument("--socket", type=Path, default=None, help="the engine's admin socket")
    bundle.add_argument("--out", type=Path, default=Path.cwd(), help="directory for the zip")


# --- the socket client ------------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.path))
        self.sock = sock


class SocketClient:
    """HTTP over the local admin socket, with the standard library (CORE-311).

    Args:
        path: The socket.
        timeout: Seconds per request.
    """

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout

    def request(
        self,
        method: str,
        target: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        """Send a request; a ``body`` is sent as JSON.

        Args:
            method: ``GET``, ``POST`` or ``PUT``.
            target: Path and query, such as ``/api/v1/status``.
            body: A JSON-serializable body, or ``None`` for none.
            headers: Further headers, such as ``If-Match`` (CORE-300).

        Returns:
            The status code and the body.

        Raises:
            OSError: The socket does not answer.
        """
        conn = _UnixConnection(self.path, self.timeout)
        sent = dict(headers or {})
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        try:
            conn.request(method, target, body=data, headers=sent)
            response = conn.getresponse()
            return response.status, response.read()
        except http.client.HTTPException as exc:
            raise OSError(str(exc)) from exc
        finally:
            conn.close()

    def get(self, target: str) -> tuple[int, bytes]:
        """Send ``GET target``.

        Args:
            target: Path and query, such as ``/api/v1/status``.

        Returns:
            The status code and the body.

        Raises:
            OSError: The socket does not answer.
        """
        return self.request("GET", target)

    def json(self, target: str) -> Any:
        """Send ``GET target`` and decode a 200 JSON body.

        Raises:
            OSError: No answer, or an answer other than 200.
        """
        status, body = self.get(target)
        if status != 200:
            raise OSError(f"{target} answered {status}")
        return json.loads(body)


def _socket(args: argparse.Namespace) -> SocketClient:
    return SocketClient(args.socket or runtime_dir() / "admin.sock")


# --- mm3 diag bundle --------------------------------------------------------------------


def journal_lines(lines: int = JOURNAL_LINES) -> str | None:
    """Return the last journal lines of ``mm3.service``, or ``None`` without journalctl."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            ["journalctl", "-u", "mm3.service", "-n", str(lines), "--no-pager", "-o", "short-iso"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def module_versions(registry: Registry) -> dict[str, str]:
    """Return the version of each installed module's distribution."""
    out: dict[str, str] = {}
    for entry in registry.entries():
        version = "0"
        if entry.distribution:
            with contextlib.suppress(importlib.metadata.PackageNotFoundError):
                version = importlib.metadata.version(entry.distribution)
        out[entry.name] = version
    return out


def partial_bundle(
    result: ConfigResult | None,
    env_file: Path,
    registry: Registry,
    journal: Callable[[], str | None] = journal_lines,
) -> Bundle:
    """Build the bundle the CLI makes when the engine does not answer (CORE-318).

    Args:
        result: The loaded configuration, if it could be read.
        env_file: The env file.
        registry: Installed modules, for their versions.
        journal: Returns the journal lines.

    Returns:
        The bundle with ``partial: true``.

    Implements:
        CORE-318: Core spec 0.24, §10.5 Diagnostics and debugging.
        CORE-116: Core spec 0.22, §10.5 Diagnostics and debugging.
    """
    masker = Masker(config_secrets(result, env_file)) if result is not None else Masker()
    bundle = Bundle(masker=masker, partial=True)
    bundle.modules = module_versions(registry)
    bundle.add_bytes("packages.txt", package_list().encode("utf-8"))
    if result is not None:
        config_dir = result.path.parent
        files = [p for p in result.loaded.files if p.is_file()]
        css = config_dir / "custom.css"
        if css.is_file():
            files.append(css)
        bundle.add_config(files, config_dir)
    else:
        bundle.missing.append("config/")
    text = journal()
    if text is None:
        bundle.missing.append("journal.txt")
    else:
        bundle.add_text("journal.txt", text)
    bundle.missing.extend(["logs.jsonl", "health.json", "browser-errors.jsonl", "kiosk.json"])
    return bundle


def run_bundle(
    args: argparse.Namespace,
    registry: Registry,
    *,
    out: TextIO,
    journal: Callable[[], str | None] = journal_lines,
) -> int:
    """Run ``mm3 diag bundle`` (CORE-311).

    Returns:
        0 when the zip was written, 2 when it could not be.
    """
    directory: Path = args.out
    client = _socket(args)
    try:
        status, data = client.get("/api/v1/debug/bundle")
        if status != 200:
            raise OSError(f"the engine answered {status}")
        name = bundle_name()
        out.write("bundle from the running engine\n")
    except OSError as exc:
        out.write(f"engine not reachable on {client.path} ({exc}); building a partial bundle\n")
        result = _load(args, registry)
        partial = partial_bundle(result, args.env_file, registry, journal)
        data = partial.build()
        name = bundle_name(partial.now)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.write_bytes(data)
    except OSError as exc:
        out.write(f"cannot write the bundle: {exc}\n")
        return EXIT_TOOL
    out.write(f"{target}\n")
    return EXIT_OK


# --- mm3 diag check ---------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One line of the self-test (CORE-312).

    Attributes:
        name: What was checked.
        outcome: ``pass``, ``fail`` or ``skip``.
        detail: The hint for ``fail``, the reason for ``skip``, or a short note.
    """

    name: str
    outcome: Outcome
    detail: str = ""

    def line(self) -> str:
        """Return the printed line."""
        return f"{self.outcome:<4}  {self.name}" + (f": {self.detail}" if self.detail else "")


def _load(args: argparse.Namespace, registry: Registry) -> ConfigResult | None:
    try:
        return load_config(args.config, registry=registry, env_file=args.env_file)
    except OSError:
        return None


def _port_free(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError:
        return False
    return True


def catalog_problems(roots: dict[str, Path | None]) -> list[str]:
    """Return what is missing from the compiled catalogs (CORE-312).

    Args:
        roots: Catalog root per module (``mm3`` for the core); ``None`` when a
            module has no catalogs.

    Returns:
        One short text per problem: a missing language or keys missing
        compared with English.
    """
    problems: list[str] = []
    for name, root in sorted(roots.items()):
        if root is None:
            continue
        catalogs: dict[str, dict[str, Any]] = {}
        for lang in LANGUAGES:
            path = root / lang / "messages.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                problems.append(f"{name}: no compiled {lang} catalog")
                continue
            catalogs[lang] = data if isinstance(data, dict) else {}
        english = set(catalogs.get("en", {}))
        for lang, messages in catalogs.items():
            missing = english - set(messages)
            if missing:
                problems.append(f"{name}: {lang} lacks {len(missing)} key(s)")
    return problems


def _display_driver_check(
    result: ConfigResult | None, client: SocketClient, status: dict[str, Any] | None
) -> Check:
    """Report the display driver of the presence module (CORE-312, PRS-06).

    The module runs the driver's ``check()`` at start and after a failed call
    and keeps the answer in its health fields: ``driver`` is the driver's name
    and ``driver_error`` the reason it cannot work, absent when it can.

    Args:
        result: The loaded configuration, or ``None``.
        client: The admin socket.
        status: The engine status, or ``None`` when the engine is not reachable.

    Returns:
        The check: skipped without a presence instance or an engine.
    """
    name = "display driver"
    ids = (
        [i.id for i in result.instances if i.module == "presence" and i.enabled]
        if result is not None
        else []
    )
    if not ids:
        return Check(name, "skip", "no presence module is configured")
    if status is None:
        return Check(name, "skip", "engine not reachable")
    try:
        health = client.json(f"/api/v1/modules/{ids[0]}/health")
    except (OSError, ValueError) as exc:
        return Check(name, "skip", f"the health of {ids[0]} is not available: {exc}")
    state = str(health.get("state", "")) if isinstance(health, dict) else ""
    fields = health.get("fields") if isinstance(health, dict) else None
    fields = fields if isinstance(fields, dict) else {}
    if state == "FAILED":
        return Check(name, "fail", f"{ids[0]} failed to start; see the debug page")
    if state not in ("RUNNING", "SUSPENDED"):
        return Check(name, "skip", f"{ids[0]} is {state.lower() or 'not running'}")
    driver = fields.get("driver")
    if fields.get("driver_error"):
        return Check(name, "fail", f"{driver or 'driver'}: {fields['driver_error']}")
    return Check(name, "pass", str(driver) if driver else "")


def run_checks(
    result: ConfigResult | None,
    client: SocketClient,
    registry: Registry,
    *,
    port_free: Callable[[str, int], bool] = _port_free,
) -> list[Check]:
    """Run the self-test of CORE-312.

    Args:
        result: The loaded configuration, or ``None`` when it cannot be read.
        client: The admin socket.
        registry: Installed modules.
        port_free: Tells whether a listener address is free (tests replace it).

    Returns:
        The checks in the order of CORE-312.

    Implements:
        CORE-312: Core spec 0.22, §10.5 Diagnostics and debugging.
        CORE-339: Core spec 0.34, §9.3 MQTT bridge.
        PRS-06: Core spec 0.34, §9.2 Presence and power: module boundary.
    """
    checks: list[Check] = []
    config = result.config if result is not None and result.ok else None
    if result is None:
        checks.append(Check("config valid", "fail", "the configuration file cannot be read"))
    elif config is None:
        errors = [r for r in result.records if r.severity == "error"]
        checks.append(
            Check("config valid", "fail", f"{len(errors)} error(s); run mm3 check-config")
        )
    else:
        checks.append(Check("config valid", "pass"))

    status: dict[str, Any] | None = None
    try:
        status = client.json("/api/v1/status")
        checks.append(Check("engine reachable on the socket", "pass", str(client.path)))
    except (OSError, ValueError) as exc:
        checks.append(
            Check(
                "engine reachable on the socket",
                "fail",
                f"{client.path}: {exc}; is mm3.service running and are you in group mm3?",
            )
        )

    if config is None:
        checks.append(Check("listener ports", "skip", "no valid configuration"))
    else:
        busy = [
            f"{s.host}:{s.port}"
            for s in (config.engine.server, config.engine.admin)
            if not port_free(s.host, s.port)
        ]
        if not busy:
            checks.append(Check("listener ports", "pass", "free"))
        elif status is not None:
            checks.append(Check("listener ports", "pass", "held by the engine"))
        else:
            checks.append(
                Check("listener ports", "fail", f"{', '.join(busy)} in use by another process")
            )

    kiosk = status.get("kiosk") if status is not None else None
    if status is None:
        checks.append(Check("kiosk agent", "skip", "engine not reachable"))
        checks.append(Check("audio output", "skip", "engine not reachable"))
    elif kiosk is None:
        checks.append(Check("kiosk agent", "skip", "no kiosk agent has connected"))
        checks.append(Check("audio output", "skip", "no kiosk agent has connected"))
    else:
        chromium = kiosk.get("chromium") or {}
        if not kiosk.get("connected"):
            checks.append(Check("kiosk agent", "fail", "disconnected; check mm3-kiosk.service"))
        elif chromium.get("failed"):
            checks.append(
                Check("kiosk agent", "fail", "Chromium failed repeatedly; see the debug page")
            )
        else:
            checks.append(Check("kiosk agent", "pass", f"Chromium {chromium.get('state')}"))
        audio = kiosk.get("audio") or {}
        if audio.get("failure"):
            checks.append(Check("audio output", "fail", f"PipeWire: {audio['failure']}"))
        elif audio.get("mismatch"):
            checks.append(
                Check("audio output", "fail", "the configured output was not found or is off")
            )
        else:
            checks.append(Check("audio output", "pass"))

    if config is None or config.engine.mqtt is None:
        checks.append(Check("MQTT broker", "skip", "no broker is configured"))
    elif status is None:
        checks.append(Check("MQTT broker", "skip", "engine not reachable"))
    else:
        mqtt = status.get("mqtt")
        if not isinstance(mqtt, dict):
            checks.append(Check("MQTT broker", "skip", "the bridge is not running"))
        elif mqtt.get("connected"):
            checks.append(Check("MQTT broker", "pass", f"connected since {mqtt.get('since')}"))
        else:
            reason = mqtt.get("error") or "not connected yet"
            checks.append(Check("MQTT broker", "fail", f"not connected: {reason}"))

    checks.append(_display_driver_check(result, client, status))

    roots: dict[str, Path | None] = {"mm3": CORE_CATALOG}
    if result is not None:
        for inst in result.instances:
            entry = registry.get(inst.module)
            if entry is not None and entry.cls is not None:
                roots[inst.module] = module_catalog_root(entry.cls)
    problems = catalog_problems(roots)
    if problems:
        checks.append(Check("locale catalogs", "fail", "; ".join(problems[:5])))
    else:
        checks.append(Check("locale catalogs", "pass"))
    return checks


def run_check(args: argparse.Namespace, registry: Registry, *, out: TextIO) -> int:
    """Run ``mm3 diag check`` and print one line per check (CORE-312)."""
    checks = run_checks(_load(args, registry), _socket(args), registry)
    for check in checks:
        out.write(check.line() + "\n")
    return EXIT_FAILED if any(c.outcome == "fail" for c in checks) else EXIT_OK


def run_diag(
    args: argparse.Namespace, registry: Registry | None = None, out: TextIO | None = None
) -> int:
    """Run ``mm3 diag``.

    Args:
        args: Parsed arguments.
        registry: Installed modules; discovered by default.
        out: Output stream; stdout by default.

    Returns:
        0, 1 when a check failed, 2 on a tool error.
    """
    stream = out or sys.stdout
    try:
        if registry is None:
            registry = Registry()
            registry.discover()
        if args.diag_command == "bundle":
            return run_bundle(args, registry, out=stream)
        return run_check(args, registry, out=stream)
    except Exception as exc:
        stream.write(f"mm3 diag: {exc}\n")
        return EXIT_TOOL
