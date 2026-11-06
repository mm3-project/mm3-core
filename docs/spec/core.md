# MagicMirror 3 — Core Engine Specification

Oct 27, 2026 · @Nicola Reina · Version 0.36 (draft)

## 1. Scope and requirements baseline

The core engine (package `mm3-core`) is a Python 3.11 asyncio server that loads modules, routes notifications between them, and serves a browser shell that a Chromium kiosk displays. It keeps the MagicMirror² concepts (regions, notifications, module lifecycle, helper/front-end split) but exposes a clean Python API; MM² modules are not run directly.

Keywords MUST, SHOULD, MAY follow RFC 2119. Requirement ids (`CORE-xx`) are referenced by tests.

**Versioning.** This document is versioned `MAJOR.MINOR`; the current version is in the byline and every change is listed in section 13. Any requirement added, changed or removed bumps MINOR; 1.0 is the version the first engine release implements. Code cites the requirement ids and the version it implements (section 10.3, CORE-74).

### 1.1 Decisions taken

| Topic | Decision |
| --- | --- |
| Target | Debian 12+ on x86-64, Python ≥ 3.11; the kiosk needs Debian 13+ (cage ≥ 0.1.5, 6.7) |
| Rendering | Web server + Chromium kiosk; any LAN browser in server.allow MAY be an extra client, view-only unless paired with a display token (10.1) |
| Module shape | Python backend (optional) + JavaScript Web Component front-end (optional) |
| MM² compatibility | Concepts and naming; migration tool mm3 migrate for config and modules (10.4) |
| Config | YAML, edited as a file or in the admin web editor, both validated by Pydantic v2; hot reload |
| Discovery | pip-installable packages, `importlib.metadata` entry points (group `mm3.modules`) |
| Front-end | Vanilla Custom Elements, no build step |
| Isolation | In-process asyncio; faulty modules are contained and restarted |
| i18n | Babel/gettext `.po` per module, compiled to JSON for the browser; EN, IT, FR, ES, DE shipped |
| Core services | Hot reload, remote-control API, admin web UI with validated YAML editor, MQTT bus with Home Assistant discovery. Presence & power is a module (mm3-presence) |
| Deployment | systemd + virtualenv; LAN-only by default; named admin and display tokens stored as argon2 hashes, admin sessions by cookie (10.1) |
| Layout | Configurable cell grid (default 12 × 12); position, size, layer and appearance per instance; MM² region names kept as presets (6.5) |
| Custom widgets | No-code widget builder module mm3-widget: MQTT, HA, bus and HTTP sources; extensible elements; touch actions; templates (11) |
| Kiosk display | Wayland with cage + Chromium by default; display power through the kiosk agent with wlr-randr (CORE-346), with DDC/CI and HDMI-CEC drivers in mm3-presence; no X11 driver (changed 6 Nov) |
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
| Pages and visibility | Pages in the core: own layout per page, all-pages instances, rotation, notifications, swipe, HA select; one set of visibility rules; runtime stop and start; pages out of the cycle, timed jumps, idle return and pause holds cover MMM-pages and MMM-ProfileSwitcher, a profile being a page (6.9; decided 6 Nov) |
| Requirements traceability | Specs synced to the repository with changed\_in per requirement; review only of code citing changed requirements; coverage matrix; issues for closed milestones (10.3) |
| Shell and WebSocket | hello/welcome handshake with client roles kiosk, browser, preview; bounded queues with coalesced state; full resync on reconnect; templates with lit-html behind /core/mm3.js (5.6, 6.5) |
| Theme | MM²-like core theme as CSS variables, Roboto Condensed vendored; optional /etc/mm3/custom.css in the config set (6.10) |
| Kiosk | System unit as user mm3-kiosk running cage with a kiosk agent that applies the display block and the audio output and volume live and supervises Chromium; sound through PipeWire in the kiosk session; Debian 13+ (6.7) |
| Admin and REST API | Errors as problem+json with CORE\_API\_\* codes; the draft lives in the browser, the server applies JSON Patch without storing; ETag = hash of the config set; secrets round-trip as !masked; one admin WebSocket /admin/ws with status, logs and bus topics; Show on mirror reaches the kiosk only; OpenAPI as JSON without an interactive page; the CLI reaches the engine on the Unix socket /run/mm3/admin.sock; a request without a reply in time is 504 and an error reply is an ordinary 200 reply; instance controls are idempotent; log levels are set as {level} with the fixed debug\_ttl; log filters by minimum level; status frames on change at most once a second; a diagnostic bundle with a fixed file list; secrets masked by value in the bus inspector; tagged nodes in JSON Patch as one-key objects and an in-house JSON Patch on the ruamel nodes; one /admin/config page whose History tab comes first; YAML lint through validate and CodeMirror bundled once with esbuild; the session locale only with an admin session (6.4, 6.6, 6.8, 9.1, 10.5; decided 4 and 5 Nov) |
| MQTT bridge | One shared aiomqtt connection with username/password and optional CA, no client certificates; inbound notifications only for names in mqtt.import, none by default; envelopes out for mqtt.export; no offline queue (TransientError); Home Assistant entities for modules through ctx.mqtt.ha\_entity() with the capability ha.discovery and no grant; stale entities cleared (9.3; decided 6 Nov) |
| Backups | Run and scheduled by the installer service, which has CAP\_DAC\_OVERRIDE, CAP\_CHOWN and CAP\_FOWNER inside ProtectSystem=strict; no mm3-backup.timer; age-encrypted tar.gz with an unencrypted sidecar; rclone remotes of four types kept by the installer; pre-operation backups kept 3 (10.8; changed 6 Nov) |

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
| Kiosk agent WebSocket client | websockets (≥ 13, < 18) | Already installed as Uvicorn's WebSocket implementation; used only by the kiosk agent, behind one adapter (httpx has no WebSocket client) |
| Logging | stdlib logging + structlog | JSON logs to journald |
| Packaging | pyproject.toml, hatchling build backend | PEP 621 standard |
| Catalog build hook | mm3-build: hatchling build hook mm3-i18n on Babel, in the mm3-core repository (CORE-280) | Module wheels compile their catalogs without installing the engine; one compiler for core and modules (decided 30 Oct) |
| Testing & docs | pytest + pytest-asyncio, Playwright for UI, offered to module developers through the extra `mm3-core[testing]` (CORE-279); MkDocs Material + mkdocstrings | Docstrings render to reference docs |
| Quality | ruff, mypy --strict | Lint and typing gate in CI |
| Admin UI | CodeMirror 6, in-house schema form renderer, in-house layout overlay | YAML editing with schema lint from the server; grid drag/resize; no build step at run time (CodeMirror is bundled once with esbuild at vendoring, CORE-323) |
| Widgets | Jinja2 (sandboxed), uPlot, Material Design Icons | Safe expressions; small fast charts; same icon set as Home Assistant |
| MM² migration | esprima (Python port) | Parses config.js without executing it |
| Package provenance | PyPI Trusted Publishing + PEP 740 attestations (Sigstore) | PyPI's own signing standard; no keys for maintainers to manage |
| Installer re-auth | argon2-cffi, pyotp | Passphrase hashing; standard TOTP codes for authenticator apps |
| Supply-chain verification | sigstore 4.x and pypi-attestations (bounded to the current 0.0.x release, every update reviewed), behind mm3.installer.verify in the extra mm3-core\[installer\] (CORE-349) | sigstore-python is the reference Sigstore client; pypi-attestations is PyPI's own PEP 740 library, maintained by the PyPI admins with Trail of Bits; only the installer imports them (decided 6 Nov) |
| Backup encryption | age (Debian package age), invoked as a subprocess | Standard, audited format; passphrase and public-key modes, so scheduled backups need no secret on the mirror |
| Remote backup copy | rclone (Debian package), invoked as a subprocess | One tool for SFTP, SMB, WebDAV, S3 and 70+ other targets |
| Dependency management | uv (lockfile), Renovate (update bot), pip-audit (vulnerability scan) | Reproducible installs with hashes; tested update PRs incl. vendored JS; known-CVE gate |
| Front-end templates | lit-html 3 (BSD-3-Clause), vendored ES module, re-exported only through /core/mm3.js | Escaping by default, in-place DOM updates keep element state, event bindings for touch; no build step (decided 28 Oct) |
| Shell font | Roboto Condensed (SIL OFL 1.1; Apache-2.0 before 2023), vendored woff2 | The MagicMirror² look; served locally, no external fonts (CSP) |
| Kiosk audio | PipeWire with WirePlumber and pipewire-pulse (Debian packages, user units of the kiosk session); pactl from pulseaudio-utils, invoked as a subprocess by the kiosk agent (CORE-295, CORE-296) | Debian's default sound server; mixing and live output switching; Chromium plays through its PulseAudio API; pactl gives JSON output (decided 1 Nov) |
| Display control | ddcutil (DDC/CI) and cec-client from cec-utils (HDMI-CEC), Debian packages invoked as subprocesses by the mm3-presence drivers (PRS-06, CORE-348) | Standard tools for monitor brightness and power and TV standby; no library needed (decided 6 Nov) |

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
- **HTTP client**: an MM3 `HttpClient` (backed by one pooled httpx client, Module Contract 4.3) with timeouts and a per-module User-Agent (transport rules in 3.4); external hosts need the `http.outbound` capability and an `http` grant (Contract section 8).
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
- CORE-278: One engine clock. The scheduler, the hook and job budgets (3.2) and `ctx.now()` read the same clock: wall-clock time from the system clock, intervals from CLOCK\_BOOTTIME (CORE-183). `ctx.now()` returns an aware `datetime` in `ctx.timezone`. `mm3.testing` replaces this clock with a virtual clock driven by `h.advance()` and runs the module on an event loop with the same virtual time, so the scheduler, `asyncio.sleep`, budgets and `ctx.now()` move together and tests run without waiting (Module Contract MOD-28, §12.2).

**Dates, configuration and reload**

- CORE-187: `at()` reads a naive `datetime` in the job's `timezone` or `engine.timezone`; an aware datetime is used as is. The job's timezone is the optional timezone argument of at(), as for cron(); a naive time that does not exist or occurs twice follows CORE-182. A time already past runs once as soon as possible.
- CORE-188: Configuration fields holding a cron expression use the MM3 type `CronExpression` (module `mm3.config`), validated when the config is loaded; `cron()` raises `ValueError` for an invalid string.
- CORE-189: When `engine.timezone` changes on reload, every wall-clock job is re-planned. Jobs live only in memory: runs missed while the engine was not running are not caught up.

### 3.4 HTTP transport

`ctx.http` (Module Contract 4.3) sends through one engine-owned transport, an adapter around a single pooled httpx client. Capability and grant checks (Contract section 8) run before the transport is called; the rules below are what the transport adds.

- CORE-282: One pooled `httpx.AsyncClient` serves all instances. It is created at engine start, closed at shutdown, and used only inside `mm3.core.services.http`. HTTP/1.1 only (no extra dependency); no cookie jar, no cache and no automatic retries: a module that wants a retry makes the call again. The proxy comes from `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`. At most 4 requests of one instance are in flight; further calls wait. Response bodies above 10 MiB are refused with `HttpError` while they are read, and the default timeout is 10 s, capped at 60 s.
- CORE-283: Every request carries `User-Agent: mm3/<core version> <module name>/<module version>` (the module version is read from the distribution metadata). A `User-Agent` header passed by the module replaces it, for services that ask for a contact address. `Host`, `Content-Length`, `Connection` and `Transfer-Encoding` headers passed by a module are dropped.
- CORE-284: TLS 1.2 or later, with the certificate and host name always verified against the system CA store. No setting turns verification off. A grant entry may be an object `{prefix, ca}`: `ca` is a PEM file, relative to the directory of the main configuration file, that replaces the CA store for requests matching that prefix, so devices with a self-signed certificate on the LAN stay verified. A missing or unreadable file is a validation error `CORE_CFG_FILE_UNREADABLE` with the path of the grant.
- CORE-285: Loopback, link-local, unspecified and multicast addresses are refused with `PermissionError`, because the admin listener and other local services must not be reachable through a module. The check applies to the address actually connected to, after name resolution, so a name that resolves to a local address is refused too. It is lifted only when the matching grant prefix names that host literally (`http://127.0.0.1:8123/`, `http://localhost:8123/`). Addresses of private LAN ranges need only a normal grant.
- CORE-286: Redirects: at most 5, followed only while the target URL matches a grant of the instance, passes CORE-285 again, and does not change `https` to `http`; otherwise the redirect response is returned as it is.
- CORE-287: Errors map to `mm3.errors` only: `HttpTimeoutError`, `HttpConnectionError` (including TLS errors and refused redirects), `HttpStatusError` (only through `raise_for_status()`). Library exception types and messages never reach a module. The debug log records `http_request` with method, host, path and status; query strings, headers and bodies are never logged, because they carry API keys. Per instance, `ctx.health` shows the count of requests and failures and the time of the last success.

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
| `scheduler`, `now()`, `http`, `storage`, `log`, `t()` | Shared services (3.1) |
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
| Control | core → shell | `control` | `show`, `hide`, `reload`, `locale`, `layout`, `page`, `styles` |
| Hello | shell → core | `hello` | Client id, screen size, protocol version |
| Diagnostics | shell → core | log | Browser errors and forwarded console output (CORE-114) |
| Welcome | core → shell | `welcome` | Engine build, role, layout, pages, instances with public config and catalog version (CORE-246) |
| Error | core → shell | `error` | A refused inbound frame: code, message key, reference (CORE-248) |

WebSocket frames are JSON objects `{"v": 1, "kind": "notify", "instance": "weather_1", "data": {...}}` (5.6). Endpoint `/ws`; one connection per browser.

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
| PRESENCE\_STATE | event | {state: "on" \| "idle" \| "off", reason: "start" \| "presence" \| "timeout" \| "schedule" \| "command"} | presence → any (9.2, PRS-07) |
| CORE\_INTERACTION | event, engine only | {kind: "touch"} | engine → any (CORE-347) |
| `SHOW_ALERT` | event | `{id?: str, title?: LocalizedText, message: LocalizedText, level: info \| warning \| critical, timeout_s?: number}` (0 = until hidden) | any → alert module |
| `HIDE_ALERT` | event | `{id: str}` | any → alert module |
| `PAGE_NEXT`, `PAGE_PREV` | command | `{}` | any → engine (6.9) |
| `PAGE_SET` | command | `{page: str}` | any → engine (6.9) |
| PAGE\_PAUSE, PAGE\_RESUME | command | {for?: number} (seconds, 1–3600); {} | any → engine (6.9, CORE-336) |
| `CORE_PAGE_CHANGED` | event, engine only | `{page, previous, index, count, pages: [{id, title}], reason}` | engine → any (6.9) |
| `CORE_CONFIG_CHANGED` | event, engine only | `{version: int, sections: [str]}` | engine → any (6.3) |
| `CORE_CONFIG_ERROR` | event, engine only | `{count: int, errors: [{file, line, path, code}]}` (the first 20 errors) | engine → any (6.3) |
| `CORE_LOCALE_CHANGED` | event, engine only | `{locale: str}` | engine → any, or one instance (CORE-245) |
| WEATHER\_CURRENT | event | {location: {lat, lon, name?}, condition, is\_day, temperature, feels\_like?, humidity?, wind\_speed?, units, observed\_at} (fields: WTH-11) | weather → any (9.4) |

`LocalizedText` in a payload is a string or a map from BCP 47 tags to strings (CORE-101).

### 5.6 WebSocket protocol

Every browser holds one `/ws` connection that starts with a handshake, carries frames in both directions within limits, and recovers from any loss by a full resync (CORE-246).

