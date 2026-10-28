# MagicMirror 3 — Core Engine Specification

Oct 27, 2026 · @Nicola Reina · Version 0.10 (draft)

## 1. Scope and requirements baseline

The core engine (package `mm3-core`) is a Python 3.11 asyncio server that loads modules, routes notifications between them, and serves a browser shell that a Chromium kiosk displays. It keeps the MagicMirror² concepts (regions, notifications, module lifecycle, helper/front-end split) but exposes a clean Python API; MM² modules are not run directly.

Keywords MUST, SHOULD, MAY follow RFC 2119. Requirement ids (`CORE-xx`) are referenced by tests.

**Versioning.** This document is versioned `MAJOR.MINOR`; the current version is in the byline and every change is listed in section 13. Any requirement added, changed or removed bumps MINOR; 1.0 is the version the first engine release implements. Code cites the requirement ids and the version it implements (section 10.3, CORE-74).

### 1.1 Decisions taken

| Topic | Decision |
| --- | --- |
| Target | Debian 12+ on x86-64, Python ≥ 3.11 |
| Rendering | Web server + Chromium kiosk; any LAN browser MAY be an extra client |
| Module shape | Python backend (optional) + JavaScript Web Component front-end (optional) |
| MM² compatibility | Concepts and naming; migration tool mm3 migrate for config and modules (10.4) |
| Config | YAML, edited as a file or in the admin web editor, both validated by Pydantic v2; hot reload |
| Discovery | pip-installable packages, `importlib.metadata` entry points (group `mm3.modules`) |
| Front-end | Vanilla Custom Elements, no build step |
| Isolation | In-process asyncio; faulty modules are contained and restarted |
| i18n | Babel/gettext `.po` per module, compiled to JSON for the browser; EN, IT, FR, ES, DE shipped |
| Core services | Hot reload, remote-control API, admin web UI with validated YAML editor, MQTT bus with Home Assistant discovery. Presence & power is a module (mm3-presence) |
| Deployment | systemd + virtualenv; LAN-only by default; token auth on remote API |
| Layout | Configurable cell grid (default 12 × 12); position, size, layer and appearance per instance; MM² region names kept as presets (6.5) |
| Custom widgets | No-code widget builder module mm3-widget: MQTT, HA, bus and HTTP sources; extensible elements; touch actions; templates (11) |
| Kiosk display | Wayland with cage + Chromium by default; wlr-randr for power (X11 driver remains available in mm3-presence) |
| Module distribution | Public PyPI, packages named mm3-\* |
| Module catalog | Signed index on GitHub listing PyPI packages; automatic checks, badges, API compatibility, signed packages; install via CLI or admin UI through the installer service (10.6, 10.7) |
| Repositories | GitHub organization mm3-project: mm3-core (engine, CLI, tooling, default modules) and module-index (catalog); third-party modules in their own repos; MIT license |
| Display | display block: output, mode in pixels, refresh, rotation, scale; applied by the kiosk; logical viewport drives grid and validation (6.7) |
| Undo / redo | In the editor and across saved versions; per section or whole config; append-only history (6.8) |
| Backup / restore | Config, module data, module list, secrets; manual, scheduled and remote; encrypted; selective restore with rollback (10.8) |
| Dependencies | Bounded ranges + uv.lock; Renovate automerges tested patch/minor, majors reviewed; canary, deprecation-as-error, adapters (10.9) |
| Scheduler | In-house asyncio scheduler (interval, cron, one-shot), no dependency; APScheduler dropped (3.3) |
| Bus and supervisor | Per-instance bounded inbox handled in order; trace id in the envelope; a restart builds a new instance; handler timeouts fail an instance only when repeated (5.3, 7) |
| Config loading and history | !include replaces a node inside the config directory; !env from /etc/mm3/mm3.env over the process environment, re-read on reload; explicit ids for modules used more than once; presets stack like MM² regions; history keeps the whole config set; CLI and engine share one lock (6.2, 6.3, 6.5, 6.8) |
| Requests between modules | ctx.request / on\_request with replies on the bus; registry of shared notification names, validated by the engine (5.4, 5.5) |
| Pages and visibility | Pages in the core: own layout per page, all-pages instances, rotation, notifications, swipe, HA select; one set of visibility rules; runtime stop and start (6.9) |
| Requirements traceability | Specs synced to the repository with changed\_in per requirement; review only of code citing changed requirements; coverage matrix; issues for closed milestones (10.3) |

### 1.2 Approved frameworks (27 Oct 2026)

| Need | Proposal | Why |
| --- | --- | --- |
| HTTP + WebSocket | FastAPI on Uvicorn (ASGI) | De-facto async Python web stack, OpenAPI docs for free |
| Schemas & config | Pydantic v2 + ruamel.yaml | Typed validation; ruamel keeps comments when the admin page writes YAML |
| File watching | watchfiles | Rust-backed, used by Uvicorn itself |
| Scheduling | In-house asyncio scheduler, mm3.core.services.scheduler (3.3) | Only in-memory timers are needed; replaces APScheduler 4 (alpha, unfixed CVE-2026-31072) with no new dependency (decided 27 Oct) |
| MQTT | aiomqtt (paho-mqtt based) | Maintained asyncio MQTT client |
| i18n | Babel | Standard gettext tooling, CLDR plural and date formats |
| HTTP client for modules | httpx | Async, shared pooled client provided by core |
| Logging | stdlib logging + structlog | JSON logs to journald |
| Packaging | pyproject.toml, hatchling build backend | PEP 621 standard |
| Testing & docs | pytest + pytest-asyncio, Playwright for UI; MkDocs Material + mkdocstrings | Docstrings render to reference docs |
| Quality | ruff, mypy --strict | Lint and typing gate in CI |
| Admin UI | CodeMirror 6, gridstack.js, in-house schema form renderer | YAML editing with schema lint; grid drag/resize; no build step |
| Widgets | Jinja2 (sandboxed), uPlot, Material Design Icons | Safe expressions; small fast charts; same icon set as Home Assistant |
| MM² migration | esprima (Python port) | Parses config.js without executing it |
| Package provenance | PyPI Trusted Publishing + PEP 740 attestations (Sigstore) | PyPI's own signing standard; no keys for maintainers to manage |
| Installer re-auth | argon2-cffi, pyotp | Passphrase hashing; standard TOTP codes for authenticator apps |
| Backup encryption | age (Debian package age), invoked as a subprocess | Standard, audited format; passphrase and public-key modes, so scheduled backups need no secret on the mirror |
| Remote backup copy | rclone (Debian package), invoked as a subprocess | One tool for SFTP, SMB, WebDAV, S3 and 70+ other targets |
| Dependency management | uv (lockfile), Renovate (update bot), pip-audit (vulnerability scan) | Reproducible installs with hashes; tested update PRs incl. vendored JS; known-CVE gate |

### 1.3 Non-goals for v1

- Running MM² modules unchanged; they are converted instead (10.4).
- Multi-mirror orchestration from one server.
- Out-of-process module sandboxing.

## 2. Architecture overview

One Python process hosts the engine and all module backends; browsers are thin clients over one WebSocket, and MQTT is just another bus endpoint.

&#91;embedded content: core engine architecture · 8 subsystems, 1 bus\]

Browsers never talk to modules directly: every frame becomes a `Notification` on the bus, and the Supervisor hands each backend a `ModuleContext` as its only door into the engine.

## 3. Engine components

The engine is eight subsystems wired by one `Engine` object; modules see only the `ModuleContext` facade (section 4), never the subsystems directly.

| Subsystem | Python package | Responsibility | Req. |
| --- | --- | --- | --- |
| Bootstrap | `mm3.core.app` | Parse CLI, load config, build subsystems, start Uvicorn, handle SIGTERM/SIGHUP | CORE-01 |
| Config Manager | `mm3.core.config` | Load YAML, merge defaults, validate engine + per-module schemas, watch file, emit diffs | CORE-02 |
| Module Registry | `mm3.core.registry` | Discover entry points, check API version compatibility, instantiate and own module instances | CORE-03 |
| Lifecycle Supervisor | `mm3.core.supervisor` | Drive state machine (section 7), isolate exceptions, restart with backoff | CORE-04 |
| Event Bus | `mm3.core.bus` | In-process pub/sub for notifications; routing to backends, browsers, external bridges | CORE-05 |
| Web Gateway | `mm3.core.web` | FastAPI app: shell page, static module assets, `/ws` client channel, REST API, admin UI | CORE-06 |
| i18n Service | `mm3.core.i18n` | Resolve locale chain, load catalogs, serve merged JSON per module | CORE-07 |
| Services | `mm3.core.services.*` | Built-in: scheduler, HTTP client, storage, MQTT bridge, generic power hooks | CORE-08 |

### 3.1 Shared services offered to modules

- **Scheduler**: interval/cron jobs, cancelled automatically when the module stops.
- **HTTP client**: an MM3 `HttpClient` (backed by one pooled httpx client, Module Contract 4.3) with timeouts and a per-module User-Agent; external hosts need the `http.outbound` capability and an `http` grant (Contract section 8).
- **Storage**: per-instance data directory `<state>/modules/<instance>/`, where `<state>` is `$STATE_DIRECTORY` (set by systemd, `/var/lib/mm3`) or else `$XDG_STATE_HOME/mm3`, plus a JSON key-value store with async `kv_get`, `kv_set` and `kv_delete`: one file per instance, atomic writes, at most 1 MiB. Data is kept when the instance is removed (CORE-198).
- **Logger**: `structlog` logger pre-bound with `module` and `instance` fields.
- **Translator**: `ctx.t("key", **vars)` with the active locale.

### 3.2 Concurrency rules

- One asyncio event loop. Module code MUST NOT block it; CPU or blocking I/O goes through `ctx.run_blocking(fn, ...)` (CORE-197).
- Each module instance runs in its own `asyncio.TaskGroup`; an unhandled exception cancels only that group.
- Each instance handles notifications and client messages one at a time, in arrival order (CORE-28); jobs and tasks created through `ctx` run alongside.
- All time budgets come from `engine.timeouts` (seconds): `start` 15, `stop` 10, `suspend` 5 (also used for `resume`), `reconfigure` 5, `handler` 5, `job` 60. `snapshot()` has a 50 ms budget that is measured and logged, not enforced. CORE-192 and CORE-193 say what happens when a budget is exceeded.

### 3.3 Scheduler

The scheduler is part of the engine, with no third-party dependency: in-memory interval, cron and one-shot jobs on the event loop, owned by the instance that created them (CORE-180).

```python
class Scheduler(Protocol):
    def every(self, interval: timedelta | float, fn: JobFn, *,
              start_now: bool = False, jitter: float = 0.0,
              pause_on_suspend: bool = False, name: str | None = None) -> Job: ...
    def cron(self, expr: str, fn: JobFn, *, timezone: str | None = None,
             pause_on_suspend: bool = False, name: str | None = None) -> Job: ...
    def at(self, when: datetime, fn: JobFn, *, timezone: str | None = None, name: str | None = None) -> Job: ...

class Job(Protocol):
    name: str
    next_run: datetime | None        # aware, UTC; None when finished or cancelled
    def cancel(self) -> None: ...

JobFn = Callable[[], Awaitable[None]]
```

**Cron expressions**

