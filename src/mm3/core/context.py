"""Engine-side implementation of ``ModuleContext``.

One context per instance incarnation: a restart builds a new one and closes
the old, after which every member raises ``ContextClosedError``.

Implements:
    -: Module Contract 1.0 rev 6, §4 ModuleContext reference.
    -: Module Contract 1.0 rev 6, §4.1 Exceptions raised by the context.
    MOD-21, MOD-22: Module Contract 1.0 rev 6, §4.2 Guarantees.
    MOD-91: Module Contract 1.0 rev 6, §10 API versioning and deprecation.
    CORE-22: Core spec 0.9, §5.3 Routing rules.
    CORE-26, CORE-29: Core spec 0.7, §5.3 Routing rules.
    CORE-220, CORE-221, CORE-222: Core spec 0.7, §5.4 Request and reply.
    CORE-225: Core spec 0.7, §5.5 Shared notification registry.
    CORE-111, CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
    CORE-185, CORE-193, CORE-197: Core spec 0.7, §7 Lifecycle and error handling.
    CORE-211: Core spec 0.19, §6.3 Hot reload.
    CORE-278: Core spec 0.17, §3.3 Scheduler.
    MOD-28: Module Contract 1.0 rev 8, §3.3 Rules.
    CORE-100: Core spec 0.18, §11.1 Engine additions.
    MOD-85: Module Contract 1.0 rev 9, §9 Logging, health and errors.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from mm3.__about__ import __version__
from mm3.context import Location, PageInfo
from mm3.core.bus import Bus, ClientChannel, MqttDelivery
from mm3.core.clock import Clock
from mm3.core.i18n.catalogs import I18n, format_date, format_number, locale_chain, substitute
from mm3.core.logging import Logger, get_logger
from mm3.core.services.grants import GrantSet
from mm3.core.services.http import HttpTransport, InstanceHttp, UnavailableTransport
from mm3.core.services.mqtt import InstanceMqtt, MqttBroker, NoBroker
from mm3.core.services.scheduler import InstanceScheduler
from mm3.core.services.storage import InstanceStorage
from mm3.core.settings import LocationSettings, Timeouts
from mm3.core.trace import current_trace, trace_scope
from mm3.errors import (
    ContextClosedError,
    RemoteError,
    RequestTimeoutError,
    ReservedNameError,
    TransientError,
)
from mm3.messages import NAME_PATTERN, Notification, check_payload
from mm3.notifications import validate_payload
from mm3.scheduler import JobFn

__all__ = [
    "IMPLICIT_PAGE",
    "REQUEST_FAILED",
    "REQUEST_TIMEOUT_RANGE",
    "EngineContext",
    "InstanceCounters",
    "LayoutControl",
    "PageControl",
    "error_reply_payload",
]

REQUEST_TIMEOUT_RANGE = (0.1, 60.0)
"""Allowed ``timeout`` of ``ctx.request``, in seconds (CORE-220)."""
REQUEST_FAILED = "REQUEST_FAILED"
"""Core catalog key of an ``internal`` error reply (CORE-221)."""


def error_reply_payload(code: str, message_key: str) -> dict[str, Any]:
    """Build the payload of an error reply.

    Args:
        code: ``transient``, ``configuration`` or ``internal``.
        message_key: Catalog key.

    Returns:
        ``{"error": {"code": ..., "message_key": ...}}``.

    Implements:
        CORE-221: Core spec 0.7, §5.4 Request and reply.
    """
    return {"error": {"code": code, "message_key": message_key}}


def _as_error_reply(payload: dict[str, Any]) -> tuple[str, str] | None:
    error = payload.get("error") if len(payload) == 1 else None
    if isinstance(error, dict) and set(error) == {"code", "message_key"}:
        code, key = error["code"], error["message_key"]
        if isinstance(code, str) and isinstance(key, str):
            return code, key
    return None


T = TypeVar("T")

IMPLICIT_PAGE = "main"
"""The single page of a mirror without a ``pages`` block (CORE-230)."""


class PageControl(Protocol):
    """What contexts need from the page service (core spec 6.9)."""

    @property
    def page(self) -> str:
        """Current page id."""
        ...

    def page_infos(self) -> list[PageInfo]:
        """Pages with resolved titles."""
        ...

    def set_module_reason(self, by: str, target: str, hidden: bool) -> None:
        """Set or clear the hide reason ``module:<by>`` on ``target``."""
        ...


class _Unset:
    """Marks an argument that was not passed, as ``None`` is a value for some."""


_UNSET = _Unset()

_HEALTH_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class InstanceHealth:
    """``ctx.health``: extra fields of the instance health record (MOD-85).

    Args:
        counters: The instance's counters, which hold the fields.
        guard: Raises ``ContextClosedError`` once the context is closed.

    Implements:
        MOD-85: Module Contract 1.0 rev 9, §9 Logging, health and errors.
        CORE-113: Core spec 0.7, §10.5 Diagnostics and debugging.
    """

    def __init__(self, counters: InstanceCounters, guard: Callable[[], None]) -> None:
        self._counters = counters
        self._guard = guard

    def set(self, key: str, value: Any) -> None:
        """Set a health field, e.g. ``last_refresh`` or ``items``.

        Args:
            key: Lower snake-case field name.
            value: JSON-serialisable value; ``None`` removes the field.

        Raises:
            ValueError: Bad key or value not JSON-serialisable.
        """
        self._guard()
        if not _HEALTH_KEY.match(key):
            raise ValueError(f"health field {key!r} is not lower snake case")
        if value is None:
            self._counters.health_fields.pop(key, None)
            return
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            raise ValueError(f"health field {key} is not JSON-serialisable") from None
        self._counters.health_fields[key] = value


class LayoutControl:
    """``ctx.layout``: hide and show other instances (CORE-241).

    Args:
        pages: The page service.
        owner: Id of the instance that uses it.

    Implements:
        CORE-241: Core spec 0.14, §6.9 Pages and visibility.
    """

    def __init__(self, pages: PageControl, owner: str) -> None:
        self._pages = pages
        self._owner = owner

    def hide(self, instance_id: str) -> None:
        """Hide an instance with the reason ``module:<own id>``.

        Args:
            instance_id: The instance to hide.
        """
        self._pages.set_module_reason(self._owner, instance_id, True)

    def show(self, instance_id: str) -> None:
        """Clear this module's hide reason on an instance.

        Args:
            instance_id: The instance.
        """
        self._pages.set_module_reason(self._owner, instance_id, False)


@dataclass
class InstanceCounters:
    """Counters shared by all incarnations of an instance (health record, CORE-113).

    Attributes:
        handler_calls: Handlers run.
        handler_timeouts: Handlers cancelled for exceeding their budget.
        job_timeouts: Job runs cancelled for exceeding their budget.
        transient_errors: ``TransientError`` raised by hooks, tasks or jobs.
        durations: Recent handler durations in seconds (for p95).
        last_out: Time the instance last sent a notification, reply or
            client message.
        deprecated_used: Deprecated members already reported (MOD-91).
        health_fields: Extra health fields set with ``ctx.health.set`` (MOD-85).
    """

    handler_calls: int = 0
    handler_timeouts: int = 0
    job_timeouts: int = 0
    transient_errors: int = 0
    durations: list[float] = field(default_factory=list)
    last_out: datetime | None = None
    deprecated_used: set[str] = field(default_factory=set)
    health_fields: dict[str, Any] = field(default_factory=dict)


class EngineContext:
    """The context handed to one instance incarnation.

    Args:
        instance_id: Instance id.
        module_name: Module name.
        bus: The bus.
        clients: Path to the instance's front-end elements.
        clock: Time source.
        executor: Shared ``run_blocking`` pool.
        timeouts: Time budgets.
        timezone: Engine time zone.
        locale: Active locale.
        units: Unit system.
        state_dir: Engine state directory.
        counters: Health counters of the instance.
        spawn: Starts a coroutine in the instance's task group.
        pages: Current page, pages and visibility control; ``None`` until
            the page service runs (one implicit page ``main``).
        capabilities: The module's declared capabilities.
        i18n: Catalogs for ``t``.
        engine_locale: Returns the current ``engine.locale``.
        grants: Scopes granted to the instance (Contract section 8).
        http_transport: Sends ``ctx.http`` requests (the test kit's fake in tests).
        mqtt_broker: Broker connection behind ``ctx.mqtt``.
        enqueue: Puts an MQTT delivery into the instance inbox.
        on_denied: Called with a capability used but not declared (MOD-70).
        module_version: Version of the module's distribution, for the
            default User-Agent (CORE-283).
        location: ``engine.location``, or ``None`` (CORE-288).

    Implements:
        -: Module Contract 1.0 rev 9, §4 ModuleContext reference.
        CORE-283, CORE-287: Core spec 0.19, §3.4 HTTP transport.
        CORE-288: Core spec 0.19, §6.2 Rules.
        CORE-237, CORE-241: Core spec 0.14, §6.9 Pages and visibility.
        CORE-100: Core spec 0.18, §11.1 Engine additions.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        module_name: str,
        bus: Bus,
        clients: ClientChannel,
        clock: Clock,
        executor: Executor,
        timeouts: Timeouts,
        timezone: ZoneInfo,
        locale: str,
        units: Literal["metric", "imperial"],
        state_dir: Path,
        counters: InstanceCounters,
        spawn: Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]],
        pages: PageControl | None = None,
        capabilities: frozenset[str] = frozenset(),
        i18n: I18n | None = None,
        engine_locale: Callable[[], str] | None = None,
        grants: GrantSet | None = None,
        http_transport: HttpTransport | None = None,
        mqtt_broker: MqttBroker | None = None,
        enqueue: Callable[[MqttDelivery], None] | None = None,
        on_denied: Callable[[str], None] | None = None,
        module_version: str = "0",
        location: LocationSettings | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._module_name = module_name
        self._bus = bus
        self._clients = clients
        self._clock = clock
        self._executor = executor
        self._timeouts = timeouts
        self._timezone = timezone
        self._locale = locale
        self._units = units
        self._location = location
        self._counters = counters
        self._spawn = spawn
        self._pages = pages
        self._capabilities = capabilities
        self._i18n = i18n
        self._engine_locale = engine_locale or (lambda: locale)
        self._closed = False
        self._state: dict[str, Any] = {}
        self._log = get_logger(module_name, instance_id)
        self._storage = InstanceStorage(state_dir, instance_id, self.run_blocking)
        self._scheduler = InstanceScheduler(
            clock, timezone, self._run_job, self._spawn_job, self._log
        )
        granted = grants or GrantSet()
        self._grants = granted
        self._http = InstanceHttp(
            instance_id,
            http_transport or UnavailableTransport(),
            capabilities,
            granted.http,
            self._guard,
            on_denied,
            ca_files=dict(granted.http_ca),
            user_agent=f"mm3/{__version__} {module_name}/{module_version}",
            log=self._log,
            on_result=self._http_result,
        )
        self._mqtt = InstanceMqtt(
            instance_id,
            mqtt_broker or NoBroker(),
            capabilities,
            granted.mqtt_publish,
            granted.mqtt_subscribe,
            enqueue or (lambda _item: None),
            self._guard,
            on_denied,
        )
        self._health = InstanceHealth(counters, self._guard)
        self._on_denied = on_denied

    # --- engine side ----------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once the instance incarnation has stopped."""
        return self._closed

    def close(self) -> None:
        """Close the context and cancel its jobs (MOD-22)."""
        self._closed = True
        self._scheduler.cancel_all()
        self._mqtt.close()

    def update(
        self,
        *,
        locale: str | None = None,
        timezone: ZoneInfo | None = None,
        units: Literal["metric", "imperial"] | None = None,
        timeouts: Timeouts | None = None,
        location: LocationSettings | _Unset | None = _UNSET,
    ) -> None:
        """Apply reloaded engine values, so the context always holds the current ones.

        A new time zone also re-plans wall-clock jobs (CORE-189).

        Args:
            locale: New active locale.
            timezone: New engine time zone.
            units: New unit system.
            timeouts: New time budgets, used by calls started afterwards.
            location: New ``engine.location``; ``None`` clears it.

        Implements:
            CORE-211: Core spec 0.19, §6.3 Hot reload.
        """
        if locale is not None:
            self._locale = locale
        if timezone is not None:
            self._timezone = timezone
            self._scheduler.set_timezone(timezone)
        if units is not None:
            self._units = units
        if timeouts is not None:
            self._timeouts = timeouts
        if not isinstance(location, _Unset):
            self._location = location

    @property
    def engine_scheduler(self) -> InstanceScheduler:
        """The scheduler with its engine-side controls."""
        return self._scheduler

    def _guard(self) -> None:
        if self._closed:
            raise ContextClosedError(f"context of {self._instance_id} is closed")

    @property
    def _source(self) -> str:
        return f"{self._module_name}/{self._instance_id}"

    def _sent(self) -> None:
        self._counters.last_out = self._clock.wall()

    def deprecated(self, member: str, replacement: str) -> None:
        """Report use of a deprecated member, once per instance.

        Deprecated members call this and keep working for at least one
        further minor release.

        Args:
            member: The deprecated member, e.g. ``"ctx.old()"``.
            replacement: What to use instead.

        Implements:
            MOD-91: Module Contract 1.0 rev 6, §10 API versioning and deprecation.
        """
        if member in self._counters.deprecated_used:
            return
        self._counters.deprecated_used.add(member)
        self._log.warning("deprecated_api", member=member, replacement=replacement)

    def _outgoing(self, name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if name.startswith("CORE_"):
            raise ReservedNameError(f"{name}: CORE_* names are reserved for the engine")
        if not NAME_PATTERN.match(name):
            raise ValueError(f"notification name {name!r} is not UPPER_SNAKE")
        body = {} if payload is None else payload
        check_payload(body)
        validate_payload(name, body)
        return body

    def send_reply(self, request: Notification, payload: dict[str, Any]) -> None:
        """Answer a request on behalf of this instance (engine side).

        The reply has the request's name and trace, ``reply_to`` = request id
        and ``target`` = the requesting instance, if any.

        Args:
            request: The request being answered.
            payload: Reply payload (already checked).

        Implements:
            CORE-221: Core spec 0.7, §5.4 Request and reply.
        """
        requester = request.source.split("/", 1)[1] if "/" in request.source else None
        self._bus.publish(
            Notification(
                id=uuid4(),
                trace=request.trace,
                ts=self._clock.wall(),
                name=request.name,
                source=self._source,
                target=requester,
                reply_to=request.id,
                payload=payload,
            ),
            sender_instance=self._instance_id,
        )
        self._sent()

    def _spawn_job(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[Any]:
        return self._spawn(coro, name)

    async def _run_job(self, fn: JobFn, name: str) -> None:
        # Implements CORE-185 (Core spec 0.7, §3.3 Scheduler): own trace id and
        # job timeout; TransientError is a warning, other exceptions fail the instance.
        trace = uuid4()
        with trace_scope(trace):
            cm = self._clock.timeout(self._timeouts.job)
            try:
                async with cm:
                    await fn()
            except TimeoutError:
                if not cm.expired():
                    raise
                self._counters.job_timeouts += 1
                self._log.warning("job_timeout", job=name, budget_s=self._timeouts.job)
            except TransientError as exc:
                self._counters.transient_errors += 1
                self._log.warning("job_transient_error", job=name, key=exc.message_key)

    # --- identity -------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        """Instance id."""
        self._guard()
        return self._instance_id

    @property
    def module_name(self) -> str:
        """Module name."""
        self._guard()
        return self._module_name

    @property
    def locale(self) -> str:
        """Active locale."""
        self._guard()
        return self._locale

    def t(self, key: str, /, count: float | None = None, **vars: Any) -> str:
        """Return the text of a catalog key for this instance's locale chain.

        Args:
            key: The catalog key.
            count: Picks the plural form; also available as ``{count}``.
            **vars: Placeholder values.

        Returns:
            The text; the key itself when no catalog has it.

        Implements:
            CORE-264, CORE-265: Core spec 0.15, §8.5 Catalogs at run time.
        """
        self._guard()
        chain = locale_chain(self._locale, self._engine_locale())
        if self._i18n is None:
            return substitute(key, vars)
        return self._i18n.translate(self._module_name, chain, key, count, vars)

    def format_date(self, value: datetime, style: str = "medium") -> str:
        """Format a date in this instance's locale and the engine time zone (CORE-266).

        Args:
            value: The moment; a naive value is taken as UTC.
            style: ``short``, ``medium``, ``long``, ``full`` or ``time``.

        Returns:
            The text.
        """
        self._guard()
        return format_date(value, style, self._locale, self._timezone)

    def format_number(self, value: float, *, unit: str | None = None) -> str:
        """Format a number in this instance's locale, with an optional unit (CORE-266).

        Args:
            value: The number; it is not converted.
            unit: A CLDR unit identifier such as ``celsius``.

        Returns:
            The text.
        """
        self._guard()
        return format_number(value, self._locale, unit)

    def now(self) -> datetime:
        """Current wall-clock time on the engine clock, aware, in ``timezone``.

        Implements:
            CORE-278: Core spec 0.17, §3.3 Scheduler.
            MOD-28: Module Contract 1.0 rev 8, §3.3 Rules.
        """
        self._guard()
        return self._clock.wall().astimezone(self._timezone)

    def _http_result(self, ok: bool) -> None:
        """Count a request and its outcome in the health record (CORE-287)."""
        fields = self._counters.health_fields
        fields["http_requests"] = int(fields.get("http_requests", 0)) + 1
        if ok:
            fields["http_last_success"] = self._clock.wall().isoformat()
        else:
            fields["http_failures"] = int(fields.get("http_failures", 0)) + 1

    @property
    def http(self) -> InstanceHttp:
        """The MM3 HTTP client (Contract 4.3); needs ``http.outbound`` and grants."""
        self._guard()
        return self._http

    @property
    def mqtt(self) -> InstanceMqtt:
        """Publish and subscribe on granted topics (CORE-100, Contract 4.4)."""
        self._guard()
        return self._mqtt

    @property
    def health(self) -> InstanceHealth:
        """Extra fields of the instance health record (MOD-85)."""
        self._guard()
        return self._health

    @property
    def grants(self) -> GrantSet:
        """The scopes granted to this instance (engine side)."""
        return self._grants

    @property
    def timezone(self) -> ZoneInfo:
        """Engine time zone."""
        self._guard()
        return self._timezone

    @property
    def units(self) -> Literal["metric", "imperial"]:
        """Unit system."""
        self._guard()
        return self._units

    @property
    def location(self) -> Location | None:
        """``engine.location`` with its name resolved along the locale chain (CORE-288)."""
        self._guard()
        place = self._location
        if place is None:
            return None
        chain = locale_chain(self._locale, self._engine_locale())
        name = place.name.resolve(chain) if place.name is not None else None
        return Location(place.lat, place.lon, name)

    # --- pages and layout -------------------------------------------------------

    @property
    def page(self) -> str:
        """Current page id (CORE-237)."""
        self._guard()
        return self._pages.page if self._pages is not None else IMPLICIT_PAGE

    @property
    def pages(self) -> Sequence[PageInfo]:
        """Page ids and resolved titles, always current (CORE-237)."""
        self._guard()
        if self._pages is None:
            return (PageInfo(IMPLICIT_PAGE, IMPLICIT_PAGE),)
        return tuple(self._pages.page_infos())

    @property
    def layout(self) -> LayoutControl:
        """Hide or show other instances (CORE-241).

        Raises:
            PermissionError: The module does not declare ``layout.control`` (MOD-70).

        Implements:
            CORE-241: Core spec 0.14, §6.9 Pages and visibility.
        """
        self._guard()
        if "layout.control" not in self._capabilities:
            if self._on_denied is not None:
                self._on_denied("layout.control")
            raise PermissionError("capability layout.control is not declared")
        if self._pages is None:
            raise RuntimeError("the page service is not running")
        return LayoutControl(self._pages, self._instance_id)

    # --- messaging and state --------------------------------------------------

    async def send_notification(
        self, name: str, payload: dict[str, Any] | None = None, target: str | None = None
    ) -> UUID:
        """Send a notification on the bus.

        Args:
            name: ``UPPER_SNAKE`` name, not starting with ``CORE_``.
            payload: JSON object, at most 256 KiB.
            target: Instance id for a targeted notification; ``None`` broadcasts.

        Returns:
            The notification id.

        Raises:
            ReservedNameError: The name starts with ``CORE_``.
            ValueError: Bad name or payload, or the payload of a registered
                name does not match its schema (CORE-225).
        """
        self._guard()
        body = self._outgoing(name, payload)
        nid = uuid4()
        n = Notification(
            id=nid,
            trace=current_trace() or nid,
            ts=self._clock.wall(),
            name=name,
            source=self._source,
            target=target,
            payload=body,
        )
        self._bus.publish(n, sender_instance=self._instance_id)
        self._sent()
        return nid

    async def request(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        target: str | None = None,
        timeout: float = 5.0,  # noqa: ASYNC109 - signature fixed by Contract §4
    ) -> dict[str, Any]:
        """Ask another module for data and wait for the first reply.

        With ``target`` only that instance is asked; without, the request is
        delivered like a broadcast and the first reply wins. The reply
        bypasses this instance's inbox, so a handler may await it.

        Args:
            name: ``UPPER_SNAKE`` name, not starting with ``CORE_``.
            payload: JSON object, at most 256 KiB.
            target: Instance to ask; ``None`` asks every subscriber.
            timeout: Seconds to wait, 0.1 to 60.

        Returns:
            The reply payload.

        Raises:
            ReservedNameError: The name starts with ``CORE_``.
            ValueError: Bad name, payload or timeout.
            RequestTimeoutError: No reply within ``timeout``.
            RemoteError: The responder's ``on_request`` raised.

        Implements:
            CORE-220, CORE-221, CORE-222: Core spec 0.7, §5.4 Request and reply.
        """
        self._guard()
        low, high = REQUEST_TIMEOUT_RANGE
        if not low <= timeout <= high:
            raise ValueError(f"timeout must be between {low} and {high} s, got {timeout}")
        body = self._outgoing(name, payload)
        nid = uuid4()
        n = Notification(
            id=nid,
            trace=current_trace() or nid,
            ts=self._clock.wall(),
            name=name,
            source=self._source,
            target=target,
            expects_reply=True,
            payload=body,
        )
        future = self._bus.open_request(nid)
        try:
            self._bus.publish(n, sender_instance=self._instance_id)
            self._sent()
            try:
                async with self._clock.timeout(timeout):
                    reply = await future
            except TimeoutError:
                raise RequestTimeoutError(name, timeout) from None
        finally:
            self._bus.close_request(nid)
        error = _as_error_reply(reply.payload)
        if error is not None:
            raise RemoteError(error[0], error[1], reply.source)
        return dict(reply.payload)

    async def send_to_client(
        self, type: str, payload: dict[str, Any] | None = None, client: str | None = None
    ) -> None:
        """Send a private message to this instance's front-end elements.

        Args:
            type: Module-private message type.
            payload: JSON object, at most 256 KiB.
            client: One client id, or ``None`` for all.

        Raises:
            ValueError: Bad payload.
        """
        self._guard()
        body = {} if payload is None else payload
        check_payload(body)
        self._clients.send_client(self._instance_id, type, body, client)
        self._sent()

    @property
    def state(self) -> Mapping[str, Any]:
        """Last state set, read-only."""
        self._guard()
        return MappingProxyType(self._state)

    def set_state(self, state: dict[str, Any]) -> None:
        """Replace the snapshot and push it as ``state``.

        Args:
            state: The new state.

        Raises:
            ValueError: Not a JSON object or larger than 256 KiB.
        """
        self._guard()
        check_payload(state)
        self._state = dict(state)
        self._clients.send_state(self._instance_id, dict(self._state), delta=False)

    def patch_state(self, delta: dict[str, Any]) -> None:
        """Shallow-merge into the snapshot and push the delta.

        Args:
            delta: Keys to replace.

        Raises:
            ValueError: The delta or the merged state is invalid.
        """
        self._guard()
        check_payload(delta)
        merged = {**self._state, **delta}
        check_payload(merged)
        self._state = merged
        self._clients.send_state(self._instance_id, dict(delta), delta=True)

    # --- tasks and time -------------------------------------------------------

    def create_task(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Start background work in the instance's task group.

        A ``TransientError`` ends the task with a warning; any other exception
        moves the instance to FAILED.

        Args:
            coro: The coroutine.
            name: Task name.

        Returns:
            The task.
        """
        try:
            self._guard()
        except ContextClosedError:
            coro.close()
            raise

        async def guarded() -> Any:
            try:
                return await coro
            except TransientError as exc:
                self._counters.transient_errors += 1
                self._log.warning("task_transient_error", task=name, key=exc.message_key)
                return None

        return self._spawn(guarded(), name)

    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T:
        """Run blocking code in the shared thread pool.

        Args:
            fn: The function.
            *args: Its positional arguments.

        Returns:
            Its result; discarded if the caller is cancelled.
        """
        self._guard()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    @property
    def scheduler(self) -> InstanceScheduler:
        """This instance's scheduler."""
        self._guard()
        return self._scheduler

    @property
    def storage(self) -> InstanceStorage:
        """This instance's storage."""
        self._guard()
        return self._storage

    @property
    def log(self) -> Logger:
        """This instance's logger."""
        self._guard()
        return self._log
