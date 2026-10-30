"""Registers the ``mm3-i18n`` build hook with hatchling.

Implements:
    CORE-280: Core spec 0.17, §10.2 Packaging and deployment.
"""

from __future__ import annotations

from hatchling.plugin import hookimpl

from mm3_build.plugin import I18nBuildHook


@hookimpl
def hatch_register_build_hook() -> type[I18nBuildHook]:
    """Return the hook class for hatchling's plugin manager."""
    return I18nBuildHook
