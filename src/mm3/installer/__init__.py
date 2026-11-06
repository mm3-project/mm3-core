"""The installer service ``mm3-installer`` (core spec 10.7).

It runs as user ``mm3-install`` from its own venv ``/opt/mm3/installer/``
(CORE-368), listens only on ``/run/mm3-install/installer.sock`` (CORE-355) and
is the only process that verifies the catalog index, verifies releases, builds
staged venvs, switches the engine's venv and rolls back (CORE-130..136,
CORE-349..357). The engine never imports this package's verification adapter.

Implements:
    CORE-130: Core spec 0.33, §10.7 Installer service.
    -: Core spec 0.35, §10.7 Installer service.
"""
