"""Check that code cites the specification it implements, per requirement.

Implements:
    CORE-74, CORE-77: Core spec 0.6, §10.3 Documentation and quality.

Checked for every ``.py`` file under ``src/mm3`` and ``tools/``:

1. Files under ``src/mm3`` have an ``Implements:`` section in their module
   docstring.
2. Each entry has the form ``<ids or ->: <spec name> <version>, §<section> <title>.``
   and comments use ``# Implements <ids> (<spec name> <version>, §<section> <title>)``.
3. The spec and version exist in ``docs/spec/requirements.toml``; every cited
   id exists, is active and belongs to that spec.
4. A citation older than the requirement's ``changed_in`` is *stale*: the code
   must be reviewed against the new text. Stale citations are errors only in
   files changed since ``--base`` (the files a pull request touches);
   elsewhere they are warnings, also reported by ``tools/trace_report.py``.

Exit code 0 when there are no errors, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from spec_lib import ROOT, expand_ids, load_known_versions, load_requirements, version_key

ID = r"(?:CORE|WGT|MOD)-\d+(?:\.\.\d+)?"
ENTRY = re.compile(
    rf"^(?P<ids>(?:{ID})(?:,\s*{ID})*|-):\s+"
    r"(?P<spec>.+?)\s+(?P<version>\d+\.\d+(?:\s+rev\s+\d+)?),\s+"
    r"§(?P<section>\d+(?:\.\d+)*)\s+(?P<title>.+?)\.?$"
)
COMMENT = re.compile(
    rf"(?:#|//|/\*)\s*Implements\s+(?P<ids>(?:{ID})(?:,\s*{ID})*)\s+\("
    r"(?P<spec>[^)]+?)\s+(?P<version>\d+\.\d+(?:\s+rev\s+\d+)?),\s+§[^)]*\)"
)
SPEC_OF = {"CORE": "Core spec", "WGT": "Core spec", "MOD": "Module Contract"}
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class Citation:
    """One cited requirement (``rid`` is None for a section-only entry)."""

    path: Path
    rid: str | None
    spec: str
    version: str


def implements_entries(docstring: str) -> list[str] | None:
    """Extract the lines of the ``Implements:`` section of a docstring.

    Args:
        docstring: A docstring.

    Returns:
        The entry lines (continuation lines joined), or ``None`` when the
        section is missing.
    """
    lines = docstring.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "Implements:":
            indent = len(line) - len(line.lstrip())
            entries: list[str] = []
            for raw in lines[i + 1 :]:
                if not raw.strip():
                    break
                depth = len(raw) - len(raw.lstrip())
                if depth <= indent:
                    break
                if entries and depth > indent + 4:
                    entries[-1] += " " + raw.strip()
                else:
                    entries.append(raw.strip())
            return entries
    return None


def _rel(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def citations(
    path: Path, *, require_module_section: bool = True
) -> tuple[list[Citation], list[str]]:
    """Collect every citation in a source file.

    Reads the ``Implements:`` sections of the module, class and function
    docstrings and the ``# Implements`` comments of a Python file, and the
    ``// Implements`` and ``/* Implements`` comments of a front-end file.

    Args:
        path: Python, JavaScript or CSS file.
        require_module_section: Report a missing module-level section.

    Returns:
        The citations and format errors.
    """
    source = path.read_text()
    found: list[Citation] = []
    errors: list[str] = []
    if path.suffix != ".py":
        for m in COMMENT.finditer(source):
            spec = m["spec"].strip()
            found += [Citation(path, rid, spec, m["version"]) for rid in expand_ids(m["ids"])]
        if require_module_section and not found:
            errors.append(f"{_rel(path)}: no 'Implements' comment")
        return found, errors
    tree = ast.parse(source)
    for node in [tree, *[n for n in ast.walk(tree) if isinstance(n, DEFS)]]:
        doc = ast.get_docstring(node)
        entries = implements_entries(doc) if doc else None
        if node is tree and require_module_section and not entries:
            errors.append(f"{_rel(path)}: module docstring has no 'Implements:' section")
        for entry in entries or []:
            m = ENTRY.match(entry)
            if not m:
                errors.append(f"{_rel(path)}: malformed entry {entry!r}")
                continue
            ids: list[str | None] = [] if m["ids"] == "-" else list(expand_ids(m["ids"]))
            found += [Citation(path, rid, m["spec"], m["version"]) for rid in ids or [None]]
    for m in COMMENT.finditer(source):
        spec = m["spec"].strip()
        found += [Citation(path, rid, spec, m["version"]) for rid in expand_ids(m["ids"])]
    return found, errors


def check(
    files: list[Path],
    changed: set[Path],
    requirements: dict[str, dict[str, str]],
    versions: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    """Check the citations in ``files``.

    Args:
        files: Python files to check.
        changed: Resolved paths where stale citations are errors.
        requirements: Records from ``requirements.toml``.
        versions: Known versions per spec.

    Returns:
        Errors and warnings.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for path in files:
        cites, errs = citations(path, require_module_section="src" in _rel(path).parts)
        errors += errs
        for c in cites:
            where = f"{_rel(path)}: {c.rid or '-'} ({c.spec} {c.version})"
            if c.spec not in versions:
                errors.append(f"{where}: unknown spec")
            elif c.version not in versions[c.spec]:
                errors.append(f"{where}: version does not exist")
            elif c.rid is None:
                continue
            elif (req := requirements.get(c.rid)) is None or SPEC_OF[c.rid.split("-")[0]] != c.spec:
                errors.append(f"{where}: unknown requirement")
            elif req["status"] != "active":
                errors.append(f"{where}: requirement removed in {req.get('removed_in')}")
            elif version_key(c.version) < version_key(req["changed_in"]):
                msg = (
                    f"{where}: changed in {req['changed_in']}; "
                    "review the code, then update the citation"
                )
                (errors if path.resolve() in changed else warnings).append(msg)
    return errors, warnings


def changed_files(base: str) -> set[Path]:
    """List the files changed since the merge base with ``base``.

    Args:
        base: Git ref, for example ``origin/main``.

    Returns:
        Resolved paths of the changed files.
    """
    out = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", f"{base}...HEAD"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {(ROOT / line).resolve() for line in out.split()}


def source_files() -> list[Path]:
    """List the files whose citations are checked.

    Returns:
        Python files under ``src/mm3`` and ``tools``, and the engine's
        front-end files (``static/*.js``, ``static/*.css``; vendored
        libraries are not ours to cite).
    """
    src = ROOT / "src" / "mm3"
    front = sorted(src.rglob("static/*.js")) + sorted(src.rglob("static/*.css"))
    return sorted(src.rglob("*.py")) + sorted((ROOT / "tools").glob("*.py")) + front


def main(argv: list[str] | None = None) -> int:
    """Check the repository.

    Args:
        argv: Command-line arguments.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="Check spec citations (CORE-74, CORE-77)")
    parser.add_argument("--base", help="git ref; stale citations in files changed since it fail")
    args = parser.parse_args(argv)
    files = source_files()
    changed = changed_files(args.base) if args.base else set()
    errors, warnings = check(files, changed, load_requirements(), load_known_versions())
    for w in warnings:
        print(f"WARNING {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"{len(files)} files checked, {len(errors)} errors, {len(warnings)} stale citations")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
