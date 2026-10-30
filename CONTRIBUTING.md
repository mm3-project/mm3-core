# Contributing to MM3

## Specs first

The specifications in [`docs/spec/`](docs/spec/README.md) are authoritative.
A change in behaviour starts as a change to the relevant spec, citing
requirement ids (`CORE-xx`, `WGT-xx`, `MOD-xx`). Pull requests that change
behaviour without a matching spec change are not merged.

New third-party dependencies are proposed in an issue with their rationale and
added to core spec section 1.2 once approved.

## Code standards

- Python 3.11+, full type hints; `mypy --strict` and `ruff` must pass.
- Google-style docstrings on every public class, method and function.
- Configuration is always a Pydantic model.
- Never block the event loop; blocking work goes through `run_blocking`.
- Log through structlog as structured events (`log.info("thing_happened", n=3)`).
- No literal user-facing strings: use catalog keys; ship EN, IT, FR, ES, DE.
- Front-end: vanilla Web Components, no build step.

## Spec references in code (CORE-74)

Every source file states what it implements in an `Implements:` section of
its module docstring; public classes and functions that implement specific
requirements do the same in their own docstring. One entry per line:

```text
<ids or ->: <spec name> <version>, §<section> <title>.
```

```python
"""Notification routing.

Implements:
    CORE-21, CORE-22: Core spec 0.4, §5.3 Routing rules.
    CORE-180..186: Core spec 0.4, §3.3 Scheduler.
    MOD-40: Module Contract 1.0 rev 4, §6.1 Messaging.
"""
```

- Use `-` instead of ids when the code implements a section without ids.
- Smaller fragments use a comment in the same form:
  `# Implements CORE-184 (Core spec 0.5, §3.3 Scheduler): no overlap.`
  Workflow and TOML files use the same comment.
- Cite the version of the spec you implemented against. A citation stays
  valid until the cited requirement changes (CORE-77); there is no need to
  touch code when the spec version moves on.

## Specs in the repository (CORE-75..79)

- `docs/spec/core.md` and `docs/spec/contract.md` are Markdown exports of the
  specs. Every spec change ends with a **spec sync PR**: export both docs,
  run `uv run python tools/spec_sync.py`, assign new requirements to a
  milestone in `docs/spec/milestones.toml`, and regenerate
  `docs/spec/coverage.md` with `uv run python tools/trace_report.py --out docs/spec/coverage.md`.
- `spec_sync.py` writes `requirements.toml` (one record per requirement with
  `changed_in`) and `versions.toml`. It fails if a requirement changed
  without being listed in a Document history row (CORE-76).
- `check_spec_refs.py` fails on unknown ids or versions. A citation older
  than the requirement's `changed_in` is *stale*: in a pull request it fails
  only in the files the PR changes, so touching a file means reviewing it
  against the current spec. Other stale citations show up in the coverage
  matrix and in the milestone issue.
- `trace_report.py` builds the coverage matrix (implemented = cited by code,
  tested = cited by a test name). The Traceability workflow keeps one
  `spec-change` issue per closed milestone that has changed or uncovered
  requirements (CORE-79). Close it by doing the work and updating `done_at`.

## Tests

- Test names cite requirement ids: `test_core_21_broadcast_excludes_sender`.
- `mm3.core` keeps at least 85 % line coverage (CORE-72).

## Commits and pull requests

- One logical change per pull request; update `CHANGELOG.md` under *Unreleased*.
- Mention affected requirement ids in the description.
- CI (lint, types, tests, i18n checks) must be green.

## Dependencies (core spec 10.9)

- Add a dependency only after it is approved in core spec section 1.2.
- Declare it in `pyproject.toml` with a range bounded to its current major,
  then run `uv lock`. Commit `pyproject.toml` and `uv.lock` together.
- Use each third-party library through one internal adapter in `mm3.core`;
  never expose its types to modules (Module Contract MOD-94).
- Renovate opens update pull requests. Patch and minor updates merge
  automatically when CI is green; majors need a review and a spec update.
- A security advisory that cannot be fixed yet may be added to
  `.pip-audit-ignore` only with a reason, a date and a tracking issue.

### Major updates

Renovate opens one pull request per major update and never merges it.

**`ci-tooling`** (GitHub Actions, pre-commit hooks):

1. Read the release notes in the PR for breaking changes that touch our
   workflows (renamed inputs, runtime changes).
2. CI green → merge. No spec change.

**`needs-spec-review`** (runtime libraries in core spec 1.2):

1. *Assess*: read the migration guide and decide: adopt now, later, or never.
   To postpone, close the PR; Renovate ignores that major until the next one
   (reopen it from the Dependency Dashboard).
2. *Spec*: update core spec 1.2 with the new major, bump the core spec
   version, and update `docs/spec/versions.toml` (CORE-74).
3. *Code*: fix the single adapter that wraps the library (CORE-177) on the
   Renovate branch or a new one; update the bound in `pyproject.toml` and
   `uv.lock`; add a CHANGELOG entry.
4. CI green → merge.

**`module-api`** (Pydantic, FastAPI; MOD-94): a new major of either changes the
Module API major. Do not merge until a Module API major is planned; keep the
PR closed until then.

The weekly canary (`newest-available` job) usually shows a breaking major
before Renovate's PR arrives.

## Local setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
uv run pre-commit install
uv run pytest
```

The repository is a uv workspace: `mm3-build/` is its own distribution (the
`mm3-i18n` catalog build hook, CORE-280) and is installed from the workspace.
`uv build --all-packages` builds both; `mm3-core`'s wheel gets its compiled
catalogs from the hook.
