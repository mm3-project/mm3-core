# mm3-modules-default

The default [MM3](https://github.com/mm3-project/mm3-core) modules in one
install: this distribution has no code, only the list of the default modules
that are published when a release is cut (core spec 10.2, CORE-294).

| Module | Distribution |
| --- | --- |
| Clock | `mm3-clock` |
| Weather | `mm3-weather` |

Each module is also installable alone. Modules published later join in a later
release of this distribution. It declares no dependency on `mm3-core`: every
module carries the `mm3-core` range it needs (MOD-03).

Until `mm3-core` 1.0.0 is released, the modules ask for `mm3-core>=1.0,<2` while
the core is 0.x, so `mm3-install.sh` installs them with `--no-deps` and
`pip check` reports the mismatch.
