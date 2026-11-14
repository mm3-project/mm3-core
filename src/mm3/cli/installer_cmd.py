"""``mm3 installer``: serve, unit, polkit rule, passphrase, TOTP and status.

``serve`` runs the installer service (``mm3-installer.service`` starts it from
``/opt/mm3/installer/``, CORE-368). ``unit`` and ``polkit`` print the shipped
unit and polkit rule (CORE-357). ``passphrase`` and ``totp`` talk to the
installer socket and need root, which the installer checks with
``SO_PEERCRED``; they are run from the installer's own venv,
``sudo /opt/mm3/installer/bin/mm3 installer …``, so no module code runs as
root (CORE-369).

Implements:
    CORE-357: Core spec 0.42, §10.7 Installer service.
    CORE-369: Core spec 0.37, §10.7 Installer service.
    CORE-132: Core spec 0.33, §10.7 Installer service.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import signal
import sys
from importlib import resources
from pathlib import Path
from typing import TextIO

from mm3.installer.client import DEFAULT_SOCKET, InstallerClient, InstallerUnavailable

__all__ = ["add_installer_parser", "polkit_text", "run_installer", "unit_text"]

EXIT_OK, EXIT_FAILED, EXIT_TOOL = 0, 1, 2


def add_installer_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 installer …``.

    Args:
        sub: The top-level subparsers.
    """
    inst = sub.add_parser("installer", help="the installer service")
    cmds = inst.add_subparsers(dest="installer_command", required=True)
    cmds.add_parser("serve", help="run the installer service (mm3-installer.service)")
    cmds.add_parser("unit", help="print mm3-installer.service")
    cmds.add_parser("polkit", help="print the installer's polkit rule")
    for name, text in (
        ("passphrase", "set the install passphrase (root)"),
        ("status", "show the installer's state"),
    ):
        p = cmds.add_parser(name, help=text)
        p.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    totp = cmds.add_parser("totp", help="enable or disable TOTP codes (root)")
    totp.add_argument("totp_action", choices=["enable", "disable"])
    totp.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)


def unit_text() -> str:
    """Return the shipped ``mm3-installer.service``."""
    return (resources.files("mm3.installer") / "mm3-installer.service").read_text()


def polkit_text() -> str:
    """Return the shipped polkit rule."""
    return (resources.files("mm3.installer") / "mm3-installer.rules").read_text()


def _serve() -> int:
    from mm3.core.logging import configure_logging
    from mm3.installer.app import build_server
    from mm3.installer.paths import Layout

    configure_logging()
    layout = Layout()
    server = build_server(layout)

    async def main() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel if task else lambda: None)
        with contextlib.suppress(asyncio.CancelledError):
            await server.serve(layout.socket)

    asyncio.run(main())
    return EXIT_OK


def run_installer(
    args: argparse.Namespace,
    *,
    out: TextIO | None = None,
    ask: object = None,
) -> int:
    """Run ``mm3 installer …``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.
        ask: Prompt function ``(prompt) -> str`` for secrets; ``getpass`` by default.

    Returns:
        The exit code: 0 done, 1 refused or failed, 2 the installer is unreachable.
    """
    stream = out or sys.stdout
    prompt = ask if callable(ask) else getpass.getpass
    cmd = args.installer_command
    if cmd == "serve":
        return _serve()
    if cmd == "unit":
        stream.write(unit_text())
        return EXIT_OK
    if cmd == "polkit":
        stream.write(polkit_text())
        return EXIT_OK
    client = InstallerClient(args.socket)
    try:
        if cmd == "status":
            reply = client.request("GET", "/v1/status")
            stream.write(json.dumps(reply.body, indent=2) + "\n")
            return EXIT_OK if reply.status == 200 else EXIT_FAILED
        if cmd == "passphrase":
            first = str(prompt("New install passphrase: "))
            if first != str(prompt("Repeat it: ")):
                stream.write("The two passphrases differ.\n")
                return EXIT_FAILED
            reply = client.request("POST", "/v1/auth/passphrase", {"passphrase": first})
            if reply.status == 204:
                stream.write("Install passphrase set.\n")
                return EXIT_OK
            stream.write(f"Refused: {reply.error}\n")
            return EXIT_FAILED
        if args.totp_action == "disable":
            reply = client.request("DELETE", "/v1/auth/totp")
            if reply.status == 204:
                stream.write("TOTP disabled.\n")
                return EXIT_OK
            stream.write(f"Refused: {reply.error}\n")
            return EXIT_FAILED
        reply = client.request("POST", "/v1/auth/totp", {})
        if reply.status != 200:
            stream.write(f"Refused: {reply.error}\n")
            return EXIT_FAILED
        stream.write(
            "Add this secret to your authenticator app; it is shown only now.\n"
            f"  Secret: {reply.body['secret']}\n  URI:    {reply.body['uri']}\n"
        )
        code = str(prompt("Code from the app: "))
        reply = client.request("POST", "/v1/auth/totp", {"code": code})
        if reply.status == 204:
            stream.write("TOTP enabled: operations now need a code.\n")
            return EXIT_OK
        stream.write(f"Refused: {reply.error}\n")
        return EXIT_FAILED
    except InstallerUnavailable as exc:
        stream.write(f"The installer does not answer ({exc}). Is mm3-installer.service running?\n")
        return EXIT_TOOL
