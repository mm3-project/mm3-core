"""``ModuleContext``: a module's only door into the engine.

Provided so far: identity, ``now()``, messaging, state, tasks,
``run_blocking``, the scheduler, storage, the logger, ``health``, ``t``,
``format_date``, ``format_number``, the current page and pages, ``http``
and ``mqtt`` (checked against capabilities and grants; their transports
arrive in milestones 5 and 7, the test kit fakes them) and ``layout``
(capability ``layout.control``). ``system`` and ``router`` are added with
their milestones (implementation plan 6 and 7).

Implements:
    -: Module Contract 1.0 rev 6, §4 ModuleContext reference.
    MOD-20, MOD-21, MOD-22: Module Contract 1.0 rev 6, §4.2 Guarantees.
    CORE-220: Core spec 0.7, §5.4 Request and reply.
    CORE-237, CORE-241: Core spec 0.14, §6.9 Pages and visibility.
    CORE-278: Core spec 0.17, §3.3 Scheduler.
    MOD-28: Module Contract 1.0 rev 8, §3.3 Rules.
    -: Module Contract 1.0 rev 9, §4.3 HttpClient.
    -: Module Contract 1.0 rev 9, §4.4 Mqtt.
    MOD-85: Module Contract 1.0 rev 9, §9 Logging, health and errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

from mm3.http import HttpClient
from mm3.mqtt import Mqtt
from mm3.scheduler import Scheduler

__all__ = ["Layout", "Location", "Logger", "ModuleContext", "PageInfo", "Storage"]

T = TypeVar("T")


class Logger(Protocol):
    """Structured logger pre-bound with module, instance and trace id.

    Events are short snake-case identifiers with fields (MOD-81).
    """

    def debug(self, event: str, **fields: Any) -> None:
        """Log at debug level."""
        ...

    def info(self, event: str, **fields: Any) -> None:
        """Log at info level."""
        ...

    def warning(self, event: str, **fields: Any) -> None:
        """Log at warning level."""
        ...

    def error(self, event: str, **fields: Any) -> None:
        """Log at error level."""
        ...

    def exception(self, event: str, **fields: Any) -> None:
        """Log at error level with the current exception and traceback."""
        ...


class Storage(Protocol):
    """Per-instance data directory and JSON key-value store (CORE-198)."""

    @property
    def path(self) -> Path:
        """Data directory ``<state>/modules/<instance>/``."""
        ...

    async def kv_get(self, key: str, default: Any = None) -> Any:
        """Return the value stored under ``key``, or ``default``."""
        ...

    async def kv_set(self, key: str, value: Any) -> None:
        """Store a JSON-serialisable value; the whole store is at most 1 MiB."""
        ...

    async def kv_delete(self, key: str) -> None:
        """Remove ``key``; nothing happens if it is absent."""
        ...


@dataclass(frozen=True)
class Location:
    """The mirror's place, ``engine.location`` (core spec 6.2).

    Attributes:
        lat: Latitude, -90 to 90.
        lon: Longitude, -180 to 180.
        name: Display name resolved along the instance's locale chain, or
            ``None`` when none is configured.

    Implements:
        CORE-288: Core spec 0.19, §6.2 Rules.
    """

    lat: float
    lon: float
    name: str | None = None


@dataclass(frozen=True)
class PageInfo:
    """A page as modules see it (core spec 6.9).

    Attributes:
        id: Page id.
        title: Title resolved for the engine locale.
    """

    id: str
    title: str


class Layout(Protocol):
    """Hide and show other instances (capability ``layout.control``, CORE-241)."""

    def hide(self, instance_id: str) -> None:
        """Hide an instance with the reason ``module:<own id>``."""
        ...

    def show(self, instance_id: str) -> None:
        """Clear this module's hide reason on an instance."""
        ...


class Health(Protocol):
    """Extra fields of the instance health record (MOD-85)."""

    def set(self, key: str, value: Any) -> None:
        """Set a lower snake-case field to a JSON value; ``None`` removes it."""
        ...


