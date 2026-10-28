"""The web gateway: runs the mirror and admin listeners in the engine's event loop.

Each listener is a uvicorn server bound to its own ``host`` and ``port``
(``engine.server``, ``engine.admin``; both ``127.0.0.1`` by default). The
gateway is the single adapter for uvicorn (core spec 10.9); it does not let
uvicorn install signal handlers, because the engine handles SIGTERM, SIGINT
and SIGHUP itself.

Implements:
    CORE-60: Core spec 0.12, §10 Security, packaging, deployment, quality.
    CORE-61: Core spec 0.12, §10.1 Security.
    -: Core spec 0.12, §10.9 Dependency management.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp

from mm3.core.logging import get_logger
from mm3.core.settings import EngineSettings
from mm3.core.web.apps import admin_app, mirror_app, protect
from mm3.core.web.auth import WebAuth
from mm3.core.web.security import Network

__all__ = ["Listener", "ListenerError", "WebGateway"]

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
        name: ``mirror`` or ``admin``.
        app: The protected application.
        host: Bind address.
        port: Port; 0 picks a free one (tests).
    """

    def __init__(self, name: str, app: ASGIApp, host: str, port: int) -> None:
        self.name = name
        self.host = host
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            lifespan="off",
            log_config=None,
            access_log=False,
            server_header=False,
            proxy_headers=False,
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

        self._task = asyncio.create_task(serve(), name=f"mm3-web-{self.name}")
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise ListenerError(f"{self.name} listener stopped while starting")
            await asyncio.sleep(0.01)
        _log.info("web_listening", listener=self.name, host=self.host, port=self.port)

    async def stop(self) -> None:
        """Stop serving and close open connections."""
        if self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(ListenerError, asyncio.CancelledError):
            await self._task
        self._task = None


class WebGateway:
    """The two listeners with their shared token store and sessions.

    Args:
        settings: Engine settings at start; ``host`` and ``port`` of both
            listeners and ``admin.allow`` apply until the next restart.
        state_dir: Engine state directory (tokens and sessions).
        server_allow: Returns ``server.allow`` now; it applies live, to
            connections made afterwards (CORE-201). Default: the start value.

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
    ) -> None:
        self.auth = WebAuth.open(state_dir)
        start_allow: Sequence[Network] = list(settings.server.allow)
        admin_allow: Sequence[Network] = list(settings.admin.allow)
        self.mirror = Listener(
            "mirror",
            protect(mirror_app(self.auth), server_allow or (lambda: start_allow), "mirror"),
            settings.server.host,
            settings.server.port,
        )
        self.admin = Listener(
            "admin",
            protect(admin_app(self.auth), lambda: admin_allow, "admin"),
            settings.admin.host,
            settings.admin.port,
        )

    async def start(self) -> None:
        """Start both listeners; if the admin one fails, the mirror one is stopped.

        Raises:
            ListenerError: A listener could not start.
        """
        await self.mirror.start()
        try:
            await self.admin.start()
        except BaseException:
            await self.mirror.stop()
            raise

    async def stop(self) -> None:
        """Stop both listeners."""
        await self.admin.stop()
        await self.mirror.stop()
