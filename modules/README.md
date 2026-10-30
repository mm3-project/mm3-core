# Reference modules

The modules that ship with mm3-core and prove the Module Contract end to end
(core spec 9.4, CORE-289). Each is its own distribution with its own
`pyproject.toml`, README, catalogs in five languages and tests, built with
`mm3-build`, and checked in CI with `mm3 module check`.

| Directory | Module | Published |
| --- | --- | --- |
| `mm3-greeting` | The example of Module Contract section 13 | no |

`mm3-clock` and `mm3-weather` follow in the same directory (milestone 5).
Module tests run against the source tree:

```sh
uv run pytest modules/mm3-greeting/tests --no-cov -o addopts=""
```
