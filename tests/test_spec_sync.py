"""Tests for tools/spec_sync.py and tools/spec_lib.py (CORE-71, CORE-75, CORE-76)."""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from spec_lib import expand_ids, parse_spec, version_key  # noqa: E402
from spec_sync import build  # noqa: E402

CORE = """# Core

Oct 27, 2026 · @x · Version {version} (draft)

## 1. Scope

| Topic | Decision |
| --- | --- |
| Bus | see CORE-21 |

## 5. Messaging

### 5.3 Routing rules

All traffic uses one envelope (CORE-20).

- CORE-21: Broadcasts reach {who}.
{extra}

## 13. Document history

| Version | Date | Changes |
| --- | --- | --- |
{rows}
"""
CONTRACT = """# Contract

Oct 27, 2026 · @x · Version 1.0 rev 1 (draft)

## 6. Messaging

- MOD-40: Names are UPPER_SNAKE.

## 14. Document history

| Revision | Date | Changes |
| --- | --- | --- |
| 1.0 rev 1 | 2026-10-27 | First draft |
"""


def _write(tmp: Path, version: str, rows: str, who: str = "everyone", extra: str = "") -> Path:
    (tmp / "core.md").write_text(CORE.format(version=version, rows=rows, who=who, extra=extra))
    (tmp / "contract.md").write_text(CONTRACT)
    return tmp


def _sync(tmp: Path) -> list[str]:
    req, ver, errors = build(tmp)
    if not errors:
        (tmp / "requirements.toml").write_text(req)
        (tmp / "versions.toml").write_text(ver)
    return errors


def _reqs(tmp: Path) -> dict[str, dict[str, str]]:
    data = tomllib.loads((tmp / "requirements.toml").read_text())
    return {r["id"]: r for r in data["requirement"]}


ROW_01 = "| 0.1 | 2026-10-27 | First draft |"


def test_core_75_ranges_and_continuations() -> None:
    assert expand_ids("CORE-21, 24, 27\u201329; MOD-96\u201399, CORE-180..182") == [
        "CORE-21",
        "CORE-24",
        "CORE-27",
        "CORE-28",
        "CORE-29",
        "MOD-96",
        "MOD-97",
        "MOD-98",
        "MOD-99",
        "CORE-180",
        "CORE-181",
        "CORE-182",
    ]
    assert expand_ids("(CORE-213, CORE-214, 10.4)") == ["CORE-213", "CORE-214"]


def test_core_75_versions_sort() -> None:
    assert version_key("0.10") > version_key("0.9")
    assert version_key("1.0 rev 10") > version_key("1.0 rev 9")


def test_core_75_definitions_bullets_and_first_mention() -> None:
    doc = parse_spec("Core spec", CORE.format(version="0.1", rows=ROW_01, who="all", extra=""))
    assert doc.version == "0.1"
    assert doc.requirements["CORE-21"]["section"] == "5.3"
    assert doc.requirements["CORE-20"]["text"].startswith("All traffic")


def test_core_75_baseline_sync(tmp_path: Path) -> None:
    assert _sync(_write(tmp_path, "0.1", ROW_01)) == []
    reqs = _reqs(tmp_path)
    assert reqs["CORE-21"]["changed_in"] == "0.1"
    assert reqs["MOD-40"]["spec"] == "Module Contract"


def test_core_76_change_listed_in_history_updates_changed_in(tmp_path: Path) -> None:
    _sync(_write(tmp_path, "0.1", ROW_01))
    rows = "| 0.2 | 2026-10-28 | Broadcast scope (CORE-21) |\n" + ROW_01
    assert _sync(_write(tmp_path, "0.2", rows, who="all but the sender")) == []
    assert _reqs(tmp_path)["CORE-21"]["changed_in"] == "0.2"
    assert _reqs(tmp_path)["CORE-21"]["introduced_in"] == "0.1"


def test_core_76_unlisted_change_fails(tmp_path: Path) -> None:
    _sync(_write(tmp_path, "0.1", ROW_01))
    rows = "| 0.2 | 2026-10-28 | Wording |\n" + ROW_01
    errors = _sync(_write(tmp_path, "0.2", rows, who="all but the sender"))
    assert any("CORE-21" in e and "text changed" in e for e in errors)


def test_core_76_new_and_removed_requirements(tmp_path: Path) -> None:
    _sync(_write(tmp_path, "0.1", ROW_01, extra="- CORE-22: Reserved names."))
    rows = "| 0.2 | 2026-10-28 | Added CORE-23 |\n" + ROW_01
    errors = _sync(_write(tmp_path, "0.2", rows, extra="- CORE-23: Order per sender."))
    assert any("CORE-22" in e and "removed" in e for e in errors)
    rows = "| 0.2 | 2026-10-28 | Added CORE-23, removed CORE-22 |\n" + ROW_01
    assert _sync(_write(tmp_path, "0.2", rows, extra="- CORE-23: Order per sender.")) == []
    reqs = _reqs(tmp_path)
    assert reqs["CORE-22"]["status"] == "removed"
    assert reqs["CORE-23"]["changed_in"] == "0.2"


def test_core_71_repository_specs_are_synced() -> None:
    req, ver, errors = build(ROOT / "docs" / "spec")
    assert errors == []
    assert (ROOT / "docs/spec/requirements.toml").read_text() == req
    assert (ROOT / "docs/spec/versions.toml").read_text() == ver
