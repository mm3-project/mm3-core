"""Requirements coverage matrix and milestone issues.

Implements:
    CORE-78, CORE-79: Core spec 0.6, §10.3 Documentation and quality.

Usage::

    python tools/trace_report.py                     # Markdown report on stdout
    python tools/trace_report.py --out docs/spec/coverage.md
    python tools/trace_report.py --issues issues.json

A requirement is *implemented* when code cites it (``Implements:`` sections in
``src/``, ``modules/*/src`` and ``tools/``, ``# Implements`` comments in
Python, workflow, TOML and YAML files) and *tested* when a test name cites it
(``test_core_21_...``, in ``tests/`` and ``modules/*/tests``). Waivers in
``docs/spec/milestones.toml`` count as covered. For every closed milestone the
report lists requirements changed after ``done_at``, requirements not covered
and stale citations; with ``--issues`` it writes one issue per closed
milestone (``open`` true when there is something to do).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from check_spec_refs import COMMENT, check, citations, source_files
from spec_lib import ROOT, SPEC_DIR, expand_ids, load_known_versions, load_requirements, version_key

SPEC_KEYS = {"CORE": "core", "WGT": "core", "CLK": "core", "WTH": "core", "MOD": "contract"}
COMMENT_GLOBS = [".github/workflows/*.yml", "pyproject.toml", "*.toml", "mm3-build/pyproject.toml"]
TEST_RE = re.compile(r"def (test_(core|wgt|clk|wth|mod)_(\d+)\w*)\(", re.I)


@dataclass
class Milestone:
    """One milestone from ``milestones.toml``."""

    number: int
    name: str
    status: str
    ids: list[str]
    done_at: dict[str, str] = field(default_factory=dict)
    waive: dict[str, str] = field(default_factory=dict)
    waive_tests: dict[str, str] = field(default_factory=dict)


def load_milestones(path: Path = SPEC_DIR / "milestones.toml") -> list[Milestone]:
    """Read the milestone map.

    Args:
        path: ``milestones.toml``.

    Returns:
        Milestones in file order.
    """
    data = tomllib.loads(path.read_text())
    out = []
    for m in data["milestone"]:
        ids: list[str] = []
        for entry in m.get("requirements", []):
            ids += [i for i in expand_ids(entry) if i not in ids]
        out.append(
            Milestone(
                m["number"],
                m["name"],
                m["status"],
                ids,
                m.get("done_at", {}),
                m.get("waive", {}),
                m.get("waive_tests", {}),
            )
        )
    return out


def implemented_ids() -> dict[str, set[str]]:
    """Find the code that cites each requirement.

    Returns:
        Requirement id -> relative paths citing it.
    """
    found: dict[str, set[str]] = {}
    for path in source_files():
        cites, _ = citations(path, require_module_section=False)
        for c in cites:
            if c.rid:
                found.setdefault(c.rid, set()).add(str(path.relative_to(ROOT)))
    for pattern in COMMENT_GLOBS:
        for path in ROOT.glob(pattern):
            for m in COMMENT.finditer(path.read_text()):
                for rid in expand_ids(m["ids"]):
                    found.setdefault(rid, set()).add(str(path.relative_to(ROOT)))
    return found


def tested_ids() -> dict[str, set[str]]:
    """Find the tests named after each requirement.

    Returns:
        Requirement id -> test names.
    """
    found: dict[str, set[str]] = {}
    tests = [*(ROOT / "tests").rglob("test_*.py"), *(ROOT / "modules").glob("*/tests/test_*.py")]
    for path in tests:
        for m in TEST_RE.finditer(path.read_text()):
            rid = f"{m.group(2).upper()}-{int(m.group(3)):02d}"
            found.setdefault(rid, set()).add(m.group(1))
    return found


def analyse() -> dict[str, object]:
    """Compute the coverage of every milestone.

    Returns:
        Report data used by :func:`render` and :func:`issues`.
    """
    reqs = {k: v for k, v in load_requirements().items() if v["status"] == "active"}
    milestones = load_milestones()
    impl, tests = implemented_ids(), tested_ids()
    _, stale = check(source_files(), set(), load_requirements(), load_known_versions())
    stale_ids: dict[str, list[str]] = {}
    for line in dict.fromkeys(stale):
        rid = line.split(": ")[1].split(" ")[0]
        stale_ids.setdefault(rid, []).append(line)

    assigned: dict[str, int] = {}
    duplicates = []
    rows = []
    for m in milestones:
        for rid in m.ids:
            if rid in assigned:
                duplicates.append(rid)
            assigned[rid] = m.number
        active = [r for r in m.ids if r in reqs]
        unknown = [r for r in m.ids if r not in reqs]
        not_impl = [r for r in active if r not in impl and r not in m.waive]
        not_tested = [
            r for r in active if r not in tests and r not in m.waive and r not in m.waive_tests
        ]
        changed = []
        if m.status == "done":
            for r in active:
                done = m.done_at.get(SPEC_KEYS[r.split("-")[0]])
                if done and version_key(reqs[r]["changed_in"]) > version_key(done):
                    changed.append(r)
        rows.append(
            {
                "milestone": m,
                "active": active,
                "unknown": unknown,
                "not_impl": not_impl,
                "not_tested": not_tested,
                "changed": changed,
                "stale": [s for r in active for s in stale_ids.get(r, [])],
            }
        )
    unassigned = sorted(
        set(reqs) - set(assigned), key=lambda r: (r.split("-")[0], int(r.split("-")[1]))
    )
    return {"rows": rows, "unassigned": unassigned, "duplicates": duplicates, "stale": stale}


def render(data: dict[str, object]) -> str:
    """Render the report as Markdown.

    Args:
        data: Output of :func:`analyse`.

    Returns:
        Markdown text.
    """
    out = [
        "# Requirements coverage",
        "",
        "Generated by `tools/trace_report.py` (CORE-78). Implemented = cited by code; "
        "tested = cited by a test name; waivers count as covered.",
        "",
        "| # | Milestone | Status | Requirements | Implemented | Tested "
        "| Changed since done | Stale citations |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in data["rows"]:  # type: ignore[attr-defined]
        m = row["milestone"]
        n = len(row["active"])
        out.append(
            f"| {m.number} | {m.name} | {m.status} | {n} | {n - len(row['not_impl'])} | "
            f"{n - len(row['not_tested'])} | {len(row['changed'])} | {len(row['stale'])} |"
        )
    for row in data["rows"]:  # type: ignore[attr-defined]
        m = row["milestone"]
        if not any(row[k] for k in ("not_impl", "not_tested", "changed", "stale", "unknown")):
            continue
        out += ["", f"## Milestone {m.number}: {m.name}", ""]
        for key, label in (
            ("changed", "Changed since done"),
            ("not_impl", "Not implemented"),
            ("not_tested", "Not tested"),
            ("unknown", "Unknown or removed ids"),
        ):
            if row[key]:
                out.append(f"- **{label}:** " + ", ".join(row[key]))
        if row["stale"]:
            out.append("- **Stale citations:**")
            out += [f"  - {s}" for s in row["stale"]]
    if data["unassigned"]:
        out += ["", "## Not assigned to a milestone", "", ", ".join(data["unassigned"])]  # type: ignore[arg-type]
    if data["duplicates"]:
        out += ["", "## Assigned twice", "", ", ".join(data["duplicates"])]  # type: ignore[arg-type]
    return "\n".join(out) + "\n"


def issues(data: dict[str, object]) -> list[dict[str, object]]:
    """Build one issue per closed milestone (CORE-79).

    Args:
        data: Output of :func:`analyse`.

    Returns:
        ``{milestone, title, body, open}`` records.
    """
    out = []
    for row in data["rows"]:  # type: ignore[attr-defined]
        m = row["milestone"]
        if m.status != "done":
            continue
        todo = row["changed"] or row["not_impl"] or row["not_tested"] or row["stale"]
        body = [
            f"Milestone {m.number} ({m.name}) was closed at "
            + ", ".join(f"{k} {v}" for k, v in m.done_at.items())
            + ". The specs changed since then or coverage is incomplete (CORE-79).",
            "",
        ]
        for key, label in (
            ("changed", "Requirements added or changed since done"),
            ("not_impl", "Not implemented"),
            ("not_tested", "Not tested"),
        ):
            if row[key]:
                body.append(f"- **{label}:** " + ", ".join(row[key]))
        if row["stale"]:
            body.append("- **Stale citations to review:**")
            body += [f"  - {s}" for s in row["stale"]]
        body += [
            "",
            "Close by implementing and testing the items, then updating `done_at` "
            "in `docs/spec/milestones.toml`. Generated by `tools/trace_report.py`.",
        ]
        out.append(
            {
                "milestone": m.number,
                "title": f"Milestone {m.number}: spec changes to implement",
                "body": "\n".join(body),
                "open": bool(todo),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    """Print or write the report.

    Args:
        argv: Command-line arguments.

    Returns:
        Process exit code (1 when ids are unassigned, assigned twice or unknown).
    """
    parser = argparse.ArgumentParser(description="Requirements coverage (CORE-78, CORE-79)")
    parser.add_argument("--out", type=Path, help="write the Markdown report to this file")
    parser.add_argument("--issues", type=Path, help="write milestone issues as JSON")
    args = parser.parse_args(argv)
    data = analyse()
    text = render(data)
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    if args.issues:
        args.issues.write_text(json.dumps(issues(data), indent=2))
    bad = data["unassigned"] or data["duplicates"] or any(r["unknown"] for r in data["rows"])  # type: ignore[attr-defined]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
