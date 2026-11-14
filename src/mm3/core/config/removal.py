"""Which instances a module removal disables, and the patch that disables them (CORE-358).

Before a removal is forwarded to the installer, a new configuration history version sets
``disabled: true`` on every enabled instance of the module, so that the instances are kept
and the configuration stays valid once the module is gone (CORE-203). The engine does this
in process for the admin and the CLI does it through the admin socket; both call
:func:`disable_ops` on the documents of ``GET /api/v1/config`` (CORE-301), so that they
agree on what counts as an instance of the module and where it lives.

An instance is an entry of ``modules``. The list may be inline in the main file, or the
whole of ``modules`` may be an ``!include`` of another file, and an entry may itself be an
``!include`` of a file that holds one instance (CORE-31); entries are followed through
includes up to the nesting limit of CORE-31.

Implements:
    CORE-358: Core spec 0.33, §10.7 Installer service.
    CORE-31: Core spec 0.5, §6.2 Rules.
"""

from __future__ import annotations

import posixpath
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["InstanceRef", "disable_ops", "module_instances"]

MAX_INCLUDE_DEPTH = 8
"""Nesting limit of ``!include`` (CORE-31)."""


@dataclass(frozen=True)
class InstanceRef:
    """Where one instance lives in the documents of the config set.

    Attributes:
        id: The instance id: its ``id`` key, else its module name (CORE-202).
        module: The module name (the ``module`` key).
        file: The file that holds the entry, relative to the config directory.
        parts: The path of the entry inside that file, as list indices and keys.
        disabled: Whether the entry already has ``disabled: true``.
    """

    id: str
    module: str
    file: str
    parts: tuple[str | int, ...]
    disabled: bool


def _include(node: Any) -> str | None:
    """The path of an ``!include`` node (one key, CORE-320), else ``None``."""
    if isinstance(node, dict) and len(node) == 1:
        target = node.get("!include")
        if isinstance(target, str):
            return target
    return None


def _resolve(source: str, target: str) -> str:
    """The file an ``!include`` in ``source`` names, relative to the config directory."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _entries(
    documents: Mapping[str, Any],
    file: str,
    parts: tuple[str | int, ...],
    node: Any,
    depth: int,
) -> Iterator[tuple[str, tuple[str | int, ...], dict[str, Any]]]:
    """Yield ``(file, parts, entry)`` for an entry of ``modules``, following ``!include``."""
    target = _include(node)
    if target is not None:
        if depth >= MAX_INCLUDE_DEPTH:
            return
        path = _resolve(file, target)
        yield from _entries(documents, path, (), documents.get(path), depth + 1)
        return
    if isinstance(node, dict):
        yield file, parts, node


def module_instances(
    documents: Mapping[str, Any], main: str, modules: Collection[str]
) -> list[InstanceRef]:
    """Return the instances of ``modules`` in the configuration, in configuration order.

    Args:
        documents: The YAML files of the config set as data, as ``GET /api/v1/config`` and
            ``POST /api/v1/config/patch`` give them (CORE-301): tagged nodes are one-key
            objects (CORE-320); a file that does not parse maps to ``None``.
        main: The path of the main file (the file with the role ``main``).
        modules: The module names (entry-point keys) to look for.

    Returns:
        One reference for every entry of ``modules`` whose ``module`` is in ``modules``,
        enabled or not; an entry whose ``module`` is not a plain string is not matched.

    Implements:
        CORE-358: Core spec 0.33, §10.7 Installer service.
        CORE-31: Core spec 0.5, §6.2 Rules.
    """
    doc = documents.get(main)
    if not isinstance(doc, dict):
        return []
    file, node = main, doc.get("modules")
    parts: tuple[str | int, ...] = ("modules",)
    target = _include(node)
    if target is not None:
        file = _resolve(main, target)
        parts, node = (), documents.get(file)
    if not isinstance(node, list):
        return []
    found: list[InstanceRef] = []
    for index, item in enumerate(node):
        for where, at, entry in _entries(documents, file, (*parts, index), item, 0):
            name = entry.get("module")
            if not isinstance(name, str) or name not in modules:
                continue
            ident = entry.get("id")
            found.append(
                InstanceRef(
                    id=ident if isinstance(ident, str) else name,
                    module=name,
                    file=where,
                    parts=at,
                    disabled=entry.get("disabled") is True,
                )
            )
    return found


def _pointer(parts: tuple[str | int, ...]) -> str:
    """An RFC 6901 pointer for ``parts``."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def disable_ops(
    documents: Mapping[str, Any], main: str, modules: Collection[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the patch that disables every enabled instance of ``modules`` (CORE-358).

    Args:
        documents: The YAML files of the config set as data (see :func:`module_instances`).
        main: The path of the main file.
        modules: The module names the removed distribution provides.

    Returns:
        ``(ops, ids)``: ``ops`` is ``[{file, patch}]`` as ``POST /api/v1/config/patch``
        takes it, one item per file, each patch an ``add`` of ``disabled: true`` (which
        replaces a ``disabled: false`` or a tagged value, and leaves comments and order of
        the rest alone); ``ids`` are the instance ids it disables, in configuration order.
        Both are empty when no instance of the module is enabled: then no version is written.

    Implements:
        CORE-358: Core spec 0.33, §10.7 Installer service.
    """
    per_file: dict[str, list[dict[str, Any]]] = {}
    ids: list[str] = []
    for ref in module_instances(documents, main, modules):
        if ref.disabled:
            continue
        per_file.setdefault(ref.file, []).append(
            {"op": "add", "path": _pointer((*ref.parts, "disabled")), "value": True}
        )
        ids.append(ref.id)
    return [{"file": f, "patch": patch} for f, patch in per_file.items()], ids
