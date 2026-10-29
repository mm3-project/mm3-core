"""The ``Engine`` object that wires the subsystems together.

This milestone covers steps 2-4 and the shutdown of core spec 7.1:
discover modules, start the core services, start every instance
concurrently, emit ``CORE_READY``; on shutdown emit ``CORE_SHUTDOWN`` and
stop every instance. Configuration loading, the web gateway and systemd
notification are added by their milestones.

Implements:
    -: Core spec 0.4, §3 Engine components.
    -: Core spec 0.4, §7.1 Engine startup and shutdown.
    CORE-197: Core spec 0.4, §7 Lifecycle and error handling.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from mm3.core.bus import Bus, BusEndpoint, ClientChannel, NullClientChannel
from mm3.core.clock import Clock, SystemClock
from mm3.core.i18n import I18n, registry_roots
from mm3.core.logging import get_logger
from mm3.core.registry import Registry
from mm3.core.services.storage import resolve_state_dir
from mm3.core.settings import EngineSettings
from mm3.core.supervisor import InstanceSpec, Supervisor

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
    """

    def __init__(
        self,
        settings: EngineSettings | None = None,
        *,
        registry: Registry | None = None,
        clock: Clock | None = None,
        clients: ClientChannel | None = None,
        state_dir: Path | None = None,
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
        self.i18n = I18n(registry_roots(registry))
        self.supervisor.i18n = self.i18n
        self.running = False

    async def start(self, instances: Iterable[InstanceSpec]) -> None:
        """Start the engine with the given instances.

        Args:
            instances: Instances to run, in configuration order.
        """
        for spec in instances:
            self.supervisor.add(spec)
        await self.supervisor.start_all()
        self.running = True
        self.supervisor.emit("CORE_READY", {})
        _log.info("engine_ready", instances=len(self.supervisor.instances()))

    async def stop(self) -> None:
        """Stop every instance and release the engine's resources."""
        if self.running:
            self.supervisor.emit("CORE_SHUTDOWN", {})
        await self.supervisor.stop_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.running = False
        _log.info("engine_stopped")

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
