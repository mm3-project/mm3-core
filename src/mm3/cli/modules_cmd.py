"""``mm3 modules``: search, info, list, refresh, install, update, remove, restore-previous.

``search``, ``info`` and ``list`` read the engine's copy of the index, written
by the installer, and work offline, saying when it is stale (CORE-125,
CORE-351); they show only what is compatible with the running Module API
(CORE-124). ``refresh``, ``install``, ``update``, ``remove`` and ``restore-previous`` send
their request to the installer socket; the CLI never writes a venv (CORE-353).
A listed package needs the passphrase, and the TOTP code when enabled; an
unlisted one (``--from-pypi`` or a local wheel) needs root and no passphrase;
root runs the installer venv's ``mm3`` (``sudo /opt/mm3/installer/bin/mm3``),
never the engine venv's, so no module code runs as root (CORE-369).
Before an install the requested capabilities and grant scopes are shown for
confirmation (CORE-133).

``remove`` first asks the running engine, on its admin socket (CORE-311), to save a
configuration history version that sets ``disabled: true`` on every enabled instance of the
module, and only then sends the removal to the installer; instances are disabled, never
deleted. With no engine on the socket it stops with an error and changes nothing (CORE-358).

Implements:
    CORE-353: Core spec 0.33, §10.6 Module catalog.
    CORE-124, CORE-125, CORE-126: Core spec 0.33, §10.6 Module catalog.
    CORE-351: Core spec 0.35, §10.6 Module catalog.
    CORE-369: Core spec 0.37, §10.7 Installer service.
    CORE-133: Core spec 0.33, §10.7 Installer service.
    CORE-358: Core spec 0.33, §10.7 Installer service.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from packaging.utils import canonicalize_name
from packaging.version import Version

from mm3.__about__ import MODULE_API_VERSION
from mm3.cli.diag_cmd import SocketClient
from mm3.core.catalog import (
    CachedCatalog,
    CatalogError,
    Entry,
    compatible,
    find_entry,
    newest_compatible,
    read_cached,
    search,
)
from mm3.core.config.removal import disable_ops
from mm3.core.services.storage import resolve_state_dir
from mm3.core.web.kiosk import runtime_dir
from mm3.installer.client import DEFAULT_SOCKET, InstallerClient, InstallerUnavailable

__all__ = ["add_modules_parser", "run_modules"]

EXIT_OK, EXIT_FAILED, EXIT_TOOL = 0, 1, 2
ENGINE_STATE = Path("/var/lib/mm3")

Ask = Callable[[str], str]


def add_modules_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 modules …``.

    Args:
        sub: The top-level subparsers.
    """
    mods = sub.add_parser("modules", help="the module catalog and installed modules")
    cmds = mods.add_subparsers(dest="modules_command", required=True)
    s = cmds.add_parser("search", help="search the cached catalog index")
    s.add_argument("text", nargs="*", default=[])
    s.add_argument("--category", default=None)
    i = cmds.add_parser("info", help="details of a catalog module")
    i.add_argument("name")
    cmds.add_parser("list", help="installed modules and available updates")
    cmds.add_parser("refresh", help="download and verify the index now")
    ins = cmds.add_parser("install", help="install a module through the installer")
    ins.add_argument("name", help="name[==version], or the path of a wheel file (root)")
    ins.add_argument("--from-pypi", action="store_true", help="unlisted, from PyPI (root)")
    ins.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    upd = cmds.add_parser("update", help="update one module, or all with updates")
    upd.add_argument("name", nargs="?")
    upd.add_argument("--yes", action="store_true")
    rm = cmds.add_parser(
        "remove", help="remove a module through the installer; its instances are disabled"
    )
    rm.add_argument("name", help="the package to remove")
    rm.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    rm.add_argument("--admin-socket", type=Path, default=None, help="the engine's admin socket")
    rp = cmds.add_parser("restore-previous", help="go back to the previous venv")
    rp.add_argument("--yes", action="store_true")
    for p in (s, i, cmds.choices["list"], ins, upd):
        p.add_argument("--state-dir", type=Path, default=None, help="the engine's state directory")
    for p in cmds.choices.values():
        p.add_argument("--socket", type=Path, default=DEFAULT_SOCKET, help="the installer socket")


def _state_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "state_dir", None):
        return Path(args.state_dir)
    if os.environ.get("STATE_DIRECTORY"):
        return resolve_state_dir()
    return ENGINE_STATE if ENGINE_STATE.is_dir() else resolve_state_dir()


def _cached(args: argparse.Namespace, out: TextIO) -> CachedCatalog | None:
    try:
        cached = read_cached(_state_dir(args))
    except CatalogError as exc:
        out.write(f"No catalog index ({exc}); run: mm3 modules refresh\n")
        return None
    if cached.stale:
        out.write(
            f"Note: the cached index (serial {cached.meta.serial}, fetched "
            f"{cached.meta.fetched_at:%Y-%m-%d %H:%M}) is stale; run: mm3 modules refresh\n"
        )
    return cached


