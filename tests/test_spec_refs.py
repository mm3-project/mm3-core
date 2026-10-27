"""Tests for tools/check_spec_refs.py (CORE-74)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from check_spec_refs import check_file, implements_entries, load_versions  # noqa: E402

VERSIONS = {"Core spec": "0.4", "Module Contract": "1.0 rev 4"}


def _file(tmp: Path, doc: str) -> Path:
    path = tmp / "mod.py"
    path.write_text(f'"""{doc}"""\n')
    return path


def test_core_74_valid_entries(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        "Bus.\n\nImplements:\n    CORE-21, CORE-22: Core spec 0.4, §5.3 Routing rules.\n"
        "    MOD-40: Module Contract 1.0 rev 4, §6.1 Messaging.\n",
    )
    assert check_file(path, VERSIONS) == []


def test_core_74_missing_section(tmp_path: Path) -> None:
    path = _file(tmp_path, "No references here.")
    assert "no 'Implements:' section" in check_file(path, VERSIONS)[0]


def test_core_74_outdated_version(tmp_path: Path) -> None:
    path = _file(tmp_path, "X.\n\nImplements:\n    CORE-21: Core spec 0.3, §5.3 Routing rules.\n")
    assert "current is 0.4" in check_file(path, VERSIONS)[0]


def test_core_74_malformed_entry(tmp_path: Path) -> None:
    path = _file(tmp_path, "X.\n\nImplements:\n    routing stuff\n")
    assert "malformed" in check_file(path, VERSIONS)[0]


def test_core_74_section_only_and_continuation() -> None:
    doc = "X.\n\nImplements:\n    -: Core spec 0.4, §10.2 Packaging\n        and deployment.\n"
    assert implements_entries(doc) == ["-: Core spec 0.4, §10.2 Packaging and deployment."]


def test_core_74_versions_file_matches_tool() -> None:
    assert set(load_versions(ROOT)) == set(VERSIONS)


def test_core_74_repository_passes() -> None:
    files = sorted((ROOT / "src" / "mm3").rglob("*.py"))
    versions = load_versions(ROOT)
    assert [e for f in files for e in check_file(f, versions)] == []
