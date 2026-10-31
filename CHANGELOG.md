# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Module API changes are listed
separately under **Module API** in each release (Module Contract, section 10).

## [Unreleased]

### Fixed

- CORE-268 (core spec 0.20): when the group `mm3-kiosk` is missing or the
  change of group on `kiosk.key` fails, the engine now logs the warning
  `kiosk_key_group` naming the group instead of staying silent, and keeps
  running. Citations in the kiosk files reviewed against 0.20 (spec-change
  issue of milestone 3).

### Added

- Packaging after the first manual install, part 1 (core spec 0.20):
  - Service notification (CORE-290): the engine tells systemd its state with
    `sd_notify` datagrams to `$NOTIFY_SOCKET`, on the standard library only and
    a no-op without it: `READY=1` with a `STATUS=` summary once the listeners
    are up, `RELOADING=1` with `MONOTONIC_USEC=` and then `READY=1` around every
    reload (applied or rejected), `STOPPING=1` when shutdown begins. A start
    that aborts never sends `READY=1`.
  - Loop watchdog (CORE-291): with `$WATCHDOG_USEC` set, a task on the engine
    loop sends `WATCHDOG=1` every half interval on the engine clock; a module
    that blocks the loop stops the ping.
  - `mm3.service` (CORE-292) ships in the package (`mm3/core/systemd/`) and
    `mm3 service unit` prints it: `Type=notify`, `WatchdogSec=30s`, the CORE-65
    hardening, state and runtime directories.

- Packaging after the first manual install, part 2 (core spec 0.20):
  `mm3-modules-default` (CORE-294), a distribution without code in
  `modules/mm3-modules-default/` that depends on the published default modules
  `mm3-clock` and `mm3-weather`, each bounded to its current major, and on no
  version of `mm3-core`. CI and the release workflow now build `mm3-clock`,
  `mm3-weather` and `mm3-modules-default` with the engine, and the release
  publishes them with attestations (unchanged module versions are skipped).

- Packaging and deployment, part 3: `install/mm3-install.sh` installs or updates MM3 on Debian 12 and 13 (CORE-293, CORE-268, CORE-143): users and groups, a timestamped venv with a rollback symlink, config and env created only when absent, configuration validated before the switch, units from the package. `install/ci-check.sh` and the CI job `install` run it on clean Debian 12 and 13 images.

- Spec sync to core spec 0.20: sd_notify and loop watchdog (CORE-290, CORE-291), `mm3.service` unit (CORE-292), `mm3-install.sh` (CORE-293) and `mm3-modules-default` (CORE-294); CORE-268 now requires `mm3` to be a member of group `mm3-kiosk`. Milestones 6 and 9 updated.

- Spec sync for core spec 0.19 and Module Contract 1.0 rev 12 (milestone 5
  settled): HTTP transport rules behind `ctx.http` (CORE-282..287),
  `engine.location` / `ctx.location` (CORE-288; CORE-201, CORE-211 and
  CORE-246 changed), `WEATHER_CURRENT` in the notification registry, and the
  reference modules greeting, clock and weather (CORE-289, CLK-01..08,
  WTH-01..12). `tools/` learn the `CLK` and `WTH` id prefixes; milestone 5
  now lists its requirements in `milestones.toml`.