def _summary(entry: Entry) -> str:
    release = newest_compatible(entry)
    version = release.version if release else f"no release for Module API {MODULE_API_VERSION}"
    return f"{entry.pypi:32} {entry.badge:9} {version:12} {entry.text()}"


def _search(args: argparse.Namespace, out: TextIO) -> int:
    cached = _cached(args, out)
    if cached is None:
        return EXIT_TOOL
    found = search(cached.index, " ".join(args.text), args.category)
    for entry in found:
        if newest_compatible(entry) is not None:
            out.write(_summary(entry) + "\n")
    return EXIT_OK


def _grants_lines(entry: Entry) -> list[str]:
    release = newest_compatible(entry)
    if release is None:
        return []
    lines = [f"  capabilities: {', '.join(release.capabilities) or 'none'}"]
    for cap, scopes in sorted(release.grants.items()):
        lines.append(f"  grants {cap}: {scopes}")
    return lines


def _info(args: argparse.Namespace, out: TextIO) -> int:
    cached = _cached(args, out)
    if cached is None:
        return EXIT_TOOL
    entry = find_entry(cached.index, args.name)
    if entry is None:
        out.write(f"{args.name} is not in the catalog index\n")
        return EXIT_FAILED
    out.write(
        f"{entry.pypi} ({entry.badge}, {entry.category or 'uncategorized'}, "
        f"{entry.license or 'license unknown'})\n{entry.text()}\nsource: {entry.repository}\n"
    )
    out.write("versions:\n")
    for release in sorted(entry.releases, key=lambda r: Version(r.version), reverse=True):
        mark = "" if compatible(release) else f"  (needs Module API {release.api})"
        report = f"  check {release.check.result}"
        if release.check.report_url:
            report += f" {release.check.report_url}"
        out.write(f"  {release.version}{mark}{report}\n")
    out.write("\n".join(_grants_lines(entry)) + "\n")
    return EXIT_OK


def _installed(client: InstallerClient) -> list[dict[str, str]] | None:
    try:
        reply = client.request("GET", "/v1/status")
    except InstallerUnavailable:
        return None
    if reply.status != 200 or not isinstance(reply.body, dict):
        return None
    rows = reply.body.get("installed")
    return rows if isinstance(rows, list) else None


def _local_installed() -> list[dict[str, str]]:
    rows = []
    for dist in importlib.metadata.distributions():
        if any(e.group == "mm3.modules" for e in dist.entry_points):
            rows.append({"name": dist.metadata["Name"], "version": dist.version, "origin": "?"})
    return sorted(rows, key=lambda r: r["name"].lower())


def _list(args: argparse.Namespace, out: TextIO) -> int:
    client = InstallerClient(args.socket)
    rows = _installed(client)
    if rows is None:
        out.write("The installer does not answer; listing this venv (origin unknown).\n")
        rows = _local_installed()
    cached = None
    try:
        cached = read_cached(_state_dir(args))
    except CatalogError:
        out.write("No catalog index: updates are not shown; run: mm3 modules refresh\n")
    for row in rows:
        update = ""
        if cached is not None:
            entry = find_entry(cached.index, row["name"])
            newest = newest_compatible(entry) if entry else None
            if newest and Version(newest.version) > Version(row["version"]):
                update = f"  update: {newest.version}"
        origin = row.get("origin", "?")
        label = " (unlisted)" if origin in ("pypi", "local") else ""
        out.write(f"{row['name']:32} {row['version']:12} {origin}{label}{update}\n")
    if cached is not None and cached.stale:
        out.write("Note: the cached index is stale; run: mm3 modules refresh\n")
    return EXIT_OK


def _refresh(args: argparse.Namespace, out: TextIO) -> int:
    reply = InstallerClient(args.socket).request("POST", "/v1/catalog/refresh")
    if reply.status == 200:
        cat = reply.body.get("catalog", {})
        out.write(f"Index serial {cat.get('serial')}, expires {cat.get('expires')}\n")
        return EXIT_OK
    out.write(f"Refresh failed: {reply.error}\n")
    return EXIT_FAILED


def _confirm(args: argparse.Namespace, ask: Ask, text: str) -> bool:
    if getattr(args, "yes", False):
        return True
    return ask(f"{text} [y/N] ").strip().lower() in ("y", "yes")


def _secrets(secret: Ask) -> dict[str, str]:
    passphrase = secret("Install passphrase: ")
    totp = secret("TOTP code (empty if TOTP is off): ").strip()
    body = {"passphrase": passphrase}
    if totp:
        body["totp"] = totp
    return body


