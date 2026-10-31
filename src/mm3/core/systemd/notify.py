"""systemd service notification and the loop watchdog (CORE-290, CORE-291).

The engine tells systemd its state with datagrams sent to the socket named in
``$NOTIFY_SOCKET`` (``sd_notify``), on the standard library only. Without that
variable every call is a no-op. Sending never blocks the loop and never stops
the engine: a failure is logged at debug level.

The watchdog is one task on the engine loop that sends ``WATCHDOG=1`` every
``$WATCHDOG_USEC / 2`` on the engine clock. It proves only that the event loop
runs: it depends on no module, bus, listener or job.

Implements:
    CORE-290: Core spec 0.20, §7.1 Engine startup and shutdown.
    CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Mapping

from mm3.core.clock import Clock
from mm3.core.logging import get_logger

__all__ = [
    "NOTIFY_SOCKET_ENV",
    "WATCHDOG_PID_ENV",
    "WATCHDOG_USEC_ENV",
    "ServiceNotifier",
    "monotonic_usec",
    "run_watchdog",
    "watchdog_interval",
]

NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
WATCHDOG_USEC_ENV = "WATCHDOG_USEC"
WATCHDOG_PID_ENV = "WATCHDOG_PID"

_log = get_logger(subsystem="systemd")


def monotonic_usec() -> int:
    """Return ``CLOCK_MONOTONIC`` in microseconds, as ``RELOADING=1`` needs it."""
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


class ServiceNotifier:
    """Sends ``sd_notify`` datagrams to systemd.

    Args:
        address: The value of ``$NOTIFY_SOCKET``: an absolute path, or an
            abstract socket starting with ``@``. ``None`` or empty makes every
            call a no-op.

    Implements:
        CORE-290: Core spec 0.20, §7.1 Engine startup and shutdown.
    """

    def __init__(self, address: str | None = None) -> None:
        self._address = self._resolve(address)
        self._sock: socket.socket | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> ServiceNotifier:
        """Build a notifier from ``$NOTIFY_SOCKET``.

        Args:
            environ: The environment; ``os.environ`` by default.

        Returns:
            The notifier (a no-op one when the variable is not set).
        """
        env = os.environ if environ is None else environ
        return cls(env.get(NOTIFY_SOCKET_ENV))

    @staticmethod
    def _resolve(address: str | None) -> str | None:
        if not address:
            return None
        if address.startswith("@"):
            return "\0" + address[1:]
        if address.startswith("/"):
            return address
        _log.debug("sd_notify_address_ignored", address=address)
        return None

    @property
    def enabled(self) -> bool:
        """True when ``$NOTIFY_SOCKET`` named a usable socket."""
        return self._address is not None

    def send(self, *fields: str) -> bool:
        """Send one datagram made of ``fields``, one per line.

        Args:
            fields: ``KEY=value`` assignments.

        Returns:
            True when the datagram was sent; False for a no-op or a failure.
        """
        if self._address is None:
            return False
        data = "\n".join(fields).encode()
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
                self._sock.setblocking(False)
            self._sock.sendto(data, self._address)
        except OSError as exc:  # never stops the engine, never blocks the loop
            _log.debug("sd_notify_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return True

    def ready(self, status: str | None = None) -> bool:
        """Send ``READY=1``, with ``STATUS=`` when a summary is given."""
        if status is None:
            return self.send("READY=1")
        return self.send("READY=1", f"STATUS={_one_line(status)}")

    def status(self, text: str) -> bool:
        """Send ``STATUS=`` with a one-line summary."""
        return self.send(f"STATUS={_one_line(text)}")

    def reloading(self) -> bool:
        """Send ``RELOADING=1`` with ``MONOTONIC_USEC=`` (a reload begins)."""
        return self.send("RELOADING=1", f"MONOTONIC_USEC={monotonic_usec()}")

    def stopping(self) -> bool:
        """Send ``STOPPING=1`` (shutdown begins)."""
        return self.send("STOPPING=1")

    def watchdog(self) -> bool:
        """Send ``WATCHDOG=1``."""
        return self.send("WATCHDOG=1")

    def close(self) -> None:
        """Close the socket; a later send opens it again."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def watchdog_interval(
    environ: Mapping[str, str] | None = None, pid: int | None = None
) -> float | None:
    """Return the seconds between watchdog pings, or ``None`` for no watchdog.

    Args:
        environ: The environment; ``os.environ`` by default.
        pid: The engine's pid; ``os.getpid()`` by default.

    Returns:
        ``$WATCHDOG_USEC / 2`` in seconds, when ``$WATCHDOG_USEC`` is a positive
        number and ``$WATCHDOG_PID`` is absent or equals the engine's pid.

    Implements:
        CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
    """
    env = os.environ if environ is None else environ
    raw = env.get(WATCHDOG_USEC_ENV)
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        _log.debug("watchdog_usec_ignored", value=raw)
        return None
    if usec <= 0:
        return None
    owner = env.get(WATCHDOG_PID_ENV)
    if owner:
        try:
            if int(owner) != (os.getpid() if pid is None else pid):
                return None
        except ValueError:
            return None
    return usec / 2 / 1_000_000


async def run_watchdog(notifier: ServiceNotifier, interval: float, clock: Clock) -> None:
    """Ping systemd every ``interval`` seconds of the engine clock, until cancelled.

    Args:
        notifier: Where the pings go.
        interval: Seconds between pings (:func:`watchdog_interval`).
        clock: The engine clock (CORE-278).

    Implements:
        CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
    """
    while True:
        notifier.watchdog()
        await clock.sleep(interval)