- Milestone 5 part A: the engine's HTTP transport and `engine.location`.
  `ctx.http` now sends through one pooled `httpx.AsyncClient` created at
  engine start and closed at shutdown (CORE-282): HTTP/1.1 only, no cookies,
  cache or retries, proxy from `HTTP(S)_PROXY`/`NO_PROXY`, at most 4 requests
  per instance in flight, bodies above 10 MiB refused while read, 10 s default
  and 60 s maximum timeout. Requests carry
  `User-Agent: mm3/<core> <module>/<version>` unless the module sets one, and
  `Host`, `Content-Length`, `Connection` and `Transfer-Encoding` are dropped
  (CORE-283). TLS 1.2+ with verification always on; a grant entry
  `{prefix, ca}` replaces the CA store for its prefix, and a missing file is a
  `CORE_CFG_FILE_UNREADABLE` error at the grant (CORE-284). Loopback,
  link-local, unspecified and multicast addresses are refused with
  `PermissionError`, checked on the address actually connected to and lifted
  only by a grant naming `localhost` or the address literally (CORE-285).
  At most 5 redirects, only to granted URLs, never `https` to `http`, with
  credentials dropped across origins (CORE-286). Errors map to `mm3.errors`
  without library text; `http_request` is logged without query, headers or
  body, and `ctx.health` shows `http_requests`, `http_failures` and
  `http_last_success` (CORE-287). `engine.location` (`lat`, `lon`, optional
  localized `name`), `ctx.location` and `mm3.context.Location` (CORE-288);
  a change restarts instances with a backend (CORE-211), and
  `ModuleHarness(location=...)` sets it in tests. `httpcore` is now a direct
  dependency. The shell side (`welcome.location`, `this.location`) follows
  with part B.
- Milestone 5 part B: the shell side of `engine.location` and the reference
  module layout. `welcome.location` carries `{lat, lon, name}` (the name as
  written) and a change of `engine.location` resyncs the shells (CORE-246,
  CORE-275, CORE-288); `this.location` gives an element the same shape with
  the name resolved along its locale chain, and
  `BrowserHarness(location=...)` sets it in tests. `modules/` holds the
  reference modules (CORE-289): `mm3-greeting`, the code of Module Contract
  section 13 with catalogs in five languages, its own tests and a screenshot;
  CI has a job per module that runs `mm3 module check` and the module's tests.
  `tools/trace_report.py` and `tools/check_spec_refs.py` read `modules/*/src`
  and `modules/*/tests`. Tests name MOD-23 (no outbound HTTP library import).
- Milestone 5 part E: the `mm3-weather` element (WTH-06, WTH-10 and the browser
  part of WTH-12), which closes milestone 5. Icons are Material Design Icons
  (Apache-2.0) vendored as SVG with their licence in `static/icons/`, one per
  condition and a night variant for `clear` and `partly_cloudy`; `icons.js`
  holds the same path data and a test keeps them in step. The three views
  (`current`, `daily`, `hourly`) size their text to the cell and drop rows,
  hours or optional fields instead of overflowing; stale data is dimmed with
  its age (WTH-08) and a missing `http` grant shows a message naming the grant
  (WTH-09). Browser tests cover the three views in five languages and the kit's
  own checks. `mm3-weather` joins the `modules` CI matrix, the coverage report
  is regenerated and milestone 5 is marked done at core 0.19 / contract rev 12.
  `check_spec_refs.py` and `trace_report.py` now also read the `// Implements`
  comments of the reference modules' front-end files (CORE-74, CORE-77, CORE-78).
- Milestone 5 part D: the backend of `mm3-weather` (WTH-01..05, 07..09, 11
  and the backend part of 12). One request per refresh covers current, daily
  and hourly data; the built-in Open-Meteo provider maps WMO codes to the
  closed condition set (unmapped codes become `cloudy`, logged at debug) and
  providers plug in through the entry point group `mm3_weather.providers`.
  The instance publishes only the data of its `type`, stores the last good
  data under `last` (dropped after 24 h, shown as stale after a restart),
  turns network errors, 5xx and 429 into `TransientError`, other 4xx into a
  `ConfigurationError` naming `location` or `provider`, and shows a grant
  message instead of crashing without the `http` grant. `WEATHER_CURRENT` is
  registered in `mm3.notifications` (CORE-224, CORE-225) and sent with UTC
  times (MOD-42). The test kit's MOD-06 check no longer counts temporary
  directory cleanup or the test's own harness as writes outside storage, so
  modules that use `ctx.storage.kv_set` pass `check_contract()`. The element
  follows in part E.
