"""The i18n service: locale chains, catalog lookup, plurals and formatting.

Catalogs are read from compiled ``messages.json`` files: a module's
``locales/`` directory, locale packages on the entry point ``mm3.locales``
and the engine catalog (``CORE_*`` keys). A key is looked up along the
instance's locale chain in the module's catalog, then along the same chain
in the core catalog; at each locale the module's own entry wins over a
locale package's. Backends use :meth:`I18n.translate` through ``ctx.t()``;
shells fetch the merged catalog of an instance at ``/i18n/<instance>.json``.

Implements:
    CORE-07: Core spec 0.15, §3 Engine components.
    CORE-264, CORE-265, CORE-266: Core spec 0.15, §8.5 Catalogs at run time.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from babel import Locale, UnknownLocaleError
from babel.dates import format_date as babel_format_date
from babel.dates import format_time as babel_format_time
from babel.numbers import format_decimal
from babel.units import UnknownUnitError, format_unit

from mm3.core.i18n.compiler import Entry
from mm3.core.logging import get_logger

__all__ = [
    "CORE_CATALOG",
    "CORE_PACKAGE",
    "DATE_STYLES",
    "ENTRY_POINT",
    "I18n",
    "Table",
    "format_date",
    "format_number",
    "locale_chain",
    "plural_category",
    "substitute",
]

CORE_CATALOG = Path(__file__).parent.parent / "locales"
"""The engine catalog root (``CORE_*`` keys and ``REQUEST_TIMEOUT``)."""
CORE_PACKAGE = "mm3"
"""Entry point name under ``mm3.locales`` for packages that translate the engine."""
ENTRY_POINT = "mm3.locales"
"""Entry point group of locale packages (core spec 8.3, Contract MOD-66)."""
DATE_STYLES = ("short", "medium", "long", "full", "time")
"""Styles of ``format_date`` and ``fmtDate`` (CORE-266)."""

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_log = get_logger(subsystem="i18n")
_warned: set[tuple[str, ...]] = set()


def _warn_once(event: str, *key: str, **fields: Any) -> None:
    if (event, *key) not in _warned:
        _warned.add((event, *key))
        _log.warning(event, **fields)


def _base(tag: str) -> str:
    return re.split(r"[-_]", tag, maxsplit=1)[0]


def locale_chain(instance_locale: str | None, engine_locale: str) -> list[str]:
    """Return the locale chain of an instance (core spec 8.2).

    Args:
        instance_locale: The instance's ``locale`` override, if any.
        engine_locale: ``engine.locale``.

    Returns:
        Override, engine locale, each followed by its base language, then
        ``en``, without duplicates.
    """
    chain: list[str] = []
    for tag in (instance_locale, engine_locale):
        if tag:
            for item in (tag, _base(tag)):
                if item not in chain:
                    chain.append(item)
    if "en" not in chain:
        chain.append("en")
    return chain


@lru_cache(maxsize=64)
def _rule(locale: str) -> Any:
    try:
        return Locale.parse(locale.replace("-", "_")).plural_form
    except (UnknownLocaleError, ValueError):
        return Locale.parse("en").plural_form


def plural_category(locale: str, count: float) -> str:
    """Return the CLDR plural category of a number in a locale (CORE-265).

    Args:
        locale: BCP 47 tag.
        count: The number.

    Returns:
        ``zero``, ``one``, ``two``, ``few``, ``many`` or ``other``.
    """
    return str(_rule(locale)(abs(count)))


def substitute(text: str, values: Mapping[str, Any], *, key: str = "") -> str:
    """Replace ``{name}`` placeholders; a missing one stays and is logged once.

    Args:
        text: The catalog text.
        values: The variables.
        key: The catalog key, for the log.

    Returns:
        The text.
    """

    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in values:
            return str(values[name])
        _warn_once("i18n_variable_missing", key, name, key=key, variable=name)
        return m.group(0)

    return _PLACEHOLDER.sub(one, text)


class Table:
    """The compiled catalogs of one root: locale to entries, read when first used.

    Args:
        root: The catalog root (``<lang>/messages.json`` below it).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, dict[str, Entry]] = {}

    def get(self, locale: str) -> Mapping[str, Entry]:
        """Return the entries of a locale; empty when it has no compiled catalog."""
        if locale not in self._cache:
            path = self.root / locale / "messages.json"
            entries: dict[str, Entry] = {}
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    _warn_once("i18n_catalog_unreadable", str(path), path=str(path), error=str(exc))
                else:
                    if isinstance(data, dict):
                        entries = {k: v for k, v in data.items() if isinstance(v, str | dict)}
            self._cache[locale] = entries
        return self._cache[locale]


def _entry_point_roots() -> dict[str, list[Path]]:
    """Find locale packages: entry point name (module or ``mm3``) to catalog roots."""
    roots: dict[str, list[Path]] = {}
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT):
        try:
            root = Path(str(importlib.resources.files(ep.value)))
        except (ModuleNotFoundError, TypeError, ValueError) as exc:
            _warn_once(
                "i18n_locale_package_unusable", ep.name, ep.value, name=ep.name, error=str(exc)
            )
            continue
        if root.is_dir():
            roots.setdefault(ep.name, []).append(root)
    return roots


