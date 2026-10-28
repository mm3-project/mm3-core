"""YAML loading with ``!env`` and ``!include``: the single ruamel.yaml adapter.

The loader reads the main file and every included file in round-trip mode
(no code execution), resolves the two MM3 tags, and returns plain Python
data together with a source map from JSON Pointers to file, line and column.
Problems become records instead of exceptions.

Implements:
    CORE-31: Core spec 0.8, §6.2 Rules.
    CORE-200: Core spec 0.8, §6.2 Rules.
    CORE-206: Core spec 0.8, §6.2 Rules.
    -: Core spec 0.8, §10.9 Dependency management.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from mm3.core.config.records import SourceMap, ValidationRecord, make_record, pointer

__all__ = ["MAX_INCLUDE_DEPTH", "EnvRef", "LoadedConfig", "load_yaml", "read_yaml_file"]

MAX_INCLUDE_DEPTH = 8
_DUP_KEY = re.compile(r'duplicate key "(.*?)"')


@dataclass(frozen=True)
class EnvRef:
    """An ``!env`` reference as written.

    Attributes:
        name: Variable name.
        default: Default, or ``None`` when there is none.
    """

    name: str
    default: str | None = None

    def as_written(self) -> str:
        """Return the reference in YAML form, for history views (CORE-218).

        Returns:
            ``!env NAME`` or ``!env [NAME, default]``.
        """
        return (
            f"!env {self.name}" if self.default is None else f"!env [{self.name}, {self.default}]"
        )


@dataclass
class LoadedConfig:
    """Result of loading a configuration file.

    Attributes:
        main: The main file.
        files: Every file read: the main file first, then includes.
        data: Resolved plain data; ``None`` when the main file could not be parsed.
        sources: Positions of keys and values.
        env_refs: ``!env`` references by pointer, as written.
        reported: Pointers whose problem is already a record (skip their
            schema errors).
        records: Problems found while loading.
    """

    main: Path
    files: list[Path] = field(default_factory=list)
    data: Any = None
    sources: SourceMap = field(default_factory=SourceMap)
    env_refs: dict[str, EnvRef] = field(default_factory=dict)
    reported: set[str] = field(default_factory=set)
    records: list[ValidationRecord] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        """True when nothing could be parsed from the main file."""
        return self.data is None and any(r.severity == "error" for r in self.records)


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.allow_duplicate_keys = False
    return y


def read_yaml_file(path: Path) -> tuple[Any, ValidationRecord | None]:
    """Parse one YAML file in round-trip mode.

    Args:
        path: The file.

    Returns:
        ``(document, None)``, or ``(None, record)`` when the file cannot be
        read or parsed.

    Implements:
        CORE-206: Core spec 0.8, §6.2 Rules.
    """
    file = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, make_record("CORE_CFG_FILE_UNREADABLE", "", {"file": file}, file=file)
    try:
        return _yaml().load(text), None
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line, col = (mark.line + 1, mark.column + 1) if mark else (0, 0)
        if "duplicate key" in str(exc):
            m = _DUP_KEY.search(str(exc))
            params = {"key": m.group(1) if m else ""}
            return None, make_record(
                "CORE_CFG_DUPLICATE_KEY", "", params, file=file, line=line, col=col
            )
        reason = exc.problem or str(exc).splitlines()[0]
        return None, make_record(
            "CORE_CFG_YAML_SYNTAX", "", {"reason": reason}, file=file, line=line, col=col
        )
    except YAMLError as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return None, make_record("CORE_CFG_YAML_SYNTAX", "", {"reason": reason}, file=file)


_CORE_TAGS = frozenset(
    f"tag:yaml.org,2002:{t}"
    for t in ("map", "seq", "str", "int", "float", "bool", "null", "timestamp", "omap", "set")
)


def _tag(node: Any) -> str | None:
    tag = getattr(node, "tag", None)
    value = getattr(tag, "value", None)
    if not isinstance(value, str) or not value or value in _CORE_TAGS:
        return None
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


class _Loader:
    def __init__(
        self,
        main: Path,
        env_file: Mapping[str, str],
        environ: Mapping[str, str],
    ) -> None:
        self.result = LoadedConfig(main=main)
        self.root_dir = main.parent.resolve()
        self.env_file = env_file
        self.environ = environ

    def error(
        self,
        code: str,
        parts: list[str | int],
        params: dict[str, Any],
        file: str,
        line: int,
        col: int,
    ) -> None:
        ptr = pointer(parts)
        self.result.records.append(make_record(code, ptr, params, file=file, line=line, col=col))
        self.result.reported.add(ptr)

    def load(self) -> None:
        main = self.result.main
        doc, rec = read_yaml_file(main)
        self.result.files.append(main)
        if rec is not None:
            self.result.records.append(rec)
            return
        self.result.sources.add("", str(main), 1, 1)
        self.result.data = self.convert(doc, str(main), [], [main.resolve()], 1, 1)

    def convert(
        self, node: Any, file: str, parts: list[str | int], stack: list[Path], line: int, col: int
    ) -> Any:
        tag = _tag(node)
        if tag == "!env":
            return self.env(node, file, parts, line, col)
        if tag == "!include":
            return self.include(node, file, parts, stack, line, col)
        if tag is not None:
            self.error("CORE_CFG_YAML_TAG", parts, {"tag": tag}, file, line, col)
            return None
        if isinstance(node, CommentedMap):
            out: dict[Any, Any] = {}
            for key in node:
                child = [*parts, _plain(key)]
                kl, kc = node.lc.key(key)
                vl, vc = node.lc.value(key)
                ptr = pointer(child)
                self.result.sources.add(ptr, file, kl + 1, kc + 1, key=True)
                self.result.sources.add(ptr, file, vl + 1, vc + 1)
                out[_plain(key)] = self.convert(node[key], file, child, stack, vl + 1, vc + 1)
            return out
        if isinstance(node, CommentedSeq):
            items: list[Any] = []
            for i, item in enumerate(node):
                il, ic = node.lc.item(i)
                self.result.sources.add(pointer([*parts, i]), file, il + 1, ic + 1)
                items.append(self.convert(item, file, [*parts, i], stack, il + 1, ic + 1))
            return items
        return _plain(node)

    def env(self, node: Any, file: str, parts: list[str | int], line: int, col: int) -> Any:
        name: Any
        default: str | None = None
        if isinstance(node, TaggedScalar):
            name = node.value
        elif isinstance(node, CommentedSeq) and len(node) == 2:
            name, raw_default = node[0], node[1]
            default = "" if raw_default is None else str(_plain(raw_default))
        else:
            name = None
        if not isinstance(name, str) or not name:
            self.error(
                "CORE_CFG_TYPE",
                parts,
                {"expected": "!env NAME or !env [NAME, default]"},
                file,
                line,
                col,
            )
            return None
        ref = EnvRef(str(name), default)
        self.result.env_refs[pointer(parts)] = ref
        if ref.name in self.env_file:
            return self.env_file[ref.name]
        if ref.name in self.environ:
            return self.environ[ref.name]
        if default is not None:
            return default
        self.error("CORE_CFG_ENV_MISSING", parts, {"name": ref.name}, file, line, col)
        return None

    def include(
        self, node: Any, file: str, parts: list[str | int], stack: list[Path], line: int, col: int
    ) -> Any:
        value = node.value if isinstance(node, TaggedScalar) else None
        if not isinstance(value, str) or not value:
            self.error("CORE_CFG_TYPE", parts, {"expected": "!include path"}, file, line, col)
            return None
        target = (Path(file).parent / value).resolve()
        if not target.is_relative_to(self.root_dir):
            self.error("CORE_CFG_INCLUDE_OUTSIDE", parts, {"path": value}, file, line, col)
            return None
        if target in stack:
            self.error("CORE_CFG_INCLUDE_CYCLE", parts, {"path": value}, file, line, col)
            return None
        if len(stack) > MAX_INCLUDE_DEPTH:
            self.error(
                "CORE_CFG_INCLUDE_DEPTH", parts, {"limit": MAX_INCLUDE_DEPTH}, file, line, col
            )
            return None
        shown = Path(file).parent / value
        doc, rec = read_yaml_file(shown)
        if target not in self.result.files and shown not in self.result.files:
            self.result.files.append(shown)
        if rec is not None:
            if rec.code == "CORE_CFG_FILE_UNREADABLE":
                self.error(rec.code, parts, dict(rec.params), file, line, col)
            else:
                self.result.records.append(
                    make_record(
                        rec.code,
                        pointer(parts),
                        rec.params,
                        file=rec.file,
                        line=rec.line,
                        col=rec.col,
                    )
                )
                self.result.reported.add(pointer(parts))
            return None
        self.result.sources.add(pointer(parts), str(shown), 1, 1)
        return self.convert(doc, str(shown), parts, [*stack, target], 1, 1)


def load_yaml(
    path: Path,
    *,
    env_file: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """Load a configuration file with its includes and ``!env`` values.

    Args:
        path: The main file.
        env_file: Variables from the env file; they take precedence.
        environ: The process environment.

    Returns:
        The loaded data, source map and records.

    Implements:
        CORE-31: Core spec 0.8, §6.2 Rules.
        CORE-200: Core spec 0.8, §6.2 Rules.
    """
    loader = _Loader(path, env_file or {}, environ or {})
    loader.load()
    return loader.result
