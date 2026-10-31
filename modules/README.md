# Reference modules

The modules that ship with mm3-core and prove the Module Contract end to end
(core spec 9.4, CORE-289). Each is its own distribution with its own
`pyproject.toml`, README, catalogs in five languages and tests, built with
`mm3-build`, and checked in CI with `mm3 module check`.

| Directory | Module | Published |
| --- | --- | --- |
| `mm3-greeting` | The example of Module Contract section 13 | no |
| `mm3-clock` | Digital and analog clock, time zones, sun and moon (CLK-01..08) | yes |
| `mm3-weather` | Weather from pluggable providers, Open-Meteo built in (WTH-01..12) | yes |
| `mm3-modules-default` | No code: depends on the published default modules (CORE-294) | yes |

`mm3-modules-default` is not a module: it has no code and no entry point, so
`mm3 module check` does not apply to it.

Module tests run against the source tree:

```sh
uv run pytest modules/mm3-greeting/tests --no-cov -o addopts=""
```
