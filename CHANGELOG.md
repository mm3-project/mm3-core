# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Module API changes are listed
separately under **Module API** in each release (Module Contract, section 10).

## [Unreleased]

### Added

- Milestone 3 part B: the shell WebSocket. `/ws` on the mirror listener
  (roles `kiosk`, `browser`) and `/admin/preview/ws` on the admin listener
  (role `preview`, admin session and matching `Origin`) (CORE-273).
  Handshake `hello` → `welcome` + one `state` per instance, with the public
  config resolved for each element's locale, the `layout` object, pages and
  `engine_build`; a bad first frame or no `hello` within 10 s closes 4400
  (CORE-246, CORE-273, CORE-274). Roles: `kiosk` only from loopback with the
  current kiosk key, `preview` only on the admin endpoint, any other client
  `browser`, interactive with a display session; refused roles close 4403
  (CORE-247). Frames per CORE-271: `state` with `delta`, `client` both ways,
  `notify` both ways (once per browser with its matching instances),
  `request`/`reply`, `log`. Inbound limits and `error` frames with
  `CORE_WS_*` keys and the echoed `ref` (CORE-248, CORE-272); a frame over
  512 KiB closes 1009. Outbound queue of 1000 frames per client, coalesced
  `state` frames, 1013 when full (CORE-249). Ping every 15 s, drop after
  30 s (CORE-251). Requests from elements reply with `{rid, payload}` or an
  error, `timeout` after the request's timeout (CORE-223, CORE-272). Preview
  clients get no `client` frames (CORE-99). A valid display session cookie
  is renewed on every connection (CORE-254). Backends now reach their
  elements: `mm3 run` connects the engine to the hub.
- Milestone 3 part A: web gateway with the mirror and admin listeners
  (`mm3 run` starts both, `127.0.0.1:8080` and `:8081` by default, CORE-60).
  Both accept loopback plus `server.allow` / `admin.allow`; other sources
  get 403 (WebSockets are closed) and are logged at most once a minute per
  address; `server.allow` applies live (CORE-61). Token store
  `<state>/tokens.json` with named argon2-hashed admin and display tokens;
  `mm3 token new <name> [--display]`, `mm3 token list`, `mm3 token revoke`;
  `/api/*` and `/admin` need an admin token (Bearer) or session (CORE-62).
  Security headers and `Cache-Control: no-store` on every response, no inline
  script or style (CORE-63). Admin sessions via `/admin/login` (HttpOnly,
  SameSite=Strict cookie, 30 days from last use, matching `Origin` for unsafe
  requests, five failures lock an address for 15 minutes) and `/admin/logout`
  (CORE-253). Display pairing: `mm3 token new --display` prints
  `http://<host>:<port>/pair#<token>`; `/pair` sets a display session cookie
  valid until the token is revoked (CORE-254).

### Changed

- Spec sync: Core spec 0.13. New: WebSocket frame contents and `ref`
  (CORE-271), error and reply codes (CORE-272), `/ws` and
  `/admin/preview/ws` endpoints, 10 s hello timeout and `engine_build`
  (CORE-273), the `layout` object (CORE-274); all assigned to milestone 3.