def _follow(client: InstallerClient, op_id: str, out: TextIO) -> int:
    for step in client.events(op_id):
        out.write(f"  {step.get('state', ''):11} {step.get('detail', '')}\n")
    reply = client.request("GET", f"/v1/operations/{op_id}")
    op: dict[str, Any] = reply.body if isinstance(reply.body, dict) else {}
    state = op.get("state")
    if state == "done":
        out.write(f"Done: {op.get('name') or 'venv'} {op.get('from') or '-'} -> {op.get('to')}\n")
        return EXIT_OK
    out.write(f"{state}: {op.get('error')}\n")
    return EXIT_FAILED


def _send(client: InstallerClient, body: dict[str, Any], out: TextIO) -> int:
    reply = client.request("POST", "/v1/operations", body)
    if reply.status != 202:
        out.write(f"Refused ({reply.status}): {reply.error}\n")
        return EXIT_FAILED
    out.write(f"Operation {reply.body['id']}:\n")
    return _follow(client, str(reply.body["id"]), out)


def _install(args: argparse.Namespace, out: TextIO, ask: Ask, secret: Ask) -> int:
    client = InstallerClient(args.socket)
    target = str(args.name)
    body: dict[str, Any] = {"action": "install", "client": "cli"}
    if target.endswith(".whl"):
        path = Path(target).resolve()
        name = path.name.split("-")[0].replace("_", "-")
        body |= {"name": name, "source": "local", "path": str(path)}
        if not _confirm(args, ask, f"Install the unlisted local wheel {path.name}?"):
            return EXIT_FAILED
        return _send(client, body, out)
    name, _, version = target.partition("==")
    body["name"] = name
    if version:
        body["version"] = version
    if args.from_pypi:
        body["source"] = "pypi"
        if not _confirm(args, ask, f"Install {target} from PyPI, unlisted and unverified?"):
            return EXIT_FAILED
        return _send(client, body, out)
    cached = _cached(args, out)
    entry = find_entry(cached.index, name) if cached else None
    if cached is not None and entry is None:
        out.write(
            f"{name} is not in the catalog index (unlisted, as root: "
            f"sudo /opt/mm3/installer/bin/mm3 modules install {name} --from-pypi)\n"
        )
        return EXIT_FAILED
    if entry is not None:
        release = entry.release(version) if version else newest_compatible(entry)
        if release is None or not compatible(release):
            out.write(f"No release of {name} for Module API {MODULE_API_VERSION}\n")
            return EXIT_FAILED
        out.write(f"{entry.pypi} {release.version} ({entry.badge}, check {release.check.result})\n")
        out.write("\n".join(_grants_lines(entry)) + "\n")
        out.write("Granting scopes to instances stays a separate configuration step.\n")
    if not _confirm(args, ask, "Install?"):
        return EXIT_FAILED
    return _send(client, body | _secrets(secret), out)


def _update(args: argparse.Namespace, out: TextIO, ask: Ask, secret: Ask) -> int:
    client = InstallerClient(args.socket)
    names = [args.name] if args.name else []
    if not names:
        rows = _installed(client) or []
        cached = _cached(args, out)
        for row in rows:
            entry = find_entry(cached.index, row["name"]) if cached else None
            newest = newest_compatible(entry) if entry else None
            if newest and Version(newest.version) > Version(row["version"]):
                names.append(row["name"])
        if not names:
            out.write("No updates.\n")
            return EXIT_OK
    if not _confirm(args, ask, f"Update {', '.join(names)}?"):
        return EXIT_FAILED
    auth = _secrets(secret)
    code = EXIT_OK
    for name in names:
        result = _send(client, {"action": "update", "name": name, "client": "cli"} | auth, out)
        code = max(code, result)
        if len(names) > 1 and "totp" in auth:
            auth = _secrets(secret)  # a TOTP code is good for one operation
    return code


