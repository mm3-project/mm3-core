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

## Local setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
uv run pre-commit install
uv run pytest
```
