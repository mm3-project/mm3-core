"""The web gateway: runs the mirror and admin listeners in the engine's event loop.

Each listener is a uvicorn server bound to its own ``host`` and ``port``
(``engine.server``, ``engine.admin``; both ``127.0.0.1`` by default). The
gateway is the single adapter for uvicorn (core spec 10.9); it does not let
uvicorn install signal handlers, because the engine handles SIGTERM, SIGINT
and SIGHUP itself.

The gateway also serves the REST API on the local admin socket
``/run/mm3/admin.sock`` (mode 0660, group ``mm3``) for the CLI (CORE-311).

Implements:
    CORE-311: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-60: Core spec 0.12, §10 Security, packaging, deployment, quality.
    CORE-61: Core spec 0.12, §10.1 Security.
    CORE-248, CORE-251: Core spec 0.13, §5.6 WebSocket protocol.
    -: Core spec 0.12, §10.9 Dependency management.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import os
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from starlette.types import ASGIApp

from mm3.core.logging import get_logger
from mm3.core.settings import EngineSettings
from mm3.core.web.apps import admin_app, mirror_app, protect, socket_app
from mm3.core.web.auth import WebAuth
from mm3.core.web.security import Network
from mm3.core.web.shell import ShellSources
from mm3.core.web.ws import MAX_FRAME_BYTES, PING_INTERVAL_S, PING_TIMEOUT_S, ClientHub

if TYPE_CHECKING:
    from mm3.core.web.adminws import AdminChannel
    from mm3.core.web.api import ApiServices
    from mm3.core.web.kiosk import KioskAgents

__all__ = ["SOCKET_GROUP", "SOCKET_MODE", "Listener", "ListenerError", "WebGateway"]

SOCKET_MODE = 0o660
SOCKET_GROUP = "mm3"

_log = get_logger(subsystem="web")


class ListenerError(RuntimeError):
    """A listener could not start (for example, its port is in use)."""


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to the engine."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class Listener:
    """One HTTP listener.

    Args:
        name: ``mirror``, ``admin`` or ``socket``.
        app: The protected application.
        host: Bind address.
        port: Port; 0 picks a free one (tests).
        uds: A Unix socket path instead of ``host`` and ``port``.
    """

    def __init__(
        self, name: str, app: ASGIApp, host: str, port: int, *, uds: Path | None = None
    ) -> None:
        self.name = name
        self.host = host
        self.uds = uds
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            uds=str(uds) if uds is not None else None,
            lifespan="off",
            log_config=None,
            access_log=False,
            server_header=False,
            proxy_headers=False,
            # Implements CORE-251 (Core spec 0.13, §5.6 WebSocket protocol): a ping
            # every 15 s, a client silent for 30 s is dropped; CORE-248: frames
            # over 512 KiB close the connection with 1009.
            ws_ping_interval=PING_INTERVAL_S,
            ws_ping_timeout=PING_TIMEOUT_S,
            ws_max_size=MAX_FRAME_BYTES,
        )
        self._server = _Server(config)
        self._task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        """The bound port once started."""
        for server in self._server.servers:
            for sock in server.sockets:
                return int(sock.getsockname()[1])
        return int(self._server.config.port)

    async def start(self) -> None:
        """Bind and start serving.

        Raises:
            ListenerError: The address could not be bound.
        """

        async def serve() -> None:
            try:
                await self._server.serve()
            except SystemExit as exc:  # uvicorn exits when it cannot bind
                raise ListenerError(f"{self.name} listener failed to start") from exc

        if self.uds is not None:
            with contextlib.suppress(FileNotFoundError):
                self.uds.unlink()  # a stale socket from a crash
        self._task = asyncio.create_task(serve(), name=f"mm3-web-{self.name}")
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise ListenerError(f"{self.name} listener stopped while starting")
            await asyncio.sleep(0.01)
        if self.uds is not None:
            _secure_socket(self.uds)
            _log.info("web_listening", listener=self.name, path=str(self.uds))
            return
        _log.info("web_listening", listener=self.name, host=self.host, port=self.port)

    async def stop(self) -> None:
        """Stop serving and close open connections."""
        if self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(ListenerError, asyncio.CancelledError):
            await self._task
        self._task = None
        if self.uds is not None:
            with contextlib.suppress(FileNotFoundError):
                self.uds.unlink()


def _secure_socket(path: Path) -> None:
    """Give the admin socket mode 0660 and group ``mm3`` (CORE-311)."""
    os.chmod(path, SOCKET_MODE)
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
    except KeyError:
        _log.warning("admin_socket_group", group=SOCKET_GROUP, reason="group_missing")
        return
    try:
        os.chown(path, -1, gid)
    except OSError as exc:
        _log.warning(
            "admin_socket_group", group=SOCKET_GROUP, reason="chown_failed", error=str(exc)
        )


class WebGateway:
    """The two listeners with their shared token store and sessions.

    Args:
        settings: Engine settings at start; ``host`` and ``port`` of both
            listeners and ``admin.allow`` apply until the next restart.
        state_dir: Engine state directory (tokens and sessions).
        server_allow: Returns ``server.allow`` now; it applies live, to
            connections made afterwards (CORE-201). Default: the start value.
        hub: Connected shells; without one, ``/ws`` is not served.
        auth: Token store and sessions; opened in ``state_dir`` by default.
        sources: The shell's files; without them only ``/core`` is served.
        agents: The kiosk agent endpoint; ``/ws/kiosk-agent`` exists only with it.
        api: What the REST API reads and controls (core 9.1).
        channel: The admin channel ``/admin/ws`` (CORE-304).
        socket_path: The local admin socket; it is served only with ``api`` (CORE-311).
        locale: Returns the language of the admin pages.

    Implements:
        CORE-60: Core spec 0.12, §10 Security, packaging, deployment, quality.
        CORE-61: Core spec 0.12, §10.1 Security.
    """

    def __init__(
        self,
        settings: EngineSettings,
        state_dir: Path,
        *,
        server_allow: Callable[[], Sequence[Network]] | None = None,
        hub: ClientHub | None = None,
        auth: WebAuth | None = None,
        sources: ShellSources | None = None,
        agents: KioskAgents | None = None,
        api: ApiServices | None = None,
        channel: AdminChannel | None = None,
        socket_path: Path | None = None,
        locale: Callable[[], str] = lambda: "en",
    ) -> None:
        self.auth = auth or (hub.auth if hub is not None else WebAuth.open(state_dir))
        self.hub = hub
        start_allow: Sequence[Network] = list(settings.server.allow)
        admin_allow: Sequence[Network] = list(settings.admin.allow)
        self.mirror = Listener(
            "mirror",
            protect(
                mirror_app(self.auth, hub, sources, agents),
                server_allow or (lambda: start_allow),
                "mirror",
            ),
            settings.server.host,
            settings.server.port,
        )
        self.admin = Listener(
            "admin",
            protect(
                admin_app(self.auth, hub, sources, api, channel, locale),
                lambda: admin_allow,
                "admin",
            ),
            settings.admin.host,
            settings.admin.port,
        )

        self.socket: Listener | None = None
        if api is not None and socket_path is not None:
            self.socket = Listener("socket", socket_app(api), "", 0, uds=socket_path)

    async def start(self) -> None:
        """Start the listeners; if the admin one fails, the mirror one is stopped.

        The local admin socket is optional: when it cannot be created the
        engine logs a warning and runs without it (CORE-311).

        Raises:
            ListenerError: The mirror or admin listener could not start.
        """
        await self.mirror.start()
        try:
            await self.admin.start()
        except BaseException:
            await self.mirror.stop()
            raise
        if self.socket is not None:
            try:
                await self.socket.start()
            except (ListenerError, OSError) as exc:
                _log.warning("admin_socket_failed", path=str(self.socket.uds), error=str(exc))
                self.socket = None

    async def stop(self) -> None:
        """Stop the listeners and remove the admin socket."""
        if self.socket is not None:
            await self.socket.stop()
        await self.admin.stop()
        await self.mirror.stop()
