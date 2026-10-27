# Notes for Claude

- Specs are authoritative: `docs/spec/README.md` links the Core Engine
  Specification (CORE-xx, WGT-xx) and the Module Contract (MOD-xx).
- The full project rules live in the claude.ai Project "Magic Mirror 3" as
  `claude/mm3-rules.md`; follow them.
- This repository is the core engine (`mm3-core`). Modules other than the
  defaults live in their own repositories; the catalog is `mm3-project/module-index`.
- Before finishing a change: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest`; after changing dependencies run `uv lock`.
- Dependency policy: core spec 10.9 (bounded ranges, uv.lock, Renovate, pip-audit).
- Update `CHANGELOG.md` under *Unreleased* and cite requirement ids.
