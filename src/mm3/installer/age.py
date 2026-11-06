"""The adapter for age: encryption of backup archives (CORE-161, CORE-364).

``age`` and ``age-keygen`` (Debian package ``age``) run as subprocesses, only in
the installer service. A public key encrypts without a secret; a passphrase
and a private key decrypt. age asks for a passphrase only on a terminal, so
passphrase mode runs the binary on a pseudo-terminal of its own: the
passphrase never appears on a command line or in the environment and is
written to the terminal only when age asks for it. An identity (private key)
is written to a 0600 file in the operation's private directory for the one
command that needs it and removed afterwards (CORE-364).

Implements:
    CORE-161: Core spec 0.37, §10.8 Backup and restore.
    CORE-364: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Literal, Protocol

__all__ = [
    "AGE",
    "AGE_KEYGEN",
    "Age",
    "AgeError",
    "AgeTool",
    "Identity",
    "encryption_kind",
    "is_identity",
    "valid_recipient",
]

AGE = "/usr/bin/age"
AGE_KEYGEN = "/usr/bin/age-keygen"
HEADER_BYTES = 256
PROMPT_TIMEOUT_S = 120.0
_RECIPIENT = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
_IDENTITY = re.compile(r"^AGE-SECRET-KEY-1[0-9A-Z]{58}$", re.MULTILINE)
_PROMPT = re.compile(rb"(?:Enter|Confirm) passphrase[^:\r\n]*: ")
_ERROR_TAIL = 400

Kind = Literal["none", "key", "passphrase"]


class AgeError(Exception):
    """age failed; the message is safe to show (it never holds a secret)."""


class Identity:
    """A generated age identity: the text of the key file and its public key."""

    def __init__(self, public: str, text: str) -> None:
        self.public = public
        self.text = text

    def __repr__(self) -> str:
        return f"Identity(public={self.public!r})"


class AgeTool(Protocol):
    """What the backup code needs from age (a fake in tests)."""

    def generate_identity(self) -> Identity:
        """Make a new X25519 identity."""

    def encrypt_to(self, src: Path, dest: Path, recipient: str) -> None:
        """Encrypt ``src`` to a public key."""

    def encrypt_passphrase(self, src: Path, dest: Path, passphrase: str) -> None:
        """Encrypt ``src`` with a passphrase."""

    def decrypt(self, src: Path, dest: Path, secret: str, work: Path) -> None:
        """Decrypt with an identity (key file text) or an archive passphrase."""


def valid_recipient(value: str) -> bool:
    """Whether ``value`` is an age X25519 public key (``age1…``)."""
    return bool(_RECIPIENT.match(value))


def is_identity(secret: str) -> bool:
    """Whether ``secret`` holds an age identity rather than a passphrase."""
    return bool(_IDENTITY.search(secret))


def encryption_kind(path: Path) -> Kind:
    """How an archive is encrypted, from its age header (CORE-362).

    Args:
        path: The archive.

    Returns:
        ``none`` for a plain archive, ``passphrase`` for an scrypt recipient,
        ``key`` for any other age file.
    """
    with path.open("rb") as fh:
        head = fh.read(HEADER_BYTES)
    if not head.startswith(b"age-encryption.org/v1\n"):
        return "none"
    first = head.split(b"\n", 2)[1] if head.count(b"\n") >= 2 else b""
    return "passphrase" if first.startswith(b"-> scrypt") else "key"


def _tail(text: bytes) -> str:
    clean = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", text).replace(b"\r", b"")
    lines = [
        line
        for line in clean.decode("utf-8", errors="replace").splitlines()
        if line.startswith("age: error") or line.startswith("age-keygen")
    ]
    return " ".join(lines)[-_ERROR_TAIL:] or "age failed"


class Age:
    """The production adapter: ``age`` and ``age-keygen`` as subprocesses.

    Args:
        age: Path of ``age``.
        keygen: Path of ``age-keygen``.
        timeout: Seconds a command may run (archives can be large).
    """

    def __init__(self, age: str = AGE, keygen: str = AGE_KEYGEN, timeout: float = 3600.0) -> None:
        self._age = age
        self._keygen = keygen
        self._timeout = timeout

    # --- plain commands -----------------------------------------------------------

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(  # noqa: S603 - fixed binaries, no shell
                argv,
                env={"PATH": "/usr/bin:/bin"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AgeError(f"{argv[0]} is not installed (Debian package age)") from exc
        except subprocess.TimeoutExpired as exc:
            raise AgeError("age timed out") from exc

    def generate_identity(self) -> Identity:
        """Make a new X25519 identity with ``age-keygen``.

        Returns:
            The key file text and its public key.

        Raises:
            AgeError: age-keygen failed or answered something unexpected.
        """
        done = self._run([self._keygen])
        if done.returncode != 0:
            raise AgeError(_tail(done.stderr))
        text = done.stdout.decode("utf-8")
        match = re.search(r"^# public key: (age1\S+)$", text, re.MULTILINE)
        if match is None or not _IDENTITY.search(text):
            raise AgeError("age-keygen gave no key")
        return Identity(match.group(1), text)

    def encrypt_to(self, src: Path, dest: Path, recipient: str) -> None:
        """Encrypt ``src`` to the public key ``recipient`` (CORE-161).

        Raises:
            AgeError: Not a public key, or age failed.
        """
        if not valid_recipient(recipient):
            raise AgeError("encrypt_to is not an age public key (age1…)")
        done = self._run([self._age, "-r", recipient, "-o", str(dest), str(src)])
        if done.returncode != 0:
            dest.unlink(missing_ok=True)
            raise AgeError(_tail(done.stderr))

    # --- passphrase mode (a terminal of its own) ------------------------------------

    def _on_terminal(self, argv: list[str], answers: list[str]) -> tuple[int, bytes]:
        master, slave = os.openpty()
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed binary, no shell
                argv,
                env={"PATH": "/usr/bin:/bin"},
                stdin=slave,
                stdout=subprocess.DEVNULL,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            os.close(master)
            raise AgeError(f"{argv[0]} is not installed (Debian package age)") from exc
        finally:
            os.close(slave)
        out = b""
        sent = 0
        deadline = time.monotonic() + self._timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    out += data
                    asked = len(_PROMPT.findall(out))
                    while sent < min(asked, len(answers)):
                        os.write(master, answers[sent].encode() + b"\n")
                        sent += 1
                elif proc.poll() is not None:
                    break
            else:
                proc.kill()
                proc.wait()
                raise AgeError("age timed out")
        finally:
            os.close(master)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AgeError("age did not finish") from None
        return code, out

    @staticmethod
    def _check_passphrase(passphrase: str) -> None:
        if not passphrase:
            raise AgeError("the archive passphrase is empty")
        if any(ord(c) < 32 or ord(c) == 127 for c in passphrase):
            raise AgeError("the archive passphrase holds a control character")

    def encrypt_passphrase(self, src: Path, dest: Path, passphrase: str) -> None:
        """Encrypt ``src`` with ``passphrase`` (manual backups, CORE-161).

        Raises:
            AgeError: The passphrase is empty or age failed.
        """
        self._check_passphrase(passphrase)
        code, out = self._on_terminal(
            [self._age, "-p", "-o", str(dest), str(src)], [passphrase, passphrase]
        )
        if code != 0:
            dest.unlink(missing_ok=True)
            raise AgeError(_tail(out))

    def decrypt(self, src: Path, dest: Path, secret: str, work: Path) -> None:
        """Decrypt ``src`` with an identity or a passphrase (CORE-364).

        ``secret`` is an identity (key file text) when it holds an
        ``AGE-SECRET-KEY-1…`` line, else the archive passphrase. An identity is
        written to a 0600 file in ``work`` for this command only.

        Args:
            src: The encrypted archive.
            dest: Where the plain archive goes.
            secret: The identity or the passphrase.
            work: The operation's private directory.

        Raises:
            AgeError: Wrong key or passphrase, or age failed.
        """
        if is_identity(secret):
            key = work / f"identity-{os.getpid()}-{time.monotonic_ns()}.key"
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(secret if secret.endswith("\n") else secret + "\n")
                done = self._run([self._age, "-d", "-i", str(key), "-o", str(dest), str(src)])
            finally:
                with contextlib.suppress(OSError):
                    key.unlink()
            if done.returncode != 0:
                dest.unlink(missing_ok=True)
                raise AgeError(_tail(done.stderr))
            return
        self._check_passphrase(secret)
        code, out = self._on_terminal([self._age, "-d", "-o", str(dest), str(src)], [secret])
        if code != 0:
            dest.unlink(missing_ok=True)
            raise AgeError(_tail(out))
