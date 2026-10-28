# MM3 Module Contract v1.0

Oct 27, 2026 · @Nicola Reina · Version 1.0 rev 6 (draft)

## 1. Scope and conformance

This document is the normative contract between the MM3 engine and every module: a module that meets all MUST requirements here, as verified by `mm3 module check`, is guaranteed to load and run on any engine supporting Module API 1.x.

- **Audience:** module developers, reviewers, and the authors of `mm3-core`.
- **Relation to the core spec:** the [Core Engine Specification](https://claude.ai/artifact/FMsTUUoBP9wHzFtruUa6FV) defines how the engine behaves; this contract defines what a module must do and may rely on. Where they overlap, the CORE id is cited; on conflict, this document wins for module obligations and the core spec is corrected.
- **Keywords:** MUST, MUST NOT, SHOULD, MAY as in RFC 2119. Requirement ids are `MOD-xx`; each has a check method in section 11.
- **Module kinds:**

| Kind | Has backend | Has front-end | Example |
| --- | --- | --- | --- |
| Full | yes | yes | weather |
| Backend-only | yes | no | presence |
| Front-end-only | no (generated stub) | yes | clock |

- **Versioning:** the contract version equals the Module API version it describes (1.0 ↔ `api_version = "1.0"`); the revision number counts changes to this document within that version and is listed in section 14. Code cites the MOD ids with contract version and revision, e.g. "Module Contract 1.0 rev 3, §4.3 HttpClient" (core spec CORE-74).

## 2. Package and metadata

A module is one installable Python distribution exposing exactly one `MirrorModule` subclass through the `mm3.modules` entry point; front-end-only modules still ship this class, with no hooks.

```toml
[project]
name = "mm3-weather"                 # MOD-01: prefix mm3-
version = "1.2.0"                    # MOD-02: SemVer
requires-python = ">=3.11"
dependencies = ["mm3-core>=1.0,<2"] # MOD-03

[project.entry-points."mm3.modules"]
weather = "mm3_weather:WeatherModule" # MOD-04

[tool.mm3]
category = "weather"                 # admin catalog grouping
icon = "mdi:weather-partly-cloudy"
screenshot = "docs/screenshot.png"
```

- MOD-01: Distribution name MUST start with `mm3-`; import package MUST be `mm3_<name>`.
- MOD-02: `version` MUST follow Semantic Versioning.
- MOD-03: The module MUST depend on `mm3-core` with a range bounded to one API major (`>=1.0,<2`).
- MOD-04: Exactly one entry point in group `mm3.modules`; its key MUST equal `MirrorModule.name`.
- MOD-05: Static assets live under `<package>/static/`, catalogs under `<package>/locales/`; both MUST be included as package data.
- MOD-06: The package MUST NOT write outside its storage directory, install system packages, or modify other packages at runtime.
- MOD-07: Dependencies SHOULD be pure Python or ship manylinux x86-64 wheels, so installs work in a plain venv on Debian.
- MOD-08: A `README.md` with purpose, config example and screenshot SHOULD be present; it is shown in the admin catalog.

## 3. Backend contract

The engine drives a module only through the class attributes and hooks below; anything else on the class is private to the module.

### 3.1 Class attributes

| Attribute | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | required | Unique id, `[a-z][a-z0-9_]*`, equals entry point key |
| `api_version` | `str` | `"1.0"` | Module API the module was written for |
| `config_model` | `type[ModuleConfig]` | `ModuleConfig` | Pydantic model of the instance `config` block |
| `element` | `str \| None` | `None` | Custom element tag `mm3-<name>`; `None` = backend-only |
| `scripts`, `styles` | `list[str]` | `[]` | Paths under `static/`, loaded by shell |
| `locales_dir` | `str \| None` | `"locales"` | Catalog root |
| `subscribes` | `set[str] \| None` | `None` | Notification name globs to receive; `None` = all |
| `capabilities` | `set[str]` | `set()` | Privileges requested (section 8) |
| `min_size_px` | `tuple[int, int] \| None` | `None` | Smallest usable area (CORE-96) |
| `admin_element`, `admin_scripts` | `str \| None`, `list[str]` | `None`, `[]` | Admin UI extension (CORE-102) |

### 3.2 Hooks

| Hook | Called when | Budget | On unhandled exception |
| --- | --- | --- | --- |
| `__init__(ctx, config)` | instance created | sync, no I/O | instance DISABLED |
| `async start()` | after init, and after each restart | 15 s | FAILED, retry with backoff |
| `async stop()` | removal, restart, shutdown | 10 s | logged; instance treated as stopped |
| `async suspend()` / `resume()` | `CORE_SUSPEND` / `CORE_RESUME`, instance hidden/shown | 5 s | FAILED |
| `async reconfigure(new)` | instance config changed | 5 s | instance restarted |
| `async on_notification(n)` | matching bus notification | 5 s | FAILED |
| `async on_client_message(msg)` | own front-end sent a message | 5 s | FAILED |
| `snapshot()` | new client connects, state requested | sync, ≤ 50 ms | empty state sent, error logged |
| async on\_request(req) -> dict \| None | a request (expects\_reply) delivered to this instance (core 5.4) | 5 s | error reply to the requester; counts as a handler failure |

Budgets are engine defaults (`engine.timeouts.*`, core 3.2). When `start`, `suspend`, `resume` or `reconfigure` exceeds its budget, the call is cancelled and counts as an exception. A handler that exceeds its budget is cancelled and logged, and the instance stays RUNNING; three handler timeouts in a row move it to FAILED (core CORE-193). `snapshot()` is measured, not cancelled.

### 3.3 Rules

- MOD-10: `__init__` MUST only store `ctx` and `config` and set up plain attributes; no I/O, no tasks.
- MOD-11: All hooks except `snapshot` MUST be `async`, and MUST NOT block the event loop; blocking work MUST use `ctx.run_blocking` (CORE 3.2).
- MOD-12: Background work MUST be started through `ctx.create_task` or `ctx.scheduler`, never bare `asyncio.create_task`, so the engine can cancel it.
- MOD-13: `stop()` MUST be idempotent and release every resource the instance acquired.
- MOD-14: `asyncio.CancelledError` MUST be re-raised, never swallowed.
- MOD-15: Several instances of one module MUST be able to run at once; no mutable module-level state.
- MOD-16: `snapshot()` MUST return JSON-serialisable data ≤ 256 KiB (CORE-26) and contain no secrets.
- MOD-18: The engine calls `on_notification` and `on_client_message` of one instance one at a time, in arrival order (core CORE-28). Modules MAY rely on this and SHOULD hand long work to `ctx.create_task`.
- MOD-19: A restart creates a new instance object and a new context (core CORE-191). State that must survive a restart SHOULD be kept in `ctx.storage`.

### 3.4 Error semantics

Modules signal expected failures with the exception types from `mm3.errors`; everything else is treated as a bug.

| Raise | Meaning | Engine reaction | Shown on mirror |
| --- | --- | --- | --- |
| `TransientError(message_key)` | Temporary problem, e.g. network down | Warning logged, instance stays RUNNING; module retries itself | Element keeps last state, marked stale |
| `ConfigurationError(field, message_key)` | Config valid in form but rejected in use, e.g. API key refused | Instance DISABLED until config changes; admin highlights `field` | Localized "unavailable" placeholder |
| any other `Exception` | Bug | FAILED, restart with backoff (core 7) | Localized "unavailable" placeholder |

- MOD-17: `message_key` MUST be a key present in the module's catalogs, so the admin shows the reason in the user's language.

## 4. ModuleContext reference

`ModuleContext` is the module's only door into the engine; it is injected into `__init__`, valid until `stop()` returns, and every member below is stable within API 1.x.

```python
class ModuleContext(Protocol):
    # --- identity (read-only) ---
    instance_id: str                  # "weather_home"
    module_name: str                  # "weather"
    locale: str                       # resolved active locale, e.g. "it"
    timezone: ZoneInfo
    units: Literal["metric", "imperial"]
    position: GridRect | None         # None for backend-only
    page: str                         # current page id (core 6.9)
    pages: Sequence[PageInfo]         # ids and resolved titles, always current

    # --- messaging ---
    async def send_notification(self, name: str, payload: dict | None = None,
                                target: str | None = None) -> UUID: ...
    async def send_to_client(self, type: str, payload: dict | None = None,
                             client: str | None = None) -> None: ...
    async def request(self, name: str, payload: dict | None = None, *,
                      target: str | None = None, timeout: float = 5.0) -> dict: ...  # core 5.4
    state: Mapping[str, Any]          # last state set, read-only
    def set_state(self, state: dict) -> None: ...      # replace snapshot, push "state"
    def patch_state(self, delta: dict) -> None: ...    # shallow merge, push delta

    # --- tasks and time ---
    def create_task(self, coro: Coroutine, *, name: str) -> asyncio.Task: ...
    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T: ...
    scheduler: Scheduler              # every(), cron(), at() -> Job; semantics in core spec 3.3

    # --- services ---
    http: HttpClient                  # MM3 HTTP client, section 4.3
    storage: Storage                  # path: Path, async kv_get/kv_set/kv_delete, JSON values, ≤ 1 MiB (core CORE-198)
    health: Health                    # set(key, value): extra health fields (MOD-85)
    log: Logger                       # MM3 protocol: debug/info/warning/error/exception(event, **fields)
    def t(self, key: str, /, count: int | None = None, **vars: Any) -> str: ...
    def format_date(self, value: datetime, style: str = "medium") -> str: ...
    def format_number(self, value: float, *, unit: str | None = None) -> str: ...

    # --- capability-gated (section 8) ---
    mqtt: Mqtt                        # publish(), subscribe(), ha_entity()
    system: System                    # request_suspend(reason), request_resume(reason)
    router: fastapi.APIRouter         # mounted at /api/v1/modules/{instance_id}/
    layout: Layout                    # hide(instance_id), show(instance_id); layout.control
```

### 4.1 Exceptions raised by the context

| Exception | Raised by | When |
| --- | --- | --- |
| `PermissionError` | `mqtt.*`, `system.*`, `router`, `http` | Capability not declared or topic/URL not granted |
| `ValueError` | `send_*`, `set_state`, `patch_state` | Payload not JSON-serialisable or > 256 KiB; name not `UPPER_SNAKE` |
| `ReservedNameError` | `send_notification` | Name starts with `CORE_` |
| `ContextClosedError` | any member | Called after `stop()` returned |
| `mm3.errors.HttpError` | `http` | Network or HTTP failure; module SHOULD convert to `TransientError` |
| RequestTimeoutError | request | No reply within timeout (core CORE-220) |
| RemoteError | request | The responder's on\_request raised; carries code and message\_key (CORE-221) |
| ValueError | send\_notification, request | Payload of a registered name does not match its schema (CORE-225) |

### 4.2 Guarantees

- MOD-20: Members listed here keep their signatures and semantics for the whole 1.x line; new optional parameters and members may be added.
- MOD-21: `set_state` / `patch_state` updates are delivered to connected clients in call order; clients that connect later receive the latest full state.
- MOD-22: Jobs, tasks, subscriptions and routes created through the context are cancelled or removed automatically after `stop()`.

### 4.3 HttpClient

`ctx.http` is an MM3-owned client with a stable API for the whole 1.x line; the library behind it (currently httpx) can change without affecting modules (MOD-23).

```python
class HttpClient(Protocol):
    async def request(self, method: str, url: str, *,
                      params: Mapping[str, str] | None = None,
                      headers: Mapping[str, str] | None = None,
                      json: Any = None, data: bytes | None = None,
                      timeout: float = 10.0) -> HttpResponse: ...
    async def get(self, url: str, **kw: Any) -> HttpResponse: ...    # same keywords
    async def post(self, url: str, **kw: Any) -> HttpResponse: ...

class HttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]
    url: str
    def json(self) -> Any: ...
    def text(self) -> str: ...
    def bytes(self) -> bytes: ...
    def raise_for_status(self) -> None: ...     # raises HttpStatusError for 4xx/5xx
```

- Errors: `mm3.errors.HttpError` (base), with `HttpTimeoutError`, `HttpConnectionError` and `HttpStatusError` (carries `status`). Modules SHOULD convert them to `TransientError` or `ConfigurationError` (3.4).
- Limits: response bodies above 10 MiB are refused; redirects are followed only to URLs that are also granted (section 8).
- MOD-23: Modules MUST use `ctx.http` for outbound HTTP and MUST NOT import the underlying library or rely on its types.

## 5. Front-end contract

A front-end is one ES module that defines one custom element extending `MirrorElement`; the shell creates one element per instance in its grid cell and calls only the callbacks below.

```js
import { MirrorElement, html } from "/core/mm3.js";

export class WeatherElement extends MirrorElement {
  render(state) {                         // required
    return html`<div class="temp">${this.fmtNumber(state.temp, "celsius")}</div>`;
  }
}
customElements.define("mm3-weather", WeatherElement);
```

### 5.1 Callbacks (implemented by the module)

| Callback | Called when | Required |
| --- | --- | --- |
| `render(state)` → `TemplateResult \| Node \| string` | first state, and after every `set_state` / `patch_state` | yes |
| `onMessage(type, payload)` | backend `send_to_client` | no |
| `onNotification(name, payload)` | bus notification matching `subscribes` | no |
| `onResize(widthPx, heightPx)` | cell area changes (CORE-95) | no |
| `onSuspend()` / `onResume()` | instance hidden or mirror suspended | no |
| `onLocaleChange(locale)` | `CORE_LOCALE_CHANGED`; default re-renders | no |
| onPageChange(page) | current page changed (CORE-237); called while visible | no |

### 5.2 Provided by `MirrorElement`

| Member | Purpose |
| --- | --- |
| `this.instanceId`, `this.config`, `this.state`, `this.locale` | Identity, public config (secrets removed, `LocalizedText` resolved), last state, active locale |
| `this.preview` | `true` inside the admin live preview (CORE-98) |
| `this.shadow` | Open shadow root with module CSS and appearance variables applied |
| `this.t(key, vars)`, `this.fmtDate(d, style)`, `this.fmtNumber(n, unit)` | Localized text and formatting via `Intl` |
| `this.send(type, payload)` | Message to own backend (`on_client_message`) |
| `this.notify(name, payload)` | Bus notification |
| `this.requestRender()` | Re-render with current state |
| `html` tagged template | Auto-escaping templates (CORE-64) |
| this.log.debug/info/warn/error | Structured logging forwarded to the server log (MOD-80) |
| this.setInterval, this.setTimeout | Timers cleared automatically when the element disconnects (MOD-35) |
| this.request(name, payload, {target, timeout}) | Request on the bus, resolves with the reply (core 5.4) |
| this.page | Current page id (core 6.9) |

### 5.3 Rules

- MOD-30: The tag MUST be `mm3-<name>`; the file MUST define exactly that element and nothing global.
- MOD-31: Rendering MUST use `html` or DOM APIs; assigning untrusted data to `innerHTML` is forbidden.
- MOD-32: The element MUST NOT call `fetch`, `XMLHttpRequest`, `WebSocket` or `EventSource` to any origin; data comes from its backend (CORE-12).
- MOD-33: CSS MUST stay inside the shadow root; no styles on `document`, no global fonts except via the theme variables.
- MOD-34: `render` MUST be pure with respect to `state` (same state, same output) and SHOULD finish in under 16 ms.
- MOD-35: Timers MUST be created with `this.setInterval` / `this.setTimeout` (cleared automatically on disconnect), not `window.setInterval`.
- MOD-36: The element MUST render something sensible for an empty state `{}` (e.g. a skeleton), because it may receive one before data arrives.

## 6. Messaging, configuration, layout and preview

These obligations make a module interoperate with other modules, the config editor and the grid layout.

### 6.1 Messaging

- MOD-40: Notification names MUST be `UPPER_SNAKE` and SHOULD be prefixed with the module name for module-specific events (`WEATHER_UPDATED`). Names meant for many producers (`PRESENCE_DETECTED`, `SHOW_ALERT`) are registered in the core spec §5.5 and MUST use the registered payload (CORE-224).
- MOD-41: Every notification a module emits or consumes MUST be listed with its payload schema in the README under "Notifications"; `mm3 module check` compares this list with the code.
- MOD-42: Payloads MUST be JSON objects ≤ 256 KiB, contain no secrets, and use ISO 8601 UTC strings for times and SI units unless the field name says otherwise (`temp_c`, `wind_kmh`).
- MOD-43: Handlers MUST tolerate unknown payload fields and missing optional fields.
- MOD-44: Client message `type` values are private to the module and SHOULD be lower snake case.
- MOD-24: A module that answers requests MUST list the request names, request payloads and reply payloads in its README under "Notifications" (as MOD-41), and MUST return None from on\_request for requests it does not handle.
- MOD-25: on\_request MUST NOT send requests itself and SHOULD answer well within the handler budget; slow data SHOULD be served from state the module keeps up to date (core CORE-222).
- MOD-26: Callers of ctx.request and this.request MUST handle RequestTimeoutError and RemoteError and keep working (degraded) without the reply.
- MOD-27: Modules MUST tolerate being hidden and shown often (page rotation, core CORE-239): suspend() and resume() SHOULD be cheap, and costly refreshes SHOULD use pause\_on\_suspend or skip work while hidden.

### 6.2 Configuration

```python
class WeatherConfig(ModuleConfig):
    """Configuration of the weather module."""
    api_key: SecretStr = Field(json_schema_extra={"secret": True})
    lat: float = Field(ge=-90, le=90, title="CFG_LAT")
    lon: float = Field(ge=-180, le=180, title="CFG_LON")
    update_every: timedelta = Field(timedelta(minutes=10), ge=timedelta(minutes=5))
    label: LocalizedText | None = None
```

- MOD-45: `config_model` MUST subclass `ModuleConfig` (which sets `extra="forbid"`); every field MUST have a type, and either a default or a clear reason to be required.
- MOD-46: Field `title` and `description` MUST be catalog keys, so the form view is translated (core 6.4).
- MOD-47: Secrets MUST use `SecretStr` and `secret: True`; they are masked in the admin, API, logs and diagnostics.
- MOD-48: User-typed display text MUST use `LocalizedText` (CORE-101).
- MOD-49: Validation SHOULD be complete in the model (ranges, enums, patterns) so errors appear while editing; checks that need I/O raise `ConfigurationError` at runtime.
- MOD-50: `reconfigure()` SHOULD apply changes live where it is cheap; returning `False` is always correct.
- Config types provided by `mm3.config`: `ModuleConfig`, `LocalizedText`, and `CronExpression`, a five-field cron string validated when the config is loaded (core CORE-188). A `LocalizedText` is resolved in the backend with `value.resolve(ctx.locale)`; front-ends receive the resolved string in `this.config` (core CORE-101). `timedelta` fields accept seconds, ISO 8601 and the compact form `30s`, `10m`, `1h30m` (core CORE-205). `ctx.locale`, `ctx.timezone` and `ctx.units` always hold the current values after a reload (core CORE-211).

### 6.3 Layout and preview

- MOD-51: The front-end MUST fit any area its grid cell gives it: adapt via `onResize`, or rely on the instance's `overflow` setting; it MUST NOT set fixed page-level sizes.
- MOD-52: A module with a practical lower limit SHOULD declare `min_size_px`.
- MOD-53: Sizes SHOULD use `em` and the `--mm3-scale` variable so the `scale` appearance setting works.
- MOD-54: When `this.preview` is true, modules showing camera, microphone or private data MUST render a same-size placeholder (CORE-98).

## 7. Internationalization

Every user-facing module MUST ship complete catalogs for EN, IT, FR, ES and DE; more languages are added as files, never as code (CORE-40).

```text
locales/
├─ messages.pot                 # generated template
├─ en/LC_MESSAGES/messages.po   # source language
├─ it/ …  fr/ …  es/ …  de/ …
└─ (generated) <lang>/messages.json, <lang>/LC_MESSAGES/messages.mo
```

- MOD-60: Message ids are stable `UPPER_SNAKE` keys (`FEELS_LIKE`), never English sentences, so changing the English text does not break translations.
- MOD-61: No user-visible literal strings in Python or JS: notification texts, element content, admin labels, config field titles and error messages all go through `t()`.
- MOD-62: All five shipped locales MUST contain every key of `en`, with the same placeholder set (CORE-41, CORE-42).
- MOD-63: Plurals MUST use `count=` (Python) / `{count}` with plural forms in the `.po`, not string concatenation.
- MOD-64: Dates, times, numbers and units MUST be formatted with `format_date` / `format_number` (backend) or `fmtDate` / `fmtNumber` (front-end), never by hand.
- MOD-65: Text from external data (calendar titles, news headlines) is passed through as is and MUST NOT be translated.
- MOD-66: Third-party translations MAY be published as separate packages on entry point `mm3.locales`; they override nothing shipped by the module unless the language is missing.
- MOD-67: A user-facing module SHOULD provide the catalog key `HEADER`, the text shown when an instance sets `appearance.header: true` (core CORE-214); without it the module name is shown.

## 8. Capabilities and grants

Privileged context members work only if the module declares the capability in `capabilities` and, where a scope applies, the instance config grants it; both default to nothing (deny).

| Capability | Unlocks | Scope granted in instance `grants` | Admin shows |
| --- | --- | --- | --- |
| `mqtt.publish` | `ctx.mqtt.publish` | `mqtt.publish: [topic globs]` | Topics the instance may write |
| `mqtt.subscribe` | `ctx.mqtt.subscribe` | `mqtt.subscribe: [topic globs]` | Topics the instance may read |
| `ha.discovery` | `ctx.mqtt.ha_entity` | none (own device only) | Entities created |
| `http.outbound` | `ctx.http` to external hosts | `http: [URL prefixes]` | Hosts contacted |
| `system.power` | `ctx.system.request_suspend/resume` | none | Module can turn the display off |
| `api.routes` | `ctx.router` | none (always under own prefix, token-protected) | Routes added |
| `admin.extension` | `admin_element`, `admin_scripts` | none | Admin pages added |
| `input.touch` | Receiving pointer input on the kiosk | none | Module reacts to touch |
| layout.control | ctx.layout.hide / show on other instances (core CORE-241) | none | Module can hide or show other modules |

```yaml
- module: widget
  id: living_room
  grants:
    mqtt: { subscribe: ["zigbee2mqtt/living_*"], publish: ["zigbee2mqtt/living_light/set"] }
    http: ["http://inverter.lan/api/"]
```

- MOD-70: A module MUST declare every capability it uses; using an undeclared one raises `PermissionError` (section 4.1).
- MOD-71: A module MUST request the narrowest scope it needs and document required grants in its README; the admin UI shows requested vs granted scopes when the module is added.
- MOD-72: A module MUST keep working (degraded, with a clear message) when a grant is missing, rather than crash.
- MOD-73: The capability list is closed in API 1.x; new capabilities arrive only in a minor API release and are added to this table.

## 9. Logging, health and errors

Modules log through `ctx.log` only, so every record carries module, instance and trace id and shows up in the admin debug page (core 10.5).

- MOD-80: Use `ctx.log`; `print()`, the root `logging` logger and `console.log` outside debug are not allowed. In the front-end use `this.log.debug/info/warn/error`, which forwards to the server.
- MOD-81: Log events are short snake-case identifiers with structured fields, not formatted sentences: `ctx.log.info("forecast_fetched", items=12, ms=340)`.
- MOD-82: Levels: `debug` for detail useful when diagnosing, `info` for state changes (started, data refreshed), `warning` for recoverable problems, `error` only with an exception that needs attention.
- MOD-83: Secrets, tokens, full payloads of personal data (calendar entries, messages) MUST NOT be logged at `info` or above.
- MOD-84: A module SHOULD log at most one `info` record per refresh cycle; repetitive events belong at `debug`.
- MOD-85: Modules MAY add health fields with `ctx.health.set(key, value)` (e.g. `last_refresh`, `items`); they appear in the instance health record (CORE-113).
- MOD-86: Unexpected exceptions propagate to the engine; modules MUST NOT catch broad `Exception` just to hide it. Catching to convert into `TransientError` or `ConfigurationError` is the intended pattern (section 3.4).

## 10. API versioning and deprecation

The Module API has its own `MAJOR.MINOR` version, separate from the engine's release number; a module written for 1.x runs on every 1.y engine with y ≥ x.

| Change | Allowed in | Example |
| --- | --- | --- |
| New optional context member, parameter, callback, capability, notification | Minor | `a new ctx member` added in 1.1 |
| New config type or appearance field | Minor | `LocalizedText` extensions |
| Deprecating a member | Minor, with warning | `ctx.old()` logs `deprecated_api` |
| Removing or changing a member's signature or semantics | Major only | rename `send_to_client` |
| Changing timing budgets | Minor, only upward | `start` 15 s → 20 s |
| Tightening a MUST rule | Major only | — |

- MOD-90: The engine refuses a module whose `api_version` major differs from its own, or whose minor is higher than its own (CORE-03); the admin shows the reason.
- MOD-91: A deprecated member keeps working for at least one further minor release and logs `deprecated_api` once per instance, naming the replacement.
- MOD-92: Each engine release lists API additions and deprecations in its changelog and updates this document's version and section 11.
- MOD-93: A module SHOULD set `api_version` to the lowest minor whose features it uses, to run on the widest range of engines.
- MOD-94: The module API exposes MM3-owned types only, with two exceptions: Pydantic (config models) and FastAPI's APIRouter (capability api.routes). The engine keeps each of these within one major version for a whole Module API major; moving to a new major of either is a Module API major change. All other third-party libraries used by the engine can be upgraded without affecting modules.

## 11. Compliance checklist

A module is compliant when every MUST below passes; `mm3 module check` runs all **static** and **test kit** checks and prints the rest as a **review** list. Engine-side guarantees (MOD-18, 20–22, 73, 90–92, 94) are tested in `mm3-core` and not listed.

| Id | Requirement | Level | Checked by |
| --- | --- | --- | --- |
| MOD-01 | `mm3-` distribution, `mm3_<name>` package | MUST | static |
| MOD-02 | SemVer version | MUST | static |
| MOD-03 | `mm3-core` range within one API major | MUST | static |
| MOD-04 | One `mm3.modules` entry point, key = `name` | MUST | static |
| MOD-05 | `static/` and `locales/` in the built wheel | MUST | static (wheel built and inspected) |
| MOD-06 | No writes outside storage dir | MUST | test kit (sandboxed filesystem) |
| MOD-07 | Pure-Python or manylinux x86-64 wheels | SHOULD | static |
| MOD-08 | README with config example and screenshot | SHOULD | static |
| MOD-10 | No I/O or tasks in `__init__` | MUST | test kit |
| MOD-11 | Async hooks, no loop blocking | MUST | static + test kit (slow-callback detector, 100 ms) |
| MOD-12 | Tasks only via `ctx.create_task` / scheduler | MUST | static (AST scan) |
| MOD-13 | `stop()` idempotent, resources released | MUST | test kit (stop twice, leak check) |
| MOD-14 | `CancelledError` re-raised | MUST | test kit |
| MOD-15 | Two instances run side by side | MUST | test kit |
| MOD-16 | `snapshot()` JSON, ≤ 256 KiB, no secrets | MUST | test kit |
| MOD-17 | Error `message_key`s exist in catalogs | MUST | static |
| MOD-19 | State that must survive a restart kept in ctx.storage | SHOULD | review |
| MOD-30 | Tag `mm3-<name>`, no globals | MUST | static (JS scan) |
| MOD-31 | No `innerHTML` with data | MUST | static (JS scan) |
| MOD-32 | No `fetch` / XHR / WebSocket / EventSource | MUST | static + harness (CSP violations) |
| MOD-33 | CSS only inside shadow root | MUST | static + review |
| MOD-34 | Pure `render`, < 16 ms | MUST / SHOULD | harness |
| MOD-35 | Timers via `this.setInterval/setTimeout` | MUST | static |
| MOD-36 | Renders empty state `{}` | MUST | harness |
| MOD-40 | `UPPER_SNAKE` notification names | MUST | static |
| MOD-41 | Notifications documented in README | MUST | static (README vs code) |
| MOD-42 | Payload size, no secrets, ISO times | MUST | test kit |
| MOD-43 | Tolerates unknown and missing fields | MUST | test kit (payload fuzzing) |
| MOD-44 | Lower snake client message types | SHOULD | static |
| MOD-45 | `ModuleConfig` subclass, typed fields | MUST | static |
| MOD-46 | Field titles are catalog keys | MUST | static |
| MOD-47 | Secrets as `SecretStr` + `secret: True` | MUST | static (+ warning on names like key, token, password) |
| MOD-48 | User-typed text as `LocalizedText` | MUST | review |
| MOD-49 | Validation in the model | SHOULD | review |
| MOD-50 | Live `reconfigure()` where cheap | SHOULD | review |
| MOD-51 | Fits any cell area | MUST | harness (renders at 3 sizes, checks overflow) |
| MOD-52 | `min_size_px` declared | SHOULD | static |
| MOD-53 | Sizes in `em` / `--mm3-scale` | SHOULD | review |
| MOD-54 | Placeholder in preview for sensitive content | MUST | harness (`preview=true`) + review |
| MOD-60 | `UPPER_SNAKE` message ids | MUST | static |
| MOD-61 | No user-visible literal strings | MUST | static (Python AST and JS scan) |
| MOD-62 | EN, IT, FR, ES, DE complete, same placeholders | MUST | static |
| MOD-63 | Plurals via plural forms | MUST | static (heuristic) + review |
| MOD-64 | Locale-aware date and number formatting | MUST | review |
| MOD-65 | External data not translated | MUST | review |
| MOD-67 | HEADER catalog key for appearance.header: true | SHOULD | static |
| MOD-70 | All used capabilities declared | MUST | static + test kit |
| MOD-71 | Narrowest scopes, grants in README | MUST | static (README) + review |
| MOD-72 | Degrades cleanly without grants | MUST | test kit (run with no grants) |
| MOD-80 | Logging only via `ctx.log` / `this.log` | MUST | static |
| MOD-81 | Event-style log records | SHOULD | review |
| MOD-82 | Correct log levels | SHOULD | review |
| MOD-83 | No secrets or personal data at info+ | MUST | test kit (secret scan of captured logs) + review |
| MOD-84 | ≤ 1 info record per refresh | SHOULD | harness |
| MOD-86 | No broad `except Exception` that hides errors | MUST | static (warning) + review |
| MOD-93 | Lowest needed `api_version` | SHOULD | review |
| MOD-96 | Trusted Publishing with PEP 740 attestations | MUST (catalog) | index CI |
| MOD-97 | Public source, OSI license, README lists grants and notifications | MUST (catalog) | index CI + review |
| MOD-98 | Complete index entry, description in 5 languages | MUST (catalog) | index CI |
| MOD-99 | Each release passes mm3 module check | MUST (catalog) | index CI |
| MOD-23 | Outbound HTTP only via ctx.http; no import of the underlying HTTP library | MUST | static (import scan) |
| MOD-24 | Requests documented; None for requests not handled | MUST | static (README vs code) + test kit |
| MOD-25 | No requests from on\_request | MUST | test kit |
| MOD-26 | Request errors handled | MUST | test kit (no responder, failing responder) |
| MOD-27 | Frequent hide/show tolerated | MUST | test kit (rapid suspend/resume) |

## 12. Conformance tooling

Two tools ship with `mm3-core` so developers can prove compliance without a mirror: the `mm3 module check` command and the `mm3.testing` kit.

### 12.1 `mm3 module check`

```text
$ mm3 module check ./mm3-weather
Static      52 passed, 0 failed, 2 warnings
Test kit    14 passed, 0 failed
Harness      6 passed, 0 failed
Review       9 items to confirm (see report)
Result      COMPLIANT (API 1.0)
Report      ./mm3-weather/.mm3/check-report.json
```

- Builds the wheel in a temporary venv, installs it with the current `mm3-core`, then runs static checks, the test kit suite and the browser harness.
- Exit code 0 = compliant, 1 = a MUST failed, 2 = tool error; `--format json` and `--junit` for CI.
- `--only static` gives a fast check for editors and pre-commit hooks.
- MOD-95: A module published as compatible with MM3 MUST pass `mm3 module check` for the API version it declares; the admin catalog shows the check result if a report is shipped in the wheel.

### 12.2 `mm3.testing` kit

```python
from mm3.testing import ModuleHarness, fake_mqtt

async def test_core_publishes_forecast():
    async with ModuleHarness(WeatherModule, config={"api_key": "x", "lat": 42.1, "lon": 12.2},
                             grants={"http": ["https://api.openweathermap.org/"]}) as h:
        h.http.mock("GET", "https://api.openweathermap.org/...", json=SAMPLE)
        await h.advance(minutes=10)                 # drives the scheduler
        assert h.state["temp"] == 18.4
        assert h.notifications.last("WEATHER_UPDATED")
        await h.check_contract()                    # runs all test-kit MOD checks
```

- **ModuleHarness**: a real `ModuleContext` wired to fakes: in-memory bus, mocked `http`, `fake_mqtt`, temporary storage, virtual clock (`advance()`), locale switch, grant control.
- **Recorders**: `h.notifications`, `h.client_messages`, `h.logs`, `h.state_history` for assertions.
- **Contract checks**: `h.check_contract()` runs MOD-10–16, 42–43, 70, 72 and 83 against the instance.
- **Browser harness** (`mm3.testing.browser`, Playwright): loads the element in a stand-alone shell page, feeds states, resizes the cell, toggles preview and locale, records render time, CSP violations and overflow; supports screenshot comparisons per locale.
- **Scaffold**: `mm3 new-module <name>` generates a package that passes `mm3 module check` from the first run, including all five locale catalogs and a sample test.

### 12.3 Publishing to the module catalog

A module enters the public catalog (core spec 10.6) through a pull request to the index repository; from then on every PyPI release is checked automatically.

- MOD-96: Releases MUST be uploaded with PyPI Trusted Publishing from the module's public source repository, so they carry PEP 740 attestations; manual uploads with API tokens are rejected by the index.
- MOD-97: The source repository MUST be public and the license OSI-approved; the README MUST list required capabilities and grants (MOD-71) and notifications (MOD-41).
- MOD-98: The index entry `modules/<name>.yaml` MUST provide PyPI name, source repository, maintainer contact, license, category, and a `LocalizedText` description in all five languages; at least one screenshot SHOULD be provided.
- MOD-99: Each release MUST pass `mm3 module check` in the index CI for its declared `api_version`; failing releases stay on PyPI but are not listed.
- The **verified** badge is granted after a manual review of code, capabilities and data handling; it is reviewed again when a release adds capabilities.

```yaml
# modules/greeting.yaml
pypi: mm3-greeting
source: https://github.com/example/mm3-greeting
maintainer: { name: Example Dev, email: dev@example.org }
license: MIT
category: utilities
description:
  en: Shows a time-of-day greeting.
  it: Mostra un saluto in base all'ora del giorno.
  fr: Affiche une salutation selon l'heure.
  es: Muestra un saludo según la hora del día.
  de: Zeigt eine tageszeitabhängige Begrüßung.
screenshots: [docs/screenshot.png]
```

## 13. Minimal reference module

`mm3-greeting` is the smallest full module that passes `mm3 module check`: a backend computes the part of the day each minute and a front-end shows a localized greeting with an optional name.

**pyproject.toml**

```toml
[project]
name = "mm3-greeting"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["mm3-core>=1.0,<2"]
readme = "README.md"

[project.entry-points."mm3.modules"]
greeting = "mm3_greeting:GreetingModule"

[tool.hatch.build.targets.wheel]
packages = ["src/mm3_greeting"]
```

**src/mm3\_greeting/\_\_init\_\_.py**

```python
"""Greeting module: shows a time-of-day greeting."""
from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field
from mm3 import LocalizedText, MirrorModule, ModuleConfig


class GreetingConfig(ModuleConfig):
    """Configuration for :class:`GreetingModule`."""

    name: LocalizedText | None = Field(None, title="CFG_NAME", description="CFG_NAME_HELP")


class GreetingModule(MirrorModule):
    """Publishes the current part of the day to its front-end."""

    name: ClassVar[str] = "greeting"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = GreetingConfig
    element: ClassVar[str | None] = "mm3-greeting"
    scripts: ClassVar[list[str]] = ["greeting.js"]
    styles: ClassVar[list[str]] = ["greeting.css"]
    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)

    async def start(self) -> None:
        """Publish once, then every minute."""
        await self._publish()
        self.ctx.scheduler.every(60, self._publish)

    async def _publish(self) -> None:
        hour = datetime.now(self.ctx.timezone).hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        self.ctx.set_state({"part": part})
        self.ctx.log.debug("part_of_day", part=part)

    def snapshot(self) -> dict[str, str]:
        """Latest state for newly connected clients."""
        return dict(self.ctx.state)
```

**src/mm3\_greeting/static/greeting.js**

```js
import { MirrorElement, html } from "/core/mm3.js";

const KEYS = { morning: "GOOD_MORNING", afternoon: "GOOD_AFTERNOON", evening: "GOOD_EVENING" };

class GreetingElement extends MirrorElement {
  /** Render the greeting; empty state shows nothing until data arrives. */
  render(state) {
    if (!state.part) return html``;
    const name = this.config.name;            // already resolved to the active locale
    return html`<p class="greeting">${this.t(name ? `${KEYS[state.part]}_NAME` : KEYS[state.part], { name })}</p>`;
  }
}
customElements.define("mm3-greeting", GreetingElement);
```

**src/mm3\_greeting/locales/en/LC\_MESSAGES/messages.po** (excerpt; `it`, `fr`, `es`, `de` follow the same keys)

```text
msgid "GOOD_MORNING"
msgstr "Good morning"

msgid "GOOD_MORNING_NAME"
msgstr "Good morning, {name}"

# GOOD_AFTERNOON, GOOD_AFTERNOON_NAME, GOOD_EVENING, GOOD_EVENING_NAME likewise

msgid "CFG_NAME"
msgstr "Name"

msgid "CFG_NAME_HELP"
msgstr "Optional name shown after the greeting"
```

**README.md → Notifications**: emits none, consumes none. **Grants**: none.

**tests/test\_greeting.py**

```python
from mm3.testing import ModuleHarness
from mm3_greeting import GreetingModule

async def test_mod_contract_and_morning():
    async with ModuleHarness(GreetingModule, now="2026-10-27T08:00:00+01:00") as h:
        assert h.state == {"part": "morning"}
        await h.check_contract()
```

## 14. Document history

| Revision | Date | Changes |
| --- | --- | --- |
| 1.0 rev 6 | 2026-10-28 | Aligned with core spec 0.6: ctx.request, on\_request, this.request, RequestTimeoutError, RemoteError (MOD-24–26, §3.2, §4, §5.2); registered notification payloads (MOD-40 changed); ctx.page, ctx.pages, this.page, onPageChange; frequent hide/show (MOD-27); capability layout.control (§8) |
| 1.0 rev 5 | 2026-10-28 | Aligned with core spec 0.5: HEADER catalog key (MOD-67, §7); LocalizedText resolved by the engine for front-ends and with resolve() in the backend, timedelta formats, current ctx.locale, timezone and units after reload (§6.2, §5.2) |
| 1.0 rev 4 | 2026-10-27 | Aligned with core spec 0.3: timeout semantics (§3.2), handlers called in order (MOD-18), restart creates a new instance (MOD-19), async key-value store, CronExpression config type (§6.2) |
| 1.0 rev 3 | 2026-10-27 | `ctx.http` is an MM3 HttpClient (§4.3, MOD-23); only Pydantic and FastAPI's APIRouter exposed (MOD-94); scheduler semantics referenced to core spec §3.3; versioning and code references |
| 1.0 rev 2 | 2026-10-27 | Publishing to the module catalog (§12.3, MOD-96–99) |
| 1.0 rev 1 | 2026-10-27 | First draft |
