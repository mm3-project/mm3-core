"""``mm3 backup``: create, list, verify, restore and keygen (CORE-361).

Every command goes through the installer socket; the CLI never reads or writes
a backup itself. Creating, listing, verifying and previewing need no
re-authentication. A restore shows what it will change part by part, asks for
confirmation and re-authenticates with the install passphrase (and the TOTP
code when enabled), except from root, which the installer recognises by
``SO_PEERCRED`` (CORE-162, CORE-361). A backup by file path is accepted from
root only; others name an entry of the local list or, with ``--remote``, of
the remote.

Implements:
    CORE-361: Core spec 0.37, §10.8 Backup and restore.
    CORE-364: Core spec 0.37, §10.8 Backup and restore.
    CORE-162: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from mm3.installer.client import DEFAULT_SOCKET, InstallerClient, InstallerUnavailable

__all__ = ["add_backup_parser", "run_backup"]

EXIT_OK, EXIT_FAILED, EXIT_TOOL = 0, 1, 2
Ask = Callable[[str], str]
_PARTS = "config,data,modules,secrets"


def _parts(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def add_backup_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 backup …``.

    Args:
        sub: The top-level subparsers.
    """
    bk = sub.add_parser("backup", help="backups of the mirror")
    cmds = bk.add_subparsers(dest="backup_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)

    c = cmds.add_parser("create", help="make a backup now")
    common(c)
    c.add_argument("--parts", type=_parts, default=None, help=f"comma separated: {_PARTS}")
    c.add_argument(
        "--encrypt", action="store_true", help="encrypt with an archive passphrase (asked)"
    )
    c.add_argument("--recipient", default=None, help="encrypt to this age public key")
    c.add_argument("--configured-key", action="store_true", help="encrypt to backup.encrypt_to")
    c.add_argument(
        "--pre", action="store_true", help="a pre-operation backup, as before an upgrade (root)"
    )
    ls = cmds.add_parser("list", help="list backups")
    common(ls)
    ls.add_argument("--remote", action="store_true", help="the copies on backup.remote")
    ls.add_argument("--json", action="store_true")
    v = cmds.add_parser("verify", help="check an archive and its checksums")
    common(v)
    v.add_argument("file", help="a name from the list, or (root) a file path")
    v.add_argument("--remote", action="store_true")
    v.add_argument("--key-file", type=Path, default=None, help="age identity file")
    r = cmds.add_parser("restore", help="restore parts of a backup")
    common(r)
    r.add_argument("file", help="a name from the list, or (root) a file path")
    r.add_argument("--remote", action="store_true")
    r.add_argument("--parts", type=_parts, default=None, help=f"comma separated: {_PARTS}")
    r.add_argument("--instance", default=None, help="only this module instance (config, data)")
    r.add_argument("--key-file", type=Path, default=None, help="age identity file")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    k = cmds.add_parser("keygen", help="make an age key pair for encrypt_to")
    common(k)
    k.add_argument("--keep", action="store_true", help="keep the identity on the mirror (0600)")
    k.add_argument("--out", type=Path, default=None, help="write the identity to this file (0600)")


def _source(args: argparse.Namespace) -> dict[str, Any]:
    target = str(args.file)
    if not args.remote and os.sep in target:
        return {"path": str(Path(target).resolve())}
    return {"name": Path(target).name, "remote": bool(args.remote)}


def _key(args: argparse.Namespace) -> str | None:
    path = getattr(args, "key_file", None)
    return path.read_text(encoding="utf-8") if path else None


def _follow(client: InstallerClient, op_id: str, out: TextIO) -> int:
    for step in client.events(op_id):
        out.write(f"  {step.get('state', ''):11} {step.get('detail', '')}\n")
    reply = client.request("GET", f"/v1/operations/{op_id}")
    op: dict[str, Any] = reply.body if isinstance(reply.body, dict) else {}
    state = op.get("state")
    if state == "done":
        out.write(f"Done{': ' + str(op['to']) if op.get('to') else ''}\n")
        return EXIT_OK
    out.write(f"{state}: {op.get('error')}\n")
    return EXIT_FAILED


def _ask_key(
    client: InstallerClient, path: str, body: dict[str, Any], secret: Ask, out: TextIO
) -> Any:
    """Send a request; when the archive turns out to be encrypted, ask for its key and retry."""
    reply = client.request("POST", path, body)
    if reply.status == 400 and "encrypted" in reply.error and "key" not in body:
        body["key"] = secret("Backup age key or passphrase: ")
        reply = client.request("POST", path, body)
    return reply


def _create(args: argparse.Namespace, out: TextIO, secret: Ask) -> int:
    client = InstallerClient(args.socket)
    body: dict[str, Any] = {"client": "cli"}
    if args.pre:
        body["pre"] = True
    if args.parts:
        body["parts"] = args.parts
    if args.recipient:
        body["recipient"] = args.recipient
    elif args.configured_key:
        body["use_configured_key"] = True
    elif args.encrypt:
        first = secret("Archive passphrase: ")
        if first != secret("Repeat it: "):
            out.write("The two passphrases differ.\n")
            return EXIT_FAILED
        body["archive_passphrase"] = first
    reply = client.request("POST", "/v1/backups", body)
    if reply.status != 202:
        out.write(f"Refused ({reply.status}): {reply.error}\n")
        return EXIT_FAILED
    out.write(f"Operation {reply.body['id']}:\n")
    return _follow(client, str(reply.body["id"]), out)


def _list(args: argparse.Namespace, out: TextIO) -> int:
    client = InstallerClient(args.socket)
    reply = client.request("GET", "/v1/backups/remote" if args.remote else "/v1/backups")
    if reply.status != 200:
        out.write(f"Refused ({reply.status}): {reply.error}\n")
        return EXIT_FAILED
    items = reply.body["backups"]
    if args.json:
        out.write(json.dumps(reply.body, indent=2) + "\n")
        return EXIT_OK
    for item in items:
        flags = "encrypted" if item.get("encrypted") else "plain"
        out.write(
            f"{item['name']}  {item.get('kind', '?'):9} {flags:9} "
            f"{item.get('size', 0):>12} B  {','.join(item.get('parts', []))}\n"
        )
    if not items:
        out.write("No backups.\n")
    status = reply.body.get("status") or {}
    for key in ("scheduled", "remote"):
        last = status.get(key)
        if last:
            outcome = "ok" if last.get("ok") else f"failed: {last.get('error')}"
            out.write(f"Last {key} run {last.get('at')}: {outcome}\n")
    return EXIT_OK


def _verify(args: argparse.Namespace, out: TextIO, secret: Ask) -> int:
    client = InstallerClient(args.socket)
    body = _source(args)
    key = _key(args)
    if key:
        body["key"] = key
    reply = _ask_key(client, "/v1/backups/verify", body, secret, out)
    if reply.status != 200:
        out.write(f"Not valid ({reply.status}): {reply.error}\n")
        return EXIT_FAILED
    info = reply.body
    out.write(
        f"OK: {info['hostname']} {info['created']} engine {info['engine_version']}, "
        f"parts {','.join(info['parts'])}, {info['files']} files"
        f"{', encrypted' if info['encrypted'] else ''}\n"
    )
    for omitted in info.get("omitted", []):
        out.write(f"Left out: {omitted} (the backup was not encrypted)\n")
    for warning in info.get("warnings", []):
        out.write(f"Warning: {warning}\n")
    return EXIT_OK


def _restore(args: argparse.Namespace, out: TextIO, ask: Ask, secret: Ask) -> int:
    client = InstallerClient(args.socket)
    body = _source(args)
    if args.parts:
        body["parts"] = args.parts
    if args.instance:
        body["instance"] = args.instance
    key = _key(args)
    if key:
        body["key"] = key
    preview = _ask_key(client, "/v1/backups/preview", body, secret, out)
    if preview.status != 200:
        out.write(f"Refused ({preview.status}): {preview.error}\n")
        return EXIT_FAILED
    out.write("A restore will change:\n")
    for line in preview.body.get("lines", []):
        out.write(f"  {line}\n")
    out.write("A backup of the present state is taken first and put back if the restore fails.\n")
    if not args.yes and ask("Restore now? [y/N] ").strip().lower() not in ("y", "yes"):
        out.write("Cancelled.\n")
        return EXIT_FAILED
    if preview.body.get("encrypted") and "key" not in body:
        body["key"] = secret("Backup age key or passphrase: ")
    if os.geteuid() != 0:
        body["passphrase"] = secret("Install passphrase: ")
        totp = secret("TOTP code (empty if TOTP is off): ").strip()
        if totp:
            body["totp"] = totp
    body["client"] = "cli"
    reply = client.request("POST", "/v1/backups/restore", body)
    if reply.status != 202:
        out.write(f"Refused ({reply.status}): {reply.error}\n")
        return EXIT_FAILED
    out.write(f"Operation {reply.body['id']}:\n")
    return _follow(client, str(reply.body["id"]), out)


def _keygen(args: argparse.Namespace, out: TextIO) -> int:
    reply = InstallerClient(args.socket).request("POST", "/v1/backups/keygen", {"keep": args.keep})
    if reply.status != 200:
        out.write(f"Refused ({reply.status}): {reply.error}\n")
        return EXIT_FAILED
    out.write(f"Public key (for backup.encrypt_to): {reply.body['public']}\n")
    identity = str(reply.body["identity"])
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(identity)
        out.write(f"Private key written to {args.out}. Keep it safe: it is not shown again.\n")
    else:
        out.write("Private key (shown only now; keep it safe):\n" + identity)
    if args.keep:
        out.write(
            "The private key is also kept on this mirror (/var/lib/mm3-install/backup.key).\n"
        )
    return EXIT_OK


def run_backup(
    args: argparse.Namespace,
    *,
    out: TextIO | None = None,
    ask: Ask | None = None,
    secret: Ask | None = None,
) -> int:
    """Run ``mm3 backup …``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.
        ask: Confirmation prompt; ``input`` by default.
        secret: Secret prompt; ``getpass`` by default.

    Returns:
        0 done, 1 refused or failed, 2 the installer is unreachable.
    """
    stream = out or sys.stdout
    ask = ask or input
    secret = secret or getpass.getpass
    cmd = args.backup_command
    try:
        if cmd == "create":
            return _create(args, stream, secret)
        if cmd == "list":
            return _list(args, stream)
        if cmd == "verify":
            return _verify(args, stream, secret)
        if cmd == "restore":
            return _restore(args, stream, ask, secret)
        return _keygen(args, stream)
    except InstallerUnavailable as exc:
        stream.write(f"The installer does not answer ({exc}). Is mm3-installer.service running?\n")
        return EXIT_TOOL
