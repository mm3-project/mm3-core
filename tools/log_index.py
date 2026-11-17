"""Build the table of the issue and enhancement log from its entry files.

Each entry of the log is one file ``docs/log/entries/<ID>.md`` (``ISS-nnn`` for
an issue, ``ENH-nnn`` for an enhancement) that starts with a YAML front matter
holding the columns of the table, followed by the entry text under a heading
``# <ID> — <title>``. This tool checks every entry and writes the table into
``docs/log/README.md`` between the markers ``<!-- log-table:start -->`` and
``<!-- log-table:end -->``, ordered by ``seq`` (the order of reporting).

Usage::

    python tools/log_index.py           # rewrite the table
    python tools/log_index.py --check   # exit 1 if the table is out of date or an entry is wrong

The log ids are sources of spec changes, not requirement ids; the spec tools
ignore this directory.

Implements:
    -: Core spec 0.46, §10.3 Documentation and quality.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "docs" / "log"
START = "<!-- log-table:start -->"
END = "<!-- log-table:end -->"
ID_RE = re.compile(r"^(ISS|ENH)-\d{3}$")
FIELDS = (
    "id",
    "seq",
    "title",
    "type",
    "priority",
    "date",
    "area",
    "spec_versions",
    "requirements",
    "status",
    "resolved_in",
)
COLUMNS = (
    ("ID", "id"),
    ("Priority", "priority"),
    ("Date", "date"),
    ("Type", "type"),
    ("Area", "area"),
    ("Title", "title"),
    ("Spec versions at report", "spec_versions"),
    ("Requirements", "requirements"),
    ("Status", "status"),
    ("Resolved in", "resolved_in"),
)


@dataclass
class Entry:
    """One log entry: its front matter and the file it came from."""

    path: Path
    meta: dict[str, Any]


def read_entry(path: Path) -> tuple[Entry | None, list[str]]:
    """Read one entry file and check it.

    Args:
        path: The entry file.

    Returns:
        The entry (``None`` when it cannot be read) and the problems found.
    """
    text = path.read_text(encoding="utf-8")
    name = path.stem
    if not text.startswith("---\n"):
        return None, [f"{path.name}: no front matter"]
    try:
        head, body = text[4:].split("\n---\n", 1)
    except ValueError:
        return None, [f"{path.name}: front matter not closed"]
    meta = YAML(typ="safe").load(head) or {}
    if not isinstance(meta, dict):
        return None, [f"{path.name}: front matter is not a mapping"]
    problems = [f"{path.name}: missing field {f}" for f in FIELDS if f not in meta]
    unknown = sorted(set(meta) - set(FIELDS))
    problems += [f"{path.name}: unknown field {f}" for f in unknown]
    if meta.get("id") != name or not ID_RE.match(name):
        problems.append(f"{path.name}: id must be the file name (ISS-nnn or ENH-nnn)")
    if not isinstance(meta.get("seq"), int):
        problems.append(f"{path.name}: seq must be an integer")
    if not re.search(rf"(?m)^# {re.escape(name)} — \S", body):
        problems.append(f"{path.name}: heading '# {name} — <title>' missing")
    return Entry(path, {k: meta.get(k) for k in FIELDS}), problems


def load(log: Path = LOG) -> tuple[list[Entry], list[str]]:
    """Read every entry of the log.

    Args:
        log: The log directory.

    Returns:
        The entries in ``seq`` order and the problems found.
    """
    entries: list[Entry] = []
    problems: list[str] = []
    for path in sorted((log / "entries").glob("*.md")):
        entry, found = read_entry(path)
        problems += found
        if entry is not None:
            entries.append(entry)
    seqs = [e.meta["seq"] for e in entries]
    dupes = sorted({s for s in seqs if isinstance(s, int) and seqs.count(s) > 1})
    problems += [f"seq {s} is used twice" for s in dupes]
    entries.sort(key=lambda e: e.meta["seq"] if isinstance(e.meta["seq"], int) else 0)
    return entries, problems


def cell(value: Any) -> str:
    """Render a front-matter value as a table cell."""
    text = "—" if value in (None, "") else str(value)
    return " ".join(text.split()).replace("|", "\\|")


def table(entries: list[Entry]) -> str:
    """Render the log table.

    Args:
        entries: The entries in table order.

    Returns:
        The Markdown table, ending with a newline.
    """
    head = "| " + " | ".join(c for c, _ in COLUMNS) + " |"
    rule = "| " + " | ".join("---" for _ in COLUMNS) + " |"
    rows = ["| " + " | ".join(cell(e.meta[k]) for _, k in COLUMNS) + " |" for e in entries]
    return "\n".join([head, rule, *rows]) + "\n"


def render(readme: str, entries: list[Entry]) -> str:
    """Return the README with the table between the markers replaced.

    Raises:
        ValueError: The markers are missing.
    """
    try:
        before, rest = readme.split(START, 1)
        _, after = rest.split(END, 1)
    except ValueError as exc:
        raise ValueError(f"README needs the markers {START} and {END}") from exc
    return f"{before}{START}\n{table(entries)}{END}{after}"


def main(argv: list[str] | None = None) -> int:
    """Run the tool; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="fail if the table is out of date")
    parser.add_argument("--log", type=Path, default=LOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    entries, problems = load(args.log)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    readme_path = args.log / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    try:
        new = render(readme, entries)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if problems:
        return 1
    if new == readme:
        print(f"{len(entries)} entries, table up to date")
        return 0
    if args.check:
        print(
            "error: docs/log/README.md table is out of date; run tools/log_index.py",
            file=sys.stderr,
        )
        return 1
    readme_path.write_text(new, encoding="utf-8")
    print(f"{len(entries)} entries, table written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
