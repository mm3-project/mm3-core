"""systemd integration of the engine: notification, watchdog and the shipped unit.

Implements:
    CORE-290, CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
    CORE-292: Core spec 0.20, §10.2 Packaging and deployment.
"""

from mm3.core.systemd.notify import (
    NOTIFY_SOCKET_ENV,
    WATCHDOG_PID_ENV,
    WATCHDOG_USEC_ENV,
    ServiceNotifier,
    monotonic_usec,
    run_watchdog,
    watchdog_interval,
)

__all__ = [
    "NOTIFY_SOCKET_ENV",
    "WATCHDOG_PID_ENV",
    "WATCHDOG_USEC_ENV",
    "ServiceNotifier",
    "monotonic_usec",
    "run_watchdog",
    "watchdog_interval",
]
