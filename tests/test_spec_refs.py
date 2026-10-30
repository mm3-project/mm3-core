"""Tests for tools/check_spec_refs.py (CORE-74, CORE-77)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from check_spec_refs import check, citations, implements_entries, source_files  # noqa: E402
from spec_lib import load_known_versions, load_requirements  # noqa: E402

REQS = {
    "CORE-21": {"id": "CORE-21", "spec": "Core spec", "changed_in": "0.3", "status": "active"},
    "CORE-22": {"id": "CORE-22", "spec": "Core spec", "changed_in": "0.6", "status": "active"},
    "CORE-23": {
        "id": "CORE-23",
        "spec": "Core spec",
        "changed_in": "0.1",
        "status": "removed",
        "removed_in": "0.6",
    },
    "MOD-40": {
        "id": "MOD-40",
        "spec": "Module Contract",
        "changed_in": "1.0 rev 4",
        "status": "active",
    },
}
VERSIONS = {
    "Core spec": ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6"],
    "Module Contract": ["1.0 rev 1", "1.0 rev 4", "1.0 rev 6"],
}


def _src(tmp: Path, doc: str) -> Path:
    path = tmp / "src" / "mod.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'"""{doc}"""\n')
    return path


def test_core_74_valid_entries(tmp_path: Path) -> None:
    path = _src(
        tmp_path,
        "Bus.\n\nImplements:\n    CORE-21, CORE-22: Core spec 0.6, §5.3 Routing rules.\n"
        "    MOD-40: Module Contract 1.0 rev 4, §6.1 Messaging.\n",
    )
    assert check([path], set(), REQS, VERSIONS) == ([], [])


def test_core_74_missing_section(tmp_path: Path) -> None:
    path = _src(tmp_path, "No references here.")
    errors, _ = check([path], set(), REQS, VERSIONS)
    assert "no 'Implements:' section" in errors[0]


def test_core_74_malformed_entry(tmp_path: Path) -> None:
    path = _src(tmp_path, "X.\n\nImplements:\n    routing stuff\n")
    errors, _ = check([path], set(), REQS, VERSIONS)
    assert "malformed" in errors[0]


def test_core_74_section_only_and_continuation() -> None:
    doc = "X.\n\nImplements:\n    -: Core spec 0.2, §10.2 Packaging\n        and deployment.\n"
    assert implements_entries(doc) == ["-: Core spec 0.2, §10.2 Packaging and deployment."]


def test_core_74_function_docstrings_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "f.py"
    path.write_text(
        '"""M.\n\nImplements:\n    -: Core spec 0.6, §5 Messaging protocol.\n"""\n'
        "def f():\n"
        '    """F.\n\n    Implements:\n'
        '        CORE-21: Core spec 0.3, §5.3 Routing rules.\n    """\n'
        "    # Implements MOD-40 (Module Contract 1.0 rev 4, §6.1 Messaging): names.\n"
    )
    cites, errors = citations(path)
    assert errors == []
    assert {c.rid for c in cites} == {None, "CORE-21", "MOD-40"}


def test_core_74_front_end_comments(tmp_path: Path) -> None:
    js = tmp_path / "shell.js"
    js.write_text(
        "// Implements CORE-21, CORE-22 (Core spec 0.6, §5.3 Routing rules)\nconst x = 1;\n"
    )
    css = tmp_path / "theme.css"
    css.write_text("/* Implements MOD-40 (Module Contract 1.0 rev 4, §6.1 Messaging) */\n")
    bare = tmp_path / "bare.js"
    bare.write_text("const y = 2;\n")
    assert {c.rid for c in citations(js)[0]} == {"CORE-21", "CORE-22"}
    assert {c.rid for c in citations(css)[0]} == {"MOD-40"}
    assert "no 'Implements' comment" in citations(bare)[1][0]
    assert any(p.name == "shell.js" for p in source_files())
    assert not any("vendor" in p.parts for p in source_files())


def test_core_74_module_front_end_files_are_scanned() -> None:
    names = {p.name for p in source_files()}
    assert {"clock.js", "clock.css", "weather.js", "weather.css", "greeting.js"} <= names


def test_core_77_older_version_of_unchanged_requirement_is_valid(tmp_path: Path) -> None:
    path = _src(tmp_path, "X.\n\nImplements:\n    CORE-21: Core spec 0.3, §5.3 Routing rules.\n")
    assert check([path], set(), REQS, VERSIONS) == ([], [])


def test_core_77_stale_citation_warns_outside_changed_files(tmp_path: Path) -> None:
    path = _src(tmp_path, "X.\n\nImplements:\n    CORE-22: Core spec 0.4, §5.3 Routing rules.\n")
    errors, warnings = check([path], set(), REQS, VERSIONS)
    assert errors == []
    assert "changed in 0.6" in warnings[0]


def test_core_77_stale_citation_fails_in_changed_files(tmp_path: Path) -> None:
    path = _src(tmp_path, "X.\n\nImplements:\n    CORE-22: Core spec 0.4, §5.3 Routing rules.\n")
    errors, _ = check([path], {path.resolve()}, REQS, VERSIONS)
    assert "changed in 0.6" in errors[0]


def test_core_77_unknown_removed_and_bad_version(tmp_path: Path) -> None:
    path = _src(
        tmp_path,
        "X.\n\nImplements:\n    CORE-99: Core spec 0.6, §1 Scope.\n"
        "    CORE-23: Core spec 0.6, §5.3 Routing rules.\n"
        "    CORE-21: Core spec 0.9, §5.3 Routing rules.\n"
        "    MOD-40: Core spec 0.6, §6.1 Messaging.\n",
    )
    errors, _ = check([path], set(), REQS, VERSIONS)
    text = "\n".join(errors)
    assert "CORE-99" in text
    assert "unknown requirement" in text
    assert "removed in 0.6" in text
    assert "version does not exist" in text
    assert len(errors) == 4


def test_core_77_repository_has_no_errors() -> None:
    errors, _ = check(source_files(), set(), load_requirements(), load_known_versions())
    assert errors == []
