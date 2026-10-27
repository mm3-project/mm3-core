"""Engine internals (core spec section 3).

Subpackages, added milestone by milestone:

- ``mm3.core.app``: bootstrap, CLI entry into the engine, signal handling (CORE-01)
- ``mm3.core.config``: YAML loading, validation, hot reload (CORE-02, section 6)
- ``mm3.core.registry``: entry-point discovery, API version checks (CORE-03)
- ``mm3.core.supervisor``: module lifecycle state machine (CORE-04, section 7)
- ``mm3.core.bus``: notification routing (CORE-05, section 5)
- ``mm3.core.web``: shell, ``/ws``, REST API, admin UI (CORE-06)
- ``mm3.core.i18n``: catalogs and locale fallback (CORE-07, section 8)
- ``mm3.core.services``: scheduler (in-house, CORE-180..186), HTTP, storage, MQTT bridge,
  installer client (CORE-08)

Modules MUST NOT import from this package (Module Contract MOD rules).
"""