- CORE-246: Frames are JSON objects `{v: 1, kind, instance?, data}`. The shell's first frame is `hello` with `{protocol: 1, client_id, role, viewport: [w, h], dpr, key?}`; `client_id` is a random UUID kept in `sessionStorage`, `role` is `kiosk`, `browser` or `preview`. The engine answers with `welcome` `{engine_build, client_id, role, interactive, layout, page, pages, locale, timezone, touch, custom_css, instances: [{id, module, element, config, locale, catalog, scripts, styles, appearance, status, touch}]}`, where `config` is the public config (CORE-13, CORE-101), locale the instance's locale (its override, else engine.locale; this.locale, CORE-266), `catalog` the version of its catalog URL, `scripts` and `styles` the URLs of the module's `scripts` and `styles` under `/modules/<module>/<version>/`, `appearance` is `{scale, align, valign, opacity, overflow, header, classes, css}` with `header` resolved to text or null and `css` the URL of the instance's `appearance.css` or null, `status` is `ok` or `unavailable` (CORE-117), timezone is engine.timezone (CORE-266), location is engine.location as {lat, lon, name} or null (CORE-288), touch is engine.input.touch (CORE-103), an instance's touch is true when its module has the input.touch capability and engine.input.touch is on (CORE-235), and `custom_css` the version of `/custom.css` or null; then one `state` frame per instance. Another first frame, or an unsupported protocol, closes the connection with code 4400.
- CORE-247: Roles. `kiosk` is accepted only from a loopback address with the key the kiosk agent got from the engine (CORE-268); `preview` only on the admin listener with an admin session (CORE-253); any other client is `browser`. A role that cannot be verified closes the connection with code 4403. The kiosk and preview clients are interactive; a browser is interactive only with a display session (CORE-254). `welcome.interactive` tells the shell which one it is.
- CORE-248: Inbound limits. Frames from a client that is not interactive are dropped and counted, except `hello` and `log`; this covers `notify`, `client`, requests and touch. A frame larger than 512 KiB closes the connection with code 1009. A frame that names a `CORE_*` notification, an instance the client does not show, or a registered name with an invalid payload (CORE-225) is not delivered and gets an `error` frame `{code, message_key, ref}`. Notifications and requests from an element have source `<module>/<instance>` of that element and start a new trace (CORE-111).
- CORE-249: Outbound limits. Each client has a queue of 1000 frames; a newer `state` frame for an instance replaces one still queued. When the queue is full the engine closes the connection with code 1013 and the client resyncs on reconnect. A notification goes to a browser once, with the list of its instances whose `subscribes` match (CORE-25; unset means all).
- CORE-250: Reconnect. After a lost connection the shell keeps the last render and reconnects after 0.5 s, doubling up to 10 s, with ±20 % jitter. Every reconnect is a full resync through `hello` and `welcome`. When `engine_build` differs from the build the page was loaded with, the shell reloads the page.
- CORE-251: Liveness. The engine sends a WebSocket ping every 15 s and drops a client that has not answered for 30 s.
- CORE-252: URLs. The mirror listener serves only `/` (the shell), `/core/…` (engine scripts, styles, fonts and vendored libraries), `/modules/<module>/<version>/…` (static files of installed modules, `Cache-Control: public, max-age=31536000, immutable`), `/i18n/<instance>.json?v=<hash>`, `/appearance/<instance>.css?v=<hash>` (the instance's `appearance.css`), `/custom.css` (CORE-262), `/pair` (CORE-254), `/ws` and `/ws/kiosk-agent` (CORE-268); anything else is 404. The admin listener serves the same shell in preview mode under `/admin/preview` (6.6).

* CORE-271: Frame contents. Every frame is `{v: 1, kind, instance?, data, ref?}`; an inbound frame may carry `ref` (a string of at most 64 characters), which the engine echoes in the `error` or `reply` about that frame. The `data` of each kind is given in the table below; the fields of each `control` action are given in CORE-275.

| Kind | Direction | `instance` | `data` |
| --- | --- | --- | --- |
| `hello`, `welcome` | shell → core, core → shell | — | As in CORE-246 |
| `state` | core → shell | the instance | `{state, delta}`; with `delta: true` the shell merges `state` into the current snapshot (shallow), otherwise it replaces it |
| `client` | both | the instance | `{type, payload}` |
| `notify` | shell → core | the sending element's instance | `{name, payload, target?}`; the engine sets id, trace, ts and source |
| `notify` | core → shell | — | `{notification, instances}`: the envelope of 5.1 as JSON and the ids of this client's instances whose `subscribes` match (CORE-249) |
| `request` | shell → core | the instance | `{rid, name, payload, target?, timeout}` |
| `reply` | core → shell | the instance | `{rid, payload}`, or `{rid, error: {code, message_key}}` (CORE-272) |
| `control` | core → shell | the instance, or none | `{action, …}`; `action` is `show`, `hide`, `reload`, `locale`, `layout`, `page`, `styles` or status (CORE-275) |
| `log` | shell → core | the instance, or none | `{level, message, stack?, user_agent?}` (CORE-114) |
| `visibility` | shell → core | the instance | `{hidden, fade?}` (CORE-276) |
| `touch` | shell → core | — | `{gesture}` (CORE-276) |
| `error` | core → shell | the instance, or none | `{code, message_key, ref}` (CORE-272) |

- CORE-272: Error and reply codes. An `error` frame carries `code` `reserved_name` (key `CORE_WS_RESERVED_NAME`, a `CORE_*` name), `unknown_instance` (`CORE_WS_UNKNOWN_INSTANCE`, an instance the client does not show), `invalid_payload` (`CORE_WS_INVALID_PAYLOAD`, a registered name with an invalid payload) or `invalid_frame` (`CORE_WS_INVALID_FRAME`, an unknown kind or a frame of the wrong shape after the handshake), and the inbound frame's `ref` or null. A `reply` to a request from the shell carries `error.code` `transient`, `configuration` or `internal` as in CORE-221, or `timeout` with key `REQUEST_TIMEOUT` when no reply arrives within the request's `timeout`.
- CORE-273: Endpoints. The mirror listener's `/ws` accepts the roles `kiosk` and `browser`. The preview shell connects to `/admin/preview/ws` on the admin listener, which accepts only the role `preview` and needs an admin session (CORE-253) and an `Origin` matching the listener. A role an endpoint does not accept closes the connection with code 4403. A connection whose `hello` does not arrive within 10 s is closed with code 4400. `engine_build` is `<mm3-core version>+<first 12 hex digits of the SHA-256 of the files under /core>`, so any change to the engine's front-end files makes open shells reload (CORE-250).
- CORE-274: The `layout` object in `welcome` and in `control` `layout` frames is `{viewport: [w, h], cell: [w, h], grid: {columns, rows, gap, padding}, pages: {<page id>: [{instance, col, row, width, height, z, preset, stack_index, stack_size, area: [w, h]}]}}`: the logical viewport and cell size in CSS pixels (6.7), the grid size with layout.gap and layout.padding in CSS pixels (6.5) and, per page, each visible instance's cells, its preset (or null), its place in a preset stack (CORE-212) and its area in CSS pixels.

* CORE-275: Control actions. The `data` of a `control` frame per `action`: `page` (no instance) `{page, index, count, transition, visible}`, where `transition` is `fade` or `none` and `visible` lists the ids of the instances visible after the change, so the shell switches page and visibility in one step (CORE-237, CORE-238); `show` and `hide` (one instance) `{fade}` in milliseconds (CORE-259); `status` (one instance) `{status}`, `ok` or `unavailable`, which shows or removes the failure placeholder (CORE-117); `locale` (one instance) `{locale, config, catalog}` (CORE-267); `styles` (no instance) `{custom_css}`, the new version of `/custom.css` or null, which the shell reloads; `layout` (no instance) `{layout}`, the object of CORE-274, used by the preview for the admin's draft (6.6); `reload` (no instance) `{scope}`: with `resync` the shell sends `hello` again and rebuilds from the new `welcome`, keeping elements whose instance, module and version are unchanged; with `page` it reloads the page. A hot reload that adds, removes or replaces instances, or changes their public config, appearance, position or pages, sends `reload` `resync` to every shell.
* CORE-276: Shell input frames. An element's `this.hide()` and `this.show()` send `visibility` `{hidden, fade?}` for its instance, which sets or clears the hide reason `self` (CORE-238). The shell sends `touch` `{gesture}` with `gesture` `swipe_left`, `swipe_right` (CORE-260) or `tap` (any other touch, which only pauses rotation, CORE-234). Both are accepted only from interactive clients (CORE-248); a preview client's `visibility` changes only its own view.

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
    allow: []           # extra LAN browsers (CIDR); view-only unless paired
  admin:                # admin UI + REST API, never shown on the mirror
    host: 0.0.0.0
    port: 8081
    allow: ["192.168.1.0/24"]
                        # tokens: mm3 token new (10.1), never in this file
  mqtt:
    url: mqtt://broker.lan:1883
    base_topic: mm3/mirror1
    ha_discovery: true

display:                # see 6.7
  output: HDMI-A-1
  width: 1920
  height: 1080
  rotation: 90          # portrait mirror → viewport 1080 × 1920

audio:                  # see 6.7
  sink: hdmi
  volume: 60

layout:                 # see 6.5
  grid: { columns: 12, rows: 12 }
  gap: 16px
  padding: 32px

modules:
  - module: presence      # backend-only, no position
    config:
      mqtt:
        - { topic: zigbee2mqtt/hall_pir, field: occupancy }
      off_after: 10m
      schedule:
        - { off: "23:30", on: "06:45" }
      display: kiosk          # through the kiosk agent (CORE-346)
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

- CORE-201: The top-level keys are `config_version` (integer, default 1; a value above the engine's own is an error), `engine`, `display` (6.7), `audio` (6.7), `layout` (6.5), `modules` and `backup` (10.8); any other key is an error. All `engine` keys are optional:

| Key | Values | Default | On hot reload (6.3) |
| --- | --- | --- | --- |
| `locale` | BCP 47 tag | `en` | live (`CORE_LOCALE_CHANGED`) |
| `timezone` | IANA name | system time zone, else UTC | live (CORE-189) |
| `units` | `metric`, `imperial` | `metric` | live; instances with a backend restart |
| location | lat, lon, name (CORE-288) | absent: no location | live; instances with a backend restart |
| `server.host`, `server.port` | address, port | `127.0.0.1`, `8080` | next engine restart |
| `server.allow` | CIDR list (CORE-61) | `[]` (loopback only) | live, for connections made afterwards |
| `admin.host`, `admin.port`, `admin.allow` | address, port, CIDR list (no token: CORE-62) | `127.0.0.1`, `8081`, `[]` | next engine restart |
| `mqtt` | see 9.3 | absent: bridge off | live (reconnect) |
| `timeouts.*` | seconds | as in 3.2 | live, for calls started afterwards |
| `bus.queue_size` | integer ≥ 1 | 1000 | live, for inboxes created afterwards |
| `threads` | integer ≥ 1 | 8 | next engine restart |
| `log_level` | `debug`, `info`, `warning`, `error` | `info` | live |
| `debug_ttl` | duration | 30 min | live |
| `history.keep` | integer ≥ 10 | 200 | live |
| `input.touch` | boolean | `false` | live (shell reload) |

- CORE-288: `engine.location` is an optional object `{lat, lon, name}`: `lat` from −90 to 90 and `lon` from −180 to 180 (`CORE_CFG_RANGE` otherwise), `name` an optional `LocalizedText` for display. It is the mirror's place for modules that need one (clock sun and moon times, weather); a module's instance may override it in its own `config`. Backends read it as `ctx.location`, an object with `lat`, `lon` and `name` (resolved along the instance's locale chain, 8.2, or `None`), or `None` when it is not set; browsers read `this.location` with the same shape, built from the `location` entry of the `welcome` frame (CORE-246) and resolved the same way. A change is applied like `units`: shells resync (CORE-275) and instances with a backend restart (CORE-211). The location reaches every browser that may connect, so it is part of the public configuration and not a secret.

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
| `CORE_CFG_CSS` | error | `appearance.css` or custom.css breaks a limit of CORE-214 or CORE-262 | `reason` |
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
| audio changed | Kiosk agent applies output, volume and mute live; nothing else restarts (CORE-296) |
| pages block changed | Re-layout; current page kept if it still exists, otherwise default (CORE-233) |
| Instance pages or positions changed | Front-end re-layout only, backend untouched |
| `custom.css` changed | Every shell reloads its styles (control `styles`), no re-render, backends untouched (CORE-262) |

- CORE-208: A reload starts on a change to the main file, an included file, the widget template directory, custom.css or the env file (all watched), on SIGHUP, and after a history operation (6.8). It starts 500 ms after the last change event. Reloads run one at a time; changes that arrive during a reload cause exactly one more.
- CORE-209: An invalid configuration never replaces the running one, whatever started the reload. The engine keeps its current version, logs every record (CORE-206), emits `CORE_CONFIG_ERROR` with `{count, errors}` (file, line, path and code of the first 20) and reports `config: {version, disk_valid: false}` in `GET /api/v1/status`. Invalid files never become history versions.
- CORE-210: Once validation passes, the new version is recorded (6.8) and applied in this order: stop removed and replaced instances; call `reconfigure()` on instances whose config changed, restarting those that return False; start added instances; push layout and display changes to browsers. A DISABLED instance whose config changed is restarted (CORE-195). Then the engine emits `CORE_CONFIG_CHANGED` with `{version, sections}`.
- CORE-211: `ctx.locale`, `ctx.timezone` and `ctx.units` always hold the current values. Changes to `engine.locale`, `engine.timezone` or an instance `locale` are applied live; a change to `engine.units` restarts every running instance that has a backend; a change to engine.location does the same, and ctx.location holds its current value (CORE-288).

* CORE-245: `CORE_LOCALE_CHANGED` carries `{locale}`. A change to `engine.locale` broadcasts it with the new `engine.locale`; a change to an instance's `locale` sends it only to that instance, with the instance's new locale. Modules read `ctx.locale` for the locale that applies to them.

### 6.4 Web configuration editor

The configuration can be edited through three synchronized views (layout, forms, YAML) at `/admin/config`, on the admin listener only; the mirror display never serves or shows it (CORE-34).

- **Two listeners.** `engine.server` (default 127.0.0.1:8080) serves only the shell, module assets and `/ws`; `/admin` and `/api` return 404 there. `engine.admin` (default 8081) serves admin UI and REST, guarded by the CIDR allow list and token (CORE-61, CORE-62).
- **Schema.** `GET /api/v1/config/schema` returns one JSON Schema generated from the engine model and every installed module's `config_model`, including the `!env` and `!include` tags and the region enum. All three views are driven by it.
- **Layout view.** A live, scaled preview of the real mirror with an editing overlay (6.6); module instances show their real content, snap to cells and can be moved, resized from their corners, layered, or dropped on a region preset, added from a catalog of installed modules, enabled/disabled, or deleted. Backend-only modules (e.g. presence) appear in a separate "Services" lane.
- **Form view.** Selecting a card opens a form generated from that module's schema: text, number with min/max, enum dropdowns, toggles, lists, nested objects, duration and time pickers, and a secret field that can hold a value or an `!env` reference. Field labels and help text are translation keys resolved from the module catalog (CORE-310), so forms appear in EN, IT, FR, ES, DE.
- **YAML view.** CodeMirror 6 with YAML language support, JSON-Schema linting and autocompletion, for advanced edits and for anything the forms cannot express.
- **Synchronization.** Layout and form edits are sent as JSON Patch operations; the server applies them to the ruamel.yaml document and returns the new YAML, so comments and key order survive GUI edits. Switching to the YAML view always shows the true file content, one tab per file of the config set (CORE-301).
- **Validation while editing.** Forms validate each field on change; the YAML view checks syntax and schema 300 ms after the last keystroke (CORE-323). Errors show inline on the field, on the layout card, and in the YAML gutter, with localized messages.
- **Authoritative validation.** `POST /api/v1/config/validate` runs the full engine validation (unknown module, duplicate instance id, missing env var, module-specific checks) and returns `[{line, col, severity, path, message}]`; `path` lets the GUI map errors to fields. Save is disabled while any error remains (CORE-35).
- **Save.** `PUT /api/v1/config` with `If-Match: <etag>`: the server validates again (the diff preview comes from validate, CORE-302), writes atomically (temp file + rename), and records a new version in the configuration history with undo, redo and restore (6.8). Hot reload (6.3) then applies it.
- **Conflicts.** If the file changed on disk since it was loaded, save returns 409 with the current files so the user can merge (CORE-300).
- **Secrets.** `!env NAME` references are shown as written; literal secret values are masked and kept unchanged unless the user types a new one (CORE-303).
- **Front-end stack.** Admin UI built with the same no-build Web Components as the shell: an in-house schema-to-form renderer (`<mm3-schema-form>`), an in-house editing overlay for grid drag, resize and snapping (CORE-326), CodeMirror 6 as a vendored ES-module bundle (CORE-323).
- CORE-36: File edits and GUI edits go through the same validator and produce identical runtime config; the GUI can never write a file that fails `mm3 check-config`.
- CORE-37: A module MAY add `json_schema_extra={"ui": {...}}` hints (widget, order, group, hidden) to its fields; without hints, the form is generated from types alone.

**Editor API**

- CORE-300: Config ETag and conflicts. The ETag of the config is `"<SHA-256 of the config set>"`, the hash of CORE-153, quoted; `GET /api/v1/config` returns it in the `ETag` header and the body. `PUT /api/v1/config` and the history actions that write (undo, redo, restore) need `If-Match` with it; without it they return 428 `CORE_API_PRECONDITION_REQUIRED`. When it no longer matches the files on disk, they return 409 `CORE_API_CONFLICT` with `etag` (the current one) and `files` (the current content of every file of the set, secrets masked as in CORE-303); when the files on disk are invalid (CORE-209), the code is `CORE_API_DISK_INVALID` and `errors` carries their records. Nothing is written in either case.
- CORE-301: Editing protocol. The admin page holds the draft (6.6, CORE-151); the server keeps none. `GET /api/v1/config` returns `{etag, version, files: [{path, role, content}]}`, with `path` relative to the config directory and `role` `main`, `include`, `widget_template` or `custom_css`; the YAML view shows one tab per file. `POST /api/v1/config/patch` takes `{base, files, ops}`: `base` is an ETag, `files` are `[{path, content}]` edited in the YAML view since `base`, and `ops` are `[{file, patch}]`, each `patch` an RFC 6902 JSON Patch on that file's document before `!env` substitution, with `!include` and `!env` nodes kept as nodes. The server applies them to the ruamel.yaml documents, so comments and key order survive, and returns `{files, records, documents, layout}`: the new content of every changed file, the full validation records (CORE-35), and `documents`, the data of every YAML file of the draft by path, before `!env` substitution, with tagged nodes as in CORE-320 and secret values as `{"!masked": null}`; a file that does not parse maps to null. layout is the layout object of CORE-274 for the draft, with an entry for every page of the draft; instances whose position cannot be resolved are left out (their records say why), and it is null when the top-level configuration does not validate (CORE-201), for example a bad layout, display or pages block. The Form view reads its values from `documents`, so the page calls the route with no `ops` after loading. It writes nothing. A `base` that is not current returns 409 as in CORE-300; an operation that cannot be applied returns 422 `CORE_API_PATCH_FAILED` naming its index.
- CORE-302: Validate and save. `POST /api/v1/config/validate` takes `{base, files}`, the full draft content of the changed files, and returns `{records, diff, layout}`: `records` as in CORE-35 and `diff` the change against the current version per section (CORE-216), each `{section, before, after}` with secrets masked; the admin shows `diff` as the preview before Save, and layout is as in CORE-301. `PUT /api/v1/config` takes the same body with `If-Match`, validates again and writes as in CORE-217; it returns 200 with `{version, etag}`. With any record of severity error it writes nothing and returns 422 `CORE_API_CONFIG_INVALID` with the records. A save whose content equals the current version returns 200 with the current version and creates none.
- CORE-303: Masked secrets. Every API response and admin view shows the literal value of a secret field (CORE-13) as the YAML tag `!masked` with no value. A `!masked` node sent back means "keep the value of `base`": the server puts the original value back before it validates or writes. A `!masked` node at a path with no secret value in `base` returns 422 `CORE_API_MASKED_UNKNOWN`. `!env` references are shown and kept as written (CORE-218).
- CORE-310: Form texts. Labels and help come from the catalog of the module that owns the model, keys `CONFIG_<PATH>_LABEL` and `CONFIG_<PATH>_HELP`, where `<PATH>` is the field path in UPPER\_SNAKE with nested names joined by `_` (`CONFIG_UNITS_LABEL`, `CONFIG_PROVIDER_API_KEY_HELP`); engine fields use the core catalog with the section first (`CONFIG_ENGINE_LOCALE_LABEL`). A missing label shows the field name and a missing help shows nothing; `mm3 i18n check` does not require them. A `LocalizedText` field (CORE-101) shows one input per language it holds, plus an "add language" control.
- CORE-320: Tagged nodes in JSON. In JSON Patch values (CORE-301) a tagged node is an object with one key, the tag: `{"!env": "NAME"}` or `{"!env": ["NAME", "default"]}`, `{"!include": "path.yaml"}` and `{"!masked": null}` (CORE-303); the server writes them as the YAML tags. A patch path never points inside a tagged node. Any other one-key object whose key starts with `!` is refused with 422 `CORE_API_PATCH_FAILED`. The patch implementation is in-house, in the YAML adapter, and applies `add`, `remove`, `replace`, `move`, `copy` and `test` to the ruamel.yaml nodes, so comments and key order survive.
- CORE-322: Admin config page. `/admin/config` on the admin listener is one page with the tabs Layout, Form, YAML (6.4, 6.6) and History (6.8); the History tab lists the versions with their changed sections, shows the diff between any two versions as a whole or per section, and offers Restore all and Restore section, each after a confirmation that shows the diff, and Open in editor, which loads the version as a draft.
- CORE-323: YAML view checks and bundle. The YAML view's schema check is `POST /api/v1/config/validate` with the draft, 300 ms after the last keystroke; its records are the authoritative ones of CORE-35, shown in the gutter. Syntax errors show at once from the CodeMirror parser. Autocompletion of keys and enum values is in-house, from `GET /api/v1/config/schema`; there is no client-side schema library. CodeMirror 6 is vendored as one ES-module bundle built once by `tools/vendor_codemirror.mjs` with esbuild from pinned npm packages; the bundle and `vendor.json` (packages, versions, licenses) are committed. esbuild is a development tool of that script only, never a dependency of `mm3-core` or present on a mirror.
- CORE-324: Version files. `GET /api/v1/config/history/{version}` returns `{version, files}` with the files of that version as in `GET /api/v1/config` (roles, secrets as `!masked`, CORE-303); an unknown version is 404 `CORE_API_NOT_FOUND`. Open in editor (CORE-322) loads them as the draft; Save then writes it against the current ETag, and `!masked` nodes keep the values of the current files.

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
- CORE-255: Element setup. For each visible instance the shell creates its element, adopts the module styles, the appearance variables and `appearance.css` as constructable stylesheets, calls `onResize` with its area, then `render` with the snapshot (`{}` until one arrives, Contract MOD-36). An exception in `render` or in any callback is caught and logged through the element's logger, and the cell shows the failure placeholder (CORE-117) until the next successful render.
- CORE-256: Header and size. With `appearance.header` the shell draws the header above the element, outside its shadow root, styled by the theme (6.10). `onResize` reports the area below the header in whole CSS pixels and is called at most once per animation frame.
- CORE-257: In a preset stack (CORE-212) each element's `onResize` gets the preset width and the preset height divided by the number of visible instances in the stack, as the validator assumes.
- CORE-258: `overflow: fit` scales the element down uniformly with a CSS transform until its content fits its area, never up; it is recomputed after every render and resize. `scroll` makes the area scrollable by touch with no visible scrollbar.
- CORE-259: Transitions. `this.hide()` and `this.show()`, and visibility changes (CORE-239), fade over 300 ms; `this.hide({fade: ms})` and `this.show({fade: ms})` set another duration, 0 for none. A page change cross-fades over the page transition (CORE-237). Hidden elements stay in the DOM with `display: none` and keep their state.

### 6.6 Live layout preview

The layout view shows the actual mirror, with real module content, scaled into the admin page; positions are edited directly on it and reach the real display only on Save (CORE-97).

- **Preview shell.** The admin listener serves the normal shell at `/admin/preview` in preview mode, embedded in an iframe. It loads the same Web Components and receives the same `state` frames as the kiosk, so no module backend does extra work or extra API calls.
- **Resolution.** The preview renders at the logical viewport the engine uses (6.7): the one the kiosk agent applied and reported in its `outputs` frame (CORE-145, CORE-277), or, with no kiosk connected, the one derived from the `display` block (CORE-215). There is only one kiosk (CORE-268), so there is nothing to pick. The iframe is scaled with a CSS transform to fit the admin window, keeping the aspect ratio.
- **Editing overlay.** A transparent layer above the iframe draws grid lines and a handle box per instance. Drag moves, corner handles resize, a toolbar sets layer, appearance and enable/disable. Everything snaps to whole cells; invalid placements (overlap on the same layer, outside the grid) turn red and cannot be dropped.
- **Draft.** Edits form a draft layout held in the admin page and applied to the preview through `control` / `layout` frames. The same draft is converted to JSON Patch for the YAML and form views (6.4), so all three views stay in sync before saving.
- **Show on mirror.** An optional toggle pushes the draft to the real display for checking from across the room; it reverts automatically after 60 s or when the admin page closes, unless saved (CORE-307).
- CORE-98: In preview mode `MirrorElement.preview` is true. Modules with side effects or sensitive content (camera, microphone, private calendars) SHOULD render a placeholder of the same size instead.
- CORE-99: Preview clients are not counted as displays: they never trigger presence, suspend or HA state, and they receive no `client` messages meant for the kiosk.
- CORE-307: Show on mirror. The toggle sends the draft layout object (CORE-274) with `POST /api/v1/preview/mirror`; the engine sends it to the kiosk shell only, as a `control` frame with action `layout` (CORE-275), never to LAN browsers. A push answers 204. The draft stays until `DELETE /api/v1/preview/mirror`, a save, 60 s after the last push, or until the admin session that pushed it has no open `/admin/ws` connection (CORE-304); then the engine sends the running layout again; the admin turns its toggle off 60 s after its own last push. A push from another admin replaces the draft shown. Without a connected kiosk the request returns 409 `CORE_API_NO_KIOSK`.
- CORE-325: Preview messages. The admin page and its preview iframe (`/admin/preview`, same origin) talk with `postMessage`: the page sends `{kind: "control", action: "layout", layout}` with the draft layout of CORE-301 after every draft change, and `{kind: "control", action: "page", page}` with the page of the selector (CORE-242). The shell in preview mode accepts messages only from its parent window with its own origin and applies them as the `control` frames of CORE-275; once a page was set this way it ignores `page` frames from the engine. An instance of the draft layout without a running element (new in the draft, or a changed module) is shown as an empty cell labelled with its id. The page selector starts on the engine's current page.
- CORE-326: Editing overlay. The overlay is in-house, with no grid library: pointer drag moves an instance, corner handles resize it, and both snap to whole cells. While dragging, a placement that overlaps another instance on the same `z` (a preset stack counts as one rectangle, CORE-93) or leaves the grid is drawn red and is not applied on drop. Moving or resizing an instance placed on a preset writes a rectangle; the toolbar's preset picker writes a preset name. Stacked instances are drawn at their own place in the stack (CORE-274), and moving one takes it out of the stack. Every overlay edit is one draft step (CORE-151), applied as JSON Patch on the file that holds the instance (CORE-301). Disabled instances are listed below the preview with an Enable action.
- CORE-327: Page writes. An overlay edit on the selected page writes `positions.<page>` when the instance has one; otherwise `position` when the instance has `pages: all` or a single page; otherwise it creates `positions.<page>`. An override that afterwards equals `position` is removed. Without a `pages` block the page is `main` and edits write `position`.
- CORE-328: Overlay toolbar. For the selected instance the toolbar shows its id, module and size in cells and in CSS pixels (CORE-142), and sets `z` (rectangles only; presets have a fixed `z`, CORE-92), the preset or none, `appearance.scale`, `align`, `valign`, `opacity`, `overflow` and `header` as true or false (a `LocalizedText` header is edited in the Form view), and Enable or Disable, which writes `disabled` (the "Save as disabled" of CORE-240 goes through the normal save). "Edit in Form" opens the instance in the Form view. When rectangles of the draft lie outside its grid, the Layout view offers "Rescale proportionally" (CORE-94): every 0-based cell boundary b of a rectangle becomes round(b × new / old) on each axis, halves rounded up, each side at least one cell, as one draft step.

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

- CORE-143: `mm3-kiosk.service` is a system unit running as the user `mm3-kiosk` (groups `video`, `input`, `render`; no write access to `/etc/mm3` or the engine state directory) with `PAMName=login` and `TTYPath=/dev/tty1`, so it owns a logind seat session. It runs `cage -- mm3 kiosk session`. The kiosk session applies output, mode, refresh, rotation and scale through the compositor's output management (`wlr-randr`), then starts Chromium. Display changes on hot reload are re-applied live; no restart of the kiosk. The kiosk needs cage ≥ 0.1.5 (wlr-output-management) and is supported on Debian 13 and later. Before anything else, `mm3 kiosk session` reads the version printed by `cage -v` and compares only its leading `MAJOR.MINOR.PATCH`, ignoring any suffix (Debian 13 ships `0.1.5+20240127`); it does not check the Debian release. If the version cannot be read or is older than 0.1.5, it refuses to start and says why. The kiosk uses no cage option or behaviour added after 0.1.5. The agent lists outputs with wlr-randr --json (Debian 13 ships wlr-randr 0.4.1) and applies them with wlr-randr --output, --mode WxH@RHz, --transform and --scale. The unit file ships inside the mm3-core package (mm3/core/kiosk/mm3-kiosk.service) and mm3 kiosk unit prints it; the installer puts it in place. Sound plays in this session through PipeWire (CORE-295); no group or unit setting is added for it.
- CORE-144: The kiosk agent reports the outputs, the modes each supports, and the applied viewport and device pixel ratio in its `outputs` frame (CORE-277) when it connects and after every change. The admin form offers `output` and the mode as dropdowns of these reported values.
- CORE-145: If the requested mode is not supported, the kiosk uses the closest supported mode (same aspect ratio first, then the largest not above the request). The mismatch is shown in the admin and the debug page, and the engine uses the viewport actually applied.
- CORE-146: When the applied viewport changes, the engine emits `CORE_DISPLAY_CHANGED` with `{viewport: [w, h], scale, rotation}`, and every element receives `onResize` (CORE-95). When the applied mode differs from the one the engine used, the engine validates the running configuration again with the applied mode (CORE-215): if it stays valid it replaces the running one without a new history version, the shells resync (CORE-275) and CORE\_DISPLAY\_CHANGED is emitted when the viewport changed; if not, the running configuration stays and the errors are reported as for a reload (CORE-209).
- CORE-268: Kiosk agent. The kiosk session connects to `/ws/kiosk-agent` on the mirror listener, which accepts only loopback connections that present the agent key the engine writes at start to `/run/mm3/kiosk.key` (mode 0640, group `mm3-kiosk`); the agent presents it as Authorization: Bearer \<key> on the WebSocket upgrade, and a missing or wrong key or a source that is not loopback closes the connection with code 4403. Over it the engine sends the `display` block, and the agent applies it and reports outputs, modes, the applied mode and failures (CORE-144, CORE-145). For each Chromium launch the engine issues a one-time kiosk key, which the agent passes in the start URL fragment (`/#kiosk=<key>`) so the shell can claim the `kiosk` role (CORE-247). The engine runs as user mm3, which is a member of group mm3-kiosk (created by mm3-install.sh, CORE-293); that membership lets it give the key file the group mm3-kiosk. The kiosk user is not a member of group mm3. When the group is missing or the change of group fails, the engine logs a warning kiosk\_key\_group naming the group and keeps running; the key then stays readable by group mm3 only and the kiosk agent cannot connect (CORE-145).
- CORE-269: Supervision. The agent starts Chromium only once the mirror listener answers (the screen stays black meanwhile). It restarts Chromium when it exits, and when the engine reports that the kiosk shell has not connected or answered pings for 60 s, with backoff from 1 s to 30 s. After 5 restarts in 10 minutes it stops trying, and the engine reports the kiosk as failed in `GET /api/v1/status` and on the debug page.
- CORE-270: Chromium runs with a profile in the kiosk user's state directory and at least `--kiosk`, `--ozone-platform=wayland`, `--no-first-run`, `--noerrdialogs`, `--disable-session-crashed-bubble`, `--disable-translate`, `--disable-pinch`, `--overscroll-history-navigation=0` and `--autoplay-policy=no-user-gesture-required`, plus the hardware video decoding features of CORE-297; the pointer cursor is hidden. The full list lives in `mm3.core.kiosk` and is shown by `mm3 kiosk session --print-command`.
- CORE-277: Kiosk agent protocol. Frames on `/ws/kiosk-agent` are `{v: 1, kind, data}`. The agent sends `hello` `{protocol: 1, cage_version}` first, then `outputs` `{outputs: [{name, modes: [{width, height, refresh}], current}], applied: {output, width, height, refresh, rotation, scale}, viewport: [w, h], dpr}` (CORE-144, CORE-145), `chromium` `{state, restarts}` with `state` `started`, `exited` or `failed` (CORE-269), and `failure` `{reason}` when the display block cannot be applied. The engine sends `display` `{display}` (the `display` block, at connect and on every change), `launch` `{key}` (the kiosk key for the next Chromium launch, sent before each launch when the agent asks with `chromium` `exited` or at connect) and `restart` `{reason}` when the kiosk shell has not connected or answered pings for 60 s (CORE-269). Another first frame or protocol closes the connection with code 4400. For audio (CORE-295, CORE-296) the agent also sends sinks {sinks: \[{name, description, kind, available}\], applied: {sink, volume, mute, matched}} after hello and after every change, where kind is hdmi, analog, usb, bluetooth or other, sink is the name of the output in use (null when none) and matched is false when the requested output was not found; the engine sends audio {sink, volume, mute} at connect and on every change, carrying the audio block with any run-time volume and mute. failure carries area, display or audio (absent means display). The protocol stays 1. In outputs, current is the output's current mode {width, height, refresh}, or null when the output is off; the agent itself picks the mode to apply (CORE-145) and reports it in applied.
- The `display` block describes the local kiosk only. Other LAN browsers showing the shell render at their own window size with the same grid.

**Audio**

The kiosk plays sound through PipeWire in the kiosk user's own session. The optional `audio` block chooses the output and its level; the kiosk agent applies it live, as it does for `display`. It describes the local kiosk only: what other browsers do with media is settled with the media service (milestone 8).

```yaml
audio:
  sink: hdmi      # auto | hdmi | analog | name of an output reported by the agent
  volume: 60      # optional, percent
  mute: false
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `sink` | string | `auto` | `auto` keeps WirePlumber's default output; `hdmi` or `analog` picks the first available output of that kind; any other value is the exact name of an output reported by the agent (CORE-296) |
| `volume` | int 0–100 | not set | Volume of the chosen output; when absent the kiosk leaves the volume as it is |
| `mute` | bool | `false` | Mutes the chosen output |

Without the block the kiosk keeps PipeWire's default output and volume. An output name that the agent does not report is not a configuration error; the mismatch is shown as for a mode (CORE-145).

- CORE-295: Audio session. The logind session of `mm3-kiosk.service` (CORE-143) starts the systemd user manager of `mm3-kiosk`, which runs PipeWire, pipewire-pulse and WirePlumber as the socket-activated user units Debian ships; there is no linger and no system-wide PipeWire. Chromium plays through pipewire-pulse at `$XDG_RUNTIME_DIR/pulse/native`. Sound devices are reached through the seat ACLs that logind gives the active session (uaccess); `mm3-kiosk` is not a member of group `audio`. The home of `mm3-kiosk` is its state directory `/var/lib/mm3-kiosk`, where WirePlumber keeps its state. The kiosk needs the Debian packages `pipewire-audio`, `wireplumber`, `dbus-user-session` and `pulseaudio-utils` (CORE-293). When the PipeWire server cannot be reached, the agent sends `sinks` with an empty list and a `failure` with area `audio` (CORE-277); the kiosk keeps running without sound.
- CORE-296: Output and volume. The engine sends the `audio` block to the agent at connect and on every change (CORE-277), and a change is applied live, without restarting Chromium or the kiosk. The agent lists outputs and cards with `pactl --format=json list sinks` and `pactl --format=json list cards`, classifies each output as `hdmi`, `analog`, `usb`, `bluetooth` or `other` from its card and port, and applies the block with `pactl set-default-sink`, `set-sink-volume` and `set-sink-mute`. When the requested output belongs to a card profile that is not active (HDMI and analog outputs of one HDA card), the agent first switches the card with `pactl set-card-profile`. When the requested output is missing or unavailable, the agent keeps the current default output and reports `matched: false`; the engine shows the mismatch in the admin and on the debug page, as for a mode (CORE-145). The agent reports again whenever an output appears, disappears or changes availability, checking at the same interval as the display outputs (CORE-144). Volume and mute changed at run time (admin, milestone 6; MQTT, milestone 7) reach the agent the same way and are never written to the configuration or its history; when the agent connects again, the configured values apply.
- CORE-297: Hardware video decoding. On systems with the kiosk the installer installs `mesa-va-drivers` and `intel-media-va-driver` (CORE-293); `mm3-kiosk` reaches the render node through group `render` (CORE-143). Chromium gets the VA-API features for Wayland (`--enable-features=AcceleratedVideoDecodeLinuxGL`), kept with the other flags in `mm3.core.kiosk` (CORE-270). Hardware decoding is best effort: without a working VA-API driver Chromium decodes in software, and the kiosk does not report it.

### 6.8 Configuration history, undo and redo

Every change to the configuration can be undone and redone, both while editing and after saving, for one module or core section or for the whole file (CORE-150).

**Editor level (unsaved changes)**

- CORE-151: The admin editor keeps one undo/redo history for the current draft, shared by layout, form and YAML views: Ctrl+Z undo, Ctrl+Shift+Z or Ctrl+Y redo, plus toolbar buttons. Each step is a JSON Patch with a readable label ("Move clock to 1,1", "weather.lat 42.1 → 42.2"); at least 200 steps.
- Typing in the YAML view is grouped into one step per pause of 1 s or per view switch. The history is lost on page reload; the draft itself is kept in the browser until saved or discarded.
- CORE-152: Section undo: each module card, form and section of CORE-216 has "Undo last change here", which reverts only the latest step touching that section, leaving later steps elsewhere intact.

**Saved versions**

- CORE-153: Every valid saved configuration becomes a numbered version in `<state>/config-history/` (`<state>` as in 3.1), one directory per version. It holds the whole config set as it was (the main file, every included file, the widget templates and custom.css, with their paths relative to the config directory) and `meta.json` with version, time (UTC), origin (initial, admin, file, cli, restore, undo, redo), author (admin client address, `cli:<user>` or `file`), parent version, changed sections and the SHA-256 of the set. The env file is never stored. When the history is empty, the engine records the running config as version 1 (origin initial). History is append-only; nothing is rewritten.
- CORE-216: The sections are `engine`, `display`, `layout`, pages, `backup`, each module instance (`modules/<id>`), the order of the modules list (`modules/_order`), audio (CORE-296), custom\_css (CORE-262) and each widget template (`widget_templates/<name>`). Changed sections are computed on the resolved values before `!env` substitution; a change to comments or formatting only is a version with no changed sections.
- CORE-154: Undo and redo of saves. An ordinary version's parent is the version current when it was made; an undo or redo version takes the parent of the version whose content it restores. "Undo last save" creates a new version (origin undo) with the content of the current version's parent. "Redo" creates a new version (origin redo) with the content undone most recently; it is offered while the latest versions are undos and redos, and any other save ends the redo chain. Example: after saves v1–v3, undo gives v4 = v2, a second undo v5 = v1, redo v6 = v2, redo v7 = v3. If the target no longer validates, nothing is applied and no version is created (CORE-156). Hot reload (6.3) applies each new version.
- CORE-155: Section restore: from any version, a single section (one module instance by id, `engine`, `display`, `layout`, pages, `backup`, a widget template, custom\_css, audio) can be restored alone; the rest of the current config stays as it is. The section is written back into the file that holds it now; an instance that no longer exists is appended to the `modules` list, in the file that holds that list. The result is a new version.
- The history view lists versions with their changed sections, shows the diff between any two versions as a whole or per section, and offers "Restore all", "Restore section" and "Open in editor".
- CORE-156: A restored version or section always goes through full validation. If it fails (for example a module no longer installed), nothing is applied and it can be opened in the editor to fix.
- CORE-217: The engine, the admin save and the `mm3 config` commands write config files and history under one lock (`<state>/config-history/.lock`, flock). Each file is written atomically (temp file + rename), keeping its owner and mode. When the watcher sees a config set whose SHA-256 equals the latest version's, it records no new version. `mm3 config` runs as the `mm3` user and works whether the engine runs or not; a running engine applies the result through its watcher. `mm3.service` therefore has write access to `/etc/mm3` and to its state directory (`ReadWritePaths`, CORE-65).
- CORE-218: Retention: a version is kept while it is among the newest `engine.history.keep` (default 200) or younger than 180 days; the current version is never removed. History directories are 0750 and files 0640, owned by `mm3`. In every view, the CLI and the API, `SecretStr` fields (CORE-13) are masked and `!env` references are shown as written.

**CLI**: `mm3 config history [--limit N] [--json]`, `mm3 config diff <a> <b> [--section <path>] [--json]`, `mm3 config undo`, `mm3 config redo`, `mm3 config restore <version> [--section <path>]`.

- CORE-219: Commands that create a version print its number. Exit codes: 0 done, 1 nothing to undo or redo, 2 validation failed and nothing applied (CORE-156), 3 refused because the files on disk differ from the current version and are invalid (CORE-209); with `--force` the on-disk edit is overwritten and lost.
- CORE-321: History API. `GET /api/v1/config/history` returns `{items, next}` paged as in CORE-299, newest first, each item `{version, time, origin, author, parent, sections, sha256, current}`. `GET /api/v1/config/history/{a}/diff/{b}` returns `[{section, before, after}]` as the diff of CORE-302, with `?section=` for one section. `POST /api/v1/config/undo` and `/config/redo` take no body; `POST /api/v1/config/restore` takes `{version, section}` (`section` optional, a section of CORE-216). All three need `If-Match` (CORE-300) and return `{version, etag}` with the new version. Nothing to undo or redo is 409 `CORE_API_NOTHING_TO_DO`; a target that fails validation (CORE-156) is 422 `CORE_API_CONFIG_INVALID` with the records and nothing is applied; an unknown version or section is 404 `CORE_API_NOT_FOUND`.

### 6.9 Pages and visibility

Pages are alternative layouts of the same instances on one screen: the core shows one page at a time, and the visibility of every instance follows one set of rules (CORE-230).

```yaml
pages:
  default: home
  rotate: 30s              # 0 = no automatic rotation
  pause_on_interaction: 2m
  reset_on_resume: true
  return_after: 10m        # back to default when idle; 0 = off (CORE-335)
  transition: fade          # fade | none
  list:
    - id: home
      title: { en: Home, it: Casa }
    - id: energy
      title: { en: Energy, it: Energia }
      rotate: 15s           # per-page override
    - id: guest
      title: { en: Guest, it: Ospite }
      cycle: false          # only PAGE_SET, REST, HA (CORE-333)
      after: { time: 5m }   # then back to default (CORE-334)
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
- CORE-235: Touch: with `engine.input.touch`, a horizontal swipe across at least 25 % of the viewport width changes page (swipe left = next, right = previous). A swipe that starts on the element of a module with the `input.touch` capability goes to that element and does not change page; the shell knows those elements from the touch flag of their welcome entry (CORE-246).
- CORE-260: A swipe is a single-pointer gesture whose horizontal travel is at least 25 % of the viewport width and at least twice its vertical travel, completed within 1 s. Swipes, and touches that pause rotation (CORE-234), count only from interactive clients (CORE-247); preview clients never change the page (CORE-239).
- CORE-236: Control surfaces: REST `GET /api/v1/pages` (list and current page), `POST /api/v1/pages/{id}`, `/pages/next`, `/pages/prev`; a Home Assistant select entity "page" (options are the page titles, state is the current page) through MQTT discovery (9.3).
- CORE-237: After each change the engine emits `CORE_PAGE_CHANGED` (5.5; `reason` is rotate, notification, touch, rest, ha, resume, reload or start) and sends a `control` frame `page` to every shell, which switches with the configured transition (fade 300 ms, or none). `ctx.page` (current page id) and `ctx.pages` (ids and resolved titles) are always current; front-ends read `this.page`.
- CORE-333: Pages out of the cycle (extends CORE-230 and CORE-233–235). A pages.list entry may set cycle: false (default true). Such a page is never reached by rotation, PAGE\_NEXT, PAGE\_PREV or a swipe; it is shown only through PAGE\_SET, REST, Home Assistant, a timed jump (CORE-334) or as default. From a page out of the cycle, PAGE\_NEXT, PAGE\_PREV and swipes go to the next or previous page in the cycle, in list order from its position, and rotation does not run on it. When no page is in the cycle they change nothing. The Home Assistant select and GET /api/v1/pages list every page, and GET /api/v1/pages gives each page's cycle flag.
- CORE-334: Timed jump (extends CORE-230 and CORE-234). A pages.list entry may set after: {time, page}, where time is a duration above 0 and page a listed page id other than the entry's own (default: pages.default). An unknown id is CORE\_CFG\_PAGE\_UNKNOWN; the entry's own id is CORE\_CFG\_INVALID. When that page has been current for time, the engine changes to page. A touch on the kiosk (CORE-260) restarts the count. Rotation does not run on a page with after. The count stops while the mirror is suspended or paused (CORE-336) and then starts again from zero.
- CORE-335: Idle return (extends CORE-230). pages.return\_after is a duration (default 0, off). On a page other than default where neither rotation (CORE-234) nor a timed jump (CORE-334) is active, the engine changes to default once return\_after has passed without interaction. An interaction is a page change not made by rotation, a timed jump or idle return, or a touch on the kiosk, and restarts the count. The count stops while the mirror is suspended or paused (CORE-336).
- CORE-336: Pause holds. PAGE\_PAUSE {for?} and PAGE\_RESUME {} (5.5), and REST POST /api/v1/pages/pause with {for?} and POST /api/v1/pages/resume, set and clear pause holds. Each holder has at most one hold: the sending instance, or rest for REST; a new pause from the same holder replaces its hold, and a resume without a hold changes nothing. While any hold is active, rotation, timed jumps and idle return make no change and their counts stop; every other page change still works. A hold ends on a resume from its holder, when for (1 to 3600 s) has passed, or when the holding instance stops. When the last hold ends, the counts start again from zero. Both routes return the body of GET /api/v1/pages, which carries paused, the list of holders. The interaction pause of CORE-234 is separate and unchanged.
- CORE-337: Page change details (extends CORE-237). CORE\_PAGE\_CHANGED also carries previous, the page id before the change (null with reason start), and reason may also be after (CORE-334) or idle (CORE-335). The field is added, so the registered name stays.

**Visibility**

- CORE-238: A running instance is visible when no hide reason applies. The reasons are `page` (not on the current page), `manual` (admin, REST `/hide`, Home Assistant visibility switch), `self` (its element called `this.hide()`) and `module:<instance_id>` (set by a module with `layout.control`, CORE-241). Each source clears only its own reason: `/show` clears `manual`, `this.show()` clears `self`. To show an instance on a page it is not on, change its `pages`.
- CORE-239: When an instance becomes hidden, its elements fade out and receive `onSuspend()` and its backend's `suspend()` is called; when it becomes visible again, `resume()` and `onResume()`. Page rotation causes the same calls. While the mirror is suspended (`CORE_SUSPEND`) every instance is suspended whatever its visibility. Preview clients (6.6) show the page selected in the admin and never change the current page or any visibility.
- CORE-240: Runtime stop and start: REST `/api/v1/modules/{id}/stop` and `/start`, the admin and the Home Assistant "enabled" switch stop or start an instance without changing the configuration (state STOPPED, reason runtime). This is not recorded in the history and is lost on engine restart, when the instance returns to its configured state; any config change to the instance clears it. The admin offers "Save as disabled", which writes `disabled: true` through the normal save (6.4).
- CORE-241: A module with the capability `layout.control` may call `ctx.layout.hide(instance_id)` and `ctx.layout.show(instance_id)` on other instances (reason `module:<own id>`). Hide reasons set by a module are cleared when that module stops. Without the capability a module can only change pages, through the notifications above.
- CORE-242: The admin layout view (6.6) has a page selector: position edits apply to the selected page, or to every page for `pages: all` instances, and the live preview renders the selected page.

### 6.10 Theme and custom CSS

The shell looks like MagicMirror² by default, and every colour, size and font is a CSS variable that modules use and the user can override in one file (CORE-261).

- CORE-261: The shell defines the theme variables on `:root`; they are inherited into every shadow root. Font: `--mm3-font-family` (Roboto Condensed, vendored, then `sans-serif`). Sizes, all multiplied by the instance's `--mm3-scale`: `--mm3-size-xsmall` 15px, `--mm3-size-small` 20px, `--mm3-size-medium` 30px, `--mm3-size-large` 65px, `--mm3-size-xlarge` 75px, with `--mm3-font-size` equal to small. Colours: `--mm3-color-bg` #000, `--mm3-color-fg` #aaa, `--mm3-color-bright` #fff, `--mm3-color-dimmed` #666, and the state tokens `--mm3-color-good`, `--mm3-color-warning`, `--mm3-color-critical`, `--mm3-color-accent`, `--mm3-color-muted` (also used by WGT-11). Spacing: `--mm3-gap` (from `layout.gap`). The list is part of the Module API (Contract MOD-33): adding a variable is a minor change, removing or renaming one is a major change.
- CORE-262: The optional file `custom.css` next to the main config file (`/etc/mm3/custom.css`) is served at `/custom.css` and loaded after the theme. It may set theme variables for the whole mirror or for one instance (`[data-instance="weather_home"]`), and style the shell: page background, cells, headers. It cannot reach inside shadow roots. It follows the limits of CORE-214 except that it may be up to 64 KiB, and a file that breaks them is a validation error `CORE_CFG_CSS` with path `/custom_css`. It belongs to the config set as the history section `custom_css` (CORE-153, CORE-216) and is watched for hot reload (CORE-208).

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
5. Start the Web Gateway, emit `CORE_READY`, notify systemd (CORE-290: `READY=1`).
6. On SIGTERM: send STOPPING=1 to systemd (CORE-290), emit `CORE_SHUTDOWN`, call `stop()` on all instances (10 s budget), close MQTT with offline state, exit 0. SIGHUP forces a config reload.

- CORE-290: Service notification. The engine reports its state to systemd with datagrams sent to the socket named in `$NOTIFY_SOCKET` (a path, or an abstract socket starting with `@`), implemented in `mm3.core` with the standard library only (no new dependency, 1.2). Without `$NOTIFY_SOCKET` every call is a no-op. Messages: `READY=1` at step 5, together with `CORE_READY`; `STATUS=` with a one-line summary (running and failed instances) at start and after each reload; `RELOADING=1` with `MONOTONIC_USEC=` when a reload begins (SIGHUP or watcher, 6.3), followed by `READY=1` when it ends, whether the new configuration was applied or rejected; `STOPPING=1` when shutdown begins (step 6). Sending never blocks the loop; a failure is logged at debug level and never stops the engine. A start that aborts (CORE-30) never sends `READY=1`.
- CORE-291: Watchdog. When systemd sets `$WATCHDOG_USEC` (and `$WATCHDOG_PID` is absent or equals the engine's pid), a task on the engine loop sends `WATCHDOG=1` every `WATCHDOG_USEC / 2`, measured on the engine clock (CORE-278). The ping proves only that the event loop runs: it does not depend on modules, the bus, the web listeners or scheduled jobs. A module that blocks the loop (3.2) stops the ping, and systemd then kills and restarts the engine. The task starts with the engine and ends with the process, so reloads and shutdown do not interrupt it. Without `$WATCHDOG_USEC` no task is started. `mm3.service` sets `WatchdogSec=30s` (CORE-292).

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
2. `pybabel extract / update / compile` via a `mm3 i18n` CLI wrapper; a `.mo` and a generated `messages.json` per locale; the engine reads `messages.json` for backends and browsers alike, so `ctx.t()` and `this.t()` resolve the same way.
3. Engine endpoint `GET /i18n/{instance}.json` returns the merged catalog for the active chain (CORE-264).
4. `ctx.t()` and `this.t()` use the same keys, ICU-style `{var}` placeholders, and CLDR plural forms (`ngettext`).
5. Dates, numbers, units: Babel on the backend, `Intl.*` APIs in the browser, both fed `engine.locale` and `engine.timezone`.

### 8.2 Locale resolution

Chain = instance override (`modules[].locale`) → `engine.locale` → its base language (`de-CH` → `de`) → `en`. A missing key falls through the chain; a key missing even in `en` renders as the key itself and logs once.

### 8.3 Core catalogs

The engine has its own catalog for shell and admin UI strings (`CORE_*` keys). Third-party translations are accepted as separate packages registering entry point `mm3.locales`, so a community can add, for example, Portuguese without forking a module.

### 8.4 Quality gate

- CORE-41: CI fails if any shipped locale lacks a key present in `en`.
- CORE-42: CI fails if a placeholder set differs between `en` and a translation.

### 8.5 Catalogs at run time

Catalogs are compiled before they reach a mirror, looked up in a fixed order, and swapped live when a locale changes (CORE-263).

- CORE-263: Catalogs are compiled when a package is built: every module wheel and `mm3-core` ship a `.mo` and a `messages.json` next to each `.po`, and the engine never compiles at run time. `mm3 i18n compile` builds them, and mm3 i18n check, run in CI, fails when one is missing or does not match its `.po` (8.4). The English messages of the validation codes (CORE-243) are part of the core catalog.
- CORE-264: A key is looked up along the instance's locale chain (8.2) in the module's catalog, then along the same chain in the core catalog. Locale packages (`mm3.locales`, 8.3) add locales and keys a module lacks; a key the module ships itself always wins. A locale package registers one entry point per catalog it extends, named after the module (mm3 for the core catalog), whose value is a package laid out like a module's locales/ directory (\<lang>/messages.json); at each locale of the chain the module's own entry comes before the package's. /i18n/\<instance>.json returns one flat JSON object with every key of the module's and the core catalogs found in any locale of the instance's chain, each resolved as above, plural entries as objects keyed by CLDR category; its v is a hash of that content, which welcome.instances\[\].catalog and control locale carry.
- CORE-265: Placeholders are `{name}`. An entry with plural forms is stored in `messages.json` as an object keyed by CLDR plural category (`zero`, `one`, `two`, `few`, `many`, `other`), converted from the `.po` plural indices at compile time; `t(key, {count})` picks the form with `Intl.PluralRules` in the browser and Babel in the backend. A variable missing from the call renders as `{name}` and is logged once.
- CORE-266: `this.fmtDate` and `this.fmtNumber` use the element's locale, and `fmtDate` uses `engine.timezone`, not the browser's time zone, so every client shows the mirror's time. `fmtDate(d, style)` takes `short`, `medium`, `long`, `full`, `time` or an `Intl.DateTimeFormat` options object; `fmtNumber(n, unit)` takes an `Intl` unit identifier and formats without converting. short, medium, long and full format the date only and time formats a short time; ctx.format\_date and ctx.format\_number on the backend take the same styles and units and give the same results through Babel.
- CORE-267: On a locale change for an instance (CORE-245) the shell fetches its catalog again (new `v` hash), receives its public config with `LocalizedText` resolved for the new locale (CORE-101), updates `this.locale` and calls `onLocaleChange`, which re-renders by default.

## 9. Built-in services

Remote control and the MQTT bridge are engine services; presence and power is a module that uses generic engine hooks. Everything is driven through ordinary bus notifications (CORE-50).

### 9.1 Remote-control API

REST under `/api/v1`, OpenAPI at `/api/openapi.json`, admin single-page UI at `/admin` built from core Web Components.

| Method | Path | Effect |
| --- | --- | --- |
| GET | `/api/v1/status` | Engine, config, page, clients and kiosk status (CORE-305) |
| GET | `/api/v1/modules` | Instances, state, visibility, positions, health (CORE-315) |
| POST | `/api/v1/modules/{id}/show` · `/hide` · `/restart` | Per-instance control |
| ANY | `/api/v1/modules/{id}/...` | Routes a module registers on `ctx.router` |
| GET | `/api/v1/config` · `/config/schema` | Config set with ETag, secrets masked (CORE-301) · JSON Schema |
| POST | `/api/v1/config/validate` | Records and diff preview, nothing written (CORE-302) |
| PUT | `/api/v1/config` | Validate and save as a new history version (CORE-302); needs `If-Match` |
| POST | `/api/v1/notify` | Inject a `Notification` (source `remote`) |
| POST | `/api/v1/shell/reload` | Reload all browsers |
| GET | /api/v1/catalog · /catalog/{name} | Cached catalog index, module details, compatibility |
| POST | /api/v1/installer/operations · /installer/operations/{id} | Forwarded to the installer; needs install passphrase (+ TOTP); streams progress |
| POST | /api/v1/installer/rollback | Restore the previous venv; same re-authentication |
| GET / POST | /api/v1/config/history · /history/{v} · /history/{a}/diff/{b} · /config/undo · /config/redo · /config/restore | Version list, version files, diffs, undo/redo of saves, whole or section restore (6.8, CORE-321, CORE-324) |
| GET / POST | /api/v1/backups · /backups/create · /backups/{id}/download · /backups/restore | List, create, download, upload and restore (restore needs installer re-authentication) (10.8) |
| POST | /api/v1/modules/{id}/stop · /start | Runtime stop and start, not saved (CORE-240) |
| GET / POST | /api/v1/pages · /pages/{id} · /pages/next · /pages/prev · /pages/pause · /pages/resume | Pages and current page; change page (CORE-236); pause and resume automatic changes (CORE-336) |
| POST | /api/v1/request | Send a request on the bus and return the reply (CORE-223, CORE-314) |
| POST | /api/v1/config/patch | Apply JSON Patch and YAML edits to the draft, nothing written (CORE-301) |
| GET | /api/v1/modules/{id}/health | Health record of one instance (CORE-113) |
| GET / PUT | /api/v1/modules/{id}/log-level · /debug/log-level · /debug/log-level/{logger} | Engine loggers and levels · run-time log level, expires after debug\_ttl (CORE-112) |
| GET | /api/v1/debug/logs · /debug/bundle | Ring buffer with filters, paged · diagnostic bundle (CORE-304, CORE-311) |
| GET / PUT | /api/v1/kiosk · /kiosk/audio | Reported outputs, modes and sinks · run-time volume and mute, not saved (CORE-306) |
| POST / DELETE | /api/v1/preview/mirror | Show the draft layout on the kiosk, or stop (CORE-307) |
| PUT | /api/v1/session/locale | Language of the admin UI for this session (CORE-309) |

- CORE-298: API errors. Every error response of the engine's own `/api` routes is `application/problem+json` (RFC 9457) with `type` (`about:blank`), `title`, `status`, `detail` and `code`, a stable name from this list; validation failures add `errors`, the records of CORE-35. Codes: `CORE_API_UNAUTHORIZED` 401, `CORE_API_FORBIDDEN` 403 (origin mismatch, CORE-253), `CORE_API_NOT_FOUND` 404, `CORE_API_INVALID_REQUEST` 422 (body or query does not match the endpoint's model), `CORE_API_TOO_LARGE` 413 (CORE-26), `CORE_API_PRECONDITION_REQUIRED` 428, `CORE_API_CONFLICT` 409, `CORE_API_DISK_INVALID` 409, `CORE_API_NO_KIOSK` 409, `CORE_API_CONFIG_INVALID` 422, `CORE_API_PATCH_FAILED` 422, `CORE_API_MASKED_UNKNOWN` 422, `CORE_API_RESERVED_NAME` 403, `CORE_API_PAYLOAD_INVALID` 422, `CORE_API_UNAVAILABLE` 503 (engine starting or stopping), `CORE_API_REQUEST_TIMEOUT` 504 (no reply in time, CORE-314), `CORE_API_NOTHING_TO_DO` 409 (nothing to undo or redo, CORE-321). `title` and `detail` are English; the admin shows the core catalog text of `code`. A new code is a spec change. Routes a module registers on `ctx.router` answer in their own format.
- CORE-332: Module routes. A module with the capability `api.routes` gets `ctx.router`, a FastAPI `APIRouter`; without it the attribute raises `PermissionError` (MOD-70). Routes registered on it are served under `/api/v1/modules/<instance id>/` on the admin listener and the admin socket (CORE-311), with the same authentication as the engine's own routes, from the moment `start()` returns until the instance stops (MOD-22); before that, and once it has stopped, those paths return 503 `CORE_API_UNAVAILABLE`. The engine's own sub-paths of an instance (`health`, `show`, `hide`, `restart`, `stop`, `start`, `log-level`) take precedence; a module route on one of them is ignored with a warning. Each request runs on the event loop under the handler budget (engine.timeouts.handler); a handler over budget is cancelled, counted as in CORE-193, and the request returns 504 `CORE_API_REQUEST_TIMEOUT`. An unhandled exception in a route handler returns 500 with an empty body and is logged with the instance's trace id; unlike CORE-192, the instance keeps running.
- CORE-299: Paging. `GET /api/v1/config/history` and `GET /api/v1/debug/logs` take `limit` (default 50, at most 500) and `before`, an opaque cursor, and return `{items, next}`, newest first, with `next` the cursor of the following page or null. Every other list is returned whole. `/debug/logs` takes the filters of CORE-304 and returns JSON lines with `Accept: application/x-ndjson`, for the download of the live logs view.
- CORE-305: Status. `GET /api/v1/status` returns `{version, engine_build, started_at, uptime_s, suspended, config, page, clients, kiosk}`: `config` as in CORE-209; `page` `{id, index, count}`; `clients` `[{role, address, user_agent, since}]` (CORE-247); `kiosk` `{connected, cage_version, display: {applied, viewport, dpr, mismatch}, chromium: {state, restarts, failed}, audio: {sinks, applied, mismatch, failure}}` from the agent's reports (CORE-145, CORE-269, CORE-277, CORE-296), with `connected: false` and the last reported values when the agent has gone, or null when no agent has connected since start. The admin home and the debug overview show it, with mismatches and failures highlighted.
- CORE-306: Kiosk controls. `GET /api/v1/kiosk` returns the `kiosk` object of CORE-305 with the agent's last `outputs` and `sinks` reports (CORE-277). The form view offers `display.output`, the mode and `audio.sink` as dropdowns of these values, the sink with `auto`, `hdmi` and `analog` first (CORE-144). `PUT /api/v1/kiosk/audio` takes `{volume?, mute?}` (volume int 0–100) and sends them to the agent as run-time values (CORE-296); the admin shows a volume slider and a mute toggle beside the kiosk status. They are never written to the configuration and are cleared when the `audio` block changes or the agent connects again. Without a connected agent it returns 409 `CORE_API_NO_KIOSK`.
- CORE-308: Notify. `POST /api/v1/notify` takes `{name, payload, target}` (`target` optional) and sends the notification with source `remote`. A name starting with `CORE_` returns 403 `CORE_API_RESERVED_NAME`; a name in the shared registry (5.5) whose payload does not match the registered schema returns 422 `CORE_API_PAYLOAD_INVALID`; other names are sent as they are. The bus inspector's re-send (10.5) uses this endpoint.
- CORE-309: Admin language. The admin UI uses `engine.locale` (8.2) unless the admin picks another language in its header, which calls `PUT /api/v1/session/locale` with `{locale}`: one of the shipped languages (EN, IT, FR, ES, DE), or null to follow `engine.locale` again. It returns `{locale, effective}`, the choice and the language now used. The choice is kept with the admin session on the server and is not part of the configuration; it works only with an admin session cookie, and a request with a bearer token returns 422 `CORE_API_INVALID_REQUEST`. `Accept-Language` is ignored.
- CORE-313: OpenAPI. The schema is served at `GET /api/openapi.json` on the admin listener, with the authentication of `/api/*`. There is no interactive documentation page, because it would need inline or third-party scripts (CORE-63).
- CORE-314: REST requests. `POST /api/v1/request` takes `{name, payload, target, timeout}` (`payload` default `{}`, `target` optional, `timeout` 0.1–60 s, default 5) and sends the request of CORE-220 with source `remote`. The first reply comes back with 200 as `{payload}`; an error reply of CORE-221 is a reply too and comes back with 200 as `{error: {code, message_key}}`. No reply within `timeout` returns 504 `CORE_API_REQUEST_TIMEOUT`. A `CORE_` name returns 403 `CORE_API_RESERVED_NAME`, and a registered name whose payload does not match returns 422 `CORE_API_PAYLOAD_INVALID`, as for notify.
- CORE-315: Instances. `GET /api/v1/modules` returns `[{id, module, version, state, reason, hidden, pages, position, positions, health}]` in configuration order: `state` and `reason` as in section 7 (`reason` `runtime` after a runtime stop), `hidden` the active hide reasons, `pages`, `position` and `positions` as configured, `health` the record of CORE-113. `show`, `hide`, `restart`, `stop` and `start` are idempotent: they return 200 with the instance's entry after the change, also when it was already in that state; `start` on a FAILED or DISABLED instance starts it again as `restart` does, and `show` and `hide` on a backend-only instance change nothing. An unknown instance id, in a path or as `target` of notify or request, and an unknown page id return 404 `CORE_API_NOT_FOUND`.

### 9.2 Presence and power: module boundary

Presence and power ships as the backend-only reference module `mm3-presence`; the engine keeps only the generic suspend/resume hooks, so presence can be replaced (camera, BLE, phone) without touching core.

- **In the module:** sensor inputs (MQTT topics, `PRESENCE_DETECTED` / `PRESENCE_CLEARED` from other modules, touch on the kiosk), the timer `ON → IDLE → OFF`, schedule windows, and display drivers through its own entry point group `mm3_presence.display` (`kiosk` through the engine hook, `ddcutil` for DDC/CI brightness and power, `cec` with `cec-client` for HDMI-CEC); requirements PRS-01 to PRS-08 below.
- **In the engine:** `ctx.system.request_suspend()` / `request_resume()` and `ctx.system.set_display_power()` (CORE-346). On a suspend or resume request the engine emits `CORE_SUSPEND` or `CORE_RESUME` and calls `suspend()` / `resume()` on every instance.
- CORE-52: Only modules declaring `capabilities = {"system.power"}` may call these hooks; a request from any other module raises `PermissionError` and is logged.
- CORE-53: Several modules may hold power; the engine stays resumed while any holder requests resume (reference-counted by instance id).
- **Control and Home Assistant:** the module adds its routes through `ctx.router` and its entities through `ctx.mqtt.ha_entity()` (PRS-08).

**Engine hooks**

- CORE-346: Display power (extends CORE-277 and CORE-146). `ctx.system.set_display_power(on)` needs the capability `system.power` (CORE-52). The engine sends the kiosk agent `power {on}`, and at every connect `power` with the last value (default true); the protocol stays 1. The agent runs `wlr-randr --output <output> --off`, or `--on` with the mode, rotation and scale of `applied`, and then sends `outputs`, where `current` is null while the output is off. The call returns when that `outputs` arrives and raises `TransientError` when no agent is connected or none arrives within 5 s. The power state is a run-time value, never saved; a display block change made while the output is off is applied when it is turned on. Every change emits `CORE_DISPLAY_CHANGED`, which also carries `power`, `on` or `off`; the field is added, so the name stays.
- CORE-347: Interaction event (extends CORE-22 and CORE-260). On a touch from the kiosk shell (CORE-260) the engine broadcasts `CORE_INTERACTION {kind: "touch"}`, at most once a second; previews and LAN browsers never cause it (CORE-99). It is registered in 5.5. `CORE_PRESENCE` stays reserved and is never emitted; presence is reported by `PRESENCE_STATE` (PRS-07).
- CORE-348: Display control packages (extends CORE-293). On kiosk systems `mm3-install.sh` also installs the Debian packages `ddcutil` and `cec-utils` and adds the user `mm3` to the groups `i2c` and `dialout` where they exist; a failed group change is logged. The groups take effect at the next start of `mm3.service`.

**mm3-presence**

- PRS-01: Module. `mm3-presence` (module name `presence`) is a backend-only reference module in `modules/presence` of mm3-core, published and listed in `mm3-modules-default` (CORE-289, CORE-294). It declares the capabilities `system.power`, `mqtt.subscribe`, `ha.discovery` and `api.routes`, emits `PRESENCE_STATE` and consumes `PRESENCE_DETECTED`, `PRESENCE_CLEARED` and `CORE_INTERACTION`. Only one instance may run: a second one fails its start with `ConfigurationError`. Its texts ship in EN, IT, FR, ES and DE.
- PRS-02: Configuration. `mqtt`, a list of `{topic, field?, value?}` (default `[]`); `touch` (default true); `idle_after` (a duration, default 0: no IDLE); `idle_brightness` (0–100, default 30); `brightness` (0–100, or null to leave the display as it is; default null); `off_after` (a duration above 0, default 10m); `schedule` (windows, PRS-05; default `[]`); `wake_in_schedule` (default false); `display` (driver name, default `kiosk`) and `display_options` (a mapping passed to the driver, default `{}`). A non-zero `idle_after` not below `off_after` is an error, and so is an unknown driver. Each topic needs a `mqtt.subscribe` grant.
- PRS-03: Presence. A message on a configured topic sets presence when it matches, and any other message on it clears it. With `field`, it matches when the value at that dotted path of the JSON payload equals `value` (default true); without `field`, when the payload text equals `value` (default `ON`), ignoring case. `PRESENCE_DETECTED` and `PRESENCE_CLEARED` set and clear presence for their sender and `sensor`. Presence is held while any topic or sensor has it set. With `touch: true`, `CORE_INTERACTION` is a moment of presence.
- PRS-04: States. The state is `on` while presence is held. From the moment it is no longer held, or from a moment of presence, the module counts: after `idle_after`, if above 0, the state is `idle` and a driver with brightness is set to `idle_brightness`; after `off_after` the state is `off`, the display is turned off through the driver and the module calls `ctx.system.request_suspend("presence")`. Presence while `idle` or `off` makes the state `on`: the module calls `request_resume("presence")`, turns the display on and sets the brightness back to `brightness`, or to the level before `idle` when `brightness` is null. At start the state is `on`, the display is turned on and the count starts.
- PRS-05: Schedule. A window is `{off: "HH:MM", on: "HH:MM", days?}` in `ctx.timezone`; an `on` not after `off` falls on the next day, and `days` (`mon` to `sun`, default all) are the days a window starts. Inside a window the state is `off` with reason `schedule` and presence is ignored; with `wake_in_schedule: true`, presence makes the state `on` and `off_after` brings it back to `off`. When a window ends the state is `on` with reason `schedule` and the count starts. Overlapping windows act as one; a time that does not exist on a day because of a clock change is taken as the next minute that does.
- PRS-06: Display drivers. Drivers are plug-ins on the entry point group `mm3_presence.display`, built with the module context and `display_options`. A driver has `name`, `supports_brightness`, `async set_power(on)`, `async set_brightness(percent)` and `async check()`, which returns null when the driver can work or a reason, reported by `mm3 diag check` (CORE-312). Built in: `kiosk` (`ctx.system.set_display_power`, no brightness); `ddcutil` (VCP 0x10 for brightness and 0xD6 for power, 1 on and 4 off; option `display`, default 1); `cec` (`cec-client -s -d 1` with `on 0` and `standby 0`, no brightness; option `adapter`). Subprocess drivers run through `ctx.run_blocking` with a 10 s timeout. A failed driver call is logged, sets the health field `driver_error` and is tried again at the next change; the state changes anyway.
- PRS-07: State notification. `PRESENCE_STATE {state, reason}` is broadcast at start and on every change, with `state` `on`, `idle` or `off` and `reason` `start`, `presence`, `timeout`, `schedule` or `command`. It is registered in 5.5.
- PRS-08: Control. Under its instance path (CORE-332), `POST display/on` is a moment of presence with reason `command`, `POST display/off` makes the state `off` at once with reason `command` until the next presence, and `GET state` returns `{state, reason, since, held, driver}`, where `held` lists the sources holding presence. Home Assistant entities (CORE-344): switch `display`, acting as those routes; binary sensor `presence` (device class occupancy, on while presence is held); sensor `state`; and, when the driver supports brightness, number `brightness` 0–100, a run-time value that replaces `brightness` until the next start.

### 9.3 MQTT bridge

| Topic | Direction | Meaning |
| --- | --- | --- |
| `<base>/notify/<NAME>` | in | Publish a bus notification (payload JSON) |
| `<base>/event/<NAME>` | out | Notifications listed in `mqtt.export` (glob list) |
| `<base>/state` | out, retained | `online` / `offline` (Last Will) |
| \<base>/ha/\<instance>/\<object\_id>/state | out, retained | State of a Home Assistant entity; the engine's own entities use the instance \_engine (CORE-343, CORE-344) |
| \<base>/ha/\<instance>/\<object\_id>/set | in | Command for that entity |

With `ha_discovery: true` the engine publishes `homeassistant/<component>/<node_id>/<object>/config` so Home Assistant creates one device with: a reload button, a page select (CORE-236), and per instance a visibility switch and an enabled switch (runtime stop and start, CORE-240); modules add their own entities through ctx.mqtt.ha\_entity() (mm3-presence adds the display switch, brightness and presence sensor). Echo loops are prevented by dropping inbound messages whose `source` is the bridge itself (CORE-51).

The presence module defines its own topics (9.2); the engine's topics are only those above.

**Connection**

- CORE-338: Bridge configuration. `engine.mqtt` is optional; without it the bridge is off. Keys: `url` (required: `mqtt://host[:port]`, default port 1883, or `mqtts://host[:port]`, default port 8883); `username` and `password` (a secret field, CORE-13, usually `!env`); `ca`, a PEM file relative to the directory of the main configuration file that replaces the system CA store for this connection (missing or unreadable: `CORE_CFG_FILE_UNREADABLE`); `client_id` (default `mm3-<hostname>`); `keepalive` (seconds, 10–3600, default 60); `base_topic` (default `mm3/<hostname>`; no `+` or `#`, no leading or trailing `/`); `export` and `import` (glob lists of notification names, default `[]`); `ha_discovery` (default false); `discovery_prefix` (default `homeassistant`); `node_id` (matching `[a-z0-9_]{1,64}`; default `base_topic` lower-cased with every other character replaced by `_`). `<hostname>` is the host name, lower-cased. Any other key is an error. With `mqtts` the connection uses TLS 1.2 or later and always verifies the certificate and host name; there are no client certificates.
- CORE-339: Connection. The bridge owns one MQTT client (aiomqtt behind one adapter in `mm3.core.services.mqtt`), shared by the bridge and every instance. Its Last Will is `offline` on `<base>/state`, retained, QoS 1; after every connect it publishes `online` there, retained, and before a clean stop `offline`. A lost or refused connection is retried after 1 s, doubling up to 60 s, with ±20 % jitter; after every connect the subscriptions of the bridge and of the instances are restored. A change of `engine.mqtt` on hot reload reconnects with the new settings. Extending CORE-305, `GET /api/v1/status` carries `mqtt`: `{connected, since, error}`, with `error` a short reason without credentials or null, or null when the bridge is off; `mm3 diag check` reports the bridge as ok, fail (configured but not connected) or skip (off). Credentials are never logged.

**Notifications**

- CORE-340: Inbound. The bridge subscribes to `<base>/notify/+`. A message on `<base>/notify/<NAME>` is sent on the bus as a broadcast with source `mqtt` and a new trace only when NAME matches a glob of `mqtt.import`, does not start with `CORE_`, and its payload is empty (taken as `{}`) or a JSON object of at most 256 KiB that passes the registry check (CORE-225). Any other message is dropped, logged as a warning at most once a minute per name, and counted in the bridge's health. MQTT cannot target an instance or send a request.
- CORE-341: Outbound. Every broadcast notification whose name matches a glob of `mqtt.export`, `CORE_*` names included, is published on `<base>/event/<NAME>` as the envelope of 5.1 in JSON, QoS 0, not retained. Notifications with source `mqtt` are never exported (CORE-51). While the bridge is disconnected, exports are dropped, not queued.

**Modules**

- CORE-342: `ctx.mqtt` and the connection (extends CORE-100). With the bridge off, `publish`, `subscribe` and `ha_entity` raise `ConfigurationError`. `publish` while disconnected raises `TransientError`; nothing is queued. `subscribe` while disconnected succeeds and takes effect on connect. The topics `<base>/notify/`, `<base>/event/`, `<base>/state` and `<base>/ha/` belong to the engine: publishing to them raises `PermissionError` even when a grant matches.

**Home Assistant**

- CORE-343: Discovery and engine entities. With `ha_discovery: true` the bridge publishes, retained, one discovery config per entity on `<discovery_prefix>/<component>/<node_id>/<object>/config`, all on one device `{identifiers: [node_id], name, manufacturer: "MagicMirror 3", sw_version}`, whose name is `engine.location.name` resolved in `engine.locale`, else `node_id`, and with availability `<base>/state`. `<object>` is `_engine-<object_id>` for the engine's entities and `<instance>-<object_id>` for a module's, and `unique_id` is `<node_id>-<object>`. The configs are published after every connect, when `<discovery_prefix>/status` receives `online`, and on `CORE_LOCALE_CHANGED`. The engine's entities, named by the core catalog keys `HA_*` in `engine.locale`: button `reload` (as `POST /api/v1/shell/reload`); select `page` with the page ids as options (as `PAGE_SET`, CORE-236); switch `pages_paused`, a pause hold with holder `mqtt` (extending CORE-336); number `kiosk_volume` 0–100 and switch `kiosk_mute`, run-time values as in CORE-306, never saved, with state `None` while no kiosk is connected; and per instance the switches `<id>_visible` (as show and hide, CORE-315) and `<id>_enabled` (runtime stop and start, CORE-240). Their states are published on change and after every connect.
- CORE-344: Module entities. `ctx.mqtt.ha_entity()` (Module Contract §4.4) needs the capability `ha.discovery` and no grant. `object_id` matches `[a-z0-9_]{1,64}` and is unique within the instance (`ValueError` otherwise); `name` is a key of the module's catalog, resolved in `engine.locale`. The state topic is `<base>/ha/<instance>/<object_id>/state` (retained, QoS 1) and, when `on_command` is given, the command topic is `<base>/ha/<instance>/<object_id>/set`. `set_state` publishes `ON` or `OFF` for a bool, a number or string as text, and `None` as `None` (unknown in Home Assistant); the last state is published again after every connect. A command reaches `on_command` with the payload as text through the instance inbox, one at a time under the handler budget, as in CORE-100; an exception counts as a handler failure (CORE-192). With `ha_discovery: false` the call returns an entity that publishes nothing.
- CORE-345: Stale entities. The bridge records the discovery topics it has published in `<state>/mqtt/discovery.json`. Once the instances of a start or a hot reload have started, it publishes an empty retained payload on the topics of entities no longer registered: those of removed or disabled instances, and those a RUNNING instance did not register again by the end of its `start()`. `HaEntity.remove()` clears its entity at once. The entities of suspended, runtime-stopped or FAILED instances stay.

### 9.4 Reference modules

MM3 ships three reference modules that prove the Module Contract end to end: `mm3-greeting` (the example of Contract §13), `mm3-clock` (front-end only) and `mm3-weather` (full). Requirements of the clock are `CLK-xx`, those of the weather module `WTH-xx`.

- CORE-289: The three live in mm3-core under `modules/mm3-greeting`, `modules/mm3-clock` and `modules/mm3-weather`. Each is its own distribution with its own `pyproject.toml`, SemVer version starting at 1.0.0, README, catalogs in the five languages and tests, built with `mm3-build` (CORE-280) and citing the Module Contract like any other module. CI runs `mm3 module check` on each (CORE-174) and their tests with `mm3-core[testing]`. `mm3-greeting` is the code of Contract §13: it is not published and not part of `mm3-modules-default`. `mm3-clock` and `mm3-weather` are published to PyPI (10.2) and belong to `mm3-modules-default`. All three use only the Module API: no engine internals.

**Clock (`mm3-clock`)**

- CLK-01: A front-end-only module (Contract 1): element `mm3-clock`, no hooks, no capabilities, no notifications. Time comes from `Date` in the browser and is shown in the mirror's time zone with `this.fmtDate` (CORE-266), never the browser's. `min_size_px` is (180, 80).
- CLK-02: Configuration: `display` (`digital`, `analog`, `both`; default `digital`), `show_seconds` (default true), `time_format` (`locale`, `12`, `24`; default `locale`, which follows the element's locale), `show_date` (default true), `date_style` (`short`, `medium`, `long`, `full`; default `long`), `show_week` (ISO 8601 week number; default false) and `timezone` (IANA name replacing `engine.timezone` for this instance; default none). Values outside these sets are validation errors (`CORE_CFG_CHOICE`).
- CLK-03: `zones` is a list of at most 4 entries `{timezone, label}` (`CORE_CFG_LENGTH` beyond that); `label` is a `LocalizedText`, default the city part of the IANA name. Each entry shows its time in the same format as the main clock, with a localized marker such as "+1 d" when its date differs from the main clock's date.
- CLK-04: The analog face is an SVG drawn with `svg` from `/core/mm3.js`, scaled to the element and kept square. Option `face` (`numbers`, `ticks`, `none`; default `ticks`). Hour and minute hands always; the second hand only with `show_seconds`. Colours come from theme variables (MOD-33). With `display: both` the digital time sits beside the face when the element is wider than tall, below it otherwise.
- CLK-05: Updates are scheduled with `this.setTimeout` for the time left to the next second (or minute when `show_seconds` is false) boundary, not with a fixed interval, so drift and suspended tabs correct themselves at the next tick. With `show_seconds: false` nothing runs between minute boundaries.
- CLK-06: `sun` (default false) shows sunrise, sunset and day length; `moon` (default false) shows the phase name, illumination in percent, and moonrise and moonset. Both use the instance's `location` config (`{lat, lon}`) or else `this.location` (CORE-288); with neither, the element shows a localized hint and nothing else changes. Values are computed in the browser by in-house JavaScript, no library: sun events from the NOAA solar position equations, moon from a Meeus low-precision series. Times are shown in the clock's time zone; polar day and polar night show localized text instead of times.
- CLK-07: Accuracy: sun events within 2 minutes and moon events within 10 minutes of published almanac values between latitudes −60° and 60°; illumination within 1 percentage point. The tests compare against a fixture table of at least five places (one above 60° latitude, one in the southern hemisphere) and four dates; the browser harness renders the clock in five languages and at three sizes.
- CLK-08: All fixed text uses catalog keys in the five languages, including the eight moon phase names and the `HEADER` key (MOD-67); dates and numbers go through `fmtDate` and `fmtNumber`.

**Weather (`mm3-weather`)**

- WTH-01: A full module (backend and element `mm3-weather`) with capability `http.outbound`. Its README lists the grant it needs (`http: ["https://api.open-meteo.com/"]` for the built-in provider) and the notification it emits (MOD-24, MOD-71).
- WTH-02: Configuration: `type` (`current`, `daily`, `hourly`; default `current`), `provider` (default `open_meteo`), `location` (`{lat, lon, name?}`; default `ctx.location`; when neither exists the instance is DISABLED with `ConfigurationError("location", "CFG_LOCATION_REQUIRED")`), `units` (`metric`, `imperial`; default `ctx.units`), `refresh` (duration, default 10 min, at least 5 min: `CORE_CFG_RANGE`), `days` (1–16, default 5), `hours` (1–48, default 8), `hour_step` (1–6, default 1), `emit` (default true, WTH-11) and the display options `show_feels_like` (default true), `show_humidity`, `show_wind`, `show_precipitation`, `show_sun` (default false).
- WTH-03: Providers are plug-ins on the entry point group `mm3_weather.providers`: a class with a `name`, a Pydantic model for its own options (config key `provider_options`) and `async fetch(http, request) -> WeatherData`, where `WeatherData` is the model of WTH-04. The module loads the provider named by `provider` at start; an unknown name is `ConfigurationError("provider", "CFG_PROVIDER_UNKNOWN")`. Open-Meteo (`open_meteo`, no API key, `https://api.open-meteo.com/v1/forecast`) is built in. A provider names the URL prefix it contacts; without a matching grant WTH-09 applies.
- WTH-04: Normalised data. Current: `observed_at`, `condition`, `is_day`, `temperature`, optional `feels_like`, `humidity`, `wind_speed`, `wind_direction`, `precipitation`. Daily entry: `date`, `condition`, `temp_min`, `temp_max`, optional `precipitation_probability`, `precipitation_sum`, `sunrise`, `sunset`. Hourly entry: `time`, `condition`, `is_day`, `temperature`, optional `precipitation_probability`. Temperatures are in °C or °F and wind speed in km/h or mph according to `units`, converted once in the backend; humidity and probabilities are 0–100; times are aware ISO 8601 strings in `ctx.timezone`.
- WTH-05: The condition set is closed: `clear`, `partly_cloudy`, `cloudy`, `fog`, `drizzle`, `rain`, `heavy_rain`, `freezing_rain`, `snow`, `sleet`, `thunderstorm`, `hail`; `is_day` selects the day or night variant. A provider maps its codes to this set (Open-Meteo: WMO weather codes, table in the module's documentation); an unmapped code becomes `cloudy` and is logged once per refresh at debug level as `condition_unmapped`.
- WTH-06: Icons are Material Design Icons weather glyphs (Apache-2.0), vendored as SVG files in the module's own `static/icons/` (MOD-37) with the licence text in the package, one per condition and, where MDI has both, per day and night variant. The element draws them with `svg` and theme colours.
- WTH-07: The backend fetches at start and then every `refresh` with `ctx.scheduler.every` (no overlap, CORE-184), one request per refresh covering current, daily and hourly data. After a success it calls `ctx.set_state` with the data of the instance's `type` only, sets `last_refresh` and `items` with `ctx.health.set` (MOD-85), logs one `info` record `weather_refreshed` (MOD-84), and stores the last good data with its fetch time in `ctx.storage` under the key `last`.
- WTH-08: Network errors, timeouts, HTTP 5xx and 429 raise `TransientError("WEATHER_UNAVAILABLE")`; other 4xx raise `ConfigurationError` naming `location` or `provider`. Data without a success for more than 2 × `refresh` is shown stale (dimmed, with its age). After a restart the stored `last` data is shown as stale until the first success; data older than 24 h is dropped.
- WTH-09: A missing `http` grant (`PermissionError` from `ctx.http`) does not crash the instance (MOD-72): its state becomes `{error: "grant"}` and the element shows a localized message naming the grant to add.
- WTH-10: Views: `current` shows icon, temperature, the localized condition text and the optional fields; `daily` lists `days` entries (weekday, icon, minimum and maximum, optional probability); `hourly` lists `hours` entries every `hour_step` hours. Condition texts are catalog keys `WEATHER_<CONDITION>` (with `_NIGHT` variants for `clear` and `partly_cloudy`); numbers and units use `fmtNumber`; the `HEADER` key is provided (MOD-67).
- WTH-11: With `emit` on, every instance sends `WEATHER_CURRENT` after each successful refresh (the current block is always fetched), with the registered payload (CORE-224, 5.5): `location` `{lat, lon, name?}`, `condition` (WTH-05), `is_day`, `temperature`, optional `feels_like`, `humidity`, `wind_speed`, `units` (`metric` or `imperial`) and `observed_at` (aware ISO 8601). The JSON Schema is published in `mm3.notifications` (CORE-225).
- WTH-12: Tests: `ModuleHarness` with HTTP mocks of Open-Meteo responses (fixtures for each condition group and for a response without optional fields); virtual-clock tests for the refresh cycle, stale marking and restart with stored data; `check_contract()`; browser harness renders of the three views in five languages.

## 10. Security, packaging, deployment, quality

The server binds to `127.0.0.1` by default, and nothing beyond the shell itself is reachable without a token (CORE-60).

### 10.1 Security

- CORE-61: Both listeners always accept loopback. Other sources need the listener's allow list, `server.allow` or `admin.allow` (CIDR lists, default `[]`), and `server.host` or `admin.host` bound to a non-loopback address; requests from other sources get 403 and are logged at info level, at most once a minute per address.
- CORE-62: `/api/*`, `/admin` and the admin WebSocket require an admin token, sent as `Authorization: Bearer <token>` or held in an admin session (CORE-253). `mm3 token new <name> [--display]` creates a token and shows it once; tokens are stored as argon2 hashes with name, kind (admin or display), creation and last-use time in `<state>/tokens.json` (0600, owner `mm3`). `mm3 token list` shows them and `mm3 token revoke <name>` revokes one and ends its sessions at once. No token is ever kept in the configuration.
- CORE-63: Every response of both listeners sends `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'` (`frame-ancestors 'self'` for `/admin/preview`; `/admin/config` adds `frame-src 'self'` for the preview iframe of 6.6), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, a `Permissions-Policy` that denies camera, microphone, geolocation, USB, serial, Bluetooth and payment, and `Cache-Control: no-store` except on versioned static files (CORE-252). Pages use no inline script or style.
- CORE-253: Admin sessions. `/admin/login` exchanges an admin token for a session cookie (HttpOnly, SameSite=Strict, Path=/) valid 30 days from its last use. Requests with unsafe methods also need an `Origin` header matching the admin listener. Five failed logins from one address lock logins from it for 15 minutes.
- CORE-254: Display pairing. `mm3 token new <name> --display` prints a link `http://<host>:<port>/pair#<token>`. The pairing page sends the token from the fragment to `/pair`, which sets a display session cookie (as CORE-253, valid until the token is revoked) and redirects to `/`; that browser is then interactive (CORE-247). A display token grants nothing on the admin listener.
- CORE-64: Front-end templates MUST escape data; `MirrorElement` offers an `html` tagged template that escapes by default.
- CORE-65: Service runs as unprivileged user `mm3`; systemd hardening (`ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`).
- CORE-66: Only modules installed in the engine's venv load. The engine process itself never installs anything: installs go through the CLI or the separate installer service (10.7), from the signed catalog.

### 10.2 Packaging and deployment

| Artifact | Content |
| --- | --- |
| `mm3-core` (PyPI) | Engine, shell, admin UI, `mm3` CLI; extra `[testing]`: the `mm3.testing` kit, the browser harness and the dependencies of `mm3 module check` (CORE-279) |
| mm3-build (PyPI) | Hatchling build hook mm3-i18n that compiles catalogs into each wheel; depends only on hatchling and Babel (CORE-280) |
| `mm3-modules-default` | clock, calendar, weather, newsfeed, compliments, alert, presence, widget (each also separate; CORE-294) |
| `mm3-install.sh` | Creates `mm3` user, venv in `/opt/mm3`, config in `/etc/mm3`, systemd units (CORE-293) |
| `mm3.service` | Engine (`Type=notify`, NotifyAccess=main, WatchdogSec=30s; CORE-290–292) |
| `mm3-kiosk.service` | System unit as user `mm3-kiosk` on tty1 running `cage -- mm3 kiosk session`: applies the display block (6.7), then launches and supervises Chromium on `http://127.0.0.1:8080` (CORE-143, CORE-268–270) |
| mm3-installer.service | Privileged installer for admin-initiated installs, Unix socket only (10.7) |

CLI: `mm3 run`, `mm3 check-config`, `mm3 modules list`, `mm3 new-module <name> [--kind full|backend|frontend]` (scaffold with all five locales), `mm3 module check <path>`, `mm3 i18n compile|check`, `mm3 token new|list|revoke`, `mm3 kiosk session`.

- CORE-279: The test kit ships in `mm3-core` behind the extra `mm3-core[testing]`, which adds pytest, pytest-asyncio and Playwright; mirrors install `mm3-core` without it. `mm3 module check` and `import mm3.testing` without the extra stop with a message naming the install command (exit code 2 for the check, CORE-281). The kit itself is specified in Module Contract §12.
- CORE-280: `mm3-build` is a separate distribution in the mm3-core repository, released with it and versioned with the Module API (`>=1.0,<2`). Its hatchling build hook `mm3-i18n` compiles every `.po` under the package's `locales/` directory into `.mo` and `messages.json` inside the wheel (CORE-263) and fails the build on a catalog error. It depends only on hatchling and Babel. It is the only catalog compiler: `mm3-core` builds its own catalogs with it, and `mm3 i18n compile` and `mm3 i18n check` use its code. Modules declare `requires = ["hatchling", "mm3-build>=1.0,<2"]` and enable `[tool.hatch.build.hooks.mm3-i18n]`.
- CORE-281: `mm3 module check <path>` builds the module's wheel from its source directory with `uv build`, installs it in a temporary venv together with the running `mm3-core` and its testing extra, then runs the static checks, the test-kit suite and the browser harness of Module Contract §11–12. `--only static|kit|harness` limits the run; `--format json` and `--junit <file>` write machine-readable results; the exit code is 0 when compliant, 1 when a MUST failed and 2 on a tool error. `mm3 new-module <name> --kind full|backend|frontend` (default `full`) writes a package that passes the check on its first run (MOD-95).
- CORE-292: `mm3.service`. The unit ships inside the `mm3-core` package and `mm3 service unit` prints it, as `mm3 kiosk unit` does for the kiosk (CORE-143); the installer puts it in place. It runs `mm3 run` as user and group `mm3` with `Type=notify` and `NotifyAccess=main` (CORE-290), `WatchdogSec=30s` (CORE-291), `Restart=on-failure`, `RestartSec=2s`, `TimeoutStartSec=60s`, `TimeoutStopSec=20s` (the 10 s stop budget of 7.1 plus margin) and `ExecReload=/bin/kill -HUP $MAINPID`. It sets `StateDirectory=mm3` (mode 0750), `RuntimeDirectory=mm3` (mode 0755, so the kiosk user can reach `kiosk.key`, CORE-268) and `ReadWritePaths=/etc/mm3` (CORE-217), carries the hardening of CORE-65, and is wanted by `multi-user.target`. Because the unit is `Type=notify`, `mm3-kiosk.service`, which starts after it, begins only once the engine has sent `READY=1`.
- CORE-293: `mm3-install.sh`. A script run as root on Debian 12 or 13 (x86-64) that is safe to run again. It creates, when missing, the system groups and users `mm3` and `mm3-kiosk` (no login shell) and makes `mm3` a member of `mm3-kiosk` (CORE-268); installs the Debian packages the engine and the kiosk need (for the kiosk also the audio packages of CORE-295 and the video drivers of CORE-297); builds a venv in `/opt/mm3/venvs/<timestamp>` and points the symlink `/opt/mm3/venv` to it (the layout of 10.7), installing `mm3-core` and `mm3-modules-default` from PyPI or, with `--find-links <dir>`, from local wheels; writes `/etc/mm3/config.yaml` and the env file `/etc/mm3/mm3.env` (mode 0640, group `mm3`, CORE-200) only when they are absent; installs `mm3.service` (CORE-292) and, unless `--no-kiosk` is given or the system is older than Debian 13 (CORE-143), `mm3-kiosk.service`; enables and starts them. It never overwrites a configuration, env file, state directory or token store, and it creates no token: it prints the `mm3 token new` command. Run again for a new release, it builds a new venv, moves the symlink, restarts the units and keeps the previous venv for rollback. Until release 1.0 the core package is 0.x while modules require `mm3-core>=1.0,<2` (MOD-03): the script installs the core first and the modules with `--no-deps`, and `pip check` reports the mismatch until `mm3-core` 1.0.0 is released. Exit code 0 on success, 1 on an unsupported system, 2 on any other failure. The Release 1.0 CI job runs it on clean Debian 12 and 13 images.
- CORE-294: `mm3-modules-default`. A distribution without code in `modules/mm3-modules-default/` of the mm3-core repository, published to PyPI with the other distributions (10.6). Its dependencies list the default modules of the 10.2 table that are published when the release is cut (clock and weather belong to it, CORE-289), each bounded to its current major (10.9); modules published later join in a later release of it. It declares no dependency on `mm3-core`: each module carries the range it needs (MOD-03). Each module also stays installable alone.

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
- CORE-112: Log level is set per instance at runtime, from the admin UI or `PUT /api/v1/modules/{id}/log-level`, with no restart. Engine subsystems have the same control under `PUT /api/v1/debug/log-level/{logger}`, where `logger` is the `subsystem` of their log records; `GET /api/v1/debug/log-level` lists them as `[{logger, level, default, expires_at}]`. The body is `{level}`, one of `debug`, `info`, `warning` and `error`, or null for the default (`engine.log_level`); the response is `{level, expires_at}`. A level more verbose than the default expires after `debug_ttl` (default 30 min), and the default applies again; any other level does not expire (`expires_at` null). An unknown instance or logger returns 404 `CORE_API_NOT_FOUND`.
- CORE-113: Each instance keeps a health record: state, start time, restart count, last error (message, traceback, time, trace id), handler call count, p95 duration and timeouts, queue depth, and time of last message in and out. Served at `GET /api/v1/modules/{id}/health`.
- CORE-114: Browser errors are captured. `MirrorElement` wraps `render`, `onMessage` and `onNotification`, and the shell installs `window.onerror` and `unhandledrejection`. Errors go to the server as WebSocket frames of kind `log` with instance id, stack and user agent, rate-limited to 10 per minute per instance, and are logged with source `browser`.

**Admin debug page** (`/admin/debug`, admin listener only)

- **Overview**: engine status and a row per instance with state, restarts and last error; opening a row shows the traceback, health record and that instance's recent logs, with its log-level control.
- **Live logs**: streamed over the admin WebSocket (CORE-304); filter by level, module, instance, trace id and text; pause, and download as JSON lines.
- **Bus inspector**: live list of notifications, WebSocket frames and MQTT messages with time, name, source → target and size; JSON payload viewer; filter by name glob and source; pause and search; "follow trace" shows every log and message sharing a trace id; "re-send" injects a captured notification again through `/api/v1/notify`.
- CORE-115: The inspector captures only while an admin page has it open (last 1 000 entries), so it costs nothing in normal operation. Config fields marked `secret` are masked in captured payloads.

* CORE-304: Admin channel. `/admin/ws` on the admin listener needs an admin session (CORE-253) and an `Origin` matching the listener, else it closes with code 4403; liveness and size limits are those of `/ws` (5.6). Frames are `{v: 1, kind, data}`. The admin sends `subscribe` `{topic, filter}` and `unsubscribe` `{topic}`. Topics: `status`, the object of CORE-305 at subscribe and on every change; `logs`, with `filter` keys `level`, `module`, `instance`, `trace` and `text`, first the newest 200 matching records of the ring buffer (CORE-110), then live ones; `bus`, with `filter` keys `name` (a glob) and `source`, the captured entries of CORE-115. The engine sends `status` `{status}`, `log` `{record}`, `bus` `{entry}` with entry `{ts, channel, name, source, target, size, trace, payload}` (`channel` `bus`, `ws` or `mqtt`; requests and replies paired, CORE-223), and `dropped` `{topic, count}` when an admin falls behind its queue of 1 000 per topic. The inspector captures while at least one `bus` subscription is open. A new frame kind is a spec change.
* CORE-316: Log filters. In the `logs` topic (CORE-304) and in `GET /api/v1/debug/logs` (CORE-299), `level` keeps records of that level and above; `module`, `instance` and `trace` keep records whose `module`, `instance` or `trace_id` equals the value; `text` keeps records in which it occurs, ignoring case, in `event` or in the string form of any other field. Filters combine; an absent or empty key does not filter. An unknown key or level is 422 `CORE_API_INVALID_REQUEST` on the REST route; on `/admin/ws` a `subscribe` with an unknown topic, filter key or level is ignored and logged as a warning.
* CORE-317: Status frames. While a `status` subscription is open, the engine compares the object of CORE-305 at most once a second and sends a `status` frame when any field other than `uptime_s` differs from the last frame it sent; the frame carries the current `uptime_s`.
* CORE-318: Bundle layout. The zip of `GET /api/v1/debug/bundle` and of `mm3 diag bundle` (named `mm3-diag-<host>-<UTC time>.zip`) holds `manifest.json` `{created_at, partial, missing, versions, python, os, redacted}`, where `versions` is `{mm3_core, modules: {name: version}}`, `missing` names the parts that could not be collected and `redacted` lists every redacted field as `{file, path}`; `packages.txt`, the venv package list as `name==version` lines; `config/`, the redacted config set (CORE-116); `logs.jsonl`, the ring buffer oldest first; `health.json`, the health records (CORE-113); `browser-errors.jsonl`, the records with source `browser` (CORE-114); `kiosk.json`, the kiosk object of CORE-305 with the clients, their user agent and viewport. A partial bundle has `partial: true`, `journal.txt` with the last 1 000 journal lines of `mm3.service` instead of `logs.jsonl`, and names the other parts in `missing`.
* CORE-319: Inspector masking. Before a payload is captured (CORE-115), every string equal to a secret value of the running configuration (a field marked `secret` in a module's config model, an `!env` value, the kiosk agent key) is replaced by `!masked`, the marker of CORE-303, also inside nested objects and lists; strings shorter than 4 characters are never matched. `size` stays that of the original payload.

**Diagnostic bundle and checks**

- `mm3 diag bundle` or the admin "Download diagnostics" button produces a zip with engine and module versions, Python and OS information, the venv package list, redacted config, recent logs, health records, browser errors, and kiosk details (resolution, user agent, the kiosk object of CORE-305) (CORE-311).
- CORE-116: Redaction is mandatory: secrets, tokens and `!env` values are removed, and the bundle lists every redacted field.
- `mm3 diag check` runs a self-test: config valid, ports free, MQTT broker reachable, kiosk connected, display driver available, locale catalogs complete. Each check prints pass, fail with a hint, or skip (CORE-312).

* CORE-311: Local admin socket. The engine also serves the REST API (without `/admin` pages and WebSockets) on the Unix socket `/run/mm3/admin.sock`, mode 0660, owner and group `mm3`, created at start and removed at stop. Requests on it need no token, because only `mm3` and members of group `mm3` can open it; the kiosk user is not one (CORE-268). CLI commands that need the running engine, starting with `mm3 diag`, use it. `mm3 diag bundle` builds the zip from `GET /api/v1/debug/bundle`, the same endpoint as the admin button; when the socket does not answer, it builds a partial bundle (versions, Python and OS, package list, redacted config, the last 1 000 journal lines of `mm3.service`) that names what is missing. Redaction follows CORE-116 in both cases.
* CORE-312: Self-test. `mm3 diag check` prints one line per check: `pass`, `fail` with a hint, or `skip` with the reason, such as MQTT when no broker is configured or the display driver when no presence module is configured. Checks: config valid, engine reachable on the socket, listener ports free or held by the engine, kiosk agent connected with Chromium not failed, audio output matched, MQTT broker reachable, display driver available, locale catalogs complete. Exit code 0 when no check failed, 1 when one failed, 2 on a tool error.

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
| `install <name>[==ver]` | Verify index signature, SHA-256 and attestation, then `pip install` through the installer service, which builds the staged venv (CORE-353, CORE-354); show requested capabilities before confirming |
| `update [<name>]` | Upgrade to the newest compatible release |
| `remove <name>` | Uninstall; instances in config are disabled, not deleted (CORE-358) |
| `list` | Installed modules, versions, available updates |

- CORE-125: The index is cached locally and refreshed at most daily; `search`, `info` and `list` work offline from the cache.
- CORE-126: `install` and `update` abort if any verification fails; nothing is installed from outside the index unless `--from-pypi` or a local path is given explicitly, and such modules are marked **unlisted** in the admin.

**Index and verification**

- CORE-349: Verification libraries (extends CORE-131). Signatures and attestations are verified with `sigstore` and `pypi-attestations` through one adapter, `mm3.installer.verify`. Both come with the extra `mm3-core[installer]`, which `mm3-install.sh` installs; the engine never imports them.
- CORE-350: Index format. The index is `https://mm3-project.github.io/module-index/index.json` with its Sigstore bundle `index.json.sigstore.json`, signed keyless by the workflow `.github/workflows/publish.yml` of `mm3-project/module-index` on `refs/heads/main`, with the GitHub Actions OIDC issuer; that identity and both URLs are fixed in mm3-core. The index holds `schema` (1), `serial` (an integer that rises with every publication), `generated_at`, `expires` (at most 14 days later; the index is published at least weekly) and `modules`, each with `pypi`, `repository`, `badge` (`official`, `verified` or `community`, CORE-122), `publisher` (the Trusted Publisher repository and workflow, CORE-123), `category`, `license`, `description` (`LocalizedText`) and `releases`, each with `version`, `api` (the supported Module API range), `capabilities`, `grants` (the scopes the module asks for), `check` (`{result, report_url}`) and `files` (`[{filename, sha256}]`, wheels only).
- CORE-351: Cache and freshness (extends CORE-125). Only the installer verifies the index. It keeps the verified index, its bundle and the highest `serial` it has accepted in its own state, `/var/lib/mm3-install/catalog/`, and writes a copy of index and bundle with `meta.json` (`serial`, `fetched_at`, `expires`) to `<state>/catalog/` (`/var/lib/mm3/catalog/`, owner `mm3`) for the engine and the CLI. It refreshes the index itself when its copy is more than a day old and on demand (`mm3 modules refresh` and `POST /api/v1/catalog/refresh`, both through `POST /v1/catalog/refresh`, CORE-355). A downloaded index replaces the installer's copy only when its signature verifies, its `serial` is not lower than the highest accepted one and it has not expired. `install` and `update` use only the installer's own copy, verified again before each operation, and need it unexpired; `search`, `info` and `list` read the engine's copy, work from any cached index and then report it as stale.
- CORE-352: Release verification (extends CORE-131). The installer downloads the release's wheel from PyPI, checks its SHA-256 against the index and verifies its PEP 740 provenance from PyPI's Integrity API against the entry's `publisher`. Any failure aborts the operation with the reason, before anything is built.

**CLI through the installer**

- CORE-353: CLI operations (extends CORE-126 and CORE-293). `mm3 modules install`, `update`, `remove` and `restore-previous` send their operation to the installer socket (10.7); the CLI never writes a venv. A listed package needs the passphrase, and the TOTP code when enabled, as from the admin (CORE-132). An unlisted package (`--from-pypi` or a local path) is accepted only when the socket peer, read with `SO_PEERCRED`, is root (`sudo mm3 modules install …`), without a passphrase; the installer records it as unlisted. `mm3-install.sh` creates the system user `mm3-install`, a member of group `mm3`, and gives it `/opt/mm3/venvs/`.

**Admin UI** adds a Catalog page: search, details, badges, compatibility, required grants, and Install, Update and Remove buttons that use the installer service (10.7); the equivalent CLI command is shown as an alternative. The installed-modules list shows available updates.

### 10.7 Installer service

Installs, updates and removals started from the admin UI run in a separate privileged service; the engine stays read-only and only forwards the request (CORE-130).

- **Separation.** `mm3-installer.service` runs as user `mm3-install`, the only account with write access to `/opt/mm3/venvs/`, from its own venv `/opt/mm3/installer/` (CORE-368). It listens only on the Unix socket `/run/mm3-install/installer.sock`, accessible to the `mm3` group, and has no network listener. The engine keeps its read-only venv (CORE-65).
- CORE-131: The installer accepts only package names and versions listed in the signed catalog index, and verifies the index signature, SHA-256 and PEP 740 attestation itself, never trusting values passed by the engine. Unlisted packages and local paths remain CLI-only.
- CORE-132: Every install, update or removal requires a fresh confirmation with an install passphrase (argon2 hash, set with `mm3 installer passphrase`), separate from the API token, plus a TOTP code if enabled (`mm3 installer totp enable`). Five failures lock the installer for 15 minutes.
- CORE-133: Before confirmation the admin shows the module's badge, check report and requested capabilities and grant scopes; granting scopes to instances stays a separate config step.
- CORE-134: Staged install: the installer builds a new venv under `/opt/mm3/venvs/<timestamp>/` with the change applied, runs an import test and `mm3 module check --only static`, then switches the `/opt/mm3/venv` symlink atomically and restarts `mm3.service`.
- CORE-135: Rollback: if the engine is not `READY` within 60 s of the restart, or `mm3 diag check` fails, the installer switches the symlink back and restarts again. The last 3 venvs are kept, and the admin offers "Restore previous version".
- CORE-136: Every request is logged with time, admin client address, package, versions and outcome, and listed on the admin debug page.
- One operation runs at a time; progress streams to the admin page.

* CORE-354: Staged venv (extends CORE-134). The installer creates the new venv with `python3 -m venv` and installs with `pip`, constrained to the versions frozen in the current venv, the verified wheel by its downloaded file and the module's dependencies from PyPI. The operation aborts, listing the conflicts, when it would change the version of any package that something other than the module being changed requires. A removal uninstalls the module and the packages only it required.
* CORE-355: Installer protocol. `mm3 installer serve` answers HTTP/JSON on `/run/mm3-install/installer.sock`, in the installer unit's own `RuntimeDirectory` (mode 0755), so stopping `mm3.service` never removes it; the socket has mode 0660 and group `mm3`. `GET /v1/status` (`{busy, locked_until, previous, installed, catalog}`: the kept venvs; the installed modules as `[{name, version, origin}]` with `origin` `catalog`, `pypi` or `local`; the `serial`, `fetched_at` and `expires` of the installer's index, or null), `POST /v1/catalog/refresh` (downloads and verifies the index now, CORE-351; 200 with the new `catalog`, 502 `{error}` when it cannot be fetched or verified; the engine answers a failed refresh with 503 `CORE_API_CATALOG_UNAVAILABLE` and the reason), `POST /v1/operations` (`{action, name?, version?, source?, path?, passphrase?, totp?, client}`, with `action` `install`, `update`, `remove` or `restore_previous`, and `source` `catalog` (the default), `pypi` or `local` with `path` a wheel file, the last two only from a root peer, CORE-353) answering 202 `{id}`, `GET /v1/operations` (the last 100), `GET /v1/operations/{id}` (`{id, action, name, from, to, state, steps, error}`, with `state` `queued`, `verifying`, `building`, `switching`, `restarting`, `checking`, `done`, `rolled_back` or `failed`) and `GET /v1/operations/{id}/events` (NDJSON, one line per step). A failed re-authentication answers 403, a locked installer 423 and a second operation while one runs 409. Operations are kept in the installer's state directory, so they outlive the engine's restart.
* CORE-356: Engine catalog and installer API (extends CORE-298 and CORE-304). The engine serves `GET /api/v1/catalog` (`q`, `category`; entries with badge, compatibility, installed and available versions), `GET /api/v1/catalog/{name}` (the details of CORE-133), `POST /api/v1/catalog/refresh`, `GET /api/v1/installer` and `GET`, `POST /api/v1/installer/operations[/{id}]`, forwarding operations to the installer. Problem codes added: `CORE_API_INSTALLER_UNAVAILABLE` (503), `CORE_API_REAUTH_FAILED` (403), `CORE_API_INSTALLER_LOCKED` (423), `CORE_API_INSTALLER_BUSY` (409) and `CORE_API_CATALOG_UNAVAILABLE` (503, no cached index). The passphrase and TOTP code pass through unchanged and are never logged or stored. `/admin/ws` gains the topic `installer`, whose frames carry an operation on every change; after the engine's restart the admin page subscribes again and reads the operation.
* CORE-357: Restart and checks (extends CORE-134 and CORE-135). `mm3-installer.service` (user `mm3-install`) and a polkit rule that lets `mm3-install` restart `mm3.service` and nothing else ship in the package, are printed by `mm3 installer unit` and `mm3 installer polkit`, and are installed by `mm3-install.sh`. After a restart the engine counts as READY when the unit is active within 60 s (`Type=notify`, CORE-290); then the installer runs `mm3 diag check` over the admin socket (CORE-311), and exit 1 means rollback.
* CORE-358: Removal (extends CORE-203). Before a removal is forwarded to the installer, the engine (for the admin) or the CLI (through the admin socket) saves a new configuration history version that sets `disabled: true` on every enabled instance of the module; with no such instance no version is written. The version stays if the removal fails and can be undone. When the engine cannot be reached, `mm3 modules remove` stops with an error.
* CORE-367: Untrusted code (extends CORE-134 and CORE-354). Code from packages never runs with the installer's rights. The installer creates the staged venv owned by the system user `mm3-build`; `pip` (with `--only-binary=:all:`, so no source distribution is built and a package without a wheel aborts the operation, naming it), the import test and `mm3 module check --only static` run as child processes started as `mm3-build` with no supplementary groups, every capability cleared (`setpriv --reuid --regid --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs`) and a private home in the installer's tmp directory. The verified wheel stays owned by `mm3-install` with mode 0644. After `pip` the installer gives the whole venv to `mm3-install`, removes every write bit, and only then runs the import test and the check, then switches the symlink.
* CORE-368: Installer venv and build user (extends CORE-293, CORE-353 and CORE-357). The installer runs `mm3 installer serve` from its own venv `/opt/mm3/installer/`, which holds `mm3-core[installer]` and its dependencies and never a module, so no module code (including `.pth` files) runs in the installer. `mm3-install.sh` builds that venv as root and upgrades it together with the engine's, creates the system user `mm3-build` (no login shell, no home, no supplementary groups), and installs the installer unit and its polkit rule. Unlisted wheels and local paths remain root-only (CORE-353).
* CORE-369: Passphrase and TOTP setup (extends CORE-132). `mm3 installer passphrase`, `mm3 installer totp enable` and `mm3 installer totp disable` talk to the installer socket (`POST /v1/auth/passphrase`, `POST /v1/auth/totp`, `DELETE /v1/auth/totp`) and are accepted only from a root peer (`SO_PEERCRED`), so they need `sudo`. The installer keeps the passphrase's argon2 hash and the TOTP secret in `/var/lib/mm3-install/auth.json` (mode 0600). `totp enable` returns the secret and its `otpauth://` URI once and turns TOTP on only after a valid code for that secret is sent back. Without a passphrase every listed operation answers 403 with the reason.
* CORE-370: Installer downloads (extends CORE-350 and CORE-352). The installer fetches the index, its bundle, wheels and provenance with the standard library in one place, `mm3.installer.fetch`: HTTPS only, TLS always verified against the system CA store, only the hosts `mm3-project.github.io`, `pypi.org` and `files.pythonhosted.org`, redirects only to those hosts, 30 s per read, and at most 16 MiB for the index, 1 MiB for a bundle or provenance and 256 MiB for a wheel. The module's dependencies are downloaded by `pip` (CORE-354, CORE-367).
* CORE-371: Check of a wheel (extends CORE-281 and CORE-134). `mm3 module check --wheel <file> --only static` checks a built wheel without its source tree and without `uv`: it reads the wheel's metadata, entry points and files, runs every static check that needs only those, and reports the checks that need the project files (`pyproject.toml`, the build configuration) as skipped. Exit codes are those of CORE-281. The installer runs it on the verified wheel in the read-only staged venv (CORE-367).
* CORE-372: Rebuilds before 1.0 (extends CORE-293 and CORE-354). While the current venv's `mm3-core` is 0.x, the installer installs the frozen pins and the module's wheel with `--no-deps`, then installs the wheel's other `Requires-Dist` entries (all but `mm3-core`, markers evaluated, extras left out) in the same venv with the same pins as constraints. The conflict check of CORE-354 applies unchanged. From `mm3-core` 1.0.0 on the installer resolves normally.

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
- **Scheduled:** run by the installer service itself, daily at `backup.schedule` (default 03:30; CORE-360). Kept in `/var/lib/mm3/backups/` with retention of 7 daily, 4 weekly and 6 monthly by default (`backup.keep`).
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

**Service, archives and keys**

- CORE-359: Installer rights (extends CORE-130 and CORE-357). `mm3-installer.service` runs as `mm3-install` with only the capabilities `CAP_DAC_OVERRIDE`, `CAP_CHOWN`, `CAP_FOWNER`, `CAP_SETUID` and `CAP_SETGID` (the last two only to start the build children as `mm3-build`, CORE-367), the same bounding set, `NoNewPrivileges=yes`, `PrivateTmp=yes` and `ProtectSystem=strict`, writing only `/etc/mm3`, `/var/lib/mm3`, `/opt/mm3`, its runtime directory `/run/mm3-install` and its state directory `/var/lib/mm3-install`. Files it writes for the engine keep owner `mm3` and their modes. The polkit rule of CORE-357 lets `mm3-install` stop, start and restart `mm3.service` and nothing else.
- CORE-360: Scheduled backups (extends CORE-160 and CORE-244). The installer runs them itself; there is no `mm3-backup.timer`. It reads the `backup` block of the running configuration over the admin socket (CORE-311) at start and every hour, and runs at `backup.schedule` in `engine.timezone`. When the mirror was off at that time and the last scheduled backup is older than 24 h, it runs within 10 minutes of the installer's start. Each run applies `keep`, then copies to the remote (CORE-365). Results are logged as in CORE-136 and shown in the admin and on the debug page.
- CORE-361: Requests (extends CORE-162 and CORE-353). The admin and `mm3 backup create|list|verify|restore|keygen` reach backups only through the installer socket; the engine forwards `GET` and `POST /api/v1/backups`, `GET /api/v1/backups/{name}` (download), `POST /api/v1/backups/upload` (at most 2 GiB, else 413 `CORE_API_UPLOAD_TOO_LARGE`, added to CORE-298) and `POST /api/v1/backups/restore` (`{source, parts, instance?, key?, passphrase, totp?}`), with the codes of CORE-356. Creating, listing, verifying and downloading need no re-authentication; a restore always does, except from a root peer.
- CORE-362: Archive layout (extends CORE-161). The tar holds `manifest.json`, `config/` (the config set and its history), `data/<instance>/`, `modules.json` (`[{name, version, origin}]`) and `secrets/`. An encrypted archive is the whole `.tar.gz` encrypted with age, recognised by its header. Archives are named `<hostname>-<YYYYmmdd-HHMMSS>-<kind>.mm3backup`, with `kind` `scheduled`, `manual` or `pre`, and each has an unencrypted sidecar `<name>.mm3backup.json` with `{created, kind, hostname, engine_version, api_version, parts, size, encrypted, sha256}` and no secrets, so local and remote lists work without the key. Restoring an archive from another hostname gives a warning, not a refusal.
- CORE-363: Restore order (extends CORE-162 and CORE-165). After decryption, checksums, compatibility, the preview and the pre-restore backup, the installer stops `mm3.service`, writes `secrets`, `data` (each selected instance's directory replaced whole) and `config` (the whole set, or for one instance only its section, the rest of the files kept), rebuilds the venv when `modules` is selected (CORE-354) and starts `mm3.service`. The engine records the restored config set as a new history version with reason `restore`, as for an external edit; then CORE-163 applies.
- CORE-364: Keys. `mm3 backup keygen` and the admin generate an age X25519 identity and return its public key for `encrypt_to`; the identity is shown and downloadable once and is kept on the mirror only on request, as `/var/lib/mm3-install/backup.key` (0600). A restore uses the kept identity or the `key` sent with the request (an identity or an archive passphrase), which exists only in the installer's private temporary directory for that operation.
- CORE-365: Remotes (extends CORE-244). rclone remotes live in `/var/lib/mm3-install/rclone.conf` (0600), with the types `sftp`, `smb`, `webdav` and `s3` only, managed through the installer and forwarded by the engine as `GET`, `PUT` and `DELETE /api/v1/backups/remotes/{name}` and `POST /api/v1/backups/remotes/{name}/test`. Secret parameters are returned as `!masked` and kept when sent back so (CORE-303). After each scheduled backup the archive and its sidecar are copied to `backup.remote` and `keep` is applied there; a failure is logged, shown in the admin and on the debug page, and does not fail the local backup.
- CORE-366: Pre-operation backups. Before every restore, install, update, removal and restore of the previous venv, and before an engine upgrade by `mm3-install.sh` (asking the installer as a root peer), the installer takes a backup of kind `pre` with `config`, `data`, `modules` and, when `encrypt_to` is set, `secrets`, in `/var/lib/mm3/backups/pre/`. The last 3 are kept, outside `keep`; when this backup fails, the operation does not start.

**CLI**: `mm3 backup create [--parts …] [--encrypt]`, `mm3 backup list [--remote]`, `mm3 backup verify <file>`, `mm3 backup restore <file> [--parts …] [--instance <id>]`, `mm3 backup keygen`.

Diagnostic bundles (10.5) never include backups or keys.

### 10.9 Dependency management

Dependencies are updated automatically through tested pull requests; nothing reaches a mirror unless it passed CI as part of an engine release with its lockfile (CORE-170).

**Pinning**

- CORE-171: `pyproject.toml` declares ranges bounded to the current major of each dependency (`>=x.y,<next`). `uv.lock` pins exact versions with hashes for every platform target; CI, release builds and the installer install only from the lock.
- CORE-172: Each engine release ships its lock as a constraints file; the installer (10.7) resolves module dependencies within those constraints and refuses a module whose requirements conflict with them.
- Vendored front-end libraries (CodeMirror, uPlot, Material Design Icons) live under `src/mm3/core/web/vendor/` with a `vendor.json` that records name, version, source URL and SHA-256.
- System packages (Chromium, cage, wlr-randr, PipeWire, pulseaudio-utils, VA-API drivers, ddcutil, cec-utils, age, rclone) are taken from Debian stable; the install script declares minimum versions.

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

- CORE-100: `await ctx.mqtt.subscribe(filter, handler, *, qos=0)` returns a `Subscription`, cancelled when the instance stops; `await ctx.mqtt.publish(topic, payload, *, qos=0, retain=False)` publishes a mapping as JSON, a string as UTF-8 and bytes as they are. Messages for a subscription are queued in the instance inbox and handled like notifications: one at a time, in arrival order, under the handler budget (CORE-28, CORE-193). Subscribe and publish are allowed only on topics matching the instance's `grants.mqtt.subscribe` / `grants.mqtt.publish` glob lists; default is deny. Signatures, `MqttMessage` and `Subscription`: Module Contract §4.4.
- CORE-101: Config type `LocalizedText` (`mm3.config`) accepts a string, the same in every language, or a non-empty map from BCP 47 tags to strings (`{en: …, it: …}`); an invalid tag is an error. It resolves through the locale chain (8.2); if no tag of the chain is present, the first entry in file order is used. Backend code calls `value.resolve(ctx.locale)`. In the public config sent to browsers the engine replaces each `LocalizedText` with its resolved string for that element's locale, and sends the config again when the locale changes. The form view shows one field per shipped locale; a missing `en` is a validation warning.
- CORE-102: A module MAY declare `admin_element` and `admin_scripts`; these load only inside the admin UI on the admin listener and get an authenticated API helper. The builder (11.7) is such an extension.
- CORE-329: Admin extension pages. For every installed module that declares `admin_element` and the capability `admin.extension` and has at least one instance in the running configuration (enabled or disabled), the admin listener serves the page `/admin/ext/<module>` with the CSP of CORE-63, and the admin header shows a link to it labelled with the module's `HEADER` text (MOD-67), else its name. The page loads the module's `admin_scripts` from `/admin/ext/<module>/<version>/<file>` (versioned and cached like CORE-252) and creates one `admin_element`. These files are served nowhere else; on the mirror listener the paths return 404. A module that declares `admin_element` without the capability gets no page, and the engine logs a warning once. A module with no instance, or an unknown module, returns 404. The Form view lists `admin.extension` with the other capabilities of the instance (MOD-71).
- CORE-330: Admin element. Before it is connected the element receives the properties `api` (CORE-331), `module` (its name), `instances` (`[{id, enabled, state}]` of its instances, as in `GET /api/v1/modules`), `locale` (the admin language, CORE-309) and `t(key, params)`, which looks the key up in the module's catalog along that locale's chain, then in the core catalog (CORE-264). The page sends nothing else; the element reads anything more through `api`.
- CORE-331: API helper. `api.get(path)`, `api.post(path, body)`, `api.put(path, body)`, `api.patch(path, body)` and `api.delete(path)` call `/api/v1/<path>` with the admin session (CORE-253), JSON in and out; they resolve with the parsed body, or null for 204. Any other status rejects with an `ApiError` carrying `status`, `code` and the problem (CORE-298) or, for module routes, the body as sent. `api.module(instanceId)` returns the same helper rooted at `/api/v1/modules/<instanceId>/` (CORE-332). The helper reaches the whole API: an extension runs with the admin's rights, which the admin sees as the capability `admin.extension`.
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
- [x] Framework list (1.2) approved, including CodeMirror 6, gridstack.js and the in-house form renderer (gridstack.js replaced by the in-house overlay in 0.28).
- [x] Widget and migration libraries approved: Jinja2 sandbox, uPlot, Material Design Icons, esprima.
- [x] Default kiosk display stack: Wayland with cage + Chromium, `wlr-randr` for power.
- [x] Module distribution: public PyPI.
- [x] Admin UI scope for v1: full editor (layout, forms, YAML) with live validation, on a separate admin listener.

After approval: repository scaffold and `mm3.core` skeleton with tests for CORE-01–26, then the reference modules (clock, weather) to prove the contract end to end.

## 13. Document history

| Version | Date | Changes |
| --- | --- | --- |
| 0.36 | 2026-11-06 | Catalog and installer, milestone 7 development: check of a wheel (CORE-371) and rebuilds before 1.0 (CORE-372) added |
| 0.35 | 2026-11-06 | Catalog and installer, milestone 7 development: cache verified only by the installer (CORE-351), socket in the installer's runtime directory, refresh route, installed modules and unlisted sources (CORE-355) and capabilities and write paths (CORE-359) changed; untrusted code as mm3-build (CORE-367), installer venv and build user (CORE-368), passphrase and TOTP setup (CORE-369) and installer downloads (CORE-370) added; 9.1 table and 10.7 Separation updated |
| 0.34 | 2026-11-06 | Backup and restore, milestone 7: installer rights (CORE-359), scheduled backups by the installer (CORE-360), requests and routes (CORE-361), archive layout and sidecar (CORE-362), restore order (CORE-363), keys (CORE-364), remotes (CORE-365) and pre-operation backups (CORE-366) added; mm3-backup.timer removed from the unit table, 10.8 scheduled backups and 1.1 updated |
| 0.33 | 2026-11-06 | Catalog and installer, milestone 7: verification libraries (CORE-349), index format (CORE-350), cache and freshness (CORE-351), release verification (CORE-352), CLI through the installer (CORE-353), staged venv (CORE-354), installer protocol (CORE-355), engine catalog and installer API (CORE-356), restart and checks (CORE-357) and removal (CORE-358) added; 10.6 CLI table and 1.2 (supply-chain verification) updated |
| 0.32 | 2026-11-06 | Presence and power, milestone 7: display power through the kiosk agent (CORE-346), CORE\_INTERACTION on touch (CORE-347), ddcutil and cec-utils with the groups i2c and dialout (CORE-348) and the mm3-presence reference module (PRS-01, PRS-02, PRS-03, PRS-04, PRS-05, PRS-06, PRS-07, PRS-08) added; 9.2, 5.5 registry, 6.1 example, system packages, 1.1 (no X11 driver) and 1.2 (display control) updated |
| 0.31 | 2026-11-06 | MQTT bridge and Home Assistant, milestone 7: bridge configuration (CORE-338), connection, Last Will and status (CORE-339), inbound notifications through mqtt.import (CORE-340), outbound envelopes (CORE-341), ctx.mqtt without a connection (CORE-342), discovery and engine entities (CORE-343), module entities through ha\_entity() (CORE-344) and stale entities (CORE-345) added; 9.3 topic table (display and presence topics moved to the presence module, ha topics added) and 1.1 updated |
| 0.30 | 2026-11-06 | Pages for MMM-pages and MMM-ProfileSwitcher, milestone 7: pages out of the cycle (CORE-333), timed jump to a page (CORE-334), idle return to default (CORE-335), pause holds with PAGE\_PAUSE, PAGE\_RESUME and REST (CORE-336), previous page and reasons after and idle in CORE\_PAGE\_CHANGED (CORE-337) added; 5.5 registry, 9.1 table and 1.1 updated |
| 0.29 | 2026-11-05 | Admin extensions of milestone 6: one page per module at /admin/ext/\<module> (CORE-329), the admin element's properties (CORE-330), the API helper (CORE-331) and module routes on ctx.router (CORE-332) added |
| 0.28 | 2026-11-05 | Layout view of milestone 6: patch and validate also return the draft layout object (CORE-301, CORE-302 changed); Show on mirror answers 204 and the toggle turns off after 60 s without a push (CORE-307 changed); /admin/config may frame the preview (CORE-63 changed); preview iframe messages and pinned page (CORE-325), in-house editing overlay (CORE-326), page write rule (CORE-327) and overlay toolbar with Rescale proportionally (CORE-328) added; gridstack.js removed from 1.2 |
| 0.27 | 2026-11-05 | POST /config/patch also returns documents, the data of every YAML file of the draft, for the Form view (CORE-301 changed) |
| 0.26 | 2026-11-05 | Editor details settled at the start of milestone 6 part D: YAML view lint through validate, in-house completion, CodeMirror bundled once with esbuild (CORE-323 added); GET /config/history/{version} for Open in editor (CORE-324 added); session locale body, answer and session-only use (CORE-309 changed); 1.1, 1.2 and 9.1 updated |
| 0.25 | 2026-11-04 | Editor and history API details settled at the start of milestone 6 part C: CORE\_API\_NOTHING\_TO\_DO 409 (CORE-298 changed); tagged nodes in JSON Patch values as one-key objects, in-house JSON Patch on ruamel nodes (CORE-320 added); history routes, item and diff shapes, undo, redo and restore answers (CORE-321 added); /admin/config as one page with Layout, Form, YAML and History tabs (CORE-322 added); 9.1 table and 1.1 updated |
| 0.24 | 2026-11-04 | Diagnostics details settled at the start of milestone 6 part B: log filters with minimum level and case-insensitive text (CORE-316 added); status frames on change, at most once a second, ignoring uptime (CORE-317 added); fixed layout of the diagnostic bundle (CORE-318 added); secret values masked as !masked in captured payloads (CORE-319 added); 1.1 updated |
| 0.23 | 2026-11-04 | REST base gaps settled at the start of milestone 6 development: CORE\_API\_REQUEST\_TIMEOUT 504 (CORE-298 changed); log-level body {level}, null for the default, GET of the engine loggers, fixed debug\_ttl expiry, 404 for unknown loggers (CORE-112 changed); POST /api/v1/request returns {payload}, an error reply as a 200 reply, 504 on timeout (CORE-314 added); GET /modules entries, idempotent instance controls, 404 for unknown instances and pages (CORE-315 added); 9.1 table and 1.1 updated |
| 0.22 | 2026-11-04 | Milestone 6 settled: API errors as problem+json with CORE\_API\_\* codes (CORE-298 added); paging of history and logs (CORE-299 added); config ETag, 428 and 409 conflicts with disk\_invalid (CORE-300 added); editing protocol with files per role and POST /config/patch (CORE-301 added); validate returns the diff preview, save responses (CORE-302 added); !masked secrets (CORE-303 added); admin channel /admin/ws with status, logs and bus topics (CORE-304 added); GET /status contents with the kiosk object (CORE-305 added); kiosk dropdowns and run-time volume and mute (CORE-306 added); Show on mirror on the kiosk only (CORE-307 added); notify rejects CORE\_ names and payloads that do not match the registry (CORE-308 added); admin language per session (CORE-309 added); form label and help keys (CORE-310 added); local admin socket and mm3 diag bundle (CORE-311 added); mm3 diag check with skip and exit codes (CORE-312 added); OpenAPI as JSON only (CORE-313 added); section undo uses the history sections (CORE-152 changed); preview resolution from the agent's outputs, one kiosk (6.6); 9.1 table and 1.1 updated |
| 0.21 | 2026-11-01 | Kiosk audio base: PipeWire in the kiosk user's session, seat ACLs instead of group audio (CORE-295 added); audio block with sink, volume and mute applied live by the agent with pactl, run-time volume never persisted (CORE-296 added); hardware video decoding with VA-API drivers and Chromium features (CORE-297 added); media-src 'self' in the CSP (CORE-63 changed); kiosk session plays sound (CORE-143 changed); audio as a top-level key and a history section (CORE-201, CORE-216, CORE-155 changed); VA-API features in the Chromium flags (CORE-270 changed); sinks and audio frames, failure area (CORE-277 changed); installer adds the audio and video packages (CORE-293 changed); PipeWire and pactl approved (1.2); reload table, 1.1 and 10.9 updated |
| 0.20 | 2026-10-31 | Packaging and deployment settled after the first manual install: sd\_notify messages READY, STATUS, RELOADING and STOPPING (CORE-290 added; 7.1 steps 5 and 6 cite it); watchdog on event-loop liveness only (CORE-291 added); mm3.service unit shipped in the package, mm3 service unit (CORE-292 added); mm3-install.sh behaviour, staged venv, no overwrite, modules installed with --no-deps until mm3-core 1.0 (CORE-293 added); mm3-modules-default as a distribution without code (CORE-294 added); the engine user mm3 is a member of group mm3-kiosk and a failed key group change is logged (CORE-268 changed); 10.2 table cites the new ids |
| 0.19 | 2026-10-30 | Milestone 5 settled: HTTP transport behind ctx.http (new 3.4: CORE-282–287); engine.location and ctx.location (CORE-288; CORE-201, CORE-211 and CORE-246 changed); WEATHER\_CURRENT in the registry (5.5); reference modules mm3-greeting, mm3-clock and mm3-weather (new 9.4: CORE-289, CLK-01–08, WTH-01–12) |
| 0.18 | 2026-10-30 | ctx.mqtt for the test kit: async publish and subscribe signatures, payload encoding, messages delivered through the instance inbox under the handler budget (CORE-100 changed); details in Module Contract 1.0 rev 9 §4.4 |
| 0.17 | 2026-10-30 | Milestone 4 tooling settled: one engine clock for the scheduler, budgets and ctx.now(), virtual in mm3.testing (CORE-278 added); mm3-core\[testing\] extra (CORE-279 added); mm3-build with the mm3-i18n build hook (CORE-280 added); mm3 module check and mm3 new-module --kind (CORE-281 added); now() in the 4.2 context table; mm3-build added to 1.2 and 10.2; CLI list updated |
| 0.16 | 2026-10-29 | Kiosk details settled: agent key in an Authorization Bearer header, 4403 otherwise (CORE-268 changed); outputs.current and mode choice by the agent (CORE-277 changed); applied mode re-validates the running configuration, resync and CORE\_DISPLAY\_CHANGED (CORE-146 changed); wlr-randr --json and the shipped unit file with mm3 kiosk unit (CORE-143 changed); websockets approved for the kiosk agent (1.2) |
| 0.15 | 2026-10-29 | i18n details settled: merged catalog at /i18n/\<instance>.json, its version, locale packages per module or mm3 (CORE-264 changed); mm3 i18n check fails on compiled files that do not match their .po, backends read messages.json (CORE-263 changed); date styles and backend formatting (CORE-266 changed); per-instance locale and touch flag in welcome (CORE-246 changed) for element-owned swipes (CORE-235 changed) |
| 0.14 | 2026-10-29 | Shell and kiosk details settled: welcome instance entries carry scripts, styles, appearance and status, and custom\_css (CORE-246 changed); /appearance/\<instance>.css URL (CORE-252 changed); visibility and touch frames, status control action (CORE-271 changed); control action contents and resync on reload (CORE-275); shell input frames (CORE-276); kiosk agent protocol (CORE-277); outputs reported by the agent (CORE-144 changed); welcome carries timezone and touch (CORE-246 changed); grid size, gap and padding in the layout object (CORE-274 changed) |
| 0.13 | 2026-10-28 | WebSocket frame contents and ref (CORE-271); error and reply codes (CORE-272); /ws and /admin/preview/ws endpoints, hello timeout, engine\_build (CORE-273); layout object (CORE-274) |
| 0.12 | 2026-10-28 | Kiosk version check reads cage -v and compares the leading MAJOR.MINOR.PATCH, not the Debian release; no cage feature after 0.1.5 is used (CORE-143 changed) |
| 0.11 | 2026-10-28 | Milestone 3 settled: WebSocket handshake, roles, limits, reconnect, liveness and URLs (CORE-246–252); allow lists on both listeners, named tokens in a token store, no token in the config, admin sessions, display pairing, security headers (CORE-61–63 changed, CORE-201 changed, CORE-253, CORE-254); element setup, header and size, stacks, fit, transitions (CORE-255–259); swipe (CORE-260); theme variables and custom.css as a config section (CORE-261, CORE-262; CORE-153, CORE-155, CORE-208, CORE-216, CORE-243 changed); catalogs compiled at build, lookup, plurals, formatting, locale change (CORE-263–267); kiosk as a system unit with an agent, Debian 13+ (CORE-143 changed, CORE-268–270); lit-html and Roboto Condensed approved (1.2) |
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