- Milestone 5 part C: `mm3-clock` (CLK-01..08), a front-end-only module in
  `modules/mm3-clock`. Digital and analog display (SVG face with ticks,
  numbers or none), 12/24-hour or locale format, date styles, ISO 8601 week
  number, a time zone per clock and up to four extra zones with a "+1 d"
  marker, always in the mirror's time zone through `fmtDate` (CLK-01..04).
  Updates are scheduled at the next second or minute boundary with
  `this.setTimeout`, so nothing runs between minute boundaries when seconds
  are off (CLK-05). Sunrise, sunset, day length, moon phase, illumination,
  moonrise and moonset are computed in the browser by `astro.js` (NOAA solar
  equations, Meeus low-precision lunar series, no library), with localized
  polar day and night text and a hint when no location is set (CLK-06). The
  tests run `astro.js` in Node against a 28-row table (7 places, 4 dates,
  one above 60 degrees, one in the southern hemisphere) generated with PyEphem:
  sun within 2 minutes, moon within 10, illumination within 1 point (CLK-07).
  Catalogs in five languages with the eight moon phase names (CLK-08). CI
  checks it in the `modules` job.
- Milestone 4 part D: the browser harness and `mm3 new-module` (closes
  milestone 4). `mm3.testing.BrowserHarness` loads a module's element into a
  stand-alone shell page in Chromium (Playwright; `MM3_CHROMIUM` selects the
  executable), served without a socket, under the engine's CSP and with the
  real `mm3.js`; it renders states, resizes the cell, toggles preview and
  locale, takes screenshots, and records CSP violations, network attempts,
  errors and overflow. `run_browser_checks` runs MOD-32 (CSP), 34, 36, 51, 54
  and 84. `mm3 module check` gains the `harness` stage (`--only harness`;
  default is static, kit, harness; exit codes unchanged, a browser that cannot
  start is exit 2). `mm3 new-module <name> --kind full|backend|frontend`
  (CORE-281, MOD-95) writes a src-layout package with the `mm3-build` build
  requirement, all five locale catalogs, a README with the Notifications and
  Grants sections, a sample test and, for front-end kinds, an element that
  passes the check; the front-end-only kind has no hooks. Milestone 4 is done
  in `docs/spec/milestones.toml` (core 0.18, contract rev 11).
- Milestone 4 part C: `mm3 module check <path>` (CORE-281, Module Contract 12.1).
  It builds the module's wheel with `uv build`, installs it in a temporary venv
  with the running `mm3-core[testing]`, and runs the static checks and the test
  kit; `--only static|kit` limits the run, `--format json` and `--junit <file>`
  write machine-readable results, and `.mm3/check-report.json` is always
  written. Exit code 0 means compliant, 1 a MUST failed (a wheel that does not
  build fails MOD-05), 2 the tool could not run (no `uv`, missing testing extra,
  engine not installable). Static checks read the source and the built wheel
  without importing the module: Python by AST and the JavaScript under
  `static/` by an in-house tokenizer for MOD-01–05, 07, 08, 11, 12, 17, 23, 24,
  28–33, 35, 37, 40, 41, 44–47, 52, 60–63, 67, 70, 71, 80 and 86; the 17 kit rows
  run in the venv; the review list names the rows a person confirms.
  `# mm3: allow MOD-xx <reason>` (`//` in JavaScript) silences a SHOULD
  finding and is listed in the report, with no effect on a MUST. The kit
  builds its configuration from the module's `config_model` and opens the
  grants its capabilities need; `[tool.mm3.check] config = {...}, grants = {...}`
  in `pyproject.toml` overrides both. `packaging` is now a dependency.
