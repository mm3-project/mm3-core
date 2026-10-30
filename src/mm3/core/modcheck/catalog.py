"""The rows of Module Contract section 11 that ``mm3 module check`` reports.

Engine-side rows (MOD-18, 20-22, 73, 90-92, 94) and the catalog rows
(MOD-96 to 99, checked by the module index) are not listed. A row with
several categories gets one report entry per category.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 10, §11 Compliance checklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["CATALOG", "Row", "row"]

Level = Literal["MUST", "SHOULD"]


@dataclass(frozen=True)
class Row:
    """One checklist row.

    Attributes:
        id: ``MOD-xx``.
        level: ``MUST`` or ``SHOULD``.
        categories: ``static``, ``kit``, ``harness`` and ``review``, in the order of §11.
        review: What a person confirms (the review entry's text).
    """

    id: str
    level: Level
    categories: tuple[str, ...]
    review: str = ""


def _rows() -> dict[str, Row]:
    m: Level = "MUST"
    s: Level = "SHOULD"
    table: list[tuple[str, Level, tuple[str, ...], str]] = [
        ("MOD-01", m, ("static",), ""),
        ("MOD-02", m, ("static",), ""),
        ("MOD-03", m, ("static",), ""),
        ("MOD-04", m, ("static",), ""),
        ("MOD-05", m, ("static",), ""),
        ("MOD-06", m, ("kit",), ""),
        ("MOD-07", s, ("static",), ""),
        ("MOD-08", s, ("static",), ""),
        ("MOD-10", m, ("kit",), ""),
        ("MOD-11", m, ("static", "kit"), ""),
        ("MOD-12", m, ("static",), ""),
        ("MOD-13", m, ("kit",), ""),
        ("MOD-14", m, ("kit",), ""),
        ("MOD-15", m, ("kit",), ""),
        ("MOD-16", m, ("kit",), ""),
        ("MOD-17", m, ("static",), ""),
        ("MOD-19", s, ("review",), "State that must survive a restart is kept in ctx.storage."),
        ("MOD-23", m, ("static",), ""),
        ("MOD-24", m, ("static", "kit"), ""),
        ("MOD-25", m, ("kit",), ""),
        ("MOD-26", m, ("kit",), ""),
        ("MOD-27", m, ("kit",), ""),
        ("MOD-28", m, ("static",), ""),
        ("MOD-29", m, ("static", "kit"), ""),
        ("MOD-30", m, ("static",), ""),
        ("MOD-31", m, ("static",), ""),
        ("MOD-32", m, ("static", "harness"), ""),
        (
            "MOD-33",
            m,
            ("static", "review"),
            "CSS stays inside the shadow root; theme variables used.",
        ),
        ("MOD-34", m, ("harness",), ""),
        ("MOD-35", m, ("static",), ""),
        ("MOD-36", m, ("harness",), ""),
        ("MOD-37", m, ("static",), ""),
        ("MOD-40", m, ("static",), ""),
        ("MOD-41", m, ("static",), ""),
        ("MOD-42", m, ("kit",), ""),
        ("MOD-43", m, ("kit",), ""),
        ("MOD-44", s, ("static",), ""),
        ("MOD-45", m, ("static",), ""),
        ("MOD-46", m, ("static",), ""),
        ("MOD-47", m, ("static",), ""),
        ("MOD-48", m, ("review",), "User-typed display text uses LocalizedText."),
        ("MOD-49", s, ("review",), "Validation is complete in the config model."),
        ("MOD-50", s, ("review",), "reconfigure() applies changes live where it is cheap."),
        ("MOD-51", m, ("harness",), ""),
        ("MOD-52", s, ("static",), ""),
        ("MOD-53", s, ("review",), "Sizes use em and --mm3-scale."),
        (
            "MOD-54",
            m,
            ("harness", "review"),
            "Sensitive content shows a placeholder in the preview.",
        ),
        ("MOD-60", m, ("static",), ""),
        ("MOD-61", m, ("static",), ""),
        ("MOD-62", m, ("static",), ""),
        ("MOD-63", m, ("static", "review"), "Plurals use plural forms, never concatenation."),
        ("MOD-64", m, ("review",), "Dates, times, numbers and units use the formatting helpers."),
        ("MOD-65", m, ("review",), "Text from external data is passed through, not translated."),
        ("MOD-67", s, ("static",), ""),
        ("MOD-70", m, ("static", "kit"), ""),
        (
            "MOD-71",
            m,
            ("static", "review"),
            "Each requested scope is the narrowest the module needs.",
        ),
        ("MOD-72", m, ("kit",), ""),
        ("MOD-80", m, ("static",), ""),
        ("MOD-81", s, ("review",), "Log events are short snake-case identifiers with fields."),
        ("MOD-82", s, ("review",), "Log levels are used as defined."),
        ("MOD-83", m, ("kit", "review"), "No personal data in logs at info or above."),
        ("MOD-84", s, ("harness",), ""),
        ("MOD-86", m, ("static", "review"), "No broad except hides an error."),
        ("MOD-93", s, ("review",), "api_version is the lowest minor whose features are used."),
    ]
    return {i: Row(i, lv, cats, text) for i, lv, cats, text in table}


CATALOG: dict[str, Row] = _rows()
"""Every checked row by id, in section 11 order."""


def row(mod_id: str) -> Row:
    """Return a row.

    Args:
        mod_id: ``MOD-xx``.

    Returns:
        The row.

    Raises:
        KeyError: The id is not a checked row.
    """
    return CATALOG[mod_id]
