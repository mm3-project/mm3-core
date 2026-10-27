"""MM3: a modular smart-mirror platform in Python.

The public module API (``MirrorModule``, ``ModuleConfig``, ``ModuleContext``,
``LocalizedText``) will be exported from this package, as defined in the
MM3 Module Contract v1.0. Nothing outside the names exported here is part of
the module API.
"""

from mm3.__about__ import MODULE_API_VERSION, __version__

__all__ = ["MODULE_API_VERSION", "__version__"]
