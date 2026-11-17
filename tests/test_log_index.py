"""Tests of the issue log tool (``tools/log_index.py``) and of the log itself."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("log_index", ROOT / "tools" / "log_index.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["log_index"] = module
    spec.loader.exec_module(module)
    return module


ENTRY = """---
id: {id}
seq: {seq}
title: A title | with a pipe
type: Issue
priority: P2
date: '2026-11-17'
area: Core
spec_versions: Core 0.46, Contract 1.0 rev 15
requirements: CORE-275
status: open
resolved_in: —
---

# {id} — A title
- Reported: 2026-11-17
"""


def _log(tmp: Path) -> Path:
    log = tmp / "log"
    (log / "entries").mkdir(parents=True)
    (log / "README.md").write_text("# Log\n\n<!-- log-table:start -->\n<!-- log-table:end -->\n")
    return log


def test_core_75_the_log_table_in_the_repository_is_up_to_date() -> None:
    tool = _tool()
    entries, problems = tool.load()
    assert problems == []
    assert len({e.meta["id"] for e in entries}) == len(entries)
    readme = (ROOT / "docs" / "log" / "README.md").read_text(encoding="utf-8")
    assert tool.render(readme, entries) == readme


def test_core_75_log_tool_writes_the_table_in_seq_order(tmp_path: Path) -> None:
    tool = _tool()
    log = _log(tmp_path)
    (log / "entries" / "ENH-001.md").write_text(ENTRY.format(id="ENH-001", seq=2))
    (log / "entries" / "ISS-001.md").write_text(ENTRY.format(id="ISS-001", seq=1))
    assert tool.main(["--log", str(log), "--check"]) == 1  # table still empty
    assert tool.main(["--log", str(log)]) == 0
    assert tool.main(["--log", str(log), "--check"]) == 0
    rows = [
        r
        for r in (log / "README.md").read_text().splitlines()
        if r.startswith("| ISS") or r.startswith("| ENH")
    ]
    assert [r.split(" | ")[0] for r in rows] == ["| ISS-001", "| ENH-001"]
    assert "A title \\| with a pipe" in rows[0]


def test_core_75_log_tool_refuses_wrong_entries(tmp_path: Path) -> None:
    tool = _tool()
    log = _log(tmp_path)
    (log / "entries" / "ISS-001.md").write_text(ENTRY.format(id="ISS-002", seq=1))
    (log / "entries" / "ISS-003.md").write_text(
        ENTRY.format(id="ISS-003", seq=1).replace("status: open\n", "")
    )
    _, problems = tool.load(log)
    assert any("id must be the file name" in p for p in problems)
    assert any("missing field status" in p for p in problems)
    assert any("seq 1 is used twice" in p for p in problems)
    assert tool.main(["--log", str(log)]) == 1


def test_core_75_log_tool_needs_the_markers(tmp_path: Path) -> None:
    tool = _tool()
    log = _log(tmp_path)
    (log / "README.md").write_text("# Log\n")
    shutil.copy(ROOT / "docs" / "log" / "entries" / "ISS-001.md", log / "entries" / "ISS-001.md")
    assert tool.main(["--log", str(log)]) == 1
