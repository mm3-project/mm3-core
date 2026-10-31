"""``mm3-modules-default``: the default modules in one install (CORE-294)."""

from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest
from hatchling.builders.wheel import WheelBuilder
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"
META = MODULES / "mm3-modules-default"
DEFAULTS = {"mm3-clock", "mm3-weather"}  # the published reference modules (CORE-289)


def project(path: Path) -> dict[str, object]:
    return tomllib.loads((path / "pyproject.toml").read_text())["project"]  # type: ignore[no-any-return]


def requirements() -> list[Requirement]:
    return [Requirement(d) for d in project(META)["dependencies"]]  # type: ignore[attr-defined]


def test_core_294_lists_the_published_default_modules() -> None:
    assert {r.name for r in requirements()} == DEFAULTS


def test_core_294_the_greeting_example_is_not_part_of_it() -> None:
    assert "mm3-greeting" not in {r.name for r in requirements()}


def test_core_294_declares_no_dependency_on_the_core() -> None:
    names = {r.name for r in requirements()}
    assert "mm3-core" not in names
    assert "mm3-build" not in names


@pytest.mark.parametrize("req", requirements(), ids=lambda r: r.name)
def test_core_294_each_module_is_bounded_to_its_current_major(req: Requirement) -> None:
    current = Version(str(project(MODULES / req.name)["version"]))
    assert req.specifier.contains(current)
    assert not req.specifier.contains(Version(f"{current.major + 1}.0"))
    assert not req.specifier.contains(Version(f"{current.major}.{current.minor}.{current.micro}a0"))
    assert req.specifier.contains(Version(f"{current.major}.{current.minor + 1}"))


def test_core_294_is_a_distribution_without_code(tmp_path: Path) -> None:
    meta = project(META)
    assert meta["name"] == "mm3-modules-default"
    assert "entry-points" not in meta
    assert not list((META).rglob("*.py"))
    [wheel] = WheelBuilder(str(META)).build(directory=str(tmp_path), versions=["standard"])
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        assert all(".dist-info/" in n for n in names), names
        metadata = next(z.read(n).decode() for n in names if n.endswith("/METADATA"))
    assert "Requires-Dist: mm3-clock<2,>=1.0" in metadata
    assert "Requires-Dist: mm3-weather<2,>=1.0" in metadata
    requires = [line for line in metadata.splitlines() if line.startswith("Requires-Dist:")]
    assert len(requires) == 2
    assert not any("mm3-core" in line for line in requires)


def test_core_294_each_module_stays_installable_alone() -> None:
    for name in DEFAULTS:
        meta = project(MODULES / name)
        assert meta["name"] == name
        assert "mm3-core>=1.0,<2" in meta["dependencies"]  # type: ignore[operator]
        assert "mm3-modules-default" not in str(meta["dependencies"])
