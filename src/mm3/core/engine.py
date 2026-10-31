"""The ``Engine`` object that wires the subsystems together.

This milestone covers steps 2-4 and the shutdown of core spec 7.1:
discover modules, start the core services, start every instance
concurrently, emit ``CORE_READY``; on shutdown emit ``CORE_SHUTDOWN`` and
stop every instance. The engine also tells systemd its state and pings the
loop watchdog (CORE-290, CORE-291); both are no-ops outside systemd.

Implements:
    -: Core spec 0.4, §3 Engine components.
    -: Core spec 0.4, §7.1 Engine startup and shutdown.
    CORE-197: Core spec 0.4, §7 Lifecycle and error handling.
    CORE-290, CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from mm3.core.bus import Bus, BusEndpoint, ClientChannel, NullClientChannel
from mm3.core.clock import Clock, SystemClock
from mm3.core.i18n import I18n, registry_roots
from mm3.core.logging import get_logger
from mm3.core.registry import Registry
from mm3.core.services.http import HttpxTransport
from mm3.core.services.storage import resolve_state_dir
from mm3.core.settings import EngineSettings
from mm3.core.supervisor import InstanceSpec, State, Supervisor
from mm3.core.systemd import ServiceNotifier, run_watchdog, watchdog_interval

__all__ = ["Engine"]


class ClientHub(ClientChannel, BusEndpoint, Protocol):
    """The web gateway's shells: a client channel and a bus endpoint."""

    def attach(self, bus: Bus) -> None:
        """Start receiving notifications from the bus."""
        ...


_log = get_logger(subsystem="engine")


class Engine:
    """The MM3 engine.

    Args:
        settings: Engine settings.
        registry: Module registry; when ``None``, one is built from the
            ``mm3.modules`` entry points.
        clock: Time source; the system clock by default.
        clients: Path to front-end elements; dropped until the web gateway exists.
        state_dir: State directory; default from the environment (CORE-198).
        notifier: Where service notifications go; built from ``$NOTIFY_SOCKET``
            by default (CORE-290).
    """

    def __init__(
        self,
        settings: EngineSettings | None = None,
        *,
        registry: Registry | None = None,
        clock: Clock | None = None,
        clients: ClientChannel | None = None,
        state_dir: Path | None = None,
        notifier: ServiceNotifier | None = None,
    ) -> None:
        self.settings = settings or EngineSettings()
        if registry is None:
            registry = Registry()
            registry.discover()
        self.registry = registry
        self.bus = Bus()
        self.clock = clock or SystemClock()
        self.executor = ThreadPoolExecutor(
            max_workers=self.settings.threads, thread_name_prefix="mm3-blocking"
        )
        self.supervisor = Supervisor(
            settings=self.settings,
            registry=registry,
            bus=self.bus,
            clients=clients or NullClientChannel(),
            clock=self.clock,
            executor=self.executor,
            state_dir=state_dir or resolve_state_dir(),
        )
        self.http = HttpxTransport()
        self.supervisor.http_transport = self.http
        self.i18n = I18n(registry_roots(registry))
        self.supervisor.i18n = self.i18n
        self.running = False
        self.notifier = notifier or ServiceNotifier.from_environ()
        self.watchdog_task: asyncio.Task[None] | None = None
        self._ready_sent = False
        self._stopping = False
        self._reloads = 0

    async def start(self, instances: Iterable[InstanceSpec]) -> None:
        """Start the engine with the given instances.

        Args:
            instances: Instances to run, in configuration order.
        """
        await self.http.start()
        for spec in instances:
            self.supervisor.add(spec)
        await self.supervisor.start_all()
        self.running = True
        self.supervisor.emit("CORE_READY", {})
        _log.info("engine_ready", instances=len(self.supervisor.instances()))

    async def stop(self) -> None:
        """Stop every instance and release the engine's resources."""
        self.notify_stopping()
        if self.running:
            self.supervisor.emit("CORE_SHUTDOWN", {})
        await self.supervisor.stop_all()
        await self.http.aclose()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.running = False
        _log.info("engine_stopped")

    # --- systemd (CORE-290, CORE-291) ----------------------------------------------

    def status_line(self) -> str:
        """Return the one-line summary sent as ``STATUS=`` (running and failed instances)."""
        states = [i.state for i in self.supervisor.instances()]
        running = sum(1 for s in states if s in (State.RUNNING, State.SUSPENDED))
        failed = sum(1 for s in states if s in (State.FAILED, State.DISABLED))
        return f"{running} running, {failed} failed"

    def notify_ready(self) -> None:
        """Tell systemd the engine is ready (``READY=1``, with ``STATUS=``).

        Called once the web gateway listens (step 5 of 7.1). A start that aborts
        never gets here, so it never sends ``READY=1``.

        Implements:
            CORE-290: Core spec 0.20, §7.1 Engine startup and shutdown.
        """
        if self._stopping:
            return
        self._ready_sent = True
        self.notifier.ready(self.status_line())

    def notify_reloading(self) -> None:
        """Tell systemd a reload begins (``RELOADING=1``); only after ``READY=1``."""
        if self._ready_sent and not self._stopping:
            self._reloads += 1
            self.notifier.reloading()

    def notify_reloaded(self) -> None:
        """Tell systemd the reload ended, applied or rejected (``READY=1``, ``STATUS=``)."""
        if self._reloads == 0:
            return
        self._reloads -= 1
        if self._reloads == 0 and not self._stopping:
            self.notifier.ready(self.status_line())

    def notify_stopping(self) -> None:
        """Tell systemd shutdown begins (``STOPPING=1``), once."""
        if not self._stopping:
            self._stopping = True
            self.notifier.stopping()

    def start_watchdog(self, environ: Mapping[str, str] | None = None) -> asyncio.Task[None] | None:
        """Start the loop watchdog when systemd asks for one.

        The task lives as long as the process: reloads and shutdown do not stop it.

        Args:
            environ: The environment; ``os.environ`` by default.

        Returns:
            The task, or ``None`` without ``$WATCHDOG_USEC`` (or when it is not for this process).

        Implements:
            CORE-291: Core spec 0.20, §7.1 Engine startup and shutdown.
        """
        interval = watchdog_interval(environ)
        if interval is None or self.watchdog_task is not None:
            return self.watchdog_task
        self.watchdog_task = asyncio.create_task(
            run_watchdog(self.notifier, interval, self.clock), name="mm3-watchdog"
        )
        return self.watchdog_task

    def attach_clients(self, hub: ClientHub) -> None:
        """Connect the web gateway's shells before :meth:`start`.

        Backends reach their elements through the hub, and it receives bus
        notifications for the browsers.

        Args:
            hub: The connected shells (``mm3.core.web.ws.ClientHub``).
        """
        self.supervisor.clients = hub
        hub.attach(self.bus)

    def health(self) -> list[dict[str, Any]]:
        """Return the health record of every instance.

        Returns:
            One record per instance.
        """
        return [i.health() for i in self.supervisor.instances()]
