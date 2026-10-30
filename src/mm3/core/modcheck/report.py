"""The result model of ``mm3 module check``: checks, findings and report formats.

A check is one (row, category) pair with a status; a report is the list of
checks plus the module's identity. The JSON form is the one of Module
Contract 12.1; ``junit`` writes one test case per check and ``summary`` the
text shown in a terminal.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 10, §12.1 mm3 module check.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from xml.etree import ElementTree as ET

from mm3.core.modcheck.catalog import CATALOG

__all__ = [
    "Check",
    "Finding",
    "Report",
    "apply_suppressions",
    "checks_from_findings",
    "details",
    "review_checks",
    "summary",
    "to_junit",
]

Status = Literal["pass", "fail", "warn", "review", "suppressed"]

_ALLOW = re.compile(r"(?:#|//)\s*mm3:\s*allow\s+(MOD-\d+)(?:\s+(\S.*?))?\s*(?:\*/)?$")


@dataclass
class Finding:
    """One thing a check found.

    Attributes:
        message: What is wrong, or what to confirm.
        file: Path relative to the module's root.
        line: 1-based line.
        severity: ``fail`` makes a MUST check fail; ``warn`` only warns.
        suppressed: The reason of a ``mm3: allow`` comment that silenced it.
    """

    message: str
    file: str | None = None
    line: int | None = None
    severity: Literal["fail", "warn"] = "fail"
    suppressed: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form ``{file, line, message}`` (plus ``suppressed``)."""
        out: dict[str, Any] = {"file": self.file, "line": self.line, "message": self.message}
        if self.suppressed is not None:
            out["suppressed"] = self.suppressed
        return out


@dataclass
class Check:
    """The outcome of one row in one category.

    Attributes:
        id: ``MOD-xx``.
        level: ``MUST`` or ``SHOULD``.
        category: ``static``, ``kit``, ``harness`` or ``review``.
        status: ``pass``, ``fail``, ``warn``, ``review`` or ``suppressed``.
        findings: Findings, including suppressed ones.
    """

    id: str
    level: Literal["MUST", "SHOULD"]
    category: str
    status: Status = "pass"
    findings: list[Finding] = field(default_factory=list)

    def settle(self) -> None:
        """Derive the status from the findings (not for review entries)."""
        if self.category == "review":
            self.status = "review"
            return
        active = [f for f in self.findings if f.suppressed is None]
        if not self.findings:
            self.status = "pass"
        elif not active:
            self.status = "suppressed"
        elif any(f.severity == "fail" for f in active):
            self.status = "fail" if self.level == "MUST" else "warn"
        else:
            self.status = "warn"

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form of the check."""
        return {
            "id": self.id,
            "level": self.level,
            "category": self.category,
            "status": self.status,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass
class Report:
    """A finished run.

    Attributes:
        tool_version: ``mm3-core`` version that ran the check.
        module: Distribution name.
        version: Module version.
        api_version: Module API version the module declares.
        checks: Every check, in section 11 order.
        stages: The stages that ran (``static``, ``kit``, ``harness``).
        notes: Remarks of the kit run, such as HTTP calls no mock answered.
    """

    tool_version: str
    module: str
    version: str
    api_version: str
    checks: list[Check] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def result(self) -> Literal["compliant", "non_compliant"]:
        """``non_compliant`` when any MUST check failed."""
        failed = any(c.level == "MUST" and c.status == "fail" for c in self.checks)
        return "non_compliant" if failed else "compliant"

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON report of Module Contract 12.1."""
        out: dict[str, Any] = {
            "tool_version": self.tool_version,
            "module": self.module,
            "version": self.version,
            "api_version": self.api_version,
            "result": self.result,
            "stages": list(self.stages),
            "checks": [c.as_dict() for c in self.checks],
        }
        if self.notes:
            out["notes"] = list(self.notes)
        return out

    def in_category(self, category: str) -> list[Check]:
        """Return the checks of one category."""
        return [c for c in self.checks if c.category == category]


def checks_from_findings(
    category: str,
    found: Mapping[str, list[Finding]],
    ids: Iterable[str] | None = None,
) -> list[Check]:
    """Make one check per row of a category from the findings collected.

    Args:
        category: ``static``, ``kit`` or ``harness``.
        found: Findings by MOD id.
        ids: Rows to report; default every row with the category.

    Returns:
        The settled checks in section 11 order.
    """
    wanted = set(ids) if ids is not None else None
    checks: list[Check] = []
    for mod_id, row in CATALOG.items():
        if category not in row.categories or (wanted is not None and mod_id not in wanted):
            continue
        check = Check(mod_id, row.level, category, findings=list(found.get(mod_id, [])))
        check.settle()
        checks.append(check)
    return checks


