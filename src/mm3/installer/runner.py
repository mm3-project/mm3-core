"""Child processes without the installer's rights.

Package code (``python3 -m venv``, ``pip``, the import test, ``mm3 module
check``) never runs with the installer's rights (CORE-367): every child is
started through ``setpriv`` as ``mm3-build`` with no supplementary groups,
every capability set cleared and no new privileges, with a minimal environment
and a private home under the installer's private ``/tmp``. ``mm3 diag check``
runs the engine's own code, so it runs as the engine's user ``mm3`` (the only
user besides the installer that may open the admin socket, CORE-311, CORE-357),
also without capabilities. After each build the installer ends every process
``mm3-build`` may have left behind.

Implements:
    CORE-367: Core spec 0.35, §10.7 Installer service.
    CORE-357: Core spec 0.33, §10.7 Installer service.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mm3.installer.paths import BUILD_USER, ENGINE_USER

__all__ = ["SETPRIV", "Completed", "DirectRunner", "Runner", "SetprivRunner", "drop_argv"]

SETPRIV = "/usr/bin/setpriv"
_PASS_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
_OUTPUT_TAIL = 16 * 1024


@dataclass(frozen=True)
class Completed:
    """A finished child: exit code and the tail of its combined output."""

    returncode: int
    output: str


class Runner(Protocol):
    """Starts children (a recording fake in tests)."""

    def run(
        self,
        argv: Sequence[str],
        *,
        user: str,
        home: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 900.0,
    ) -> Completed:
        """Run ``argv`` as ``user`` and wait for it."""

    def reap(self, user: str) -> None:
        """End every process left running as ``user``."""


def drop_argv(user: str, argv: Sequence[str]) -> list[str]:
    """The ``setpriv`` command line that runs ``argv`` as ``user`` without rights.

    ``mm3-build`` gets no groups at all; ``mm3`` keeps its own groups (``mm3``
    gives it the admin socket). Capabilities are cleared from every set and
    the bounding set, and ``no_new_privs`` is set, so the child can never
    regain any.
    """
    groups = ["--clear-groups"] if user == BUILD_USER else ["--init-groups"]
    return [
        SETPRIV,
        f"--reuid={user}",
        f"--regid={user}",
        *groups,
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        "--",
        *argv,
    ]


def child_env(home: Path, extra: Mapping[str, str] | None) -> dict[str, str]:
    """A minimal environment: no secrets of the installer reach the child."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update({k: os.environ[k] for k in _PASS_ENV if k in os.environ})
    env.update(extra or {})
    return env


def _tail(text: str) -> str:
    return text if len(text) <= _OUTPUT_TAIL else "…" + text[-_OUTPUT_TAIL:]


class SetprivRunner:
    """Runs children through ``setpriv`` (production)."""

    def run(
        self,
        argv: Sequence[str],
        *,
        user: str,
        home: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 900.0,
    ) -> Completed:
        """Run ``argv`` as ``user`` without capabilities.

        Returns:
            The exit code (124 on a timeout) and the output tail.
        """
        if user not in (BUILD_USER, ENGINE_USER):
            raise ValueError(f"children run only as {BUILD_USER} or {ENGINE_USER}")
        try:
            done = subprocess.run(  # noqa: S603 - fixed setpriv prefix, no shell
                drop_argv(user, argv),
                env=child_env(home, env),
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output if isinstance(exc.output, str) else ""
            return Completed(124, _tail(out + f"\ntimed out after {timeout:.0f} s"))
        return Completed(done.returncode, _tail(done.stdout or ""))

    def reap(self, user: str) -> None:
        """Kill every process of ``user`` (``kill -KILL -1`` as that user)."""
        if user != BUILD_USER:
            return
        subprocess.run(  # noqa: S603 - fixed argv
            drop_argv(user, ["/bin/kill", "-KILL", "-1"]),
            env={"PATH": "/usr/bin:/bin"},
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


class DirectRunner:
    """Runs children as the current user, without ``setpriv`` (tests only)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        user: str,
        home: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 900.0,
    ) -> Completed:
        """Run ``argv`` directly and record ``(user, argv)``."""
        self.calls.append((user, list(argv)))
        done = subprocess.run(  # noqa: S603 - tests only
            list(argv),
            env=child_env(home, env),
            cwd=home,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return Completed(done.returncode, _tail(done.stdout or ""))

    def reap(self, user: str) -> None:
        """Nothing to do."""