def _engine(
    admin: SocketClient,
    method: str,
    target: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """Send a request to the engine's admin socket and decode the JSON answer.

    Raises:
        OSError: The socket does not answer.
    """
    status, raw = admin.request(method, target, body, headers)
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _why(data: Any) -> str:
    """The ``detail`` of a problem answer of the engine (CORE-298), or a placeholder."""
    if isinstance(data, dict) and isinstance(data.get("detail"), str):
        return str(data["detail"])
    return "no reason given"


def _remove(args: argparse.Namespace, out: TextIO, ask: Ask, secret: Ask) -> int:
    """``mm3 modules remove <name>``: disable the instances, then remove (CORE-358)."""
    client = InstallerClient(args.socket)
    admin = SocketClient(args.admin_socket or runtime_dir() / "admin.sock")
    name = str(args.name)
    key = canonicalize_name(name)
    try:
        status, modules = _engine(admin, "GET", "/api/v1/installed-modules")
    except OSError as exc:
        out.write(
            f"The engine does not answer on {admin.path} ({exc}). Removing a module first "
            "disables its instances through the running engine; start it "
            "(systemctl start mm3.service) and run this again. Nothing was changed.\n"
        )
        return EXIT_TOOL
    if status != 200 or not isinstance(modules, list):
        out.write(f"The engine refused the list of modules ({status}: {_why(modules)}).\n")
        return EXIT_TOOL
    reply = client.request("GET", "/v1/status")
    state: dict[str, Any] = (
        reply.body if reply.status == 200 and isinstance(reply.body, dict) else {}
    )
    if not any(canonicalize_name(str(r.get("name"))) == key for r in state.get("installed") or []):
        out.write(f"{name} is not installed\n")
        return EXIT_FAILED
    if state.get("busy") or state.get("locked_until"):
        why = (
            "is locked until " + str(state["locked_until"])
            if state.get("locked_until")
            else "is busy"
        )
        out.write(f"The installer {why}; nothing was changed.\n")
        return EXIT_FAILED
    names = [
        str(m["name"])
        for m in modules
        if isinstance(m, dict)
        and isinstance(m.get("distribution"), str)
        and canonicalize_name(m["distribution"]) == key
    ]
    ops: list[dict[str, Any]] = []
    ids: list[str] = []
    etag = ""
    if names:
        status, config = _engine(admin, "GET", "/api/v1/config")
        if status != 200 or not isinstance(config, dict):
            out.write(f"The engine refused the configuration ({status}: {_why(config)}).\n")
            return EXIT_TOOL
        etag = str(config["etag"])
        main = next(f["path"] for f in config["files"] if f["role"] == "main")
        status, draft = _engine(
            admin, "POST", "/api/v1/config/patch", {"base": etag, "files": [], "ops": []}
        )
        if status != 200 or not isinstance(draft, dict):
            out.write(f"The engine refused the configuration ({status}: {_why(draft)}).\n")
            return EXIT_TOOL
        ops, ids = disable_ops(draft["documents"], main, names)
    if ids:
        out.write(
            f"{len(ids)} enabled instance(s) will be disabled, not deleted: {', '.join(ids)}\n"
        )
    if not _confirm(args, ask, f"Remove {name} and restart the engine?"):
        return EXIT_FAILED
    auth = _secrets(secret)
    if ops:
        status, draft = _engine(
            admin, "POST", "/api/v1/config/patch", {"base": etag, "files": [], "ops": ops}
        )
        if status != 200 or not isinstance(draft, dict):
            out.write(f"The engine refused the change ({status}: {_why(draft)}).\n")
            return EXIT_FAILED
        files = [{"path": f["path"], "content": f["content"]} for f in draft["files"]]
        status, saved = _engine(
            admin, "PUT", "/api/v1/config", {"base": etag, "files": files}, {"If-Match": etag}
        )
        if status != 200 or not isinstance(saved, dict):
            out.write(
                f"The instances were not disabled ({status}: {_why(saved)}); nothing was removed.\n"
            )
            return EXIT_FAILED
        out.write(
            f"Saved configuration version {saved.get('version')} with the instances disabled "
            "(mm3 config undo restores them).\n"
        )
    return _send(client, {"action": "remove", "name": name, "client": "cli"} | auth, out)


def _restore(args: argparse.Namespace, out: TextIO, ask: Ask, secret: Ask) -> int:
    if not _confirm(args, ask, "Switch back to the previous venv and restart the engine?"):
        return EXIT_FAILED
    body = {"action": "restore_previous", "client": "cli"} | _secrets(secret)
    return _send(InstallerClient(args.socket), body, out)


def run_modules(
    args: argparse.Namespace,
    *,
    out: TextIO | None = None,
    ask: Ask | None = None,
    secret: Ask | None = None,
) -> int:
    """Run ``mm3 modules …``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.
        ask: Confirmation prompt; ``input`` by default.
        secret: Secret prompt; ``getpass`` by default.

    Returns:
        0 done, 1 refused or failed, 2 no index or the installer is unreachable.
    """
    stream = out or sys.stdout
    ask = ask or input
    secret = secret or getpass.getpass
    cmd = args.modules_command
    try:
        if cmd == "search":
            return _search(args, stream)
        if cmd == "info":
            return _info(args, stream)
        if cmd == "list":
            return _list(args, stream)
        if cmd == "refresh":
            return _refresh(args, stream)
        if cmd == "install":
            return _install(args, stream, ask, secret)
        if cmd == "update":
            return _update(args, stream, ask, secret)
        if cmd == "remove":
            return _remove(args, stream, ask, secret)
        return _restore(args, stream, ask, secret)
    except InstallerUnavailable as exc:
        stream.write(f"The installer does not answer ({exc}). Is mm3-installer.service running?\n")
        return EXIT_TOOL