- Milestone 4 part B: the `mm3.testing` kit (Contract 12.2, core spec 0.18,
  Module Contract 1.0 rev 9). `ModuleHarness` runs one instance on a real
  engine with fakes: `h.http` (mocks, unmocked calls reported) and `h.mqtt`
  (records publishes, `inject()` delivers through the instance inbox), with
  grants checked as on a mirror. It also records notifications, client
  messages, state history and logs, and provides the inputs `notify`,
  `client_message`, `request`, `respond`, `set_locale`, `set_grants`,
  `set_page`, `suspend`/`resume`, `reconfigure` and `restart`. Time is
  virtual on the test's own event loop: `asyncio.sleep`, timeouts, the
  scheduler, budgets and `ctx.now()` move only with `h.advance()`, while
  thread-pool work runs for real (CORE-278). `await h.check_contract()` runs
  every test-kit row of Contract section 11 (MOD-06, 10, 11, 13–16, 24–27, 29,
  42, 43, 70, 72, 83) on fresh instances whose secrets are canaries, and
  raises `ContractError` with the failed ids.
- Engine side of the kit: `ctx.http` (`mm3.http`, HTTP errors in
  `mm3.errors`) and `ctx.mqtt` (`mm3.mqtt`) check the declared capabilities and
  the instance grants, which now reach the engine from the `grants` block. MQTT
  messages are handled through the instance inbox under the handler budget
  (CORE-100). The real transports arrive in milestones 5 and 7. `ctx.health.set()`
  adds fields to the health record (MOD-85).
- Milestone 4 part A: engine prerequisites of the test kit. One engine clock:
  the scheduler, the hook, handler and job budgets, request timeouts and the
  new `ctx.now()` (aware, in `ctx.timezone`) all read it, so a virtual clock
  moves them together (CORE-278, MOD-28). `MirrorModule.emits`, `consumes`
  and `answers` declare notifications and requests with their Pydantic
  models, checked by `mm3.notifications.declaration_problems()` (names,
  model types, registered names use the registry model, no engine-only
  names emitted or answered; MOD-29, MOD-24). New distribution `mm3-build`
  in this repository (a uv workspace member, versioned with the Module API):
  its hatchling build hook `mm3-i18n` compiles every catalog into the wheel
  and fails the build on a catalog error; `mm3-core` builds with it and
  `mm3 i18n compile`/`check` use its compiler (CORE-280). New extra
  `mm3-core[testing]` (pytest, pytest-asyncio, Playwright); `import
  mm3.testing` without it raises `MissingExtraError` naming the install
  command (CORE-279).

- Milestone 3 part E: the kiosk. `mm3 kiosk session` (run by
  `mm3-kiosk.service` inside cage) refuses to start unless `cage -v` reports
  0.1.5 or later, reads the agent key the engine writes to
  `/run/mm3/kiosk.key` (mode 0640, group `mm3-kiosk`) and connects to
  `/ws/kiosk-agent` (loopback, `Authorization: Bearer`) (CORE-143, CORE-268).
  It applies the `display` block with `wlr-randr`, picking the closest
  supported mode, and reports outputs, modes, the applied mode, viewport and
  DPR, also after hot-plugs (CORE-144, CORE-145, CORE-277). It starts
  Chromium once the engine answers, with the CORE-270 flags and a one-time
  kiosk key in `/#kiosk=`, restarts it with a 1-30 s backoff and gives up
  after 5 restarts in 10 minutes (CORE-269). The engine sends the display
  block on connect and on every change, a new kiosk key before each launch
  and `restart` when no kiosk shell has connected for 60 s; it keeps the
  kiosk status (mismatch, Chromium state, restarts, failed). An applied mode
  re-validates the running configuration, resyncs the shells and emits
  `CORE_DISPLAY_CHANGED` (CORE-146). The shell claims the `kiosk` role with
  the key and falls back to a view-only browser when the key is stale.
  `mm3 kiosk session --print-command`, `mm3 kiosk unit`. CI installs
  Chromium for the browser tests, including a smoke test that renders a
  module in five languages and follows the grid on resize (CORE-06).
  Milestone 3 is done.
