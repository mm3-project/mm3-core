"""Catalog compiler and quality gate, from ``mm3-build`` (``mm3_build.catalogs``).

``mm3-build`` is the only catalog compiler (CORE-280): the ``mm3-i18n`` build
hook puts compiled catalogs into every wheel, and ``mm3 i18n compile`` and
``mm3 i18n check`` use the same code through this module.

Implements:
    CORE-280: Core spec 0.17, §10.2 Packaging and deployment.
"""

from __future__ import annotations

from mm3_build.catalogs import (
    REQUIRED_LOCALES,
    CatalogError,
    CatalogProblem,
    Entry,
    check_locales,
    compile_locales,
    compiled,
    placeholders,
    po_files,
    targets,
)

__all__ = [
    "REQUIRED_LOCALES",
    "CatalogError",
    "CatalogProblem",
    "Entry",
    "check_locales",
    "compile_locales",
    "compiled",
    "placeholders",
    "po_files",
    "targets",
]