def review_checks() -> list[Check]:
    """Return the review list: one entry for every row with a review category."""
    out: list[Check] = []
    for mod_id, row in CATALOG.items():
        if "review" in row.categories:
            out.append(
                Check(mod_id, row.level, "review", "review", [Finding(row.review, severity="warn")])
            )
    return out


def apply_suppressions(checks: Iterable[Check], sources: Mapping[str, str]) -> None:
    """Silence SHOULD findings that have a ``mm3: allow`` comment (Contract 12.1).

    The comment sits on the finding's line or the line above, names the MOD
    id and gives a reason. A comment on a MUST finding has no effect; the
    finding says so.

    Args:
        checks: Checks to update (and settle again).
        sources: Source text by relative path.
    """
    lines = {path: text.splitlines() for path, text in sources.items()}
    for check in checks:
        for f in check.findings:
            if f.file is None or f.line is None or f.file not in lines:
                continue
            src = lines[f.file]
            for n in (f.line, f.line - 1):
                if not 1 <= n <= len(src):
                    continue
                m = _ALLOW.search(src[n - 1].strip()) or _find_allow(src[n - 1])
                if m is None or m.group(1) != check.id:
                    continue
                reason = (m.group(2) or "").strip()
                if not reason:
                    f.message += " (mm3: allow needs a reason)"
                elif check.level == "MUST":
                    f.message += " (mm3: allow has no effect on a MUST)"
                else:
                    f.suppressed = reason
                break
        check.settle()


def _find_allow(line: str) -> re.Match[str] | None:
    """Find a trailing ``# mm3: allow`` or ``// mm3: allow`` comment in a code line."""
    for marker in ("#", "//"):
        idx = line.find(f"{marker} mm3:")
        if idx < 0:
            idx = line.find(f"{marker}mm3:")
        if idx >= 0:
            m = _ALLOW.search(line[idx:].strip())
            if m:
                return m
    return None


def summary(report: Report) -> str:
    """Return the terminal summary of Module Contract 12.1.

    Args:
        report: The finished report.

    Returns:
        The text, one line per stage, without a trailing newline.
    """
    lines: list[str] = []
    labels = (("static", "Static"), ("kit", "Test kit"), ("harness", "Harness"))
    for cat, label in labels:
        if cat not in report.stages:
            if cat == "harness" and {"static", "kit"} <= set(report.stages):
                lines.append(f"{label:<11} not run")
            continue
        items = report.in_category(cat)
        passed = sum(c.status in ("pass", "suppressed") for c in items)
        failed = sum(c.status == "fail" for c in items)
        warned = sum(c.status == "warn" for c in items)
        text = f"{passed} passed, {failed} failed"
        if cat == "static" or warned:
            text += f", {warned} warnings"
        lines.append(f"{label:<11} {text}")
    reviews = len(report.in_category("review"))
    lines.append(f"{'Review':<11} {reviews} items to confirm (see report)")
    verdict = "COMPLIANT" if report.result == "compliant" else "NOT COMPLIANT"
    lines.append(f"{'Result':<11} {verdict} (API {report.api_version})")
    return "\n".join(lines)


def to_junit(report: Report) -> str:
    """Return the JUnit XML: one test case per check.

    MUST failures are failures, review items are skipped.

    Args:
        report: The finished report.

    Returns:
        The XML document.
    """
    root = ET.Element("testsuites")
    suite = ET.SubElement(
        root,
        "testsuite",
        name=f"mm3 module check {report.module}",
        tests=str(len(report.checks)),
        failures=str(sum(c.level == "MUST" and c.status == "fail" for c in report.checks)),
        skipped=str(sum(c.status == "review" for c in report.checks)),
    )
    for c in report.checks:
        case = ET.SubElement(suite, "testcase", classname=c.category, name=c.id)
        text = "\n".join(
            f"{f.file or ''}{':' + str(f.line) if f.line else ''} {f.message}".strip()
            for f in c.findings
        )
        if c.status == "fail":
            ET.SubElement(case, "failure", message=c.id).text = text
        elif c.status == "review":
            ET.SubElement(case, "skipped", message=text or "review")
        elif text:
            ET.SubElement(case, "system-out").text = text
    ET.indent(root)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def details(report: Report) -> str:
    """Return one line per finding that needs attention (failures, warnings).

    Args:
        report: The finished report.

    Returns:
        The text, empty when everything passed.
    """
    lines: list[str] = []
    for c in report.checks:
        if c.status not in ("fail", "warn"):
            continue
        tag = "FAIL" if c.status == "fail" else "warn"
        for f in c.findings:
            if f.suppressed is not None:
                continue
            where = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "")
            lines.append(f"{tag} {c.id} [{c.category}] {where + ' ' if where else ''}{f.message}")
    return "\n".join(lines)