- New dependency `websockets` (kiosk agent client only, core spec 1.2).
- Milestone 3 part D: internationalization. The engine catalog
  (`mm3/core/locales`, EN, IT, FR, ES, DE) holds every `CORE_*` text: the
  validation messages (now read from it), the engine pages, the WebSocket
  error keys, `REQUEST_TIMEOUT` and the shell's "unavailable" placeholder
  (CORE-11, CORE-40, CORE-117, CORE-263). `mm3 i18n compile` writes
  `messages.mo` and `messages.json` next to every `.po` (plural indices
  converted to CLDR categories); `mm3 i18n check`, now a CI step, fails on
  missing or out-of-date compiled files, missing locales, missing keys and
  different placeholders (CORE-41, CORE-42, CORE-263). The i18n service
  looks keys up along the instance's locale chain in the module catalog,
  locale packages (`mm3.locales`) and the core catalog (CORE-07, CORE-264);
  `/i18n/<instance>.json?v=` serves the merged catalog, whose version
  `welcome` carries with each instance's `locale` and `touch` flag
  (CORE-246). `ctx.t()` with plurals and placeholders, `ctx.format_date()`
  and `ctx.format_number()` (Babel), and in the browser `this.t()`,
  `fmtDate()` (engine time zone) and `fmtNumber()` (CORE-265, CORE-266).
  `HEADER` comes from the module catalog (MOD-67). A locale change sends
  `control locale` and the shell fetches the new catalog and calls
  `onLocaleChange` (CORE-267). Swipes that start on an element with
  `input.touch` stay with it (CORE-235).
- Milestone 3 part C: the shell. The mirror serves the shell at `/` and the
  admin listener the same shell in preview mode at `/admin/preview` (admin
  session required), with no inline script or style (CORE-63, CORE-252).
  Files: `/core/…` (shell, theme, `mm3.js`), `/core/vendor/…` (lit-html
  3.3.2 and Roboto Condensed 5.3.0, vendored with licences and SHA-256 in
  `vendor.json`), `/modules/<module>/<version>/…` (immutable),
  `/appearance/<instance>.css?v=` and `/custom.css?v=` (immutable when the
  version matches) (CORE-252, CORE-262). `/core/mm3.js` exports `html`,
  `svg`, `nothing` and `MirrorElement` (Contract section 5). The shell
  places instances on the CSS grid of the `layout` object, splits preset
  stacks among visible instances, draws headers outside the shadow root,
  adopts module styles, appearance variables and `appearance.css` as
  constructable stylesheets, reports sizes once per frame, applies
  `overflow: fit|scroll`, fades hide/show and page changes, shows the
  failure placeholder, reconnects with 0.5–10 s back-off and jitter and
  reloads on a new `engine_build` (CORE-117, CORE-250, CORE-255..259).
  Theme variables on `:root` (CORE-261). Swipes (25 % of the width, twice
  the vertical travel, within 1 s) and taps are sent as `touch` (CORE-260,
  CORE-276).
- Pages and visibility: a page service keeps the current page, reacts to
  `PAGE_NEXT`, `PAGE_PREV`, `PAGE_SET`, rotation (per page or global,
  paused by interaction for `pause_on_interaction`), touch and
  `CORE_RESUME` (`reset_on_resume`), emits `CORE_PAGE_CHANGED`, and keeps
  the hide reasons `page`, `manual`, `self` and `module:<id>`; hidden
  instances are suspended and resumed when visible again (CORE-233..239,
  CORE-241). Control frames `page`, `show`, `hide`, `status`, `styles`,
  `reload` (`resync` after a hot reload that changes what shells show)
  (CORE-275); `visibility` and `touch` frames from interactive clients
  (CORE-276). `welcome` now carries `timezone`, `touch`, `custom_css` and per
  instance `scripts`, `styles`, `appearance` and `status`; the `layout`
  object carries `grid` (CORE-246, CORE-274).
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

#### Module API