class I18n:
    """Catalog lookup for every module and the engine (core spec 8).

    Args:
        module_root: Returns a module's catalog root, or ``None`` without one.
        core_root: The engine catalog root.
        packages: Locale packages, name to roots; read from the ``mm3.locales``
            entry point by default.

    Implements:
        CORE-07: Core spec 0.15, §3 Engine components.
        CORE-264: Core spec 0.15, §8.5 Catalogs at run time.
    """

    def __init__(
        self,
        module_root: Callable[[str], Path | None],
        *,
        core_root: Path = CORE_CATALOG,
        packages: Mapping[str, Iterable[Path]] | None = None,
    ) -> None:
        self._module_root = module_root
        self._core = [Table(core_root)]
        found = _entry_point_roots() if packages is None else packages
        self._packages = {name: [Table(r) for r in roots] for name, roots in found.items()}
        self._modules: dict[str, list[Table]] = {}
        self._merged: dict[tuple[str, tuple[str, ...]], tuple[str, bytes]] = {}

    def _tables(self, module: str) -> list[Table]:
        if module not in self._modules:
            root = self._module_root(module)
            own = [Table(root)] if root is not None else []
            self._modules[module] = own + self._packages.get(module, [])
        return self._modules[module]

    def _core_tables(self) -> list[Table]:
        return self._core + self._packages.get(CORE_PACKAGE, [])

    def lookup(self, module: str | None, chain: Iterable[str], key: str) -> Entry | None:
        """Find a key: along the chain in the module's catalog, then in the core's.

        Args:
            module: Module name; ``None`` for engine text only.
            chain: The locale chain.
            key: The catalog key.

        Returns:
            The entry, or ``None`` when no catalog has it.
        """
        chain = list(chain)
        groups = ([self._tables(module)] if module else []) + [self._core_tables()]
        for tables in groups:
            for locale in chain:
                for table in tables:
                    entry = table.get(locale).get(key)
                    if entry is not None:
                        return entry
        return None

    def translate(
        self,
        module: str | None,
        chain: Iterable[str],
        key: str,
        count: float | None = None,
        values: Mapping[str, Any] | None = None,
    ) -> str:
        """Return the text of a key for a chain (``ctx.t``, CORE-264, CORE-265).

        Args:
            module: Module name; ``None`` for engine text only.
            chain: The locale chain; its first locale picks the plural form.
            key: The catalog key.
            count: Picks the plural form and is available as ``{count}``.
            values: Placeholder values.

        Returns:
            The text; the key itself when no catalog has it (logged once).
        """
        chain = list(chain)
        entry = self.lookup(module, chain, key)
        if entry is None:
            _warn_once("i18n_key_missing", module or "", key, module=module, key=key)
            return key
        vars_ = dict(values or {})
        if count is not None:
            vars_.setdefault("count", count)
        if isinstance(entry, dict):
            category = plural_category(chain[0], count) if count is not None else "other"
            text = entry.get(category) or entry.get("other") or next(iter(entry.values()), key)
        else:
            text = entry
        return substitute(text, vars_, key=key)

    def merged(self, module: str, chain: Iterable[str]) -> tuple[str, bytes]:
        """Return the catalog a shell gets for an instance, and its version.

        Every key of the module's and the core catalogs in any locale of the
        chain, resolved as :meth:`lookup` does.

        Args:
            module: Module name.
            chain: The instance's locale chain.

        Returns:
            The version (the ``v`` of ``/i18n/<instance>.json``) and the JSON.
        """
        from mm3.core.web.view import content_version

        chain = tuple(chain)
        cached = self._merged.get((module, chain))
        if cached is None:
            keys: set[str] = set()
            for table in [*self._tables(module), *self._core_tables()]:
                for locale in chain:
                    keys.update(table.get(locale))
            data: dict[str, Entry] = {}
            for key in sorted(keys):
                entry = self.lookup(module, chain, key)
                if entry is not None:
                    data[key] = entry
            raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            cached = self._merged[(module, chain)] = (content_version(raw), raw)
        return cached


def format_date(value: datetime, style: str, locale: str, timezone: ZoneInfo) -> str:
    """Format a date (``ctx.format_date``, CORE-266).

    Args:
        value: The moment; a naive value is taken as UTC.
        style: ``short``, ``medium``, ``long`` or ``full`` (the date), or
            ``time`` (a short time).
        locale: BCP 47 tag.
        timezone: ``engine.timezone``.

    Returns:
        The text.

    Raises:
        ValueError: Unknown style.
    """
    if style not in DATE_STYLES:
        raise ValueError(f"unknown date style {style!r}; use one of {', '.join(DATE_STYLES)}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    local = value.astimezone(timezone)
    loc = locale.replace("-", "_")
    if style == "time":
        return str(babel_format_time(local, "short", locale=loc))
    return str(babel_format_date(local.date(), style, locale=loc))


def format_number(value: float, locale: str, unit: str | None = None) -> str:
    """Format a number, with a unit if given, without converting (CORE-266).

    Args:
        value: The number.
        locale: BCP 47 tag.
        unit: A CLDR / ``Intl`` unit identifier such as ``celsius`` or
            ``kilometer-per-hour``.

    Returns:
        The text.
    """
    loc = locale.replace("-", "_")
    if unit is None:
        return str(format_decimal(value, locale=loc))
    try:
        return str(format_unit(value, unit, length="short", locale=loc))
    except UnknownUnitError:
        _warn_once("i18n_unit_unknown", unit, unit=unit)
        return f"{format_decimal(value, locale=loc)} {unit}"
