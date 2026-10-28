"""``mm3 token``: create, list and revoke admin and display tokens.

A token is shown once, when it is created. ``--display`` creates a display
token and prints the pairing link ``http://<host>:<port>/pair#<token>`` of
the mirror listener (CORE-254). Revoking a token ends its sessions at once,
also in a running engine, which re-reads the token store.

Implements:
    CORE-62: Core spec 0.12, §10.1 Security.
    CORE-254: Core spec 0.12, §10.1 Security.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from mm3.core.services.storage import resolve_state_dir
from mm3.core.settings import ServerSettings
from mm3.core.web.tokens import TokenError, TokenKind, TokenStore

__all__ = ["add_token_parser", "pair_link", "run_token"]

EXIT_FAILED = 1


def add_token_parser(
    sub: argparse._SubParsersAction,  # type: ignore[type-arg]
    config_args: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Add ``mm3 token`` and its subcommands to the top-level parser.

    Args:
        sub: The top-level subparsers.
        config_args: Adds ``--config`` and ``--env-file`` to a parser.
    """
    tok = sub.add_parser("token", help="create, list and revoke admin and display tokens")
    cmds = tok.add_subparsers(dest="token_command", required=True)
    p = cmds.add_parser("new", help="create a token and show it once")
    config_args(p)
    p.add_argument("name")
    p.add_argument("--display", action="store_true", help="a display token, with a pairing link")
    p = cmds.add_parser("list", help="list tokens")
    p.add_argument("--json", action="store_true")
    p = cmds.add_parser("revoke", help="revoke a token and end its sessions")
    p.add_argument("name")


def pair_link(server: ServerSettings, token: str, *, hostname: str | None = None) -> str:
    """Return the pairing link of a display token (CORE-254).

    Args:
        server: The mirror listener's settings.
        token: The display token.
        hostname: Name used when the listener binds every address; the
            machine's host name by default.

    Returns:
        ``http://<host>:<port>/pair#<token>``.
    """
    host = server.host
    with_brackets = host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and addr.is_unspecified:
        with_brackets = hostname or socket.gethostname()
    elif addr is not None and addr.version == 6:
        with_brackets = f"[{host}]"
    return f"http://{with_brackets}:{server.port}/pair#{token}"


def _server_settings(args: argparse.Namespace, err: TextIO) -> ServerSettings:
    from mm3.cli.main import _discover
    from mm3.core.config import load_config

    result = load_config(args.config, registry=_discover(), env_file=args.env_file)
    if result.config is None:
        print(
            "warning: the configuration is not valid; the link uses the default mirror "
            "listener address",
            file=err,
        )
        return ServerSettings()
    return result.config.engine.server


def run_token(
    args: argparse.Namespace,
    *,
    state_dir: Path | None = None,
    server: ServerSettings | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run an ``mm3 token`` subcommand.

    Args:
        args: Parsed arguments.
        state_dir: Engine state directory; from the environment by default.
        server: Mirror listener settings for the pairing link; read from the
            configuration by default.
        out: Output stream; stdout by default.
        err: Error stream; stderr by default.

    Returns:
        0 when done, 1 when the command failed.

    Implements:
        CORE-62: Core spec 0.12, §10.1 Security.
        CORE-254: Core spec 0.12, §10.1 Security.
    """
    stdout, stderr = out or sys.stdout, err or sys.stderr
    store = TokenStore(state_dir or resolve_state_dir())
    command: str = args.token_command
    try:
        if command == "new":
            kind: TokenKind = "display" if args.display else "admin"
            token = store.new(args.name, kind)
            if args.display:
                print(pair_link(server or _server_settings(args, stderr), token), file=stdout)
            else:
                print(token, file=stdout)
            print("This token is shown only once.", file=stderr)
            return 0
        if command == "list":
            records = [r.public() for r in store.all()]
            if args.json:
                print(json.dumps(records, indent=2), file=stdout)
            else:
                for r in records:
                    last = r["last_used"] or "never used"
                    print(f"{r['name']}\t{r['kind']}\t{r['created']}\t{last}", file=stdout)
            return 0
        rec = store.revoke(args.name)
        print(f"revoked {rec.name}", file=stdout)
        return 0
    except TokenError as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_FAILED
