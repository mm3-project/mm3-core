"""The reference modules under ``modules/`` (CORE-289)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"
LANGS = {"en", "it", "fr", "es", "de"}
EXPECTED = {"mm3-greeting", "mm3-clock", "mm3-weather"}
PUBLISHED = {"mm3-clock", "mm3-weather"}
META = "mm3-modules-default"  # a distribution without code (CORE-294), not a module


def projects() -> list[Path]:
    return sorted(
        p for p in MODULES.iterdir() if (p / "pyproject.toml").is_file() and p.name != META
    )


def test_core_289_the_expected_modules_exist() -> None:
    assert {p.name for p in projects()} >= EXPECTED


@pytest.mark.parametrize("proj", projects(), ids=lambda p: p.name)
def test_core_289_each_module_is_its_own_distribution(proj: Path) -> None:
    meta = tomllib.loads((proj / "pyproject.toml").read_text())
    assert meta["project"]["name"] == proj.name
    assert re.fullmatch(r"[1-9]\d*\.\d+\.\d+", meta["project"]["version"])
    assert "mm3-build>=1.0,<2" in meta["build-system"]["requires"]
    assert "mm3-i18n" in meta["tool"]["hatch"]["build"]["hooks"]
    assert (proj / "README.md").is_file()
    assert (proj / "tests").is_dir()
    (pkg,) = (proj / "src").iterdir()
    locales = {p.name for p in (pkg / "locales").iterdir() if p.is_dir()}
    assert locales == LANGS
    assert "mm3.modules" in meta["project"]["entry-points"]


@pytest.mark.parametrize("proj", projects(), ids=lambda p: p.name)
def test_core_289_modules_use_only_the_module_api(proj: Path) -> None:
    for path in (proj / "src").rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"^\s*(from|import)\s+mm3\.core\b", text, re.M), path
    for path in (proj / "src").rglob("static/*.js"):
        assert "/core/" not in path.read_text().replace('"/core/mm3.js"', ""), path


def test_core_289_greeting_is_not_published() -> None:
    assert "mm3-greeting" not in PUBLISHED
    default = tomllib.loads((MODULES / META / "pyproject.toml").read_text())["project"]
    # The bundle lists clock and weather, never greeting (CORE-294).
    assert not any("mm3-greeting" in dep for dep in default["dependencies"])
