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
    CORE-116: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-303: Core spec 0.22, §6.4 Web configuration editor.
    CORE-301: Core spec 0.27, §6.4 Web configuration editor.
    CORE-320: Core spec 0.25, §6.4 Web configuration editor.
"""

from __future__ import annotations

import contextlib
import copy
import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.tag import Tag
from ruamel.yaml.util import load_yaml_guess_indent

from mm3.core.config.records import SourceMap, ValidationRecord, make_record, pointer

__all__ = [
    "JSON_TAGS",
    "MAX_INCLUDE_DEPTH",
    "EnvRef",
    "LoadedConfig",
    "PatchError",
    "dump_plain",
    "include_target",
    "json_to_node",
    "load_yaml",
    "node_to_json",
    "read_yaml_file",
    "rt_dump",
    "rt_load",
    "rt_mask_strings",
    "rt_patch",
    "rt_unmask",
]

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


def rt_load(text: str) -> Any:
    """Parse YAML in round-trip mode, keeping comments, order and tags.

    Args:
        text: YAML text.

    Returns:
        The document (``CommentedMap``, ``CommentedSeq`` or a scalar).
    """
    return _yaml().load(text)


def rt_dump(doc: Any, like: str | None = None) -> str:
    """Write a round-trip document back to YAML text.

    Args:
        doc: A document from :func:`rt_load`, possibly edited.
        like: The text the document came from; its indentation of mappings
            and lists is kept, so an edit changes only the edited lines.

    Returns:
        The YAML text.
    """
    out = io.StringIO()
    y = _yaml()
    if like:
        with contextlib.suppress(Exception):
            _, indent, offset = load_yaml_guess_indent(like)
            if indent:
                offset = offset or 0
                y.indent(mapping=max(indent - offset, 2), sequence=indent, offset=offset)
    y.dump(doc, out)
    return out.getvalue()


def rt_mask_strings(text: str, secret: Callable[[str], bool], tag: str) -> tuple[str, list[str]]:
    """Replace every string scalar for which ``secret`` is true by an empty tagged scalar.

    Used to redact configuration files for diagnostics (CORE-116): a secret
    becomes ``!masked ''``, the marker of CORE-303, and comments, order and
    the other values stay as written.

    Args:
        text: YAML text.
        secret: Tells whether a string is a secret value.
        tag: The tag to write, such as ``!masked``.

    Returns:
        The text (unchanged when nothing matched) and the JSON Pointers of
        the replaced values.

    Raises:
        YAMLError: The text does not parse.

    Implements:
        CORE-116: Core spec 0.22, §10.5 Diagnostics and debugging.
    """
    doc = rt_load(text)
    found: list[str] = []

    def walk(node: Any, parts: tuple[str | int, ...]) -> Any:
        if _tag(node) is not None:
            return node  # !env and !include are shown as written (CORE-303)
        if isinstance(node, CommentedMap):
            for key in list(node):
                node[key] = walk(node[key], (*parts, str(key)))
        elif isinstance(node, CommentedSeq):
            for i in range(len(node)):
                node[i] = walk(node[i], (*parts, i))
        elif isinstance(node, str) and secret(str(node)):
            found.append(pointer(parts))
            return TaggedScalar(value="", style="'", tag=tag)
        return node

    doc = walk(doc, ())
    return (rt_dump(doc, text), found) if found else (text, [])


# --- JSON Patch on round-trip documents (CORE-301, CORE-320) ---------------------------

JSON_TAGS = ("!env", "!include", "!masked")
"""Tags a JSON value may carry as a one-key object (CORE-320)."""


class PatchError(ValueError):
    """A JSON Patch operation that cannot be applied, or a ``!masked`` node without a value.

    Attributes:
        index: Index of the failing operation, or ``None``.
        pointer: The JSON Pointer concerned, if any.
    """

    def __init__(self, message: str, *, index: int | None = None, pointer: str = "") -> None:
        super().__init__(message)
        self.index = index
        self.pointer = pointer


def json_to_node(value: Any) -> Any:
    """Turn a JSON value into round-trip nodes; one-key tag objects become tagged nodes.

    ``{"!env": "NAME"}``, ``{"!env": ["NAME", "default"]}``, ``{"!include": "f.yaml"}``
    and ``{"!masked": null}`` become the YAML tags (CORE-320).

    Args:
        value: Parsed JSON.

    Returns:
        A node for a round-trip document.

    Raises:
        PatchError: Another one-key object whose key starts with ``!``, or a
            tag object of the wrong shape.

    Implements:
        CORE-320: Core spec 0.25, §6.4 Web configuration editor.
    """
    if isinstance(value, dict):
        if len(value) == 1:
            ((key, inner),) = value.items()
            if isinstance(key, str) and key.startswith("!"):
                return _tagged(key, inner)
        out = CommentedMap()
        for k, v in value.items():
            out[str(k)] = json_to_node(v)
        return out
    if isinstance(value, list):
        return CommentedSeq([json_to_node(v) for v in value])
    return value


def _tagged(tag: str, inner: Any) -> Any:
    if tag == "!masked" and inner is None:
        return TaggedScalar(value="", style="'", tag="!masked")
    if tag == "!include" and isinstance(inner, str) and inner:
        return TaggedScalar(value=inner, style=None, tag="!include")
    if tag == "!env" and isinstance(inner, str) and inner:
        return TaggedScalar(value=inner, style=None, tag="!env")
    if (
        tag == "!env"
        and isinstance(inner, list)
        and len(inner) == 2
        and all(isinstance(v, str) for v in inner)
    ):
        seq = CommentedSeq(list(inner))
        seq.yaml_set_ctag(Tag(suffix="!env"))
        seq.fa.set_flow_style()
        return seq
    raise PatchError(f"{tag} is not a tagged node of CORE-320")


def node_to_json(node: Any) -> Any:
    """Turn a round-trip node into JSON; tagged nodes become one-key objects (CORE-320)."""
    tag = _tag(node)
    if tag is not None:
        if isinstance(node, TaggedScalar):
            value = node.value
            return {tag: None if tag == "!masked" else value}
        if isinstance(node, CommentedSeq):
            return {tag: [node_to_json(v) for v in node]}
    if isinstance(node, CommentedMap):
        return {str(k): node_to_json(v) for k, v in node.items()}
    if isinstance(node, CommentedSeq):
        return [node_to_json(v) for v in node]
    return _plain(node)


def _parts(ptr: str) -> list[str]:
    if ptr == "":
        return []
    if not ptr.startswith("/"):
        raise PatchError(f"{ptr!r} is not a JSON Pointer", pointer=ptr)
    return [p.replace("~1", "/").replace("~0", "~") for p in ptr.split("/")[1:]]


def _child(node: Any, part: str, ptr: str) -> Any:
    if _tag(node) is not None:
        raise PatchError(f"{ptr} points inside a tagged node", pointer=ptr)
    if isinstance(node, CommentedMap):
        if part not in node:
            raise PatchError(f"{ptr} does not exist", pointer=ptr)
        return node[part]
    if isinstance(node, CommentedSeq):
        if not part.isdigit() or int(part) >= len(node):
            raise PatchError(f"{ptr} does not exist", pointer=ptr)
        return node[int(part)]
    raise PatchError(f"{ptr} does not exist", pointer=ptr)


def _parent(doc: Any, ptr: str) -> tuple[Any, str]:
    parts = _parts(ptr)
    if not parts:
        raise PatchError("the document root cannot be changed", pointer=ptr)
    node = doc
    for part in parts[:-1]:
        node = _child(node, part, ptr)
    if _tag(node) is not None:
        raise PatchError(f"{ptr} points inside a tagged node", pointer=ptr)
    return node, parts[-1]


def _get(doc: Any, ptr: str) -> Any:
    node = doc
    for part in _parts(ptr):
        node = _child(node, part, ptr)
    return node


def _add(doc: Any, ptr: str, value: Any) -> None:
    parent, last = _parent(doc, ptr)
    if isinstance(parent, CommentedMap):
        parent[last] = value
    elif isinstance(parent, CommentedSeq):
        if last == "-":
            parent.append(value)
        elif last.isdigit() and int(last) <= len(parent):
            parent.insert(int(last), value)
        else:
            raise PatchError(f"{ptr}: index out of range", pointer=ptr)
    else:
        raise PatchError(f"{ptr}: parent is not an object or array", pointer=ptr)


def _remove(doc: Any, ptr: str) -> Any:
    parent, last = _parent(doc, ptr)
    value = _child(parent, last, ptr)
    if isinstance(parent, CommentedMap):
        del parent[last]
    else:
        del parent[int(last)]
    return value


def rt_patch(text: str, ops: Sequence[Mapping[str, Any]]) -> str:
    """Apply an RFC 6902 JSON Patch to a YAML text, keeping comments and key order.

    Supports ``add``, ``remove``, ``replace``, ``move``, ``copy`` and ``test``;
    values may hold the tagged nodes of CORE-320. Paths address the document
    before ``!env`` substitution and never enter a tagged node.

    Args:
        text: The file's YAML text.
        ops: The operations.

    Returns:
        The new YAML text.

    Raises:
        PatchError: An operation cannot be applied (``index`` names it).
        YAMLError: The text does not parse.

    Implements:
        CORE-301: Core spec 0.27, §6.4 Web configuration editor.
        CORE-320: Core spec 0.25, §6.4 Web configuration editor.
    """
    doc = rt_load(text)
    if doc is None:
        doc = CommentedMap()
    for index, op in enumerate(ops):
        try:
            if not isinstance(op, Mapping):
                raise PatchError("an operation must be an object")
            kind, path = op.get("op"), op.get("path")
            if not isinstance(path, str):
                raise PatchError("path must be a string")
            if kind in ("add", "replace", "test") and "value" not in op:
                raise PatchError(f"{kind} needs a value", pointer=path)
            if kind == "add":
                _add(doc, path, json_to_node(op["value"]))
            elif kind == "remove":
                _remove(doc, path)
            elif kind == "replace":
                parent, last = _parent(doc, path)
                _child(parent, last, path)
                if isinstance(parent, CommentedMap):
                    parent[last] = json_to_node(op["value"])
                else:
                    parent[int(last)] = json_to_node(op["value"])
            elif kind in ("move", "copy"):
                source = op.get("from")
                if not isinstance(source, str):
                    raise PatchError(f"{kind} needs from", pointer=path)
                if kind == "move" and (path + "/").startswith(source + "/") and path != source:
                    raise PatchError("cannot move a value into itself", pointer=path)
                value = _remove(doc, source) if kind == "move" else copy.deepcopy(_get(doc, source))
                _add(doc, path, value)
            elif kind == "test":
                if node_to_json(_get(doc, path)) != op["value"]:
                    raise PatchError(f"test failed at {path}", pointer=path)
            else:
                raise PatchError(f"unknown op {kind!r}", pointer=path)
        except PatchError as exc:
            raise PatchError(str(exc), index=index, pointer=exc.pointer) from None
    return rt_dump(doc, text)


def rt_unmask(text: str, base: str | None, secret: Callable[[str], bool]) -> str:
    """Put the values of ``base`` back where a draft holds ``!masked`` (CORE-303).

    Args:
        text: The draft file's YAML text.
        base: The same file as it is now, with real values; ``None`` for a new file.
        secret: Tells whether a value of ``base`` is a secret (one that views mask).

    Returns:
        The text with real values (unchanged when it holds no ``!masked`` node).

    Raises:
        PatchError: A ``!masked`` node at a pointer where ``base`` has no secret value.
        YAMLError: A text does not parse.

    Implements:
        CORE-303: Core spec 0.22, §6.4 Web configuration editor.
    """
    if "!masked" not in text:
        return text
    doc = rt_load(text)
    base_doc = rt_load(base) if base is not None else None
    found = False

    def original(parts: tuple[str | int, ...]) -> Any:
        node = base_doc
        for part in parts:
            if (isinstance(node, CommentedMap) and part in node) or (
                isinstance(node, CommentedSeq) and isinstance(part, int) and part < len(node)
            ):
                node = node[part]
            else:
                return None
        return node

    def walk(node: Any, parts: tuple[str | int, ...]) -> Any:
        nonlocal found
        if _tag(node) == "!masked":
            found = True
            value = original(parts)
            if not isinstance(value, str) or _tag(value) is not None or not secret(str(value)):
                raise PatchError(
                    f"{pointer(parts) or '/'} is !masked but has no secret value",
                    pointer=pointer(parts),
                )
            return value
        if isinstance(node, CommentedMap):
            for key in list(node):
                node[key] = walk(node[key], (*parts, str(key)))
        elif isinstance(node, CommentedSeq):
            for i in range(len(node)):
                node[i] = walk(node[i], (*parts, i))
        return node

    doc = walk(doc, ())
    return rt_dump(doc, text) if found else text


def include_target(node: Any) -> str | None:
    """Return the path of an ``!include`` node, or ``None`` for any other node.

    Args:
        node: A round-trip node.

    Returns:
        The path as written.
    """
    if _tag(node) == "!include" and isinstance(node, TaggedScalar):
        value = node.value
        return value if isinstance(value, str) else None
    return None


def dump_plain(value: Any) -> str:
    """Write plain data (dicts, lists, scalars) as block-style YAML.

    Args:
        value: The data.

    Returns:
        The YAML text; ``""`` for ``None``.
    """
    if value is None:
        return ""
    y = YAML(typ="safe", pure=True)
    y.default_flow_style = False
    y.sort_base_mapping_type_on_output = False  # type: ignore[assignment]
    out = io.StringIO()
    y.dump(value, out)
    return out.getvalue()
