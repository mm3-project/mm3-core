# Notes for Claude

- Specs are authoritative: `docs/spec/README.md` links the Core Engine
  Specification (CORE-xx, WGT-xx) and the Module Contract (MOD-xx).
- The full project rules live in the claude.ai Project "Magic Mirror 3" as
  `claude/mm3-rules.md`; follow them.
- This repository is the core engine (`mm3-core`). Modules other than the
  defaults live in their own repositories; the catalog is `mm3-project/module-index`.
- Before finishing a change: `ruff check .`, `ruff format --check .`, `mypy`, `pytest`.
- Update `CHANGELOG.md` under *Unreleased* and cite requirement ids.
