"""The ``engine`` block of the configuration.

Every key is optional; defaults follow the table of CORE-201. The bus,
supervisor and scheduler read their settings from this model.

Implements:
    CORE-201: Core spec 0.21, §6.2 Rules.
    CORE-61: Core spec 0.11, §10.1 Security.
    CORE-62: Core spec 0.11, §10.1 Security.
    CORE-205: Core spec 0.8, §6.2 Rules.
    CORE-338: Core spec 0.34, §9.3 MQTT bridge.
    -: Core spec 0.8, §3.2 Concurrency rules.
    CORE-27: Core spec 0.4, §5.3 Routing rules.
    CORE-197: Core spec 0.4, §7 Lifecycle and error handling.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    ConfigDict,
    Field,
    IPvAnyNetwork,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from mm3.config import LocaleTag, LocalizedText, ModuleConfig

__all__ = [
    "AdminSettings",
    "BusSettings",
    "EngineSettings",
    "HistorySettings",
    "InputSettings",
    "MqttSettings",
    "ServerSettings",
    "Timeouts",
    "system_timezone",
]


def system_timezone(root: Path = Path("/")) -> str:
    """Return the system's IANA time zone, or ``UTC`` when it cannot be found.

    Looks at ``$TZ``, ``/etc/timezone`` and the ``/etc/localtime`` link.

    Args:
        root: File system root, for tests.

    Returns:
        A zone name that ``zoneinfo`` knows.
    """
    candidates: list[str] = []
    if tz := os.environ.get("TZ", "").lstrip(":"):
        candidates.append(tz)
    with contextlib.suppress(OSError):
        candidates.append((root / "etc/timezone").read_text().strip())
    try:
        target = os.readlink(root / "etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return name
    return "UTC"


class _Block(ModuleConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Timeouts(_Block):
    """Time budgets in seconds (``engine.timeouts``, core spec 3.2).

    Implements:
        -: Core spec 0.8, §3.2 Concurrency rules.
    """

    start: float = Field(
        default=15.0,
        gt=0,
        title="CFG_ENGINE_TIMEOUTS_START",
        description="CFG_ENGINE_TIMEOUTS_START_HELP",
    )
    stop: float = Field(
        default=10.0,
        gt=0,
        title="CFG_ENGINE_TIMEOUTS_STOP",
        description="CFG_ENGINE_TIMEOUTS_STOP_HELP",
    )
    suspend: float = Field(
        default=5.0,
        gt=0,
        title="CFG_ENGINE_TIMEOUTS_SUSPEND",
        description="CFG_ENGINE_TIMEOUTS_SUSPEND_HELP",
    )
    reconfigure: float = Field(
        default=5.0,
        gt=0,
        title="CFG_ENGINE_TIMEOUTS_RECONFIGURE",
        description="CFG_ENGINE_TIMEOUTS_RECONFIGURE_HELP",
    )
    handler: float = Field(
        default=5.0,
        gt=0,
        title="CFG_ENGINE_TIMEOUTS_HANDLER",
        description="CFG_ENGINE_TIMEOUTS_HANDLER_HELP",
    )
    job: float = Field(
        default=60.0,
        gt=0,
        title="CFG_ENGINE_TIMEOUTS_JOB",
        description="CFG_ENGINE_TIMEOUTS_JOB_HELP",
    )


class BusSettings(_Block):
    """Bus settings (``engine.bus``).

    Implements:
        CORE-27: Core spec 0.4, §5.3 Routing rules.
    """

    queue_size: int = Field(
        default=1000,
        ge=1,
        title="CFG_ENGINE_BUS_QUEUE_SIZE",
        description="CFG_ENGINE_BUS_QUEUE_SIZE_HELP",
    )


class ServerSettings(_Block):
    """Mirror display listener (``engine.server``): shell and ``/ws`` only.

    Attributes:
        host: Bind address.
        port: Port.
        allow: Sources other than loopback that may connect (CORE-61);
            applies live, for connections made afterwards.

    Implements:
        CORE-201: Core spec 0.21, §6.2 Rules.
        CORE-61: Core spec 0.11, §10.1 Security.
    """

    host: str = Field(
        default="127.0.0.1",
        title="CFG_ENGINE_SERVER_HOST",
        description="CFG_ENGINE_SERVER_HOST_HELP",
    )
    port: int = Field(
        default=8080,
        ge=1,
        le=65535,
        title="CFG_ENGINE_SERVER_PORT",
        description="CFG_ENGINE_SERVER_PORT_HELP",
    )
    allow: list[IPvAnyNetwork] = Field(
        default_factory=list,
        title="CFG_ENGINE_SERVER_ALLOW",
        description="CFG_ENGINE_SERVER_ALLOW_HELP",
    )


class AdminSettings(_Block):
    """Admin listener (``engine.admin``): admin UI and REST API.

    There is no token key: admin tokens live in the token store, never in
    the configuration (CORE-62).

    Attributes:
        host: Bind address.
        port: Port.
        allow: Sources other than loopback that may connect (CORE-61).

    Implements:
        CORE-201: Core spec 0.21, §6.2 Rules.
        CORE-62: Core spec 0.11, §10.1 Security.
    """

    host: str = Field(
        default="127.0.0.1", title="CFG_ENGINE_ADMIN_HOST", description="CFG_ENGINE_ADMIN_HOST_HELP"
    )
    port: int = Field(
        default=8081,
        ge=1,
        le=65535,
        title="CFG_ENGINE_ADMIN_PORT",
        description="CFG_ENGINE_ADMIN_PORT_HELP",
    )
    allow: list[IPvAnyNetwork] = Field(
        default_factory=list,
        title="CFG_ENGINE_ADMIN_ALLOW",
        description="CFG_ENGINE_ADMIN_ALLOW_HELP",
    )


def _hostname() -> str:
    """The host name, lower-cased (CORE-338)."""
    return socket.gethostname().lower()


def _node_from(base_topic: str) -> str:
    """Derive ``node_id`` from ``base_topic``: lower-cased, other characters ``_`` (CORE-338)."""
    node = re.sub(r"[^a-z0-9_]", "_", base_topic.lower())
    return node[:64]


class MqttSettings(_Block):
    """MQTT bridge (``engine.mqtt``, core spec 9.3); absent means the bridge is off.

    Attributes:
        url: ``mqtt://host[:port]`` (default port 1883) or ``mqtts://host[:port]``
            (default 8883).
        username: Broker user name.
        password: Broker password, a secret field.
        ca: PEM file, relative to the directory of the main configuration
            file, that replaces the system CA store for this connection.
        client_id: MQTT client id; default ``mm3-<hostname>``.
        keepalive: Keepalive in seconds, 10 to 3600.
        base_topic: Root of the engine's topics; default ``mm3/<hostname>``.
        export: Globs of notification names published to ``<base>/event/``.
        import_: Globs of notification names accepted from ``<base>/notify/``
            (``import`` in the file).
        ha_discovery: Publish Home Assistant discovery configs.
        discovery_prefix: Home Assistant's discovery prefix.
        node_id: Id of the Home Assistant device; derived from ``base_topic``.

    Implements:
        CORE-338: Core spec 0.34, §9.3 MQTT bridge.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    url: str = Field(title="CFG_ENGINE_MQTT_URL", description="CFG_ENGINE_MQTT_URL_HELP")
    username: str | None = Field(
        default=None, title="CFG_ENGINE_MQTT_USERNAME", description="CFG_ENGINE_MQTT_USERNAME_HELP"
    )
    password: str | None = Field(
        default=None,
        repr=False,
        json_schema_extra={"secret": True},
        title="CFG_ENGINE_MQTT_PASSWORD",
        description="CFG_ENGINE_MQTT_PASSWORD_HELP",
    )
    ca: str | None = Field(
        default=None,
        min_length=1,
        title="CFG_ENGINE_MQTT_CA",
        description="CFG_ENGINE_MQTT_CA_HELP",
    )
    client_id: str = Field(
        default_factory=lambda: f"mm3-{_hostname()}",
        min_length=1,
        title="CFG_ENGINE_MQTT_CLIENT_ID",
        description="CFG_ENGINE_MQTT_CLIENT_ID_HELP",
    )
    keepalive: int = Field(
        default=60,
        ge=10,
        le=3600,
        title="CFG_ENGINE_MQTT_KEEPALIVE",
        description="CFG_ENGINE_MQTT_KEEPALIVE_HELP",
    )
    base_topic: str = Field(
        default_factory=lambda: f"mm3/{_hostname()}",
        min_length=1,
        title="CFG_ENGINE_MQTT_BASE_TOPIC",
        description="CFG_ENGINE_MQTT_BASE_TOPIC_HELP",
    )
    export: list[str] = Field(
        default_factory=list,
        title="CFG_ENGINE_MQTT_EXPORT",
        description="CFG_ENGINE_MQTT_EXPORT_HELP",
    )
    import_: list[str] = Field(
        default_factory=list,
        alias="import",
        title="CFG_ENGINE_MQTT_IMPORT",
        description="CFG_ENGINE_MQTT_IMPORT_HELP",
    )
    ha_discovery: bool = Field(
        default=False,
        title="CFG_ENGINE_MQTT_HA_DISCOVERY",
        description="CFG_ENGINE_MQTT_HA_DISCOVERY_HELP",
    )
    discovery_prefix: str = Field(
        default="homeassistant",
        min_length=1,
        title="CFG_ENGINE_MQTT_DISCOVERY_PREFIX",
        description="CFG_ENGINE_MQTT_DISCOVERY_PREFIX_HELP",
    )
    node_id: str = Field(
        default="",
        pattern=r"^([a-z0-9_]{1,64})?$",
        title="CFG_ENGINE_MQTT_NODE_ID",
        description="CFG_ENGINE_MQTT_NODE_ID_HELP",
    )

    @model_validator(mode="before")
    @classmethod
    def _default_node(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("node_id"):
            base = data.get("base_topic")
            if not isinstance(base, str) or not base:
                base = f"mm3/{_hostname()}"
            data = {**data, "node_id": _node_from(base)}
        return data

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        parts = urlsplit(value)
        problem = None
        if parts.scheme not in ("mqtt", "mqtts"):
            problem = "the scheme must be mqtt or mqtts"
        elif not parts.hostname:
            problem = "the host is missing"
        elif parts.path not in ("", "/") or parts.query or parts.fragment:
            problem = "use mqtt[s]://host[:port] only"
        elif parts.username or parts.password:
            problem = "put the credentials in username and password"
        else:
            try:
                port = parts.port
            except ValueError:
                port = 0
            if port is not None and not 1 <= port <= 65535:
                problem = "the port must be between 1 and 65535"
        if problem:
            raise ValueError(problem)
        return value

    @field_validator("base_topic", "discovery_prefix")
    @classmethod
    def _check_topic(cls, value: str) -> str:
        if "+" in value or "#" in value:
            raise ValueError("a topic prefix has no + or #")
        if value.startswith("/") or value.endswith("/"):
            raise ValueError("a topic prefix has no leading or trailing /")
        return value

    @property
    def host(self) -> str:
        """Broker host name."""
        return urlsplit(self.url).hostname or ""

    @property
    def tls(self) -> bool:
        """True for ``mqtts``."""
        return urlsplit(self.url).scheme == "mqtts"

    @property
    def port(self) -> int:
        """Broker port: the one in the URL, else 1883 (``mqtt``) or 8883 (``mqtts``)."""
        port = urlsplit(self.url).port
        return port if port is not None else (8883 if self.tls else 1883)


class HistorySettings(_Block):
    """Configuration history (``engine.history``, core spec 6.8)."""

    keep: int = Field(
        default=200,
        ge=10,
        title="CFG_ENGINE_HISTORY_KEEP",
        description="CFG_ENGINE_HISTORY_KEEP_HELP",
    )


class InputSettings(_Block):
    """Kiosk input (``engine.input``)."""

    touch: bool = Field(
        default=False, title="CFG_ENGINE_INPUT_TOUCH", description="CFG_ENGINE_INPUT_TOUCH_HELP"
    )


class LocationSettings(_Block):
    """``engine.location``: the mirror's place for modules that need one (CORE-288).

    Attributes:
        lat: Latitude, -90 to 90.
        lon: Longitude, -180 to 180.
        name: Display name.

    Implements:
        CORE-288: Core spec 0.19, §6.2 Rules.
    """

    lat: float = Field(
        ge=-90, le=90, title="CFG_ENGINE_LOCATION_LAT", description="CFG_ENGINE_LOCATION_LAT_HELP"
    )
    lon: float = Field(
        ge=-180, le=180, title="CFG_ENGINE_LOCATION_LON", description="CFG_ENGINE_LOCATION_LON_HELP"
    )
    name: LocalizedText | None = Field(
        default=None, title="CFG_ENGINE_LOCATION_NAME", description="CFG_ENGINE_LOCATION_NAME_HELP"
    )


class EngineSettings(_Block):
    """The ``engine`` block (CORE-201).

    Attributes:
        locale: Active locale.
        timezone: IANA zone; default the system's, else UTC.
        units: Unit system.
        location: The mirror's place, ``None`` when not set (CORE-288).
        server: Mirror display listener.
        admin: Admin listener.
        mqtt: MQTT bridge, ``None`` when off.
        timeouts: Time budgets.
        bus: Bus settings.
        threads: Workers of the shared ``run_blocking`` pool (CORE-197).
        log_level: Default log level (CORE-110).
        debug_ttl: How long debug logging stays on.
        history: Configuration history settings.
        input: Kiosk input settings.

    Implements:
        CORE-201: Core spec 0.21, §6.2 Rules.
        CORE-288: Core spec 0.19, §6.2 Rules.
    """

    locale: LocaleTag = Field(
        default="en", title="CFG_ENGINE_LOCALE", description="CFG_ENGINE_LOCALE_HELP"
    )
    timezone: str = Field(
        default_factory=system_timezone,
        title="CFG_ENGINE_TIMEZONE",
        description="CFG_ENGINE_TIMEZONE_HELP",
    )
    units: Literal["metric", "imperial"] = Field(
        default="metric", title="CFG_ENGINE_UNITS", description="CFG_ENGINE_UNITS_HELP"
    )
    location: LocationSettings | None = Field(
        default=None, title="CFG_ENGINE_LOCATION", description="CFG_ENGINE_LOCATION_HELP"
    )
    server: ServerSettings = Field(
        default=ServerSettings(), title="CFG_ENGINE_SERVER", description="CFG_ENGINE_SERVER_HELP"
    )
    admin: AdminSettings = Field(
        default=AdminSettings(), title="CFG_ENGINE_ADMIN", description="CFG_ENGINE_ADMIN_HELP"
    )
    mqtt: MqttSettings | None = Field(
        default=None, title="CFG_ENGINE_MQTT", description="CFG_ENGINE_MQTT_HELP"
    )
    timeouts: Timeouts = Field(
        default=Timeouts(), title="CFG_ENGINE_TIMEOUTS", description="CFG_ENGINE_TIMEOUTS_HELP"
    )
    bus: BusSettings = Field(
        default=BusSettings(), title="CFG_ENGINE_BUS", description="CFG_ENGINE_BUS_HELP"
    )
    threads: int = Field(
        default=8, ge=1, title="CFG_ENGINE_THREADS", description="CFG_ENGINE_THREADS_HELP"
    )
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info", title="CFG_ENGINE_LOG_LEVEL", description="CFG_ENGINE_LOG_LEVEL_HELP"
    )
    debug_ttl: timedelta = Field(
        default=timedelta(minutes=30),
        title="CFG_ENGINE_DEBUG_TTL",
        description="CFG_ENGINE_DEBUG_TTL_HELP",
    )
    history: HistorySettings = Field(
        default=HistorySettings(), title="CFG_ENGINE_HISTORY", description="CFG_ENGINE_HISTORY_HELP"
    )
    input: InputSettings = Field(
        default=InputSettings(), title="CFG_ENGINE_INPUT", description="CFG_ENGINE_INPUT_HELP"
    )

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PydanticCustomError(
                "CORE_CFG_TIMEZONE", "unknown time zone", {"value": value}
            ) from exc
        return value
