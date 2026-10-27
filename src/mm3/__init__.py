"""MM3: a modular smart-mirror platform in Python.

The public module API (``MirrorModule``, ``ModuleConfig``, ``ModuleContext``,
``LocalizedText``) will be exported from this package, as defined in the
MM3 Module Contract. Nothing outside the names exported here is part of
the module API.

Implements:
    MOD-20: Module Contract 1.0 rev 3, §4.2 Guarantees.
    MOD-94: Module Contract 1.0 rev 3, §10 API versioning and deprecation.
"""

from mm3.__about__ import MODULE_API_VERSION, __version__

__all__ = ["MODULE_API_VERSION", "__version__"]