- Milestone 2 rework for core spec 0.11 (issue #19). custom.css next to the
  main file is part of the config set: stored in the history (CORE-153),
  compared as the section `custom_css` (CORE-216), restorable alone with
  `mm3 config restore --section custom_css` (CORE-155) and watched for hot
  reload even before it exists (CORE-208). It is validated with the limits
  of CORE-214 up to 64 KiB; a breach is `CORE_CFG_CSS` at `/custom_css`
  (CORE-243, CORE-262). Widget templates are now listed as changed sections
  too. `engine.server.allow` added and applied live as `server.allow`;
  `engine.admin.token` removed: a config that sets it fails with
  `CORE_CFG_UNKNOWN_KEY` (CORE-201, CORE-62).
- Spec sync: Core spec 0.12. CORE-143 changed: `mm3 kiosk session` checks the
  version printed by `cage -v`, comparing only the leading
  `MAJOR.MINOR.PATCH` (Debian 13 ships `0.1.5+20240127`), not the Debian
  release, and uses no cage option or behaviour added after 0.1.5.
- Spec sync: Core spec 0.11 and Module Contract 1.0 rev 7 (milestone 3
  requirements settled). New: WebSocket handshake, roles, limits, reconnect,
  liveness and URLs (CORE-246..252); admin sessions and display pairing
  (CORE-253, CORE-254); element setup, header, stacks, fit and transitions
  (CORE-255..259); swipe (CORE-260); theme variables and `custom.css`
  (CORE-261, CORE-262); catalogs at run time (CORE-263..267); kiosk agent,
  supervision and Chromium flags (CORE-268..270); MOD-37 (templating only
  through `/core/mm3.js`). Changed: CORE-61..63 (allow lists on both
  listeners, token store, security headers), CORE-143 (kiosk system unit,
  Debian 13+), CORE-201 (`server.allow`, no `admin.token`), CORE-153,
  CORE-155, CORE-208, CORE-216, CORE-243 (`custom.css` as section
  `custom_css`), MOD-33 (theme variables). lit-html 3 and Roboto Condensed
  approved (core spec 1.2). CORE-117 moved to milestone 3; MOD-37 assigned to
  milestone 4. Milestone 2 code citing the changed requirements is listed in
  the coverage matrix for review.

### Added

- Configuration history (milestone 2, part D; Core spec 0.10): every valid
  configuration is a numbered version in `<state>/config-history/` with the
  whole config set (main file, includes, widget templates; never the env
  file) and `meta.json` (CORE-153); undo and redo of saves with the parents
  of CORE-154, section restore into the file that holds the section
  (CORE-155), full validation before anything is written (CORE-156),
  sections including `pages` (CORE-216), one flock and atomic writes, no new
  version when the set's hash is unchanged (CORE-217), retention and masked
  secrets in diffs (CORE-218). `mm3 config history|diff|undo|redo|restore`
  with exit codes 0-3 and `--force` (CORE-219). `mm3 run` records versions
  through the history. Milestone 2 is done (53 of 53 requirements).
- Screen layout, display and pages (milestone 2, part C; Core spec 0.9):
  `mm3.core.config.layout` resolves presets to grid cells (CORE-90, CORE-92),
  checks rectangles against the grid (CORE-91, CORE-94), overlaps per `z`
  with MM²-style stacks on a preset (CORE-93, CORE-212), cell size with the
  16px / 32px defaults (CORE-213), `min_size_px` (CORE-96), appearance CSS
  limits and headers (CORE-214, MOD-67), positions required or forbidden
  (CORE-33), the logical viewport from the `display` block, assumed until a
  kiosk reports one (CORE-140..142, CORE-215), and the `pages` block with
  per-page validation that names the page (CORE-230..232). `mm3
  check-config` states the assumed viewport.
- Hot reload (milestone 2, part B; Core spec 0.9): the main file, included
  files, the env file and `widget-templates/` are watched; a reload starts
  500 ms after the last change or on SIGHUP, one at a time with exactly one
  more for changes during a reload (CORE-208). An invalid configuration is
  never applied: the records are logged, `CORE_CONFIG_ERROR` is emitted and
  `disk_valid` turns false (CORE-209). A valid one gets a version and is
  applied per instance in the order of CORE-210 (stop, reconfigure or
  restart, start, re-layout), then `CORE_CONFIG_CHANGED` with the changed
  sections (CORE-216). `ctx.locale`, `ctx.timezone` and `ctx.units` follow
  the reload, with `CORE_LOCALE_CHANGED` broadcast or sent to one instance
  (CORE-211, CORE-245); `reconfigure()` returning True keeps the instance
  running (MOD-50).
- The registry includes `CORE_CONFIG_CHANGED`, `CORE_CONFIG_ERROR` and
  `CORE_LOCALE_CHANGED`; its test now reads the spec table.
- Configuration loading and validation (milestone 2, part A; Core spec 0.8):
  `mm3.core.config` reads `/etc/mm3/config.yaml` (`--config`) with `!include`
  confined to the config directory, 8 levels deep, no cycles (CORE-31), and
  `!env` from the env file `/etc/mm3/mm3.env` (`--env-file`) or the process
  environment, with defaults (CORE-200). It validates the engine model with
  every key of CORE-201, the instance keys and ids (CORE-202), missing,
  unavailable and disabled modules (CORE-203, CORE-204), durations
  (CORE-205), the `backup` block (CORE-244), and each module's
  `config_model` (CORE-32, MOD-45, MOD-49). Every problem becomes a record
  with file, line, column, JSON Pointer and a `CORE_CFG_*` code (CORE-206,
  CORE-243).
- `mm3 check-config [--config] [--env-file] [--strict] [--json]` (CORE-207),
  and `mm3 run`, which refuses to start with exit code 2 on an invalid
  configuration (CORE-30).
- `LocalizedText` and `LocaleTag` in `mm3.config`, resolved through the
  locale chain and in the public config sent to browsers (CORE-101, MOD-48).
- Static checks of a `config_model` for `mm3 module check` (MOD-45 to MOD-47).
- Repository scaffold: packaging (hatchling), MIT license, ruff, mypy, pytest,
  pre-commit, GitHub Actions CI.
- Package skeleton `mm3` with `mm3.core`, `mm3.testing`, `mm3.cli`.
- `mm3 --version` command.
- Dependency management (core spec 10.9): bounded version ranges, `uv.lock`,
  Renovate configuration with automerge for tested patch/minor updates,
  pip-audit job, weekly canary workflow, deprecation warnings as errors,
  constraints file attached to releases.
- Spec references in code (CORE-74): every source file cites the requirement
  ids, spec version and section it implements; `docs/spec/versions.toml`
  records the implemented versions (Core spec 0.2, Module Contract 1.0 rev 3)
  and `tools/check_spec_refs.py` enforces it in CI.
- Python 3.14 in the CI test matrix; the canary tests Python 3.15.
- Renovate: workflow Python versions are never bumped automatically; CI
  tooling majors get their own label and are merged manually.
- Milestone 1, bus, lifecycle and scheduler (Core spec 0.4, Module Contract
  1.0 rev 4):
  - `Notification` envelope with trace id, `ClientMessage`, payload checks
    (CORE-20, CORE-26, CORE-111).
  - Event bus with per-instance bounded inboxes (drop oldest, rate-limited
    warning, drop counter), subscriptions, targeted delivery, sender
    exclusion and endpoints for browsers and bridges (CORE-21..29).
  - Module registry from `mm3.modules` entry points with API version checks
    (CORE-03, MOD-90).
  - Supervisor: eight-state lifecycle, one task group per instance, budgets
    from `engine.timeouts`, restarts with 1/2/4/8 s backoff and DISABLED on
    the fifth failure in 10 minutes, manual restart, live or restarting
    reconfigure, suspend/resume, `CORE_MODULE_FAILED` (CORE-04, CORE-190..197).
  - `MirrorModule`, `ModuleConfig`, `CronExpression` and the first
    `ModuleContext` members: messaging, state, tasks, `run_blocking`,
    scheduler, storage, logger (Contract §3–4, MOD-10..22).
  - In-house scheduler: interval, cron and one-shot jobs on CLOCK_BOOTTIME,
    DST rules, re-planning after clock jumps and time zone changes, no
    overlap, pause on suspend (CORE-180..189).
  - Per-instance storage directory and JSON key-value store (CORE-198).
  - Structured JSON logging to stdout with trace ids and a 5 000-record ring
    buffer (CORE-110, CORE-111).
  - `mm3.testing.VirtualClock` for tests that do not wait.
  - `Engine` wiring startup and shutdown with `CORE_READY` / `CORE_SHUTDOWN`.
- Coverage gate of 85 % enforced (CORE-72).
- Milestone 1 spec changes, Core spec 0.7 and Module Contract 1.0 rev 6
  (issue #8):
  - Envelope fields `expects_reply` and `reply_to` (CORE-20).
  - `ctx.request()` and `MirrorModule.on_request()`: targeted or broadcast
    requests, first reply wins, replies bypass the inbox and `subscribes`,
    timeouts of 0.1–60 s; error replies with `code` `transient`,
    `configuration` or `internal`; `RequestTimeoutError` and `RemoteError`
    in `mm3.errors` (CORE-220..222).
  - `mm3.notifications`: the shared notification registry with payload
    models and JSON Schemas; payloads of registered names are validated
    when modules or the engine send them (CORE-224..226).
  - `public_config()` removes secret fields before a configuration leaves
    the engine (CORE-13).
  - Health record completed with the time of the last message out
    (CORE-113); `ctx.deprecated()` logs `deprecated_api` once per instance
    (MOD-91).
  - Tests for every milestone 1 requirement (CORE-01, 05, 08, 10, 24, 195,
    197, MOD-18..20, 90, 94).

#### Module API

- Added in 1.0: `ctx.request()`, `MirrorModule.on_request()`,
  `RequestTimeoutError`, `RemoteError`, `mm3.notifications`.

### Changed

- Spec references moved to Core spec 0.4 and Module Contract 1.0 rev 4
  (`docs/spec/versions.toml`).

- Requirements traceability (CORE-75..79): Markdown exports of both specs in
  `docs/spec/`, `tools/spec_sync.py` generating `requirements.toml` with
  `changed_in` per requirement, citations checked per requirement with stale
  citations failing only in changed files, `docs/spec/milestones.toml`,
  coverage matrix (`tools/trace_report.py`) and a Traceability workflow that
  keeps one `spec-change` issue per closed milestone.

### Changed

- `docs/spec/versions.toml` is generated and records the current spec
  versions (Core spec 0.6, Module Contract 1.0 rev 6); code may cite any
  version at or after a requirement's last change.
- Spec sync to Core spec 0.7: error replies carry `code` `transient`,
  `configuration` or `internal` (`REQUEST_FAILED` for bugs), and an
  `on_request` over its budget sends no reply (CORE-221).
- Spec sync to Core spec 0.8: the list of `CORE_CFG_*` validation codes with
  severity, Pydantic mapping and params (CORE-243), and the `backup` block
  keys, defaults and checks (CORE-244); both assigned to milestone 2, which
  is now in progress.
- Spec sync to Core spec 0.9: `CORE_CONFIG_ERROR` is a reserved name
  (CORE-22); `CORE_CONFIG_CHANGED`, `CORE_CONFIG_ERROR` and
  `CORE_LOCALE_CHANGED` are registered engine events, with the delivery of
  `CORE_LOCALE_CHANGED` (CORE-245); `layout.gap` and `layout.padding`
  default to 16px and 32px (CORE-213). Milestone 1 stays done at core 0.9
  after reviewing CORE-22 (no code change).
- Spec sync to Core spec 0.10: `pages` is a configuration history section,
  shown in changed sections and restorable alone (CORE-216, CORE-155).

### Removed

- APScheduler dependency: version 4 is still alpha and has an unfixed
  advisory (PYSEC-2026-282, CVE-2026-31072). Jobs will use the in-house
  scheduler `mm3.core.services.scheduler` (core spec 3.3, CORE-180..186).