- CORE-181: Five fields, minute hour day-of-month month day-of-week, with `*`, lists, ranges, steps (`*/15`, `1-5`, `8-18/2`) and names (`jan`–`dec`, `sun`–`sat`; Sunday is 0 or 7). Macros: `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`. When both day fields are restricted, a match on either one fires (classic cron behaviour). An expression that can never match (for example 0 0 30 2 \*) is invalid. Invalid expressions in configuration are rejected at validation through the CronExpression type (CORE-188).
- CORE-182: Cron and `at` use wall-clock time in `engine.timezone` (or the job's `timezone`). Across DST changes, a time that does not exist (spring forward) runs once at the next valid instant; a time that occurs twice (fall back) runs once, at the first occurrence.

**Execution**

- CORE-183: Intervals use CLOCK\_BOOTTIME, a monotonic clock that keeps counting during host suspend, so an overdue interval job runs once right after wake. Wall-clock jobs are re-planned after a clock jump larger than 2 s (for example NTP sync after boot on a machine without RTC) and after host suspend/resume.
- CORE-184: A job never overlaps itself. If a run is still going or the loop was busy, missed runs collapse into one run as soon as possible; the next one is planned from that point.
- CORE-185: Each run is a task in the instance's task group, with the job timeout `engine.timeouts.job` (default 60 s) and its own trace id. `TransientError` is logged as a warning and the job continues; any other exception moves the instance to FAILED (section 7). A run that exceeds the timeout is cancelled and logged, and the instance stays RUNNING (CORE-193).
- CORE-186: With `pause_on_suspend=True` a job is paused while the instance is suspended and runs once on resume if a run was missed. All jobs are cancelled when the instance stops (MOD-22).
- `jitter` adds a random delay up to that many seconds to every interval run, including the first, to spread network requests across instances.
- `mm3.testing` replaces the clocks with a virtual clock (`h.advance()`), so tests run without waiting.

**Dates, configuration and reload**

- CORE-187: `at()` reads a naive `datetime` in the job's `timezone` or `engine.timezone`; an aware datetime is used as is. The job's timezone is the optional timezone argument of at(), as for cron(); a naive time that does not exist or occurs twice follows CORE-182. A time already past runs once as soon as possible.
- CORE-188: Configuration fields holding a cron expression use the MM3 type `CronExpression` (module `mm3.config`), validated when the config is loaded; `cron()` raises `ValueError` for an invalid string.
- CORE-189: When `engine.timezone` changes on reload, every wall-clock job is re-planned. Jobs live only in memory: runs missed while the engine was not running are not caught up.

## 4. Module contract

Every module is a Python distribution that declares one entry point; the engine talks to it only through `MirrorModule` (backend) and `<mm3-module>` (front-end). Module API version is `1.0`; the engine MUST refuse modules whose `api_version` major differs from its own, whose minor is higher than its own, or that cannot be parsed; such a module is DISABLED and the reason is shown in the admin (CORE-03, Contract MOD-90).

The complete, binding list of module obligations, with signatures, error semantics, capabilities, checklist and test kit, is the [MM3 Module Contract v1.0](https://claude.ai/artifact/AwhPKtk3ezmyix1NRmKZrR). This section is an overview; for module requirements the contract takes precedence.

### 4.1 Package layout

```text
mm3-weather/
├─ pyproject.toml          # [project.entry-points."mm3.modules"] weather = "mm3_weather:WeatherModule"
└─ src/mm3_weather/
   ├─ __init__.py           # WeatherModule(MirrorModule)
   ├─ config.py             # WeatherConfig(ModuleConfig)
   ├─ static/
   │  ├─ weather.js         # customElements.define("mm3-weather", ...)
   │  └─ weather.css
   └─ locales/
      ├─ en/LC_MESSAGES/messages.po
      ├─ it/ fr/ es/ de/ …
```

### 4.2 Backend interface (Python)

```python
class MirrorModule(abc.ABC):
    """Base class for every MM3 module backend."""

    # --- static metadata (class attributes) ---
    name: ClassVar[str]                    # unique id, e.g. "weather"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = ModuleConfig
    element: ClassVar[str | None] = None   # custom element tag, None = backend-only
    scripts: ClassVar[list[str]] = []      # paths under static/
    styles: ClassVar[list[str]] = []
    locales_dir: ClassVar[str | None] = "locales"

    def __init__(self, ctx: ModuleContext, config: ModuleConfig) -> None: ...

    # --- lifecycle hooks (all optional, all async) ---
    async def start(self) -> None: ...            # acquire resources, schedule jobs
    async def stop(self) -> None: ...             # release resources; MUST be idempotent
    async def suspend(self) -> None: ...          # module hidden / screen off
    async def resume(self) -> None: ...
    async def reconfigure(self, new: ModuleConfig) -> bool:
        """Return True if applied live; False asks the engine to restart the instance."""
        return False

    # --- messaging ---
    async def on_notification(self, n: Notification) -> None: ...   # broadcast bus
    async def on_client_message(self, msg: ClientMessage) -> None: ...  # from own front-end

    # --- state for new clients ---
    def snapshot(self) -> dict[str, Any]:
        """Last renderable state; sent to every browser that connects."""
        return {}
```

`ModuleContext` (injected, read-only). This table is an overview; the binding members and signatures, with all messaging calls async, are in Contract §4:

| Member | Purpose |
| --- | --- |
| `instance_id`, `position`, `locale` | Identity and placement of this instance |
| `send_notification(name, payload, target=None)` | Broadcast on the bus |
| `send_to_client(type, payload, client=None)` | Push to own front-end (all clients, or one) |
| `set_state(dict)` | Update snapshot and push it as `state` |
| `scheduler`, `http`, `storage`, `log`, `t()` | Shared services (3.1) |
| `run_blocking(fn, *a)` | Run blocking code in the thread pool |
| system.request\_suspend(reason), system.request\_resume(reason) | Ask the engine to suspend or resume all modules; needs capability system.power (9.2) |
| mqtt.publish(), mqtt.subscribe(), mqtt.ha\_entity() | Publish or subscribe on granted topics (CORE-100); register a Home Assistant discovery entity |
| router | FastAPI APIRouter mounted at /api/v1/modules/{id}/, behind the same token auth |

### 4.3 Front-end interface (JavaScript)

The shell loads each module's scripts as ES modules and creates one element per instance inside its region.

```js
import { MirrorElement, html } from "/core/mm3.js";

class WeatherElement extends MirrorElement {
  // called once with backend snapshot, then on every set_state()
  render(state) {
    return html`<div class="temp">${state.temp}°</div>
      <div>${this.t("FEELS_LIKE", { v: state.feels })}</div>`;
  }
  onMessage(type, payload) {}        // send_to_client() from backend
  onNotification(name, payload) {}   // bus notifications forwarded to browsers
  onSuspend() {} onResume() {}
}
customElements.define("mm3-weather", WeatherElement);
```

`MirrorElement` provides: `this.config` (public part of config), `this.instanceId`, `this.shadow` (open shadow root with module CSS adopted), `this.t(key, vars)`, `this.send(type, payload)` to the backend, `this.notify(name, payload)` to the bus, and `this.hide()/show()` with optional fade.

### 4.4 Contract rules

- CORE-10: A module MUST work with only one of the two halves present (backend-only or front-end-only).
- CORE-11: All user-visible text MUST come from translation catalogs; no literal strings in `render()`.
- CORE-12: Front-end code MUST NOT fetch external URLs; data comes from its backend (avoids CORS and leaks API keys).
- CORE-13: Config fields marked `secret=True` are never sent to browsers or the remote API.
- CORE-14: Every public class and method carries a Google-style docstring; mkdocstrings builds the reference.

## 5. Messaging protocol

All traffic uses one envelope, the same in Python, on the WebSocket and on MQTT, so a notification can cross any boundary unchanged (CORE-20).

### 5.1 Envelope

```python
class Notification(BaseModel, frozen=True):
    v: Literal[1] = 1                     # protocol version
    id: UUID                              # unique, for dedup
    trace: UUID                           # trace id (CORE-111); equals id when it starts a trace
    ts: datetime                          # UTC, set by sender
    name: str                             # UPPER_SNAKE, e.g. "CALENDAR_EVENTS"
    source: str                           # "<module>/<instance>", "core", "remote", "mqtt"
    target: str | None = None             # None = broadcast; else instance id
    expects_reply: bool = False           # request (5.4)
    reply_to: UUID | None = None          # on a reply: id of the request (5.4)
    payload: dict[str, Any] = {}
```

### 5.2 Channels

| Channel | Direction | Frame `kind` | Carries |
| --- | --- | --- | --- |
| Bus | any ↔ any | `notify` | Broadcast or targeted `Notification` |
| Module private | backend → own element | `client` | `send_to_client(type, payload)` |
| Module private | element → own backend | `client` | `this.send(type, payload)` |
| State | backend → element | `state` | Full snapshot on connect and on `set_state` |
| Control | core → shell | `control` | `show`, `hide`, `reload`, `locale`, `layout` |
| Hello | shell → core | `hello` | Client id, screen size, protocol version |
| Diagnostics | shell → core | log | Browser errors and forwarded console output (CORE-114) |

WebSocket frames are JSON: `{"kind": "notify", "instance": "weather_1", "data": {...}}`. Endpoint `/ws`; one connection per browser.

### 5.3 Routing rules

- CORE-21: Broadcasts reach every backend and every browser except both halves of the sender instance: its backend and its own elements.
- CORE-22: Names starting with `CORE_` are reserved; only the engine emits them (`CORE_READY`, `CORE_CONFIG_CHANGED`, `CORE_SUSPEND`, `CORE_RESUME`, `CORE_PRESENCE`, `CORE_LOCALE_CHANGED`, CORE\_DISPLAY\_CHANGED, CORE\_PAGE\_CHANGED, CORE\_CONFIG\_ERROR).
- CORE-23: Delivery order is preserved per sender; no global ordering guarantee.
- CORE-24: Delivery is at-most-once inside the process; each receiver has its own bounded queue (CORE-27).
- CORE-25: A module MAY declare `subscribes: ClassVar[set[str]]`; when set, only those names are delivered to it (glob patterns allowed, e.g. `CALENDAR_*`).
- CORE-26: Payloads MUST be JSON-serialisable and ≤ 256 KiB.
- CORE-27: Each receiving instance has its own inbox, bounded by `engine.bus.queue_size` (default 1000). When it is full the oldest entry is dropped, a warning is logged at most once a minute per instance, and the drop is counted in the health record (CORE-113).
- CORE-28: An instance handles the notifications and client messages in its inbox one at a time, in arrival order.
- CORE-29: A targeted notification goes to the target instance's backend and its elements and ignores `subscribes`; if the target is unknown or not running, it is dropped and logged at debug level. The `subscribes` filter (CORE-25) also applies to `CORE_*` names.

### 5.4 Request and reply

A module can ask another module, or whichever module answers, for data and await the reply; requests and replies are ordinary notifications on the bus (CORE-220).

- CORE-220: `ctx.request(name, payload=None, *, target=None, timeout=5.0) -> dict` sends a notification with `expects_reply: true` and waits for the first reply whose `reply_to` equals its id. With `target` only that instance is asked; without, the request is delivered like a broadcast (CORE-21, CORE-25) and the first reply wins; later replies are dropped and logged at debug level. `timeout` is 0.1–60 s; when it expires the call raises `RequestTimeoutError` (`mm3.errors`).
- CORE-221: A backend answers in `on_request(req) -> dict | None` (Contract §3.2), called in inbox order like other handlers (CORE-28) and with the handler budget. A dict is sent back as the reply: same name, `reply_to` = request id, `target` = requester, same trace. `None` means "not mine" and sends nothing. If `on_request` raises, an error reply `{error: {code, message_key}}` is sent, the requester gets `RemoteError`, and the exception counts as a handler failure (CORE-192). `code` is `transient` for `TransientError`, `configuration` for `ConfigurationError` and `internal` for any other exception; `message_key` is the exception's own key, or `REQUEST_FAILED` from the core catalog for `internal`. If `on_request` exceeds the handler budget it is cancelled as a handler timeout (CORE-193) and no reply is sent: the requester gets `RequestTimeoutError`. Requests are never passed to `on_notification`.
- CORE-222: Replies bypass the requester's inbox and `subscribes` and resolve the waiting call directly, so a module may await a request inside its own handler. A module SHOULD NOT send a request from `on_request`: two modules asking each other only resolve by timeout.
- CORE-223: Front-ends call `this.request(name, payload, {target, timeout})`, which returns a Promise; only backends answer. REST: `POST /api/v1/request` with `{name, payload, target, timeout}` returns the reply (source `remote`). Requests and replies obey the payload limit (CORE-26) and appear paired in the bus inspector (10.5).

### 5.5 Shared notification registry

Names used by more than one module are registered here with their payload, and the engine validates those payloads (CORE-224).

- CORE-224: The table below is the registry of shared notification names. A module that emits, consumes or answers a registered name MUST use its registered payload (Contract MOD-40). New shared names are added only by changing this table; module-specific names keep the module prefix.
- CORE-225: The engine validates the payload of every registered name when it is sent, against the JSON Schema published in `mm3.notifications`. An invalid payload raises `ValueError` to a module sender, is rejected with 422 on REST, and is dropped with a warning when it comes from MQTT. Unregistered names are not validated.
- CORE-226: Adding optional fields keeps a registered name; any other change creates a new name (for example `SHOW_ALERT_V2`), and the old one stays registered for the rest of the Module API major.

| Name | Kind | Payload | From → to |
| --- | --- | --- | --- |
| `PRESENCE_DETECTED`, `PRESENCE_CLEARED` | event | `{sensor: str, zone?: str}` | sensor modules → presence (9.2) |
| `SHOW_ALERT` | event | `{id?: str, title?: LocalizedText, message: LocalizedText, level: info \| warning \| critical, timeout_s?: number}` (0 = until hidden) | any → alert module |
| `HIDE_ALERT` | event | `{id: str}` | any → alert module |
| `PAGE_NEXT`, `PAGE_PREV` | command | `{}` | any → engine (6.9) |
| `PAGE_SET` | command | `{page: str}` | any → engine (6.9) |
| `CORE_PAGE_CHANGED` | event, engine only | `{page, index, count, pages: [{id, title}], reason}` | engine → any (6.9) |
| `CORE_CONFIG_CHANGED` | event, engine only | `{version: int, sections: [str]}` | engine → any (6.3) |
| `CORE_CONFIG_ERROR` | event, engine only | `{count: int, errors: [{file, line, path, code}]}` (the first 20 errors) | engine → any (6.3) |
| `CORE_LOCALE_CHANGED` | event, engine only | `{locale: str}` | engine → any, or one instance (CORE-245) |

`LocalizedText` in a payload is a string or a map from BCP 47 tags to strings (CORE-101).

## 6. Configuration

One file, `/etc/mm3/config.yaml` (override with `--config`), validated against the engine model plus each module's `config_model`; any error aborts startup with the YAML line number (CORE-30).

### 6.1 Example

```yaml
engine:
  locale: it            # BCP 47; fallback chain it → en
  timezone: Europe/Rome
  units: metric
  server:               # mirror display listener: shell + /ws only
    host: 127.0.0.1
    port: 8080
  admin:                # admin UI + REST API, never shown on the mirror
    host: 0.0.0.0
    port: 8081
    allow: ["192.168.1.0/24"]
    token: !env MM3_TOKEN
  mqtt:
    url: mqtt://broker.lan:1883
    base_topic: mm3/mirror1
    ha_discovery: true

display:                # see 6.7
  output: HDMI-A-1
  width: 1920
  height: 1080
  rotation: 90          # portrait mirror → viewport 1080 × 1920

layout:                 # see 6.5
  grid: { columns: 12, rows: 12 }
  gap: 16px
  padding: 32px

modules:
  - module: presence      # backend-only, no position
    config:
      sensor: mqtt
      topic: zigbee2mqtt/hall_pir
      off_after: 10m
      schedule: { off: "23:30", on: "06:45" }
      display: wlr-randr
  - module: clock
    position: { col: 1, row: 1, width: 4, height: 2 }
    appearance: { scale: 1.2 }
  - module: weather
    id: weather_home      # optional, default <module>_<n>
    position: top_right   # preset, resolved to grid cells
    appearance: { header: true, align: end }
    config:
      api_key: !env OWM_KEY   # secret=True in schema
      lat: 42.10
      lon: 12.18
  - module: calendar
    position: { col: 1, row: 9, width: 6, height: 4 }
    disabled: true
```

### 6.2 Rules

- CORE-31: `!include path` replaces the tagged node with the content of that YAML file; there is no merging (`modules: !include modules.yaml`). The path is relative to the including file and MUST resolve, after following symlinks, inside the directory of the main config file. Included files may use `!env` and `!include`; nesting is limited to 8 levels and a cycle is an error. Errors inside an included file cite that file and line. No code execution; `!env` is defined in CORE-200.
- CORE-32: Module config = module defaults ← YAML `config` block; unknown keys are errors (`extra="forbid"`).
- CORE-33: `position` is a grid rectangle or a region preset name, as defined in 6.5. It is required for modules with a front-end and an error for backend-only modules.

**Environment variables**

- CORE-200: `!env NAME` takes NAME from the env file `/etc/mm3/mm3.env` (override with `--env-file`), or else from the process environment; `!env [NAME, default]` adds a default. The env file holds `KEY=VALUE` lines with `#` comments and optional quotes, and no interpolation. It is read at start and on every reload, so a changed value takes effect on hot reload. A missing variable without a default is a validation error. Values are strings, converted by the field's type. A warning is logged if the file is readable by other users. The env file belongs to the backup part `secrets` (10.8) and never to the configuration history (6.8).

**Top-level and engine keys**

- CORE-201: The top-level keys are `config_version` (integer, default 1; a value above the engine's own is an error), `engine`, `display` (6.7), `layout` (6.5), `modules` and `backup` (10.8); any other key is an error. All `engine` keys are optional:

| Key | Values | Default | On hot reload (6.3) |
| --- | --- | --- | --- |
| `locale` | BCP 47 tag | `en` | live (`CORE_LOCALE_CHANGED`) |
| `timezone` | IANA name | system time zone, else UTC | live (CORE-189) |
| `units` | `metric`, `imperial` | `metric` | live; instances with a backend restart |
| `server.host`, `server.port` | address, port | `127.0.0.1`, `8080` | next engine restart |
| `admin.host`, `admin.port`, `admin.allow`, `admin.token` | address, port, CIDR list, secret | `127.0.0.1`, `8081`, `[]`, none | next engine restart |
| `mqtt` | see 9.3 | absent: bridge off | live (reconnect) |
| `timeouts.*` | seconds | as in 3.2 | live, for calls started afterwards |
| `bus.queue_size` | integer ≥ 1 | 1000 | live, for inboxes created afterwards |
| `threads` | integer ≥ 1 | 8 | next engine restart |
| `log_level` | `debug`, `info`, `warning`, `error` | `info` | live |
| `debug_ttl` | duration | 30 min | live |
| `history.keep` | integer ≥ 10 | 200 | live |
| `input.touch` | boolean | `false` | live (shell reload) |

**Module instances**

- CORE-202: Instance keys are `module` (required), `id`, `position`, `appearance` (6.5), `config`, `disabled` (default false), `locale` (BCP 47, overrides `engine.locale` for this instance, 8.2) and `grants` (Contract section 8); any other key is an error. `id` matches `[a-z][a-z0-9_]{0,63}` and is unique. A module used by a single instance gets the module name as default id; when a module is used by more than one instance, every one of them MUST have an explicit id, otherwise validation fails. `mm3 migrate` and the admin editor always write ids.
- CORE-203: An enabled instance of a module that is not installed is an error. An instance of an installed module that fails to import or fails the API check (CORE-03) is a warning: its config is not validated and the instance is DISABLED with that reason. An instance with `disabled: true` of a module that is not installed is kept and not validated, so `mm3 modules remove` can disable instances instead of deleting them (10.6).
- CORE-204: Instances with `disabled: true` of an installed module are validated in full, so enabling one never fails later; they are ignored by the overlap and stacking rules (6.5).
- CORE-205: Duration fields (`timedelta`) in the engine model and in every `ModuleConfig` accept a number of seconds, an ISO 8601 duration, or the compact form `30s`, `10m`, `1h30m`, `2d` (units ms, s, m, h, d).

**Errors and `mm3 check-config`**

- CORE-206: Validation collects every problem instead of stopping at the first. Each one is a record `{file, line, col, severity, path, code, params, message}`: severity is error or warning, `path` is a JSON Pointer into the resolved config (`/modules/2/config/lat`), `code` is an engine catalog key `CORE_CFG_*` (8.3) with `params`, and `message` is the English text. Pydantic error types are mapped to codes, so the admin shows messages in the user's language. Warnings never block loading; they are logged at start.
- CORE-207: `mm3 check-config [--config PATH] [--env-file PATH] [--strict] [--json]` runs the same validation as startup and the admin (CORE-36) against the installed modules, without starting anything. It prints one line per record (`file:line:col severity path message`) or JSON. Exit code 0 when there is no error (warnings are printed), 2 when there are errors, as at startup (7.2); `--strict` treats warnings as errors.

**Validation codes**

- CORE-243: Every record's `code` is one of the codes below, and the engine catalog (8.3) has an English message for each that uses its `params`. Pydantic error types map to codes as listed; any other Pydantic type maps to `CORE_CFG_INVALID`, and a `ValueError` raised by a validator in a `ModuleConfig` gives `CORE_CFG_INVALID` with its text as `reason`. Records about layout (6.5–6.9) carry `page` in `params` when a `pages` block exists. Adding, removing or renaming a code is a spec change.

| Code | Severity | Raised when | Params |
| --- | --- | --- | --- |
| `CORE_CFG_YAML_SYNTAX` | error | A file is not valid YAML | `reason` |
| `CORE_CFG_YAML_TAG` | error | A tag other than `!env` and `!include` | `tag` |
| `CORE_CFG_DUPLICATE_KEY` | error | A key repeated in one mapping | `key` |
| `CORE_CFG_FILE_UNREADABLE` | error | The main file or an included file is missing or unreadable | `file` |
| `CORE_CFG_INCLUDE_OUTSIDE` | error | An `!include` path resolves outside the config directory (CORE-31) | `path` |
| `CORE_CFG_INCLUDE_CYCLE` | error | An `!include` cycle | `path` |
| `CORE_CFG_INCLUDE_DEPTH` | error | More than 8 nested `!include` levels | `limit` |
| `CORE_CFG_ENV_MISSING` | error | `!env NAME` without a default and no value (CORE-200) | `name` |
| `CORE_CFG_ENV_FILE_SYNTAX` | error | An env file line that is not `KEY=VALUE`, a comment or blank; the value is never shown | — |
| `CORE_CFG_ENV_FILE_MODE` | warning | The env file is readable by other users | `mode` |
| `CORE_CFG_VERSION` | error | `config_version` above the engine's own (CORE-201) | `value`, `max` |
| `CORE_CFG_UNKNOWN_KEY` | error | Pydantic `extra_forbidden` | `key` |
| `CORE_CFG_REQUIRED` | error | Pydantic `missing` | — |
| `CORE_CFG_TYPE` | error | Pydantic `*_type` and `*_parsing` types | `expected` |
| `CORE_CFG_RANGE` | error | Pydantic `greater_than`, `greater_than_equal`, `less_than`, `less_than_equal`, `multiple_of` | the bound: `gt`, `ge`, `lt`, `le` or `multiple_of` |
| `CORE_CFG_LENGTH` | error | Pydantic `too_short`, `too_long`, `string_too_short`, `string_too_long` | `min`, `max` |
| `CORE_CFG_PATTERN` | error | Pydantic `string_pattern_mismatch` | `pattern` |
| `CORE_CFG_CHOICE` | error | Pydantic `literal_error` and `enum` | `allowed` |
| `CORE_CFG_DURATION` | error | Not a duration in a form of CORE-205 | `value` |
| `CORE_CFG_TIMEZONE` | error | Not an IANA time zone name | `value` |
| `CORE_CFG_LOCALE` | error | Not a valid BCP 47 tag | `value` |
| `CORE_CFG_CRON` | error | Invalid or never-matching cron expression (CORE-181, CORE-188) | `reason` |
| `CORE_CFG_LOCALIZED_NO_EN` | warning | A `LocalizedText` map without `en` (CORE-101) | — |
| `CORE_CFG_ID_DUPLICATE` | error | An instance id or page id used twice | `id` |
| `CORE_CFG_ID_REQUIRED` | error | A module used by more than one instance, and an instance without `id` (CORE-202) | `module` |
| `CORE_CFG_MODULE_MISSING` | error | An enabled instance of a module that is not installed (CORE-203) | `module` |
| `CORE_CFG_MODULE_UNAVAILABLE` | warning | The module fails to import or fails the API check; the instance is DISABLED (CORE-203) | `module`, `reason` |
| `CORE_CFG_POSITION_REQUIRED` | error | A module with a front-end and no `position` (CORE-33) | `module` |
| `CORE_CFG_POSITION_FORBIDDEN` | error | `position`, `positions` or `pages` on a backend-only module (CORE-33, CORE-231) | `key` |
| `CORE_CFG_OUT_OF_GRID` | error | A rectangle that does not fit inside the grid (CORE-91, CORE-94) | `columns`, `rows` |
| `CORE_CFG_PRESET_EMPTY` | error | A used preset that is empty on the configured grid (CORE-92) | `preset` |
| `CORE_CFG_OVERLAP` | error | Overlapping enabled instances on the same `z` (CORE-93) | `other` |
| `CORE_CFG_CELL_TOO_SMALL` | error | A grid cell smaller than 1 px (CORE-213) | `width`, `height` |
| `CORE_CFG_MIN_SIZE` | warning | An instance's area is below its `min_size_px` (CORE-96) | `width`, `height`, `min_width`, `min_height` |
| `CORE_CFG_CSS` | error | `appearance.css` breaks a limit of CORE-214 | `reason` |
| `CORE_CFG_PAGE_UNKNOWN` | error | A page id that is not in `pages.list` (CORE-230, CORE-231) | `page` |
| `CORE_CFG_DISPLAY_MODE` | error | `width` without `height`, or `refresh` without both (CORE-215) | — |
| `CORE_CFG_SECRETS_UNENCRYPTED` | error | `backup.parts` includes `secrets` without `encrypt_to` (CORE-244) | — |
| `CORE_CFG_INVALID` | error | Any other problem | `reason` |

### 6.3 Hot reload

Watchfiles detects a change, the new file is validated in full, then the engine computes a diff by instance id:

| Change | Action |
| --- | --- |
| Validation fails | Keep running config, log error, emit `CORE_CONFIG_ERROR` |
| Instance added / removed | Start / stop that instance only |
| Module `config` changed | Call `reconfigure()`; restart instance if it returns False |
| Instance module changed for the same id | Stop the old instance, start the new one |
| disabled toggled | Stop / start that instance |
| grants changed | Restart that instance |
| Instance locale changed | Update its ctx.locale, send it CORE\_LOCALE\_CHANGED and its catalogs, re-render |
| `position` or `appearance` changed | Front-end re-layout only (same for `layout` changes), backend untouched |
| `engine.locale` changed | Emit `CORE_LOCALE_CHANGED`, push new catalogs, re-render |
| Other engine keys | Live or at the next engine restart, as listed in CORE-201 |
| `engine.server`, `engine.admin` or `engine.threads` changed | Applied at the next engine restart; logged as a warning, the rest of the change is applied |
| display changed | Kiosk re-applies mode, rotation and scale live; emit CORE\_DISPLAY\_CHANGED, re-layout (6.7) |
| pages block changed | Re-layout; current page kept if it still exists, otherwise default (CORE-233) |
| Instance pages or positions changed | Front-end re-layout only, backend untouched |

- CORE-208: A reload starts on a change to the main file, an included file, the widget template directory or the env file (all watched), on SIGHUP, and after a history operation (6.8). It starts 500 ms after the last change event. Reloads run one at a time; changes that arrive during a reload cause exactly one more.
- CORE-209: An invalid configuration never replaces the running one, whatever started the reload. The engine keeps its current version, logs every record (CORE-206), emits `CORE_CONFIG_ERROR` with `{count, errors}` (file, line, path and code of the first 20) and reports `config: {version, disk_valid: false}` in `GET /api/v1/status`. Invalid files never become history versions.
- CORE-210: Once validation passes, the new version is recorded (6.8) and applied in this order: stop removed and replaced instances; call `reconfigure()` on instances whose config changed, restarting those that return False; start added instances; push layout and display changes to browsers. A DISABLED instance whose config changed is restarted (CORE-195). Then the engine emits `CORE_CONFIG_CHANGED` with `{version, sections}`.
- CORE-211: `ctx.locale`, `ctx.timezone` and `ctx.units` always hold the current values. Changes to `engine.locale`, `engine.timezone` or an instance `locale` are applied live; a change to `engine.units` restarts every running instance that has a backend.

- CORE-245: `CORE_LOCALE_CHANGED` carries `{locale}`. A change to `engine.locale` broadcasts it with the new `engine.locale`; a change to an instance's `locale` sends it only to that instance, with the instance's new locale. Modules read `ctx.locale` for the locale that applies to them.

### 6.4 Web configuration editor

The configuration can be edited through three synchronized views (layout, forms, YAML) at `/admin/config`, on the admin listener only; the mirror display never serves or shows it (CORE-34).

- **Two listeners.** `engine.server` (default 127.0.0.1:8080) serves only the shell, module assets and `/ws`; `/admin` and `/api` return 404 there. `engine.admin` (default 8081) serves admin UI and REST, guarded by the CIDR allow list and token (CORE-61, CORE-62).
- **Schema.** `GET /api/v1/config/schema` returns one JSON Schema generated from the engine model and every installed module's `config_model`, including the `!env` and `!include` tags and the region enum. All three views are driven by it.
- **Layout view.** A live, scaled preview of the real mirror with an editing overlay (6.6); module instances show their real content, snap to cells and can be moved, resized from their corners, layered, or dropped on a region preset, added from a catalog of installed modules, enabled/disabled, or deleted. Backend-only modules (e.g. presence) appear in a separate "Services" lane.
- **Form view.** Selecting a card opens a form generated from that module's schema: text, number with min/max, enum dropdowns, toggles, lists, nested objects, duration and time pickers, and a secret field that can hold a value or an `!env` reference. Field labels and help text are translation keys resolved from the module catalog, so forms appear in EN, IT, FR, ES, DE.
- **YAML view.** CodeMirror 6 with YAML language support, JSON-Schema linting and autocompletion, for advanced edits and for anything the forms cannot express.
- **Synchronization.** Layout and form edits are sent as JSON Patch operations; the server applies them to the ruamel.yaml document and returns the new YAML, so comments and key order survive GUI edits. Switching to the YAML view always shows the true file content.
- **Validation while editing.** Forms validate each field on change; the YAML view checks syntax and schema 300 ms after the last keystroke. Errors show inline on the field, on the layout card, and in the YAML gutter, with localized messages.
- **Authoritative validation.** `POST /api/v1/config/validate` runs the full engine validation (unknown module, duplicate instance id, missing env var, module-specific checks) and returns `[{line, col, severity, path, message}]`; `path` lets the GUI map errors to fields. Save is disabled while any error remains (CORE-35).
- **Save.** `PUT /api/v1/config` with `If-Match: <etag>`: the server validates again, shows a diff preview, writes atomically (temp file + rename), and records a new version in the configuration history with undo, redo and restore (6.8). Hot reload (6.3) then applies it.
- **Conflicts.** If the file changed on disk since it was loaded, save returns 409 with both versions so the user can merge.
- **Secrets.** `!env NAME` references are shown as written; literal secret values are masked and kept unchanged unless the user types a new one.
- **Front-end stack.** Admin UI built with the same no-build Web Components as the shell: an in-house schema-to-form renderer (`<mm3-schema-form>`), gridstack.js for grid drag, resize and snapping, CodeMirror 6 as a vendored ES-module bundle.
- CORE-36: File edits and GUI edits go through the same validator and produce identical runtime config; the GUI can never write a file that fails `mm3 check-config`.
- CORE-37: A module MAY add `json_schema_extra={"ui": {...}}` hints (widget, order, group, hidden) to its fields; without hints, the form is generated from types alone.

### 6.5 Screen layout

The screen is a grid whose size is set in config; each module instance occupies a rectangle of cells, and the 13 MM² region names survive only as presets (CORE-90).

```yaml
layout:
  grid: { columns: 12, rows: 12 }   # each 1–48
  gap: 16px
  padding: 32px
modules:
  - module: news
    position: { col: 1, row: 11, width: 12, height: 2, z: 1 }
    appearance:
      scale: 0.9
      align: center
      overflow: fit
```

**Position rules**

- CORE-91: `position` is either a preset name or `{col, row, width, height, z}`; values are 1-based whole cells, `z` defaults to 0, and the rectangle MUST fit inside the grid.
- CORE-92: Presets map to fixed fractions of the grid (preset table below). A fraction f becomes the cell boundary round(f × n) for n columns or rows, halves rounded up; thirds split the columns at 1/3 and 2/3. A used preset that comes out empty on the configured grid is a validation error. `fullscreen_below` and `fullscreen_above` cover the whole grid at z −100 and +100; every other preset has z 0.
- CORE-93: Two enabled instances on the same `z` with overlapping cells are a validation error, except instances placed on the same preset, which form a stack (CORE-212); a stack counts as one rectangle. Different `z` values may overlap.
- CORE-94: Changing the grid size never moves modules silently: out-of-bounds positions fail validation, and the GUI offers a "rescale proportionally" action that rewrites them.

**Preset table** (CORE-92)

| Preset | Rows (fraction of grid height) | Columns | On the default 12 × 12 grid |
| --- | --- | --- | --- |
| `top_bar` | 0 – 1/12 | all | row 1 |
| `top_left`, `top_center`, `top_right` | 1/12 – 1/3 | thirds | rows 2–4; columns 1–4, 5–8, 9–12 |
| `upper_third` | 1/3 – 5/12 | all | row 5 |
| `middle_center` | 5/12 – 7/12 | all | rows 6–7 |
| `lower_third` | 7/12 – 2/3 | all | row 8 |
| `bottom_left`, `bottom_center`, `bottom_right` | 2/3 – 11/12 | thirds | rows 9–11; columns 1–4, 5–8, 9–12 |
| `bottom_bar` | 11/12 – 1 | all | row 12 |
| `fullscreen_below`, `fullscreen_above` | 0 – 1 | all | whole grid, z −100 and +100 |

**Stacks, spacing and appearance checks**

- CORE-212: Enabled instances on the same preset form a stack in file order, as MM² regions do: the shell lays them out as a column inside the preset rectangle, each as tall as its content, separated by `layout.gap`, aligned to the top, or to the bottom for `bottom_*` and `bottom_bar`. Each element receives `onResize` with its own area. For `min_size_px` (CORE-96) the validator uses the preset width and the preset height divided by the number of stacked instances.
- CORE-213: `layout.gap` and `layout.padding` are pixel lengths (`16px` or a number), 0–500; defaults: gap 16px, padding 32px. A cell measures (viewport − 2 × padding − (n − 1) × gap) / n on each axis, as a fraction of a pixel; a cell smaller than 1 px is a validation error.
- CORE-214: `appearance.css` is at most 16 KiB and contains no `@import` and no `url()` other than `data:` URIs and relative paths to the module's own static files. `classes` entries match `[A-Za-z_][A-Za-z0-9_-]*`. `header` is false, true (the module's catalog key `HEADER`, or else the module name; Contract MOD-67) or a `LocalizedText`. `header` and `classes` exist only under `appearance`.

**Appearance (common to every instance)**

| Field | Values | Default |
| --- | --- | --- |
| `scale` | 0.5–3.0 (font and spacing multiplier) | 1.0 |
| `align`, `valign` | start, center, end | start |
| `opacity` | 0–1 | 1 |
| `overflow` | clip, fit (shrink to fit), scroll | clip |
| `header` | false, true (module's `HEADER` key, else the module name), or a `LocalizedText` | false |
| `classes` | list of CSS classes | \[\] |
| `css` | CSS scoped to the instance's shadow root (limits in CORE-214) | none |

**Rendering contract**

- The shell renders the grid with CSS Grid (`repeat(columns, 1fr)` × `repeat(rows, 1fr)`); each element gets `grid-area` and `z-index`, and appearance values become CSS custom properties (`--mm3-scale`, `--mm3-align`, …).
- CORE-95: `MirrorElement` calls `onResize(widthPx, heightPx)` whenever its cell area changes; modules SHOULD adapt (for example a compact mode) rather than overflow.
- CORE-96: A module MAY declare `min_size_px = (w, h)`; the layout view and validator warn when an instance's area at the configured resolution is smaller.

### 6.6 Live layout preview

The layout view shows the actual mirror, with real module content, scaled into the admin page; positions are edited directly on it and reach the real display only on Save (CORE-97).

- **Preview shell.** The admin listener serves the normal shell at `/admin/preview` in preview mode, embedded in an iframe. It loads the same Web Components and receives the same `state` frames as the kiosk, so no module backend does extra work or extra API calls.
- **Resolution.** The preview renders at the display's logical viewport (6.7), as reported by the kiosk in its `hello` frame, and with several kiosks connected the admin picks one. The iframe is scaled with a CSS transform to fit the admin window, keeping the aspect ratio.
- **Editing overlay.** A transparent layer above the iframe draws grid lines and a handle box per instance. Drag moves, corner handles resize, a toolbar sets layer, appearance and enable/disable. Everything snaps to whole cells; invalid placements (overlap on the same layer, outside the grid) turn red and cannot be dropped.
- **Draft.** Edits form a draft layout held in the admin page and applied to the preview through `control` / `layout` frames. The same draft is converted to JSON Patch for the YAML and form views (6.4), so all three views stay in sync before saving.
- **Show on mirror.** An optional toggle pushes the draft to the real display for checking from across the room; it reverts automatically after 60 s or when the admin page closes, unless saved.
- CORE-98: In preview mode `MirrorElement.preview` is true. Modules with side effects or sensitive content (camera, microphone, private calendars) SHOULD render a placeholder of the same size instead.
- CORE-99: Preview clients are not counted as displays: they never trigger presence, suspend or HA state, and they receive no `client` messages meant for the kiosk.

### 6.7 Display output

The physical display is configured in `display`: output, mode in pixels, rotation and scale. The kiosk applies it to the screen, and the engine derives the logical viewport that the grid, validation and preview use (CORE-140).

```yaml
display:
  output: HDMI-A-1   # optional; default: first connected output
  width: 1920        # monitor mode, horizontal pixels (as the monitor reports it)
  height: 1080       # monitor mode, vertical pixels
  refresh: 60        # optional, Hz
  rotation: 90       # 0 | 90 | 180 | 270, clockwise; 90/270 = portrait mirror
  scale: 1.0         # 0.5–3.0; e.g. 2.0 on a 4K panel
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `output` | string | first connected | Connector name as listed by `wlr-randr` (HDMI-A-1, DP-1, eDP-1) |
| `width`, `height` | int px, 320–7680 | monitor's preferred mode | Mode to set, in the monitor's native orientation |
| `refresh` | int Hz | highest for the mode | Refresh rate |
| `rotation` | 0, 90, 180, 270 | 0 | Clockwise rotation of the picture |
| `scale` | float | 1.0 | Device pixel ratio; enlarges everything uniformly |

The whole block is optional; without it the monitor's preferred mode is used, unrotated, at scale 1.

**Logical viewport**

- CORE-141: The viewport in CSS pixels is the mode with width and height swapped for rotation 90/270, divided by `scale`. Example: 1920 × 1080, rotation 90, scale 1.0 → viewport 1080 × 1920.
- CORE-142: The engine computes each grid cell's pixel size from the viewport, `layout.padding` and `layout.gap` (6.5). Validation uses it to check `min_size_px` (CORE-96); the admin shows each instance's size in pixels; the live preview (6.6) renders at exactly this viewport.
- CORE-215: Viewport sizes are whole CSS pixels, rounded down after dividing by `scale`. `width` and `height` are given together or not at all, and `refresh` needs both. Until a kiosk reports its applied viewport (CORE-144), and always in `mm3 check-config`, the engine uses the configured mode, or 1920 × 1080 when none is set, and states in the validation output that the viewport is assumed.

**Applying it**

- CORE-143: `mm3-kiosk.service` starts `mm3 kiosk`, which runs cage, applies output, mode, refresh, rotation and scale through the compositor's output management (`wlr-randr`), then starts Chromium. Display changes on hot reload are re-applied live; no restart of the kiosk.
- CORE-144: The kiosk reports the outputs, the modes each supports, and the applied viewport and device pixel ratio in its `hello` frame and after every change. The admin form offers `output` and the mode as dropdowns of these reported values.
- CORE-145: If the requested mode is not supported, the kiosk uses the closest supported mode (same aspect ratio first, then the largest not above the request). The mismatch is shown in the admin and the debug page, and the engine uses the viewport actually applied.
- CORE-146: When the applied viewport changes, the engine emits `CORE_DISPLAY_CHANGED` with `{viewport: [w, h], scale, rotation}`, and every element receives `onResize` (CORE-95).
- The `display` block describes the local kiosk only. Other LAN browsers showing the shell render at their own window size with the same grid.

### 6.8 Configuration history, undo and redo

Every change to the configuration can be undone and redone, both while editing and after saving, for one module or core section or for the whole file (CORE-150).

**Editor level (unsaved changes)**

- CORE-151: The admin editor keeps one undo/redo history for the current draft, shared by layout, form and YAML views: Ctrl+Z undo, Ctrl+Shift+Z or Ctrl+Y redo, plus toolbar buttons. Each step is a JSON Patch with a readable label ("Move clock to 1,1", "weather.lat 42.1 → 42.2"); at least 200 steps.
- Typing in the YAML view is grouped into one step per pause of 1 s or per view switch. The history is lost on page reload; the draft itself is kept in the browser until saved or discarded.
- CORE-152: Section undo: each module card, form and core section (`engine`, `display`, `layout`, `widget templates`) has "Undo last change here", which reverts only the latest step touching that section, leaving later steps elsewhere intact.

**Saved versions**

- CORE-153: Every valid saved configuration becomes a numbered version in `<state>/config-history/` (`<state>` as in 3.1), one directory per version. It holds the whole config set as it was (the main file, every included file and the widget templates, with their paths relative to the config directory) and `meta.json` with version, time (UTC), origin (initial, admin, file, cli, restore, undo, redo), author (admin client address, `cli:<user>` or `file`), parent version, changed sections and the SHA-256 of the set. The env file is never stored. When the history is empty, the engine records the running config as version 1 (origin initial). History is append-only; nothing is rewritten.
- CORE-216: The sections are `engine`, `display`, `layout`, pages, `backup`, each module instance (`modules/<id>`), the order of the modules list (`modules/_order`) and each widget template (`widget_templates/<name>`). Changed sections are computed on the resolved values before `!env` substitution; a change to comments or formatting only is a version with no changed sections.
- CORE-154: Undo and redo of saves. An ordinary version's parent is the version current when it was made; an undo or redo version takes the parent of the version whose content it restores. "Undo last save" creates a new version (origin undo) with the content of the current version's parent. "Redo" creates a new version (origin redo) with the content undone most recently; it is offered while the latest versions are undos and redos, and any other save ends the redo chain. Example: after saves v1–v3, undo gives v4 = v2, a second undo v5 = v1, redo v6 = v2, redo v7 = v3. If the target no longer validates, nothing is applied and no version is created (CORE-156). Hot reload (6.3) applies each new version.
- CORE-155: Section restore: from any version, a single section (one module instance by id, `engine`, `display`, `layout`, pages, `backup`, a widget template) can be restored alone; the rest of the current config stays as it is. The section is written back into the file that holds it now; an instance that no longer exists is appended to the `modules` list, in the file that holds that list. The result is a new version.
- The history view lists versions with their changed sections, shows the diff between any two versions as a whole or per section, and offers "Restore all", "Restore section" and "Open in editor".
- CORE-156: A restored version or section always goes through full validation. If it fails (for example a module no longer installed), nothing is applied and it can be opened in the editor to fix.
- CORE-217: The engine, the admin save and the `mm3 config` commands write config files and history under one lock (`<state>/config-history/.lock`, flock). Each file is written atomically (temp file + rename), keeping its owner and mode. When the watcher sees a config set whose SHA-256 equals the latest version's, it records no new version. `mm3 config` runs as the `mm3` user and works whether the engine runs or not; a running engine applies the result through its watcher. `mm3.service` therefore has write access to `/etc/mm3` and to its state directory (`ReadWritePaths`, CORE-65).
- CORE-218: Retention: a version is kept while it is among the newest `engine.history.keep` (default 200) or younger than 180 days; the current version is never removed. History directories are 0750 and files 0640, owned by `mm3`. In every view, the CLI and the API, `SecretStr` fields (CORE-13) are masked and `!env` references are shown as written.

**CLI**: `mm3 config history [--limit N] [--json]`, `mm3 config diff <a> <b> [--section <path>] [--json]`, `mm3 config undo`, `mm3 config redo`, `mm3 config restore <version> [--section <path>]`.

- CORE-219: Commands that create a version print its number. Exit codes: 0 done, 1 nothing to undo or redo, 2 validation failed and nothing applied (CORE-156), 3 refused because the files on disk differ from the current version and are invalid (CORE-209); with `--force` the on-disk edit is overwritten and lost.

### 6.9 Pages and visibility

Pages are alternative layouts of the same instances on one screen: the core shows one page at a time, and the visibility of every instance follows one set of rules (CORE-230).

```yaml
pages:
  default: home
  rotate: 30s              # 0 = no automatic rotation
  pause_on_interaction: 2m
  reset_on_resume: true
  transition: fade          # fade | none
  list:
    - id: home
      title: { en: Home, it: Casa }
    - id: energy
      title: { en: Energy, it: Energia }
      rotate: 15s           # per-page override
modules:
  - module: clock
    position: top_left      # pages: all is the default
  - module: weather
    id: weather_home
    pages: [home, energy]
    position: top_right
    positions:
      energy: { col: 1, row: 2, width: 6, height: 5 }
  - module: energy_flow
    pages: [energy]
    position: { col: 7, row: 2, width: 6, height: 9 }
```

**Pages**

- CORE-230: The top-level key `pages` is optional (it extends CORE-201). Without it the mirror has one implicit page `main`, and only the visibility rules (CORE-238–241) apply. Page ids match `[a-z][a-z0-9_]{0,31}` and are unique; at most 32 pages. `default` names a listed page (default: the first). `title` is a `LocalizedText` (default: the id).
- CORE-231: The instance key `pages` is `all` (default) or a list of page ids. The instance key `positions` maps page ids to a position (grid rectangle or preset) used on that page instead of `position`. Both are errors for backend-only modules. Both extend CORE-202.
- CORE-232: Layout validation (CORE-91–96, CORE-212, CORE-213) runs once per page, on the instances shown on that page with their position for that page; `pages: all` instances count on every page. Errors name the page.
- CORE-233: The engine keeps one current page for all displays. It changes on `PAGE_NEXT` and `PAGE_PREV` (cyclic, in list order), `PAGE_SET` (an unknown id is ignored with a warning), rotation, touch, REST and Home Assistant. At start the current page is `default`; on reload it is kept if it still exists, otherwise `default`. With `reset_on_resume: true` (default), `CORE_RESUME` returns to `default`.
- CORE-234: Rotation: when `rotate` is above 0 (per page, else global) the engine moves to the next page after that time. Any page change not made by rotation, and any touch on the kiosk, pauses rotation for `pause_on_interaction` (default 2 min). Rotation stops while the mirror is suspended.
- CORE-235: Touch: with `engine.input.touch`, a horizontal swipe across at least 25 % of the viewport width changes page (swipe left = next, right = previous). A swipe that starts on the element of a module with the `input.touch` capability goes to that element and does not change page.
- CORE-236: Control surfaces: REST `GET /api/v1/pages` (list and current page), `POST /api/v1/pages/{id}`, `/pages/next`, `/pages/prev`; a Home Assistant select entity "page" (options are the page titles, state is the current page) through MQTT discovery (9.3).
- CORE-237: After each change the engine emits `CORE_PAGE_CHANGED` (5.5; `reason` is rotate, notification, touch, rest, ha, resume, reload or start) and sends a `control` frame `page` to every shell, which switches with the configured transition (fade 300 ms, or none). `ctx.page` (current page id) and `ctx.pages` (ids and resolved titles) are always current; front-ends read `this.page`.

**Visibility**

- CORE-238: A running instance is visible when no hide reason applies. The reasons are `page` (not on the current page), `manual` (admin, REST `/hide`, Home Assistant visibility switch), `self` (its element called `this.hide()`) and `module:<instance_id>` (set by a module with `layout.control`, CORE-241). Each source clears only its own reason: `/show` clears `manual`, `this.show()` clears `self`. To show an instance on a page it is not on, change its `pages`.
- CORE-239: When an instance becomes hidden, its elements fade out and receive `onSuspend()` and its backend's `suspend()` is called; when it becomes visible again, `resume()` and `onResume()`. Page rotation causes the same calls. While the mirror is suspended (`CORE_SUSPEND`) every instance is suspended whatever its visibility. Preview clients (6.6) show the page selected in the admin and never change the current page or any visibility.
- CORE-240: Runtime stop and start: REST `/api/v1/modules/{id}/stop` and `/start`, the admin and the Home Assistant "enabled" switch stop or start an instance without changing the configuration (state STOPPED, reason runtime). This is not recorded in the history and is lost on engine restart, when the instance returns to its configured state; any config change to the instance clears it. The admin offers "Save as disabled", which writes `disabled: true` through the normal save (6.4).
- CORE-241: A module with the capability `layout.control` may call `ctx.layout.hide(instance_id)` and `ctx.layout.show(instance_id)` on other instances (reason `module:<own id>`). Hide reasons set by a module are cleared when that module stops. Without the capability a module can only change pages, through the notifications above.
- CORE-242: The admin layout view (6.6) has a page selector: position edits apply to the selected page, or to every page for `pages: all` instances, and the live preview renders the selected page.

## 7. Lifecycle and error handling

Each module instance follows one state machine owned by the Supervisor; a fault in one instance never stops the engine or other instances (CORE-04).

&#91;embedded content: module instance lifecycle · 8 states\]

Failures, retries and the DISABLED state follow the rules below; the state names are those in the diagram.

- CORE-190: The Supervisor owns one state machine per instance with the states LOADED, STARTING, RUNNING, SUSPENDED, STOPPING, STOPPED, FAILED and DISABLED.
- CORE-191: A restart builds a new instance: a new object (`__init__`) with a new `ModuleContext`. The old context is closed (`ContextClosedError`) and its tasks, jobs and subscriptions are cancelled (MOD-22). Browsers keep the last render until the new instance sets state.
- CORE-192: An exception in `start()`, `suspend()`, `resume()`, a handler, a `ctx` task or a job run moves the instance to FAILED, and so does `start()`, `suspend()` or `resume()` exceeding its budget. An exception or timeout in `reconfigure()` restarts the instance and counts as a failure. An exception or timeout in `stop()` is logged and the instance is treated as stopped. `TransientError` is logged as a warning and the instance stays RUNNING (a task that raised it ends). `ConfigurationError` moves the instance to DISABLED at once.
- CORE-193: A handler or job run that exceeds its budget is cancelled, logged as a warning and counted in the health record; the instance stays RUNNING. Three handler timeouts in a row move it to FAILED.
- CORE-194: A FAILED instance is restarted after 1, 2, 4 or 8 s, chosen by the number of failures in a sliding 10-minute window. The fifth failure in the window moves it to DISABLED. The window empties after 10 minutes without a failure.
- CORE-195: A DISABLED instance leaves that state only when its configuration changes or on a manual restart (`Supervisor.restart(instance_id)`; REST in 9.1).
- CORE-196: A SUSPENDED instance still receives notifications and client messages; its jobs keep running unless created with `pause_on_suspend` (CORE-186).
- CORE-197: `ctx.run_blocking` runs in one shared thread pool of `engine.threads` workers (default 8), with no per-call timeout; if the caller is cancelled, the result is discarded.

### 7.1 Engine startup and shutdown

1. Load and validate config; abort on error (CORE-30).
2. Discover entry points, import modules, check `api_version`; a bad module is DISABLED, the rest continue.
3. Start core services (bus, i18n, scheduler, MQTT).
4. Call `start()` on all instances concurrently, each bounded by `engine.timeouts.start` (default 15 s).
5. Start the Web Gateway, emit `CORE_READY`, notify systemd (`READY=1`).
6. On SIGTERM: emit `CORE_SHUTDOWN`, call `stop()` on all instances (10 s budget), close MQTT with offline state, exit 0. SIGHUP forces a config reload.

### 7.2 Error reporting

| Event | Logged | Visible to user | Bus |
| --- | --- | --- | --- |
| Config invalid at start | error + YAML line | console only, exit code 2 | — |
| Config invalid on reload | error | admin UI banner | `CORE_CONFIG_ERROR` |
| Module import / API mismatch | error | admin UI | `CORE_MODULE_FAILED` |
| Handler exception | exception + traceback | element shows localized "unavailable" placeholder | `CORE_MODULE_FAILED` |
| Browser disconnected | info | shell reconnects with backoff, keeps last render | — |

## 8. Internationalization

Every user-facing module ships EN, IT, FR, ES and DE catalogs; adding a language means dropping one more `.po` folder, with no code change (CORE-40).

### 8.1 Mechanism

1. Authors write message ids as stable keys (`FEELS_LIKE`), English text lives in `en/messages.po`.
2. `pybabel extract / update / compile` via a `mm3 i18n` CLI wrapper; `.mo` for Python, plus a generated `messages.json` per locale for the browser.
3. Engine endpoint `GET /i18n/{instance}.json` returns the merged catalog for the active chain.
4. `ctx.t()` and `this.t()` use the same keys, ICU-style `{var}` placeholders, and CLDR plural forms (`ngettext`).
5. Dates, numbers, units: Babel on the backend, `Intl.*` APIs in the browser, both fed `engine.locale` and `engine.timezone`.

### 8.2 Locale resolution

Chain = instance override (`modules[].locale`) → `engine.locale` → its base language (`de-CH` → `de`) → `en`. A missing key falls through the chain; a key missing even in `en` renders as the key itself and logs once.

### 8.3 Core catalogs

The engine has its own catalog for shell and admin UI strings (`CORE_*` keys). Third-party translations are accepted as separate packages registering entry point `mm3.locales`, so a community can add, for example, Portuguese without forking a module.

### 8.4 Quality gate

- CORE-41: CI fails if any shipped locale lacks a key present in `en`.
- CORE-42: CI fails if a placeholder set differs between `en` and a translation.

## 9. Built-in services

Remote control and the MQTT bridge are engine services; presence and power is a module that uses generic engine hooks. Everything is driven through ordinary bus notifications (CORE-50).

### 9.1 Remote-control API

REST under `/api/v1`, OpenAPI at `/api/docs`, admin single-page UI at `/admin` built from core Web Components.

| Method | Path | Effect |
| --- | --- | --- |
| GET | `/api/v1/status` | Engine version, uptime, suspend state, clients |
| GET | `/api/v1/modules` | Instances, state, position, health |
| POST | `/api/v1/modules/{id}/show` · `/hide` · `/restart` | Per-instance control |
| ANY | `/api/v1/modules/{id}/...` | Routes a module registers on `ctx.router` |
| GET | `/api/v1/config` · `/config/schema` | Raw YAML (secrets masked) · JSON Schema |
| POST | `/api/v1/config/validate` | Diagnostics with line and column, nothing written |
| PUT | `/api/v1/config` | Validate, back up and save; needs `If-Match` |
| POST | `/api/v1/notify` | Inject a `Notification` (source `remote`) |
| POST | `/api/v1/shell/reload` | Reload all browsers |
| GET | /api/v1/catalog · /catalog/{name} | Cached catalog index, module details, compatibility |
| POST | /api/v1/catalog/{name}/install · /update · /remove | Forwarded to the installer; needs install passphrase (+ TOTP); streams progress |
| POST | /api/v1/installer/rollback | Restore the previous venv; same re-authentication |
| GET / POST | /api/v1/config/history · /history/{a}/diff/{b} · /config/undo · /config/redo · /config/restore | Version list, diffs, undo/redo of saves, whole or section restore (6.8) |
| GET / POST | /api/v1/backups · /backups/create · /backups/{id}/download · /backups/restore | List, create, download, upload and restore (restore needs installer re-authentication) (10.8) |
| POST | /api/v1/modules/{id}/stop · /start | Runtime stop and start, not saved (CORE-240) |
| GET / POST | /api/v1/pages · /pages/{id} · /pages/next · /pages/prev | Pages and current page; change page (CORE-236) |
| POST | /api/v1/request | Send a request on the bus and return the reply (CORE-223) |

### 9.2 Presence and power: module boundary

Presence and power ships as the backend-only reference module `mm3-presence`; the engine keeps only the generic suspend/resume hooks, so presence can be replaced (camera, BLE, phone) without touching core.

- **In the module:** sensor inputs (MQTT topic, GPIO via extra `mm3-presence[gpio]`, `PRESENCE_DETECTED` from other modules), the timer `ON → IDLE → OFF`, schedule windows, and display drivers through its own entry point group `mm3_presence.display` (`xset dpms` or `wlr-randr`, `ddcutil` for brightness, `cec-client` for HDMI-CEC).
- **In the engine:** `ctx.system.request_suspend()` / `request_resume()`. On a request the engine emits `CORE_SUSPEND` or `CORE_RESUME` and calls `suspend()` / `resume()` on every instance.
- CORE-52: Only modules declaring `capabilities = {"system.power"}` may call these hooks; a request from any other module raises `PermissionError` and is logged.
- CORE-53: Several modules may hold power; the engine stays resumed while any holder requests resume (reference-counted by instance id).
- **Control and Home Assistant:** the module adds `POST /api/v1/modules/presence/display/{on,off}` via `ctx.router` and its display switch, brightness and presence sensor via `ctx.mqtt.ha_entity()`.

### 9.3 MQTT bridge

| Topic | Direction | Meaning |
| --- | --- | --- |
| `<base>/notify/<NAME>` | in | Publish a bus notification (payload JSON) |
| `<base>/event/<NAME>` | out | Notifications listed in `mqtt.export` (glob list) |
| `<base>/state` | out, retained | `online` / `offline` (Last Will) |
| `<base>/display/set` | in | `ON` / `OFF` |
| `<base>/presence` | out | `detected` / `clear` |

With `ha_discovery: true` the engine publishes `homeassistant/<component>/<node_id>/<object>/config` so Home Assistant creates one device with: a reload button, a page select (CORE-236), and per instance a visibility switch and an enabled switch (runtime stop and start, CORE-240); modules add their own entities through ctx.mqtt.ha\_entity() (mm3-presence adds the display switch, brightness and presence sensor). Echo loops are prevented by dropping inbound messages whose `source` is the bridge itself (CORE-51).

## 10. Security, packaging, deployment, quality

The server binds to `127.0.0.1` by default, and nothing beyond the shell itself is reachable without a token (CORE-60).

### 10.1 Security

- CORE-61: Exposure needs explicit `server.host` plus a CIDR `allow` list; requests from other sources get 403.
- CORE-62: `/api/*`, `/admin` and remote WebSocket commands require `Authorization: Bearer <token>`; tokens are stored as argon2 hashes, generated by `mm3 token new`.
- CORE-63: Shell responses send a strict Content-Security-Policy: scripts only from self, no inline script, `connect-src` self.
- CORE-64: Front-end templates MUST escape data; `MirrorElement` offers an `html` tagged template that escapes by default.
- CORE-65: Service runs as unprivileged user `mm3`; systemd hardening (`ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`).
- CORE-66: Only modules installed in the engine's venv load. The engine process itself never installs anything: installs go through the CLI or the separate installer service (10.7), from the signed catalog.

### 10.2 Packaging and deployment

| Artifact | Content |
| --- | --- |
| `mm3-core` (PyPI) | Engine, shell, admin UI, `mm3` CLI |
| `mm3-modules-default` | clock, calendar, weather, newsfeed, compliments, alert, presence, widget (each also separate) |
| `mm3-install.sh` | Creates `mm3` user, venv in `/opt/mm3`, config in `/etc/mm3`, systemd units |
| `mm3.service` | Engine (`Type=notify`, watchdog via sd\_notify) |
| `mm3-kiosk.service` | User unit running mm3 kiosk: applies the display block (6.7), then launches `chromium --kiosk --app=http://127.0.0.1:8080` under a minimal Wayland compositor (cage) |
| mm3-installer.service | Privileged installer for admin-initiated installs, Unix socket only (10.7) |
| mm3-backup.timer | Scheduled backup and remote copy, run as the installer user (10.8) |

CLI: `mm3 run`, `mm3 check-config`, `mm3 modules list`, `mm3 new-module <name>` (scaffold with all five locales), `mm3 i18n compile`, `mm3 token new`.

### 10.3 Documentation and quality

- CORE-70: Every public symbol has a Google-style docstring with Args, Returns, Raises; mkdocstrings renders the API reference.
- CORE-71: This spec lives in `docs/spec/` in the repo; requirement ids are cited in test names (`test_core_21_broadcast_excludes_sender`).
- CORE-72: CI gate: ruff, mypy --strict, pytest ≥ 85 % line coverage on `mm3.core`, i18n checks (8.4), Playwright smoke test of the shell.
- CORE-73: Semantic versioning for both the engine and module API; deprecations live for one minor release.
- CORE-74: Every source file, and every public class or function that implements requirements, states them in an "Implements" section of its docstring: the requirement ids, the spec name and version, and the section number and title. Comments use the same form for smaller code fragments. When a spec changes, only code citing a changed requirement has to be reviewed (CORE-77).
- CORE-75: Each spec change ends with a spec sync pull request to mm3-core. It exports both specs as Markdown to docs/spec/ (core.md, contract.md) and regenerates docs/spec/requirements.toml with tools/spec\_sync.py: for every requirement id, its spec, section, title, a hash of its text, introduced\_in and changed\_in. Removed requirements stay in the file with status removed.
- CORE-76: Every Document history row lists all requirement ids added, changed or removed in that version (ranges allowed). spec\_sync takes changed\_in from these rows and fails if the text of a requirement changed without being listed.
- CORE-77: A citation of requirement X at version V (CORE-74) is current when V is at least changed\_in(X). CI fails on unknown or removed ids and on versions that do not exist. A citation older than changed\_in means the code must be reviewed against X: in a pull request CI fails only for such citations in files the pull request changes; the others are listed in the coverage matrix and in the milestone issue (CORE-79). Citing an older version of an unchanged requirement is valid, so adopting a new spec version needs no other code change.
- CORE-78: docs/spec/milestones.toml assigns every requirement to a milestone of the implementation plan and records each milestone's status and done\_at (the spec versions when it was closed). CI computes the coverage matrix (implemented: cited by code under src/; tested: cited by a test name) and publishes it as the job summary and, in sync pull requests, as docs/spec/coverage.md. A milestone is closed only when all its requirements are implemented and tested, or waived in milestones.toml with a reason.
- CORE-79: On main, when a requirement of a closed milestone has changed\_in later than that milestone's done\_at, or a new requirement is assigned to it, CI opens or updates one issue per milestone labeled spec-change that lists the ids. The issue is closed when the milestone's done\_at is updated after the work.

### 10.4 MagicMirror² migration (`mm3 migrate`)

MM3 ships a migration tool rather than runtime compatibility: MM² helpers are Node.js and their front-ends depend on the global `Module.register` API, so running them would mean embedding Node and emulating MM² inside the browser (CORE-80).

**Config.** `mm3 migrate config ~/MagicMirror/config/config.js -o config.yaml` parses `config.js` as JavaScript without executing it (parser: `esprima`, Python port), then writes YAML:

- `language`, `timeFormat`, `units`, `address`, `port`, `ipWhitelist` map to `engine.*`.
- Each `modules[]` entry keeps `position` and `disabled`, moves `header` and `classes` under `appearance`, and gets an explicit `id` (CORE-202); `config` keys are copied, and renamed where a known MM3 equivalent exists (built-in table for default modules).
- Modules with no MM3 equivalent are written as `disabled: true` with a `# TODO` comment; non-literal JS expressions (functions, computed values) are left as comments.
- The output is run through the validator (6.4) and errors are listed in the report.

**Modules.** `mm3 migrate module ~/MagicMirror/modules/MMM-Foo -o mm3-foo/` generates an MM3 package skeleton:

- `pyproject.toml` with entry point, `config_model` generated from the MM² `defaults` object.
- `translations/*.json` converted to `locales/*/messages.po`; any of EN, IT, FR, ES, DE missing is created and marked untranslated.
- CSS from `getStyles()` copied to `static/`; `getDom()` becomes a `render(state)` stub with the original code kept as a reference comment.
- Each `socketNotificationReceived` case in `node_helper.js` becomes an `on_client_message` stub; the original JS is embedded as comments for manual porting.

| MM² | MM3 |
| --- | --- |
| `Module.register`, `start()` | `MirrorModule.start()` + `MirrorElement` |
| `getDom()`, `updateDom()` | `render(state)`, `ctx.set_state()` |
| `sendNotification` / `notificationReceived` | `ctx.send_notification` / `on_notification`, `this.notify` / `onNotification` |
| `sendSocketNotification` / `socketNotificationReceived` | `this.send` → `on_client_message`; `ctx.send_to_client` → `onMessage` |
| `getTranslations()`, `translate()` | `locales/*.po`, `this.t()` |
| `getStyles()`, `getScripts()` | `styles`, `scripts` class attributes |
| `suspend()`, `resume()` | `suspend()` / `resume()`, `onSuspend` / `onResume` |
| `ALL_MODULES_STARTED`, `DOM_OBJECTS_CREATED` | `CORE_READY` |

- CORE-81: Migration only reads the MM² directory and writes only to the output directory.
- CORE-82: Output is deterministic; re-running on the same input gives the same files.
- CORE-83: Every run writes `migration-report.md` listing each item as converted, needs review, or unsupported.

A browser shim (`mm3-compat-mm2`) for front-end-only MM² modules, those without `node_helper.js`, is a v2 candidate, not part of v1.

### 10.5 Diagnostics and debugging

Any failure can be traced to its module, message and code line from the admin UI, without SSH access to the mirror (CORE-110).

**Foundations**

- CORE-110: All logs are structured (structlog) and are written to stdout as JSON lines, which journald captures under systemd (a readable console format on a TTY), and kept in an in-memory ring buffer of the last 5 000 records. The default level is engine.log\_level (info). Each record has `ts`, `level`, `module`, `instance`, `event`, `trace_id` and, on errors, the exception with traceback.
- CORE-111: Every `Notification` carries a trace id in `trace` (5.1). A notification sent while a handler, a job run or a client message is being handled inherits that trace (through contextvars); any other starts a new trace equal to its own `id`. The trace id is bound to the logger while a handler runs, and propagated to `send_to_client`, actions, HTTP calls and MQTT messages, so one event can be followed across bus, backend, browser and broker.
- CORE-112: Log level is set per instance at runtime, from the admin UI or `PUT /api/v1/modules/{id}/log-level`, with no restart. A raised level expires after `debug_ttl` (default 30 min). Engine subsystems have the same control under `/api/v1/debug/log-level/{logger}`.
- CORE-113: Each instance keeps a health record: state, start time, restart count, last error (message, traceback, time, trace id), handler call count, p95 duration and timeouts, queue depth, and time of last message in and out. Served at `GET /api/v1/modules/{id}/health`.
- CORE-114: Browser errors are captured. `MirrorElement` wraps `render`, `onMessage` and `onNotification`, and the shell installs `window.onerror` and `unhandledrejection`. Errors go to the server as WebSocket frames of kind `log` with instance id, stack and user agent, rate-limited to 10 per minute per instance, and are logged with source `browser`.

**Admin debug page** (`/admin/debug`, admin listener only)

- **Overview**: engine status and a row per instance with state, restarts and last error; opening a row shows the traceback, health record and that instance's recent logs, with its log-level control.
- **Live logs**: streamed over the admin WebSocket; filter by level, module, instance, trace id and text; pause, and download as JSON lines.
- **Bus inspector**: live list of notifications, WebSocket frames and MQTT messages with time, name, source → target and size; JSON payload viewer; filter by name glob and source; pause and search; "follow trace" shows every log and message sharing a trace id; "re-send" injects a captured notification again through `/api/v1/notify`.
- CORE-115: The inspector captures only while an admin page has it open (last 1 000 entries), so it costs nothing in normal operation. Config fields marked `secret` are masked in captured payloads.

**Diagnostic bundle and checks**

- `mm3 diag bundle` or the admin "Download diagnostics" button produces a zip with engine and module versions, Python and OS information, the venv package list, redacted config, recent logs, health records, browser errors, and kiosk details (resolution, user agent).
- CORE-116: Redaction is mandatory: secrets, tokens and `!env` values are removed, and the bundle lists every redacted field.
- `mm3 diag check` runs a self-test: config valid, ports free, MQTT broker reachable, kiosk connected, display driver available, locale catalogs complete. Each check prints pass or fail with a hint.

**On the mirror**

- CORE-117: A failed instance shows only a localized "unavailable" placeholder in its cell. Tracebacks, ids and debug data never appear on the mirror display.

### 10.6 Module catalog

Modules are discovered through a public catalog, a GitHub repository whose signed index lists vetted PyPI packages; installation runs from the CLI or from the admin UI through the installer service of 10.7 (CORE-66, CORE-120).

**Index repository** (`mm3-project/module-index`)

- One entry file per module, `modules/<name>.yaml`: PyPI name, source repository, maintainer, license, category, `LocalizedText` description, screenshots. New modules and changes arrive as pull requests.
- CI on every pull request and every new PyPI release: download the wheel, verify its provenance, run `mm3 module check`, and store the report. A failed MUST blocks the entry or the release.
- CI publishes `index.json` on GitHub Pages: per release the version, supported API range, check result, wheel SHA-256 and attestation identity. The index itself is signed.

**Guarantees**

- CORE-121: Only releases that pass `mm3 module check` for their declared API version appear in the index.
- CORE-122: Badges: **official** (published by the MM3 project), **verified** (manually reviewed by catalog maintainers), **community** (automatic checks only).
- CORE-123: Releases MUST be published with PyPI Trusted Publishing and carry PEP 740 attestations (Sigstore); the index records the expected publisher identity, matching the entry's source repository.
- CORE-124: The CLI shows only releases compatible with the running engine's Module API and refuses to install an incompatible one; there is no override for an API major mismatch.

**CLI** (`mm3 modules …`, run on the mirror as the `mm3` user)

| Command | Effect |
| --- | --- |
| `search <text>` | Search the cached index; shows badge and compatibility |
| `info <name>` | Description, versions, required capabilities and grants, check report |
| `install <name>[==ver]` | Verify index signature, SHA-256 and attestation, then `pip install` into the engine venv; show requested capabilities before confirming |
| `update [<name>]` | Upgrade to the newest compatible release |
| `remove <name>` | Uninstall; instances in config are disabled, not deleted |
| `list` | Installed modules, versions, available updates |

- CORE-125: The index is cached locally and refreshed at most daily; `search`, `info` and `list` work offline from the cache.
- CORE-126: `install` and `update` abort if any verification fails; nothing is installed from outside the index unless `--from-pypi` or a local path is given explicitly, and such modules are marked **unlisted** in the admin.

**Admin UI** adds a Catalog page: search, details, badges, compatibility, required grants, and Install, Update and Remove buttons that use the installer service (10.7); the equivalent CLI command is shown as an alternative. The installed-modules list shows available updates.

### 10.7 Installer service

Installs, updates and removals started from the admin UI run in a separate privileged service; the engine stays read-only and only forwards the request (CORE-130).

- **Separation.** `mm3-installer.service` runs as user `mm3-install`, the only account with write access to `/opt/mm3/venvs/`. It listens only on the Unix socket `/run/mm3/installer.sock`, accessible to the `mm3` group, and has no network listener. The engine keeps its read-only venv (CORE-65).
- CORE-131: The installer accepts only package names and versions listed in the signed catalog index, and verifies the index signature, SHA-256 and PEP 740 attestation itself, never trusting values passed by the engine. Unlisted packages and local paths remain CLI-only.
- CORE-132: Every install, update or removal requires a fresh confirmation with an install passphrase (argon2 hash, set with `mm3 installer passphrase`), separate from the API token, plus a TOTP code if enabled (`mm3 installer totp enable`). Five failures lock the installer for 15 minutes.
- CORE-133: Before confirmation the admin shows the module's badge, check report and requested capabilities and grant scopes; granting scopes to instances stays a separate config step.
- CORE-134: Staged install: the installer builds a new venv under `/opt/mm3/venvs/<timestamp>/` with the change applied, runs an import test and `mm3 module check --only static`, then switches the `/opt/mm3/venv` symlink atomically and restarts `mm3.service`.
- CORE-135: Rollback: if the engine is not `READY` within 60 s of the restart, or `mm3 diag check` fails, the installer switches the symlink back and restarts again. The last 3 venvs are kept, and the admin offers "Restore previous version".
- CORE-136: Every request is logged with time, admin client address, package, versions and outcome, and listed on the admin debug page.
- One operation runs at a time; progress streams to the admin page.

### 10.8 Backup and restore

A backup captures everything needed to rebuild a mirror on a fresh Debian install; restore can bring back all of it or only selected parts, and always runs through the privileged service with automatic rollback (CORE-160).

**Contents** (each a selectable part)

| Part | Includes | Restored how |
| --- | --- | --- |
| `config` | `config.yaml`, included files, widget templates, configuration history (6.8) | Validated, written as a new history version, hot reloaded |
| `data` | Each instance's storage directory (KV store, widget history); caches excluded | Per instance, while that instance is stopped |
| `modules` | Installed modules: name, version, origin (catalog, PyPI, local) | Reinstalled from the signed catalog into a staged venv (10.7); others listed for manual install |
| `secrets` | Environment file, API token hashes, installer passphrase and TOTP secret, rclone remote credentials | Only from an encrypted archive |

Every archive contains `manifest.json`: engine version, Module API version, hostname, time, parts included, SHA-256 of every file.

**Format and encryption**

- Archive: `.mm3backup`, a gzip-compressed tar.
- CORE-161: An archive that contains `secrets` MUST be encrypted; an unencrypted backup silently omits that part and says so in its manifest.
- Encryption uses the [age](https://age-encryption.org) format, either with a passphrase (manual backups) or with a public key (scheduled backups). The matching private key is generated at setup, shown and downloadable once, and is not kept on the mirror unless the user chooses to.

**When and where**

- **Manual:** from the admin (Backups page: choose parts, encryption, download) or `mm3 backup create`.
- **Scheduled:** `mm3-backup.timer`, daily at `backup.schedule` (default 03:30). Kept in `/var/lib/mm3/backups/` with retention of 7 daily, 4 weekly and 6 monthly by default (`backup.keep`).
- **Remote:** after each scheduled backup, a copy goes to `backup.remote` (SFTP, SMB, WebDAV, S3-compatible and others) through rclone. The same retention applies there, and failures appear in the admin and the debug page.
- **Before risky operations:** a backup is taken automatically before every restore, module install or update, and engine upgrade.

```yaml
backup:
  schedule: "03:30"
  parts: [config, data, modules, secrets]
  encrypt_to: age1qyq…          # public key; required when secrets are included
  keep: { daily: 7, weekly: 4, monthly: 6 }
  remote: { name: nas, path: mm3/mirror1 }   # rclone remote, configured in the admin
```

- CORE-244: The `backup` block is optional, every key in it is optional, and any other key is an error (`extra="forbid"`). `secrets` in `parts` without `encrypt_to` is an error (CORE-161), and so is `keep` with all three counts at 0. A change applies from the next scheduled backup.

| Key | Values | Default |
| --- | --- | --- |
| `schedule` | `HH:MM`, 24-hour, in `engine.timezone` | `03:30` |
| `parts` | Non-empty list of distinct values from `config`, `data`, `modules`, `secrets` | `config`, `data`, `modules`, plus `secrets` when `encrypt_to` is set |
| `encrypt_to` | age X25519 public key (`age1…`) | none |
| `keep.daily`, `keep.weekly`, `keep.monthly` | integer 0–1000 | 7, 4, 6 |
| `remote.name` | rclone remote configured in the admin, matching `[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}`; required when `remote` is given | none: no remote copy |
| `remote.path` | Path on that remote | `""` (the remote's root) |

**Restore**

- Sources: a file uploaded in the admin, an entry in the local list, an entry on the remote, or `mm3 backup restore <file>`.
- CORE-162: Restore runs in `mm3-installer` (10.7) and requires the same re-authentication. Steps: decrypt, verify the manifest checksums, check compatibility, show what will change part by part, take a pre-restore backup, stop the engine, restore the selected parts, rebuild the venv if `modules` is selected, start the engine.
- CORE-163: If the engine is not `READY` within 60 s, or `mm3 diag check` fails, the pre-restore backup is put back automatically.
- CORE-164: Compatibility: archives from an older engine are restored, migrating the config format if needed; archives from a newer engine or a different Module API major are refused.
- CORE-165: Selective restore: any combination of parts, and within `config` and `data` a single module instance by id (its config section, its data, or both).

**CLI**: `mm3 backup create [--parts …] [--encrypt]`, `mm3 backup list [--remote]`, `mm3 backup verify <file>`, `mm3 backup restore <file> [--parts …] [--instance <id>]`, `mm3 backup keygen`.

Diagnostic bundles (10.5) never include backups or keys.

### 10.9 Dependency management

Dependencies are updated automatically through tested pull requests; nothing reaches a mirror unless it passed CI as part of an engine release with its lockfile (CORE-170).

**Pinning**

- CORE-171: `pyproject.toml` declares ranges bounded to the current major of each dependency (`>=x.y,<next`). `uv.lock` pins exact versions with hashes for every platform target; CI, release builds and the installer install only from the lock.
- CORE-172: Each engine release ships its lock as a constraints file; the installer (10.7) resolves module dependencies within those constraints and refuses a module whose requirements conflict with them.
- Vendored front-end libraries (CodeMirror, gridstack.js, uPlot, Material Design Icons) live under `src/mm3/core/web/vendor/` with a `vendor.json` that records name, version, source URL and SHA-256.
- System packages (Chromium, cage, wlr-randr, age, rclone) are taken from Debian stable; the install script declares minimum versions.

**Automatic updates (Renovate)**

| Update | Grouping | Merge |
| --- | --- | --- |
| Patch and minor, Python and GitHub Actions | One weekly pull request | Automatic when CI is green |
| Major | One pull request per package, with changelog | Manual review; core spec 1.2 updated first |
| Security advisory | Immediate pull request | Automatic for patch/minor when CI is green; otherwise manual, high priority |
| Vendored JS (`vendor.json`) | Weekly | Manual (UI check in the preview) |
| Lockfile maintenance | Monthly refresh of transitive dependencies | Automatic when CI is green |

- CORE-173: A dependency outside the approved list (1.2) is never added by automation; Renovate only updates existing ones.

**Safety net**

- CORE-174: Every update pull request runs: ruff, mypy --strict, the full test suite, Playwright UI smoke tests, `mm3 module check` on the reference modules, the i18n checks, and `pip-audit` on the locked set.
- CORE-175: Deprecation warnings from dependencies fail the test suite, so breaking changes are found before the next major.
- CORE-176: A weekly canary job installs the newest allowed and the newest available versions of all dependencies, and the next Python release, then runs the suite. Failures open or update one issue labeled `canary`; they never block merges.
- A CI job runs the install script in Debian 12 and Debian 13 containers and starts the engine headless.
- After an update that touches the Module API dependencies (MOD-94), the catalog CI re-runs `mm3 module check` on the latest release of every listed module.

**Isolation**

- CORE-177: Each third-party library is used through one internal adapter in `mm3.core` (for example `services/mqtt.py` for aiomqtt, `services/http.py` for httpx), so a breaking change is fixed in one place.
- Modules see only MM3-owned types, plus Pydantic and FastAPI's APIRouter (Module Contract MOD-94); `ctx.http` is an MM3 client, not httpx.

## 11. Widget builder (`mm3-widget`)

Users build their own mirror widgets in the admin UI from graphical elements bound to MQTT, Home Assistant, bus or HTTP data, with no code; each widget is an ordinary module instance in the YAML. Requirements for the module are `WGT-xx`; the four engine additions it needs are `CORE-100`–`103`.

### 11.1 Engine additions

These are generic, so any module may use them.

- CORE-100: `ctx.mqtt.subscribe(filter, handler)` returns a `Subscription`, cancelled when the instance stops. Subscribe and publish are allowed only on topics matching the instance's `grants.mqtt.subscribe` / `grants.mqtt.publish` glob lists; default is deny.
- CORE-101: Config type `LocalizedText` (`mm3.config`) accepts a string, the same in every language, or a non-empty map from BCP 47 tags to strings (`{en: …, it: …}`); an invalid tag is an error. It resolves through the locale chain (8.2); if no tag of the chain is present, the first entry in file order is used. Backend code calls `value.resolve(ctx.locale)`. In the public config sent to browsers the engine replaces each `LocalizedText` with its resolved string for that element's locale, and sends the config again when the locale changes. The form view shows one field per shipped locale; a missing `en` is a validation warning.
- CORE-102: A module MAY declare `admin_element` and `admin_scripts`; these load only inside the admin UI on the admin listener and get an authenticated API helper. The builder (11.7) is such an extension.
- CORE-103: With `engine.input.touch: true` the shell enables pointer events on module elements; user input reaches the backend only through `this.send()`. Preview clients never send input (CORE-99).

### 11.2 Widget definition

```yaml
- module: widget
  id: living_room
  position: { col: 9, row: 5, width: 4, height: 3 }
  grants:
    mqtt: { subscribe: ["zigbee2mqtt/living_*"], publish: [] }
  config:
    sources:
      temp:   { type: mqtt, topic: zigbee2mqtt/living_sensor, path: temperature, stale_after: 15m }
      hum:    { type: mqtt, topic: zigbee2mqtt/living_sensor, path: humidity }
      lights: { type: ha, entity: light.living_room }
      power:  { type: http, url: "http://inverter.lan/api/now", path: data.watts, every: 30s }
    history:
      temp: { keep: 24h, sample: 5m }
    grid: { columns: 4, rows: 3 }          # sub-grid inside the widget
    elements:
      - type: label
        at: [1, 1, 4, 1]                   # col, row, width, height
        text: { en: Living room, it: Soggiorno, fr: Salon, es: Salón, de: Wohnzimmer }
      - type: value
        at: [1, 2, 2, 1]
        bind: temp
        format: "{{ value | round(1) }} °C"
        rules:
          - when: "value > 26"
            style: { color: critical, icon: mdi:thermometer-alert }
      - type: gauge
        at: [3, 2, 2, 1]
        bind: hum
        min: 0
        max: 100
      - type: sparkline
        at: [1, 3, 2, 1]
        bind: temp
        window: 24h
      - type: toggle
        at: [3, 3, 2, 1]
        bind: lights
        action: { type: ha_service, service: light.toggle, target: { entity_id: light.living_room } }
```

- WGT-01: A definition is fully declarative: sources, history, sub-grid, elements. It contains no scripts.
- WGT-02: `at` places an element on the widget's sub-grid with the same bounds and overlap rules as the screen grid (6.5).
- WGT-03: `path` selects a value from a JSON payload with dot and index syntax (`data.items[0].value`); non-JSON payloads are taken as a string, then coerced to the element's expected type.
- WGT-04: `format` and `when` are Jinja2 expressions evaluated in a `SandboxedEnvironment` with `value`, `history`, `sources` and `now` in scope; evaluation errors show the element as invalid, never crash the module.
- WGT-05: A source with `stale_after` that receives nothing in time marks its elements stale (dimmed, localized tooltip in preview).

### 11.3 Data sources

| Type | Key fields | Mechanism |
| --- | --- | --- |
| `mqtt` | `topic`, `path`, `qos` | `ctx.mqtt.subscribe`, needs grant (CORE-100) |
| `ha` | `entity`, `attribute` | Home Assistant WebSocket API, `subscribe_entities`; one connection per module, long-lived token via `!env` |
| `bus` | `notification`, `path` | `on_notification` filtered by name |
| `http` | `url`, `method`, `headers`, `path`, `every` (≥ 10 s) | Polled with `ctx.http` from the backend; URL must match `grants.http` |

- WGT-10: Source types are plugins on entry point group `mm3_widget.sources`: a Pydantic config model plus `async def run(self, emit)`. New types install as packages, and the builder picks them up automatically.

### 11.4 Elements

| Family | v1 element types |
| --- | --- |
| Text and values | `label`, `value`, `icon`, `image`, `state` (light) |
| Gauges and bars | `gauge` (radial), `bar` (linear), `ring` (progress) |
| History | `sparkline`, `chart` (line, multiple series) |
| Controls | `button`, `toggle`, `slider` |

- WGT-11: Every element accepts `rules`: an ordered list of `{when, style}`; the first match applies. `style` may set `color` (theme token: good, warning, critical, accent, muted), `icon`, `visible`, `blink`.
- WGT-12: Element types are plugins on entry point group `mm3_widget.elements`. A plugin supplies a Pydantic schema (drives validation and the inspector form), a custom element `mm3-w-<type>` implementing `render(value, history, style)`, its locale catalogs, and builder metadata (palette icon, category, default size).
- Libraries: uPlot for charts, Material Design Icons for `mdi:` icons (same set as Home Assistant), both vendored.

### 11.5 Actions on touch

| Type | Fields |
| --- | --- |
| `mqtt_publish` | `topic`, `payload`, `retain`, `qos` |
| `bus_notify` | `name`, `payload` |
| `ha_service` | `service`, `target`, `data` |
| `http` | `method`, `url`, `body` |

- WGT-20: The front-end sends only `{action: <element id>}`; the backend looks the action up in the stored definition and executes it. A browser can never submit an arbitrary action.
- WGT-21: Publish topics and HTTP URLs must match the instance grants (CORE-100).
- WGT-22: `confirm: true` requires a second tap within 3 s; `cooldown` defaults to 1 s. The control's state always comes from its bound source, never from the tap.
- WGT-23: Every executed action is logged with instance, element and result.

### 11.6 History

- WGT-30: For each source listed under `history`, the backend keeps a rolling series in an SQLite file in the instance's storage directory (stdlib `sqlite3` through `ctx.run_blocking`). `keep` sets retention, `sample` sets the averaging interval; at most 10 000 points per series.
- WGT-31: History survives restarts; the front-end gets the series in its snapshot and incremental points afterwards.

### 11.7 Builder and templates

- **Builder page** `/admin/widgets` (admin extension, CORE-102): a palette of all installed element types, the widget's sub-grid drawn over its live preview (6.6) for drag and resize, an inspector form per element generated from its schema, and a rule editor.
- **Source manager**: add sources and test them live. A "tap" panel shows the latest messages on a topic, entity or URL; clicking a JSON field fills in `path`.
- **Same pipeline**: the builder edits the instance config through the JSON Patch path (6.4), so validation, backups and hot reload are unchanged.
- WGT-40: Templates: a widget can be saved as a template with named parameters (e.g. `room`, `sensor_topic`) in `/etc/mm3/widget-templates/`. An instance then uses `template: room-card` plus `params:`; `${param}` placeholders are substituted before validation.
- WGT-41: `mm3 widget export <id> -o <dir>` wraps a widget or template into an installable MM3 module package, reusing the `mm3 migrate` scaffolding (10.4).

### 11.8 Data flow

&#91;embedded content: widget data flow · data in, actions out\]

The browser only renders state and reports which control was touched; extraction, history, rules and every action run in the backend, behind the instance's grants.

## 12. Open points and next steps

All open decisions were closed on 27 Oct 2026; the spec is ready for implementation.

- [x] Approve backup tools: age for encryption, rclone for remote copies (10.8).
- [x] Framework list (1.2) approved, including CodeMirror 6, gridstack.js and the in-house form renderer.
- [x] Widget and migration libraries approved: Jinja2 sandbox, uPlot, Material Design Icons, esprima.
- [x] Default kiosk display stack: Wayland with cage + Chromium, `wlr-randr` for power.
- [x] Module distribution: public PyPI.
- [x] Admin UI scope for v1: full editor (layout, forms, YAML) with live validation, on a separate admin listener.

After approval: repository scaffold and `mm3.core` skeleton with tests for CORE-01–26, then the reference modules (clock, weather) to prove the contract end to end.

## 13. Document history

| Version | Date | Changes |
| --- | --- | --- |
| 0.10 | 2026-10-28 | pages is a configuration history section, shown in changed sections and restorable alone (CORE-216 and CORE-155 changed) |
| 0.9 | 2026-10-28 | CORE\_CONFIG\_ERROR reserved (CORE-22 changed); CORE\_CONFIG\_CHANGED, CORE\_CONFIG\_ERROR and CORE\_LOCALE\_CHANGED registered in 5.5; CORE\_LOCALE\_CHANGED payload and delivery (CORE-245); default gap 16px and padding 32px (CORE-213 changed) |
| 0.8 | 2026-10-28 | Validation codes CORE\_CFG\_\* with their severity, Pydantic mapping and params (CORE-243); backup block keys, defaults and checks (CORE-244) |
| 0.7 | 2026-10-28 | Error reply codes transient, configuration and internal with REQUEST\_FAILED for bugs; an on\_request that exceeds its budget sends no reply (CORE-221 changed) |
| 0.6 | 2026-10-28 | Request and reply on the bus, envelope fields expects\_reply and reply\_to (CORE-20, CORE-220–223); shared notification registry (CORE-224–226); CORE\_PAGE\_CHANGED reserved (CORE-22); pages, per-page positions, rotation, swipe, HA select (CORE-230–237); visibility reasons, runtime stop and start, layout.control, page selector in the admin (CORE-238–242); requirements traceability: spec sync, history rows list ids, citations checked per requirement, coverage matrix, issues for closed milestones (CORE-74 changed, CORE-75–79) |
| 0.5 | 2026-10-28 | Milestone 2 settled: !include replaces a node inside the config directory (CORE-31); env file and !env defaults (CORE-200); top-level and engine keys with reload behaviour (CORE-201); instance keys, ids required for modules used more than once (CORE-202); missing and broken modules, disabled instances, durations (CORE-203–205); error records and mm3 check-config (CORE-206–207); reload triggers, invalid config never applied, apply order, current values in ctx (CORE-208–211); position required for front-ends (CORE-33); preset table and MM²-style stacks (CORE-92, CORE-93, CORE-212); spacing and appearance checks, header and classes only under appearance (CORE-213, CORE-214, 10.4); viewport rounding and assumed viewport (CORE-215); LocalizedText resolution (CORE-101); history stores the whole config set, sections, parents for undo and redo, lock and hash, retention, CLI exit codes (CORE-153–155, CORE-216–219) |
| 0.4 | 2026-10-27 | Milestone 1 gaps closed: restart delays 1, 2, 4, 8 s, DISABLED on the fifth failure (CORE-194); at() takes an optional timezone, aware datetimes used as is, DST rules of CORE-182 apply (3.3, CORE-187); a cron expression that can never match is invalid (CORE-181) |
| 0.3 | 2026-10-27 | Milestone 1 settled: per-instance inbox, ordering, targeted delivery and sender exclusion (CORE-21, 24, 25, 27–29); trace field in the envelope (CORE-111); supervisor rules (CORE-190–197); one engine.timeouts block (3.2); CLOCK\_BOOTTIME intervals, at(), CronExpression, timezone reload (CORE-181, 183, 185, 187–189); storage location and key-value store (CORE-198); JSON logs to stdout (CORE-110); API check aligned with MOD-90 (CORE-03) |
| 0.2 | 2026-10-27 | Presence as a module; GUI config editor (layout, forms, YAML); configurable grid and appearance; live preview; MM² migration; widget builder; diagnostics; module catalog and installer service; display output; config history with undo/redo; backup and restore; dependency management; in-house scheduler; versioning and code references (CORE-74) |
| 0.1 | 2026-10-27 | First draft: architecture, components, module contract overview, messaging, configuration, lifecycle, i18n, built-in services, security and packaging |
