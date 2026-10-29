"""Internationalization: catalogs, lookup, plurals, formatting (core spec 8).

Implements:
    CORE-07: Core spec 0.15, §3 Engine components.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from mm3.core.i18n.catalogs import (
    CORE_CATALOG,
    DATE_STYLES,
    I18n,
    format_date,
    format_number,
    locale_chain,
    plural_category,
    substitute,
)
from mm3.core.i18n.compiler import (
    REQUIRED_LOCALES,
    CatalogProblem,
    check_locales,
    compile_locales,
)

if TYPE_CHECKING:
    from mm3.core.registry import Registry
    from mm3.module import MirrorModule

__all__ = [
    "CORE_CATALOG",
    "DATE_STYLES",
    "REQUIRED_LOCALES",
    "CatalogProblem",
    "I18n",
    "check_locales",
    "compile_locales",
    "format_date",
    "format_number",
    "locale_chain",
    "module_catalog_root",
    "plural_category",
    "registry_roots",
    "substitute",
]


def module_catalog_root(cls: type[MirrorModule]) -> Path | None:
    """Return a module's catalog root: ``<package>/<locales_dir>`` (Contract MOD-05).

    Args:
        cls: The module class.

    Returns:
        The directory, or ``None`` when the module has none.
    """
    if not cls.locales_dir:
        return None
    package = cls.__module__.split(".", 1)[0]
    try:
        root = Path(str(importlib.resources.files(package) / cls.locales_dir))
    except (ModuleNotFoundError, TypeError, ValueError):
        return None
    return root if root.is_dir() else None


def registry_roots(registry: Registry) -> Callable[[str], Path | None]:
    """Return a module-name to catalog-root lookup for :class:`I18n`.

    Args:
        registry: Installed modules.

    Returns:
        The lookup.
    """

    def root(module: str) -> Path | None:
        entry = registry.get(module)
        cls = entry.cls if entry is not None else None
        return module_catalog_root(cls) if cls is not None else None

    return root
