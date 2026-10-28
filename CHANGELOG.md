# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Module API changes are listed
separately under **Module API** in each release (Module Contract, section 10).

## [Unreleased]

### Added

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

### Removed

- APScheduler dependency: version 4 is still alpha and has an unfixed
  advisory (PYSEC-2026-282, CVE-2026-31072). Jobs will use the in-house
  scheduler `mm3.core.services.scheduler` (core spec 3.3, CORE-180..186).
