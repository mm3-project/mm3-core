"""One module instance's configuration section, taken from another config set.

A selective restore can bring back a single module instance by id (CORE-165):
its entry in the ``modules`` list is replaced by the archive's entry, or
appended when the instance no longer exists; every other file and every other
entry stays as it is (CORE-363). The installer has no module code, so it works
on the YAML only, through the engine's round-trip adapter (comments and order
survive); the engine validates the result when it starts (CORE-156).

Implements:
    CORE-165: Core spec 0.37, §10.8 Backup and restore.
    CORE-363: Core spec 0.37, §10.8 Backup and restore.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from mm3.core.config.loader import include_target, rt_dump, rt_load

__all__ = ["INSTANCE_ID", "MAIN_FILE", "SectionError", "instance_ids", "restore_instance"]

MAIN_FILE = "config.yaml"
"""The main file of ``/etc/mm3`` (CORE-30); the installer does not read ``--config``."""
INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SectionError(ValueError):
    """The instance or the modules list cannot be found."""


def _holding(files: Mapping[str, bytes]) -> tuple[str, Any, bool, Any]:
    """Find the ``modules`` list: ``(file, document, included, list)``."""
    raw = files.get(MAIN_FILE)
    if raw is None:
        raise SectionError(f"there is no {MAIN_FILE}")
    main = rt_load(raw.decode("utf-8")) or {}
    node = main.get("modules") if hasattr(main, "get") else None
    target = include_target(node) if node is not None else None
    if target is None:
        return MAIN_FILE, main, False, node
    rel = os.path.normpath(os.path.join(os.path.dirname(MAIN_FILE), target))
    data = files.get(rel)
    if data is None:
        raise SectionError(f"the modules list is in {rel}, which is missing")
    doc = rt_load(data.decode("utf-8"))
    return rel, doc, True, doc


def _id_of(item: Any) -> str | None:
    if not hasattr(item, "get"):
        return None
    ident = item.get("id")
    if ident is None:
        ident = item.get("module")
    return str(ident) if ident is not None else None


def instance_ids(files: Mapping[str, bytes]) -> list[str]:
    """The ids of the instances in a config set (an explicit ``id``, else the module name).

    Args:
        files: The config set, by path relative to the config directory.

    Returns:
        The ids in list order.

    Raises:
        SectionError: The set has no readable ``modules`` list.
    """
    _rel, _doc, _included, items = _holding(files)
    if items is None:
        return []
    return [i for i in (_id_of(item) for item in items) if i is not None]


def restore_instance(
    current: Mapping[str, bytes], source: Mapping[str, bytes], instance: str
) -> dict[str, bytes]:
    """Replace one instance's entry in the current set by the one in ``source``.

    Args:
        current: The config set on disk now.
        source: The config set of the backup.
        instance: The instance id.

    Returns:
        The files to write, by relative path: the one holding the ``modules`` list.

    Raises:
        SectionError: The backup has no such instance, or a set has no modules list.
    """
    _, _, _, src_items = _holding(source)
    item = next((i for i in (src_items or []) if _id_of(i) == instance), None)
    if item is None:
        raise SectionError(f"the backup has no module instance {instance!r}")
    rel, doc, included, items = _holding(current)
    if items is None:
        if included:
            raise SectionError("the modules list is missing")
        doc["modules"] = items = rt_load("[]")
    for index, existing in enumerate(items):
        if _id_of(existing) == instance:
            items[index] = item
            break
    else:
        items.append(item)
    text = rt_dump(items if included else doc, current[rel].decode("utf-8"))
    return {rel: text.encode("utf-8")}