class ModuleContext(Protocol):
    """Injected into ``MirrorModule.__init__``; valid until ``stop()`` returns.

    Every member raises ``ContextClosedError`` once the instance has stopped.

    Implements:
        -: Module Contract 1.0 rev 6, §4 ModuleContext reference.
    """

    @property
    def instance_id(self) -> str:
        """Instance id, e.g. ``"weather_home"``."""
        ...

    @property
    def module_name(self) -> str:
        """Module name, e.g. ``"weather"``."""
        ...

    @property
    def locale(self) -> str:
        """Resolved active locale, e.g. ``"it"``."""
        ...

    @property
    def timezone(self) -> ZoneInfo:
        """Engine time zone."""
        ...

    def now(self) -> datetime:
        """Current wall-clock time, aware, in ``timezone``; the only clock a module reads.

        Implements:
            CORE-278: Core spec 0.17, §3.3 Scheduler.
            MOD-28: Module Contract 1.0 rev 8, §3.3 Rules.
        """
        ...

    @property
    def units(self) -> Literal["metric", "imperial"]:
        """Unit system."""
        ...

    @property
    def location(self) -> Location | None:
        """``engine.location``, or ``None`` when it is not set (CORE-288)."""
        ...

    def t(self, key: str, /, count: float | None = None, **vars: Any) -> str:
        """Return the text of a catalog key for this instance's locale (core spec 8)."""
        ...

    def format_date(self, value: datetime, style: str = "medium") -> str:
        """Format a date: ``short``, ``medium``, ``long``, ``full`` or ``time``."""
        ...

    def format_number(self, value: float, *, unit: str | None = None) -> str:
        """Format a number with an optional CLDR unit, without converting."""
        ...

    @property
    def state(self) -> Mapping[str, Any]:
        """Last state set, read-only."""
        ...

    @property
    def scheduler(self) -> Scheduler:
        """This instance's scheduler (core spec 3.3)."""
        ...

    @property
    def storage(self) -> Storage:
        """This instance's storage."""
        ...

    @property
    def log(self) -> Logger:
        """This instance's logger."""
        ...

    @property
    def health(self) -> Health:
        """Extra health fields (MOD-85)."""
        ...

    @property
    def http(self) -> HttpClient:
        """The MM3 HTTP client; needs ``http.outbound`` and ``grants.http`` (Contract 4.3)."""
        ...

    @property
    def mqtt(self) -> Mqtt:
        """MQTT on granted topics; needs ``mqtt.publish`` / ``mqtt.subscribe`` (Contract 4.4)."""
        ...

    async def send_notification(
        self, name: str, payload: dict[str, Any] | None = None, target: str | None = None
    ) -> UUID:
        """Send a notification; broadcast when ``target`` is ``None``.

        Raises:
            ValueError: Bad name or payload, or the payload of a registered
                name does not match its schema (CORE-225).
            ReservedNameError: The name starts with ``CORE_``.
        """
        ...

    async def request(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        target: str | None = None,
        timeout: float = 5.0,  # noqa: ASYNC109 - signature fixed by Contract §4
    ) -> dict[str, Any]:
        """Ask another module and wait for the first reply (core spec 5.4).

        Raises:
            ValueError: Bad name, payload or timeout (0.1 to 60 s).
            ReservedNameError: The name starts with ``CORE_``.
            RequestTimeoutError: No reply within ``timeout``.
            RemoteError: The responder's ``on_request`` raised.
        """
        ...

    async def send_to_client(
        self, type: str, payload: dict[str, Any] | None = None, client: str | None = None
    ) -> None:
        """Send a private message to this instance's front-end elements."""
        ...

    def set_state(self, state: dict[str, Any]) -> None:
        """Replace the snapshot and push it to clients as ``state``."""
        ...

    def patch_state(self, delta: dict[str, Any]) -> None:
        """Shallow-merge into the snapshot and push the delta."""
        ...

    @property
    def page(self) -> str:
        """Current page id (CORE-237)."""
        ...

    @property
    def pages(self) -> Sequence[PageInfo]:
        """Page ids and resolved titles, always current (CORE-237)."""
        ...

    @property
    def layout(self) -> Layout:
        """Hide or show other instances; needs ``layout.control`` (CORE-241)."""
        ...

    def create_task(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Start background work owned by this instance (MOD-12)."""
        ...

    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T:
        """Run blocking code in the shared thread pool (CORE-197)."""
        ...
