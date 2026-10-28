"""Tests for tools/trace_report.py (CORE-78, CORE-79)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import trace_report  # noqa: E402
from trace_report import Milestone  # noqa: E402


def _fake(monkeypatch, milestones, reqs, impl, tests, stale=()):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(trace_report, "load_milestones", lambda: milestones)
    monkeypatch.setattr(trace_report, "load_requirements", lambda: reqs)
    monkeypatch.setattr(trace_report, "load_known_versions", dict)
    monkeypatch.setattr(trace_report, "implemented_ids", lambda: {i: {"x.py"} for i in impl})
    monkeypatch.setattr(trace_report, "tested_ids", lambda: {i: {"test_x"} for i in tests})
    monkeypatch.setattr(trace_report, "check", lambda *a: ([], list(stale)))
    monkeypatch.setattr(trace_report, "source_files", list)


REQS = {
    "CORE-21": {"id": "CORE-21", "changed_in": "0.3", "status": "active"},
    "CORE-22": {"id": "CORE-22", "changed_in": "0.6", "status": "active"},
    "CORE-30": {"id": "CORE-30", "changed_in": "0.1", "status": "active"},
}


def test_core_78_coverage_counts_and_waivers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ms = [
        Milestone(
            2, "Config", "not_started", ["CORE-30", "CORE-21"], waive_tests={"CORE-21": "why"}
        )
    ]
    _fake(monkeypatch, ms, REQS, impl=["CORE-21"], tests=[])
    row = trace_report.analyse()["rows"][0]  # type: ignore[index]
    assert row["not_impl"] == ["CORE-30"]
    assert row["not_tested"] == ["CORE-30"]
    assert trace_report.analyse()["unassigned"] == ["CORE-22"]


def test_core_79_changed_since_done_opens_issue(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ms = [Milestone(1, "Bus", "done", ["CORE-21", "CORE-22"], done_at={"core": "0.4"})]
    _fake(monkeypatch, ms, REQS, impl=["CORE-21", "CORE-22"], tests=["CORE-21", "CORE-22"])
    data = trace_report.analyse()
    assert data["rows"][0]["changed"] == ["CORE-22"]  # type: ignore[index]
    (issue,) = trace_report.issues(data)
    assert issue["open"] is True
    assert "CORE-22" in issue["body"]  # type: ignore[operator]


def test_core_79_clean_done_milestone_closes_issue(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ms = [Milestone(1, "Bus", "done", ["CORE-21"], done_at={"core": "0.6"})]
    _fake(monkeypatch, ms, REQS, impl=["CORE-21"], tests=["CORE-21"])
    (issue,) = trace_report.issues(trace_report.analyse())
    assert issue["open"] is False


def test_core_78_repository_map_is_complete() -> None:
    data = trace_report.analyse()
    assert data["unassigned"] == []
    assert data["duplicates"] == []
    assert not any(r["unknown"] for r in data["rows"])  # type: ignore[attr-defined]
