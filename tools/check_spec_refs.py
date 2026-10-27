"""Check that source files cite the specification they implement.

Implements:
    CORE-74: Core spec 0.4, §10.3 Documentation and quality.

Rules checked for every ``.py`` file under ``src/mm3``:

1. The module docstring has an ``Implements:`` section.
2. Each entry in it has the form
   ``<ids or ->: <spec name> <version>, §<section> <title>.``
   where ``<spec name> <version>`` is one of the versions in
   ``docs/spec/versions.toml``. Older versions are reported, so code
   is revisited when a spec changes.
3. Requirement ids look like ``CORE-12``, ``WGT-3``, ``MOD-40`` or ranges
   such as ``CORE-180..186``.

Exit code 0 when every file passes, 1 otherwise.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ID = r"(?:CORE|WGT|MOD)-\d+(?:\.\.\d+)?"
ENTRY = re.compile(
    rf"^(?P<ids>(?:{ID})(?:,\s*{ID})*|-):\s+"
    r"(?P<spec>.+?)\s+(?P<version>\d+\.\d+(?:\s+rev\s+\d+)?),\s+"
    r"§(?P<section>\d+(?:\.\d+)*)\s+(?P<title>.+?)\.?$"
)


def load_versions(root: Path) -> dict[str, str]:
    """Read the current spec versions.

    Args:
        root: Repository root.

    Returns:
        Mapping of spec name (e.g. ``"Core spec"``) to its current version.
    """
    data = tomllib.loads((root / "docs/spec/versions.toml").read_text())
    return {entry["name"]: entry["version"] for entry in data.values()}


def implements_entries(docstring: str) -> list[str] | None:
    """Extract the lines of the ``Implements:`` section of a docstring.

    Args:
        docstring: The module docstring.

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


def check_file(path: Path, versions: dict[str, str]) -> list[str]:
    """Check one source file.

    Args:
        path: File to check.
        versions: Current spec versions from :func:`load_versions`.

    Returns:
        Error messages for this file; empty when it passes.
    """
    rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    docstring = ast.get_docstring(ast.parse(path.read_text()))
    if not docstring:
        return [f"{rel}: missing module docstring"]
    entries = implements_entries(docstring)
    if entries is None:
        return [f"{rel}: module docstring has no 'Implements:' section"]
    if not entries:
        return [f"{rel}: 'Implements:' section is empty"]
    errors: list[str] = []
    for entry in entries:
        match = ENTRY.match(entry)
        if not match:
            errors.append(f"{rel}: malformed entry {entry!r}")
            continue
        spec, version = match["spec"], match["version"]
        if spec not in versions:
            errors.append(f"{rel}: unknown spec {spec!r} in {entry!r}")
        elif versions[spec] != version:
            errors.append(
                f"{rel}: cites {spec} {version}, current is {versions[spec]}; "
                "review the code against the new version and update the reference"
            )
    return errors


def main() -> int:
    """Check all source files and print the result.

    Returns:
        Process exit code.
    """
    versions = load_versions(ROOT)
    files = sorted((ROOT / "src" / "mm3").rglob("*.py"))
    errors = [err for path in files for err in check_file(path, versions)]
    for err in errors:
        print(f"ERROR {err}")
    print(f"{len(files)} files checked, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
