"""Form labels (CORE-310): every field of the engine and of the reference modules has a
label and a help text in the catalogs, so the admin Form view never falls back to names.

Implements:
    CORE-310: Core spec 0.40, §6.4 Web configuration editor.
    MOD-46: Module Contract 1.0 rev 15, §6.2 Configuration.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from mm3.core.config.model import MirrorConfig
from mm3.core.config.modelcheck import label_problems
from mm3.core.i18n.catalogs import CORE_CATALOG, Table
from mm3.core.modcheck.pyscan import config_findings, find_class
from mm3_build.catalogs import compiled, po_files

MODULES = Path(__file__).resolve().parents[1] / "modules"
LANGUAGES = ("en", "it", "fr", "es", "de")


def test_core_310_engine_models_have_a_label_and_a_help() -> None:
    keys = set(Table(CORE_CATALOG).get("en"))
    assert label_problems(MirrorConfig, keys) == []


@pytest.mark.parametrize("lang", LANGUAGES)
def test_core_310_engine_labels_exist_in_every_language(lang: str) -> None:
    english = {k for k in Table(CORE_CATALOG).get("en") if k.startswith("CFG_")}
    assert len(english) >= 200
    assert english <= set(Table(CORE_CATALOG).get(lang))


def test_core_310_engine_labels_are_not_the_english_text_elsewhere() -> None:
    """A catalog copied untranslated would pass the key check; spot-check a few."""
    table = Table(CORE_CATALOG)
    assert table.get("it")["CFG_ENGINE_LOCALE"] == "Lingua"
    assert table.get("fr")["CFG_DISPLAY_ROTATION"] == "Rotation"
    assert table.get("es")["CFG_PAGES_LIST"] == "Páginas"
    assert table.get("de")["CFG_BACKUP_KEEP_DAILY"] == "Täglich"


def _module_class(module: str) -> str:
    """The class the module's entry point names."""
    project = tomllib.loads((MODULES / module / "pyproject.toml").read_text())
    (entry,) = project["project"]["entry-points"]["mm3.modules"].values()
    return str(entry).split(":")[1]


@pytest.mark.parametrize("module", ["mm3-clock", "mm3-weather", "mm3-presence", "mm3-greeting"])
def test_core_310_reference_modules_have_every_label(module: str) -> None:
    package = module.replace("-", "_")
    src = MODULES / module / "src" / package
    trees = {p.name: ast.parse(p.read_text()) for p in src.glob("*.py")}
    facts = find_class(trees, _module_class(module))
    assert facts is not None
    _, _, entries = compiled(po_files(src / "locales")["en"])
    assert [(m, f.message) for m, f in config_findings(trees, facts, set(entries))] == []
