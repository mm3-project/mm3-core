# Specifications

| Document | Export | Ids | Scope |
| --- | --- | --- | --- |
| [Core Engine Specification](https://claude.ai/artifact/FMsTUUoBP9wHzFtruUa6FV) | [core.md](core.md) | `CORE-xx`, `WGT-xx` | Engine, shell, admin UI, services, widgets, catalog, installer |
| [MM3 Module Contract](https://claude.ai/artifact/AwhPKtk3ezmyix1NRmKZrR) | [contract.md](contract.md) | `MOD-xx` | Obligations of every module; takes precedence for modules |

The linked documents are edited; the Markdown exports here are synced after
every spec change (CORE-75), so each commit carries the spec it implements.

| File | Content | Maintained by |
| --- | --- | --- |
| [versions.toml](versions.toml) | Current version of each spec | `tools/spec_sync.py` |
| [requirements.toml](requirements.toml) | Every requirement with section, hash, `introduced_in`, `changed_in`, status | `tools/spec_sync.py` |
| [milestones.toml](milestones.toml) | Requirements per milestone, status, `done_at`, waivers | by hand, in spec sync PRs |
| [coverage.md](coverage.md) | Coverage matrix: implemented and tested per milestone | `tools/trace_report.py` |
