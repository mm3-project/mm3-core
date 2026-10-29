"""Compile and check message catalogs (``mm3 i18n compile`` and ``check``).

A catalog root (a module's ``locales/`` or the engine's) holds
``<lang>/LC_MESSAGES/messages.po``. Compiling writes next to each ``.po`` a
``messages.mo`` and a ``<lang>/messages.json``: a flat object of key to
text, where an entry with plural forms is an object keyed by CLDR plural
category, converted from the ``.po`` plural indices. The engine never
compiles at run time; ``check`` fails when a compiled file is missing or
does not match its ``.po``, when a locale lacks a key of ``en``, or when a
placeholder set differs from ``en``.

Implements:
    CORE-40: Core spec 0.15, §8 Internationalization.
    CORE-41, CORE-42: Core spec 0.15, §8.4 Quality gate.
    CORE-263, CORE-265: Core spec 0.15, §8.5 Catalogs at run time.
"""

from __future__ import annotations

import gettext
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from babel import Locale, UnknownLocaleError
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

__all__ = [
    "REQUIRED_LOCALES",
    "CatalogProblem",
    "Entry",
    "check_locales",
    "compile_locales",
    "compiled",
    "placeholders",
    "po_files",
]

REQUIRED_LOCALES = ("en", "it", "fr", "es", "de")
"""Locales every user-facing catalog ships (CORE-40, Contract MOD-62)."""

Entry = str | dict[str, str]
"""A ``messages.json`` value: text, or plural forms keyed by CLDR category."""

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SAMPLES = [*range(0, 1001), 1_000_000, 2_000_000]


@dataclass(frozen=True)
class CatalogProblem:
    """One reason a catalog root fails the quality gate.

    Attributes:
        path: The file concerned.
        message: What is wrong.
    """

    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def po_files(root: Path) -> dict[str, Path]:
    """Return the ``.po`` file of every locale under a catalog root.

    Args:
        root: The catalog root (``locales/``).

    Returns:
        Locale to its ``messages.po``, sorted by locale.
    """
    found = {p.parent.parent.name: p for p in root.glob("*/LC_MESSAGES/messages.po")}
    return dict(sorted(found.items()))


def placeholders(entry: Entry) -> set[str]:
    """Return the ``{name}`` placeholders of an entry (all plural forms together).

    Args:
        entry: Text or plural forms.

    Returns:
        The placeholder names.
    """
    texts = entry.values() if isinstance(entry, dict) else [entry]
    return {m for text in texts for m in _PLACEHOLDER.findall(text)}


def _categories(lang: str, catalog: Catalog) -> dict[str, int]:
    """Map each CLDR plural category of a locale to its ``.po`` plural index."""
    try:
        rule = Locale.parse(lang.replace("-", "_")).plural_form
    except (UnknownLocaleError, ValueError):
        rule = Locale.parse("en").plural_form
    index = gettext.c2py(catalog.plural_expr)
    mapping: dict[str, int] = {}
    for n in _SAMPLES:
        mapping.setdefault(rule(n), int(index(n)))
    other = mapping.get("other", catalog.num_plurals - 1)
    for category in (*sorted(rule.tags), "other"):
        mapping.setdefault(category, other)  # categories only reached by decimals
    return mapping


def _read(po: Path) -> Catalog:
    with po.open("rb") as f:
        return read_po(f, locale=po.parent.parent.name.replace("-", "_"))


def compiled(po: Path) -> tuple[bytes, bytes, dict[str, Entry]]:
    """Compile one ``.po`` file in memory.

    Args:
        po: ``<lang>/LC_MESSAGES/messages.po``.

    Returns:
        The ``.mo`` bytes, the ``messages.json`` bytes and its entries.
    """
    catalog = _read(po)
    lang = po.parent.parent.name
    cats = _categories(lang, catalog)
    entries: dict[str, Entry] = {}
    for msg in catalog:
        if not msg.id or msg.fuzzy:
            continue
        if msg.pluralizable:
            key = str(msg.id[0])
            forms = list(msg.string) if isinstance(msg.string, tuple | list) else [msg.string]
            if not any(forms):
                continue
            entries[key] = {
                cat: str(forms[i] if i < len(forms) and forms[i] else forms[-1])
                for cat, i in sorted(cats.items())
            }
        elif msg.string:
            entries[str(msg.id)] = str(msg.string)
    mo = io.BytesIO()
    write_mo(mo, catalog)
    data = json.dumps(dict(sorted(entries.items())), ensure_ascii=False, indent=1) + "\n"
    return mo.getvalue(), data.encode("utf-8"), entries


def _targets(po: Path) -> tuple[Path, Path]:
    return po.with_suffix(".mo"), po.parent.parent / "messages.json"


def compile_locales(root: Path) -> list[Path]:
    """Write ``messages.mo`` and ``messages.json`` for every locale of a root.

    Args:
        root: The catalog root.

    Returns:
        The files written or rewritten.
    """
    written: list[Path] = []
    for po in po_files(root).values():
        mo_bytes, json_bytes, _ = compiled(po)
        for path, data in zip(_targets(po), (mo_bytes, json_bytes), strict=True):
            if not path.is_file() or path.read_bytes() != data:
                path.write_bytes(data)
                written.append(path)
    return written


def check_locales(root: Path, required: Iterable[str] = REQUIRED_LOCALES) -> list[CatalogProblem]:
    """Run the catalog quality gate on a root.

    Args:
        root: The catalog root.
        required: Locales that must be present.

    Returns:
        The problems; empty when the root passes.
    """
    problems: list[CatalogProblem] = []
    pos = po_files(root)
    for lang in required:
        if lang not in pos:
            problems.append(CatalogProblem(root / lang, "locale missing (CORE-40)"))
    tables: dict[str, dict[str, Entry]] = {}
    for lang, po in pos.items():
        mo_bytes, json_bytes, entries = compiled(po)
        tables[lang] = entries
        for path, data in zip(_targets(po), (mo_bytes, json_bytes), strict=True):
            if not path.is_file():
                problems.append(CatalogProblem(path, "not compiled; run mm3 i18n compile"))
            elif path.read_bytes() != data:
                problems.append(
                    CatalogProblem(path, "does not match its .po; run mm3 i18n compile")
                )
    en = tables.get("en")
    if en is None:
        return problems
    for lang, entries in tables.items():
        if lang == "en":
            continue
        po = pos[lang]
        for key in sorted(set(en) - set(entries)):
            problems.append(CatalogProblem(po, f"{key} is missing (CORE-41)"))
        for key in sorted(set(en) & set(entries)):
            want, got = placeholders(en[key]), placeholders(entries[key])
            if want != got:
                problems.append(
                    CatalogProblem(
                        po,
                        f"{key} has placeholders {sorted(got)}, en has {sorted(want)} (CORE-42)",
                    )
                )
    return problems