- Implemented from Contract 1.0: `ctx.page`, `ctx.pages`, `ctx.layout`
  (with `layout.control`), and the front-end `MirrorElement` with
  `/core/mm3.js` (`html`, `svg`, `nothing`). `t()`, `fmtDate()` and
  `fmtNumber()` use the element's locale; catalogs arrive with the i18n part.

### Changed

- Fixed the intermittent `mm3 module check` MOD-62 failure ("does not match its
  .po"): a `.po` without a `POT-Creation-Date` header made Babel stamp the
  current minute into the compiled `.mo`, so a build and its check a minute
  apart differed. `mm3_build.catalogs` now compiles such files with a fixed
  date (CORE-263, CORE-265).
- Hot reload calls the layout listeners for every applied change, so shells
  resync when instances are added, removed or reconfigured (CORE-275).
- The `Permissions-Policy` header no longer lists `bluetooth`, which
  Chromium does not recognise (CORE-63).
- `tools/check_spec_refs.py` and `tools/trace_report.py` also read the
  `// Implements` and `/* Implements` comments of the engine's front-end
  files (`static/*.js`, `static/*.css`); vendored libraries are skipped
  (CORE-74, CORE-77, CORE-78).
- Browser tests (marker `browser`) run the shell in headless Chromium and
  are skipped where Chromium cannot start; `MM3_CHROMIUM` selects the
  executable.

- Spec sync: Core spec 0.17 and Module Contract 1.0 rev 8 (milestone 4
  requirements). New: one engine clock for the scheduler, budgets and
  `ctx.now()`, virtual in `mm3.testing` (CORE-278); the `mm3-core[testing]`
  extra (CORE-279); the separate `mm3-build` distribution with the
  `mm3-i18n` hatchling hook (CORE-280); `mm3 module check` and
  `mm3 new-module --kind` (CORE-281); wall-clock time only through
  `ctx.now()` (MOD-28); `emits`, `consumes` and `answers` declarations with
  Pydantic payload models (MOD-29). Changed: MOD-24 and MOD-41 (README lists
  the declared names). Module Contract §12 now specifies the harness API,
  static checks, suppressions, harness sizes and the report format.
  Milestone 4 requirements extended in `milestones.toml`.

  **Module API:** `ctx.now()` and the class attributes `emits`, `consumes`
  and `answers` are added to API 1.0 before its first release.
- Spec sync: Core spec 0.16 (kiosk details). Changed: CORE-268 (agent key as
  `Authorization: Bearer`, 4403 otherwise), CORE-277 (`outputs[].current`,
  the agent picks the mode), CORE-146 (the applied mode re-validates the
  running configuration, resync, `CORE_DISPLAY_CHANGED`), CORE-143
  (`wlr-randr --json`, unit file shipped with `mm3 kiosk unit`); `websockets`
  approved for the kiosk agent (1.2).
- Spec sync: Core spec 0.15 (i18n details). Changed: CORE-264 (merged
  catalog at `/i18n/<instance>.json` and its version; locale packages per
  module or `mm3`), CORE-263 (`mm3 i18n check` fails on compiled files that
  do not match their `.po`; backends read `messages.json`), CORE-266 (date
  styles, backend formatting), CORE-246 (per-instance `locale` and `touch`), CORE-235
  (element-owned swipes).
- Spec sync: Core spec 0.14 (shell and kiosk details). New: control
  action contents and `reload` `resync` on hot reload (CORE-275), shell
  input frames `visibility` and `touch` (CORE-276), kiosk agent protocol
  (CORE-277). Changed: `welcome` instance entries carry `scripts`, `styles`,
  `appearance` and `status`, and `welcome.custom_css` (CORE-246);
  `/appearance/<instance>.css` (CORE-252); `visibility`, `touch` and the
  `status` action (CORE-271); outputs reported by the kiosk agent
  (CORE-144); `welcome.timezone` and `welcome.touch` (CORE-246) and the grid size, gap and
  padding in the `layout` object (CORE-274). All in milestone 3. Shell font
  licence corrected to SIL OFL 1.1 (core spec 1.2).
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
