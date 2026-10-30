"""Load a module's source tree for the checks.

Reads ``pyproject.toml``, the package's Python and static files, the README
and, once built, the wheel; it never imports the module.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import ast
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ModuleProject", "ToolError", "load_project"]

GROUP = "mm3.modules"


class ToolError(Exception):
    """The tool cannot run the check (exit code 2), as opposed to a module failing it."""


@dataclass
class ModuleProject:
    """A module's source tree.

    Attributes:
        root: Project directory.
        meta: Parsed ``pyproject.toml``.
        dist: Distribution name as written.
        version: Version from ``pyproject.toml`` (``None`` when dynamic).
        deps: ``project.dependencies``.
        entry_points: Entries of the ``mm3.modules`` group.
        pkg: Import package named by the entry point.
        pkg_dir: Its directory, ``None`` when not found.
        sources: Text of every file read, by path relative to ``root``.
        trees: Parsed Python files.
        syntax_errors: ``(path, line, message)`` of files that do not parse.
        js: JavaScript files by path relative to ``root``.
        css: Stylesheets by path relative to ``root``.
        readme: README text and its relative path.
        wheel: The built wheel.
        wheel_names: Files in the wheel.
        wheel_dir: The wheel extracted for catalog checks.
        metadata: The wheel's ``METADATA`` headers.
    """

    root: Path
    meta: dict[str, Any]
    dist: str
    version: str | None
    deps: list[str]
    entry_points: dict[str, str]
    pkg: str
    pkg_dir: Path | None
    sources: dict[str, str] = field(default_factory=dict)
    trees: dict[str, ast.Module] = field(default_factory=dict)
    syntax_errors: list[tuple[str, int, str]] = field(default_factory=list)
    js: dict[str, str] = field(default_factory=dict)
    css: dict[str, str] = field(default_factory=dict)
    readme: tuple[str, str] | None = None
    wheel: Path | None = None
    wheel_names: list[str] = field(default_factory=list)
    wheel_dir: Path | None = None
    metadata: dict[str, list[str]] = field(default_factory=dict)

    @property
    def check_options(self) -> dict[str, Any]:
        """The optional ``[tool.mm3.check]`` table (``config`` and ``grants`` for the kit)."""
        table = self.meta.get("tool", {}).get("mm3", {}).get("check", {})
        return table if isinstance(table, dict) else {}

    def rel(self, path: Path) -> str:
        """Return a path relative to the project root, with forward slashes."""
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def attach_wheel(self, wheel: Path, extract_to: Path) -> None:
        """Read the built wheel: its file list, metadata and an extracted copy.

        Args:
            wheel: The wheel.
            extract_to: Empty directory to extract into.
        """
        self.wheel = wheel
        with zipfile.ZipFile(wheel) as z:
            self.wheel_names = z.namelist()
            z.extractall(extract_to)
            meta = next((n for n in self.wheel_names if n.endswith(".dist-info/METADATA")), None)
            if meta is not None:
                for line in (
                    z.read(meta).decode("utf-8", "replace").split("\n\n", 1)[0].splitlines()
                ):
                    key, _, value = line.partition(": ")
                    self.metadata.setdefault(key, []).append(value)
        self.wheel_dir = extract_to


def load_project(path: Path) -> ModuleProject:
    """Read a module's source tree.

    Args:
        path: The project directory (the one holding ``pyproject.toml``).

    Returns:
        The loaded project.

    Raises:
        ToolError: The directory has no readable ``pyproject.toml``.
    """
    root = path.resolve()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise ToolError(f"{root} has no pyproject.toml; pass the module's project directory")
    try:
        meta = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolError(f"cannot read {pyproject}: {exc}") from exc
    project = meta.get("project", {})
    entries = project.get("entry-points", {}).get(GROUP, {})
    entries = {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}
    pkg = next((v.split(":")[0].split(".")[0] for v in entries.values()), "")
    mod = ModuleProject(
        root=root,
        meta=meta,
        dist=str(project.get("name", "")),
        version=project.get("version"),
        deps=[str(d) for d in project.get("dependencies", [])],
        entry_points=entries,
        pkg=pkg,
        pkg_dir=_find_package(root, pkg),
    )
    _read_sources(mod)
    return mod


def _find_package(root: Path, pkg: str) -> Path | None:
    for base in (root / "src", root):
        if pkg and (base / pkg).is_dir():
            return base / pkg
    return None


def _read_sources(mod: ModuleProject) -> None:
    if mod.pkg_dir is not None:
        for file in sorted(mod.pkg_dir.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts:
                continue
            rel = mod.rel(file)
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if file.suffix == ".py":
                mod.sources[rel] = text
                try:
                    mod.trees[rel] = ast.parse(text, filename=rel)
                except SyntaxError as exc:
                    mod.syntax_errors.append((rel, exc.lineno or 1, exc.msg))
            elif file.suffix in (".js", ".mjs") and "static" in file.relative_to(mod.pkg_dir).parts:
                mod.sources[rel] = mod.js[rel] = text
            elif file.suffix == ".css" and "static" in file.relative_to(mod.pkg_dir).parts:
                mod.sources[rel] = mod.css[rel] = text
    for name in ("README.md", "README.rst", "README.txt", "README"):
        file = mod.root / name
        if file.is_file():
            mod.readme = (name, file.read_text(encoding="utf-8", errors="replace"))
            break
