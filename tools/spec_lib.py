"""Shared helpers for the specification tooling.

Implements:
    CORE-75, CORE-76, CORE-77: Core spec 0.6, §10.3 Documentation and quality.

Parses the Markdown exports of the specs in ``docs/spec/``, requirement ids
and id ranges, spec versions and the generated ``requirements.toml``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = ROOT / "docs" / "spec"

SPECS: dict[str, dict[str, str]] = {
    "Core spec": {"file": "core.md", "prefixes": "CORE|WGT", "history": "13. Document history"},
    "Module Contract": {
        "file": "contract.md",
        "prefixes": "MOD",
        "history": "14. Document history",
    },
}
"""Spec name (as cited in code) -> export file, id prefixes, history heading."""

ID_RE = re.compile(r"\b(CORE|WGT|MOD)-(\d+)\b")
VERSION_RE = re.compile(r"\d+\.\d+(?:\s+rev\s+\d+)?")
DASHES = "-\u2013\u2014"  # hyphen, en dash, em dash


def version_key(version: str) -> tuple[int, ...]:
    """Turn a spec version into a sortable key.

    Args:
        version: ``"0.6"`` or ``"1.0 rev 6"``.

    Returns:
        A tuple such as ``(0, 6)`` or ``(1, 0, 6)``.

    Raises:
        ValueError: If the string is not a spec version.
    """
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\s+rev\s+(\d+))?", version.strip())
    if not match:
        raise ValueError(f"not a spec version: {version!r}")
    major, minor, rev = match.groups()
    return (int(major), int(minor)) + ((int(rev),) if rev else ())


def norm_id(prefix: str, number: int) -> str:
    """Format a requirement id the way the specs write it.

    Args:
        prefix: ``CORE``, ``WGT`` or ``MOD``.
        number: Requirement number.

    Returns:
        ``CORE-01`` for numbers below 10, ``CORE-120`` otherwise.
    """
    return f"{prefix}-{number:02d}"


def expand_ids(text: str) -> list[str]:
    """Extract every requirement id in a text, expanding ranges.

    Understands ``CORE-220-223`` (also with an en dash), ``CORE-180..186``, ``MOD-96-99`` and
    continuations such as ``CORE-21, 24, 25, 27-29``.

    Args:
        text: Free text.

    Returns:
        Ids in order of appearance, without duplicates.
    """
    text = text.replace("\\", "")
    rng = rf"\s*(?:\.\.|[{DASHES}])\s*(?:(?:CORE|WGT|MOD)-)?(\d+)"
    cont = rf",\s*\d+(?!\.\d)(?:{rng})?\b"
    token = re.compile(rf"\b(CORE|WGT|MOD)-(\d+)(?:{rng})?((?:{cont})*)")
    out: list[str] = []
    for m in token.finditer(text):
        prefix = m.group(1)
        parts = [(int(m.group(2)), int(m.group(3)) if m.group(3) else None)]
        for c in re.finditer(rf",\s*(\d+)(?:{rng})?", m.group(4) or ""):
            parts.append((int(c.group(1)), int(c.group(2)) if c.group(2) else None))
        for start, end in parts:
            for n in range(start, (end if end and end >= start else start) + 1):
                rid = norm_id(prefix, n)
                if rid not in out:
                    out.append(rid)
    return out


@dataclass
class SpecDoc:
    """One parsed spec export."""

    name: str
    version: str
    versions: list[str]
    history: dict[str, list[str]]
    requirements: dict[str, dict[str, str]] = field(default_factory=dict)


def _section_title(heading: str) -> tuple[str, str]:
    m = re.match(r"#+\s+(\d+(?:\.\d+)*)\.?\s+(.*)", heading)
    return (m.group(1), m.group(2).strip()) if m else ("", heading.lstrip("# ").strip())


def parse_spec(name: str, text: str) -> SpecDoc:
    """Parse one exported spec.

    A requirement is defined by a bullet starting with its id
    (``- CORE-21: ...``). Ids without such a bullet are defined by the first
    line that mentions them outside the decision tables of section 1 and
    outside the Document history.

    Args:
        name: Spec name as cited in code (a key of :data:`SPECS`).
        text: Markdown export.

    Returns:
        The parsed spec.

    Raises:
        ValueError: If the version line or the history table is missing.
    """
    meta = SPECS[name]
    prefixes = meta["prefixes"]
    m = re.search(r"Version\s+(" + VERSION_RE.pattern + r")", text)
    if not m:
        raise ValueError(f"{name}: no 'Version x.y' line")
    version = re.sub(r"\s+", " ", m.group(1))

    lines = text.splitlines()
    history: dict[str, list[str]] = {}
    versions: list[str] = []
    in_hist = False
    for line in lines:
        if line.startswith("## "):
            in_hist = meta["history"] in line
            continue
        if in_hist and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and VERSION_RE.fullmatch(cells[0]):
                ver = re.sub(r"\s+", " ", cells[0])
                versions.append(ver)
                history[ver] = [i for i in expand_ids(" ".join(cells[2:])) if re.match(prefixes, i)]
    if not versions:
        raise ValueError(f"{name}: no Document history rows")

    doc = SpecDoc(name=name, version=version, versions=versions, history=history)
    section, title, top = "", "", ""
    first_seen: dict[str, tuple[str, str, str]] = {}
    for line in lines:
        if line.startswith("#"):
            section, title = _section_title(line)
            top = section.split(".")[0]
            continue
        if top == "1" or f"{section}. {title}" == meta["history"]:
            continue
        bullet = re.match(rf"^\s*[-*]\s+((?:{prefixes})-\d+):\s*(.*)", line)
        if bullet:
            rid = bullet.group(1)
            doc.requirements[rid] = {"section": section, "title": title, "text": bullet.group(2)}
            continue
        for pm in ID_RE.finditer(line):
            rid = norm_id(pm.group(1), int(pm.group(2)))
            if re.match(prefixes, rid) and rid not in first_seen:
                first_seen[rid] = (section, title, line.strip())
    for rid, (sec, tit, txt) in first_seen.items():
        if rid not in doc.requirements:
            doc.requirements[rid] = {"section": sec, "title": tit, "text": txt}
    return doc


def load_spec_docs(spec_dir: Path = SPEC_DIR) -> dict[str, SpecDoc]:
    """Parse every spec export in ``spec_dir``.

    Args:
        spec_dir: Directory holding ``core.md`` and ``contract.md``.

    Returns:
        Spec name -> parsed spec.
    """
    return {
        name: parse_spec(name, (spec_dir / meta["file"]).read_text())
        for name, meta in SPECS.items()
    }


def load_requirements(spec_dir: Path = SPEC_DIR) -> dict[str, dict[str, str]]:
    """Read the generated ``requirements.toml``.

    Args:
        spec_dir: Directory holding the file.

    Returns:
        Requirement id -> record (``spec``, ``changed_in``, ``status``, ...).
    """
    path = spec_dir / "requirements.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    return {r["id"]: r for r in data.get("requirement", [])}


def load_known_versions(spec_dir: Path = SPEC_DIR) -> dict[str, list[str]]:
    """Read every version of every spec from ``requirements.toml``.

    Args:
        spec_dir: Directory holding the file.

    Returns:
        Spec name -> versions, oldest first; the last one is current.
    """
    path = spec_dir / "requirements.toml"
    data = tomllib.loads(path.read_text())
    return {s["name"]: list(s["versions"]) for s in data.get("spec", [])}
