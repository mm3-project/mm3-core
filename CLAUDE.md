# Notes for Claude

- Specs are authoritative: `docs/spec/README.md` links the Core Engine
  Specification (CORE-xx, WGT-xx, CLK-xx, WTH-xx) and the Module Contract (MOD-xx).
- The full project rules live in the claude.ai Project "Magic Mirror 3" as
  `claude/mm3-rules.md`; follow them.
- This repository is the core engine (`mm3-core`). Modules other than the
  defaults live in their own repositories; the catalog is `mm3-project/module-index`.
- Before finishing a change: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest`; after changing dependencies run `uv lock`.
- Dependency policy: core spec 10.9 (bounded ranges, uv.lock, Renovate, pip-audit).
- Every source file has an `Implements:` docstring section citing requirement
  ids, spec name and version, section number and title (CORE-74, see
  CONTRIBUTING.md). Citations are checked per requirement (CORE-77):
  `uv run python tools/check_spec_refs.py`. Files you change must not keep
  stale citations: review them against the current spec first.
- Specs live in `docs/spec/` (exports, `requirements.toml`, `milestones.toml`,
  `coverage.md`). After a spec change open a spec sync PR (CONTRIBUTING.md).
  Coverage: `uv run python tools/trace_report.py`.
- Update `CHANGELOG.md` under *Unreleased* and cite requirement ids.
