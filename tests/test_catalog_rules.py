"""Tests of the catalog badges and of the engine's boundary with the installer.

Implements:
- CORE-122 (Core spec 0.43, §10.6 Module catalog): the three badges.
- CORE-123 (Core spec 0.43, §10.6 Module catalog): an entry records its publisher.
- CORE-349 (Core spec 0.43, §10.6 Module catalog): the engine never imports the
  verification libraries.
- CORE-130 (Core spec 0.43, §10.7 Installer service): the engine only forwards
  requests to the separate installer service.
- CORE-66 (Core spec 0.43, §10.1 Security): the engine process never installs
  anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from installer_fakes import entry, index_bytes, make_wheel, release
from mm3.cli.modules_cmd import _summary
from mm3.core.catalog import CatalogError, parse_index

SRC = Path(__file__).resolve().parents[1] / "src" / "mm3"
VERIFICATION = ("sigstore", "pypi_attestations", "mm3.installer.verify")


def _imports(path: Path) -> set[str]:
    """Every module name a source file imports, from absolute imports only."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _outside_installer() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "installer" not in p.relative_to(SRC).parts[:1]]


def _index_with(badge: str) -> bytes:
    data = make_wheel("mm3-news", "1.0.0")
    return index_bytes([entry("mm3-news", [release("mm3-news", "1.0.0", data)], badge=badge)])


@pytest.mark.parametrize("badge", ["official", "verified", "community"])
def test_core_122_the_three_badges_are_accepted_and_listed(badge: str) -> None:
    parsed = parse_index(_index_with(badge))
    assert parsed.modules[0].badge == badge
    assert badge in _summary(parsed.modules[0])


@pytest.mark.parametrize("badge", ["trusted", "Official", "", "gold"])
def test_core_122_any_other_badge_is_refused(badge: str) -> None:
    with pytest.raises(CatalogError):
        parse_index(_index_with(badge))


def test_core_123_an_entry_without_its_publisher_is_refused() -> None:
    data = make_wheel("mm3-news", "1.0.0")
    mod = entry("mm3-news", [release("mm3-news", "1.0.0", data)])
    del mod["publisher"]
    with pytest.raises(CatalogError):
        parse_index(index_bytes([mod]))


def test_core_349_only_the_installer_imports_the_verification_libraries() -> None:
    offenders = {
        str(p.relative_to(SRC)): sorted(
            name
            for name in _imports(p)
            if name.split(".")[0] in VERIFICATION or name in VERIFICATION
        )
        for p in _outside_installer()
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_core_130_the_engine_reaches_the_installer_only_through_its_client() -> None:
    offenders: dict[str, list[str]] = {}
    for path in (SRC / "core").rglob("*.py"):
        bad = sorted(
            n
            for n in _imports(path)
            if n.startswith("mm3.installer") and not n.startswith("mm3.installer.client")
        )
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert offenders == {}


def test_core_66_the_engine_runs_no_installer_command() -> None:
    """No engine module starts a package installer (modcheck builds a throwaway venv)."""
    offenders = []
    for path in (SRC / "core").rglob("*.py"):
        if "modcheck" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in {"pip", "uv", "pipx"}:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
