"""Differences between two valid configurations: changed sections and the reload plan.

The plan says, per instance id, what hot reload does (core spec 6.3 table):
start, stop, replace, reconfigure, restart, update the locale, or only
re-layout the front-end; which ``engine`` keys change live or at the
next engine restart; and whether custom.css changed.

Implements:
    CORE-02: Core spec 0.9, §3 Engine components.
    -: Core spec 0.9, §6.3 Hot reload.
    CORE-201: Core spec 0.11, §6.2 Rules.
    CORE-216: Core spec 0.11, §6.8 Configuration history, undo and redo.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mm3.core.config.loader import LoadedConfig
from mm3.core.config.validate import ConfigResult, ResolvedInstance

__all__ = [
    "LIVE_SUBKEYS",
    "RESTART_KEYS",
    "ReloadPlan",
    "changed_sections",
    "plan_reload",
    "unsubstituted",
]

RESTART_KEYS = frozenset({"server", "admin", "threads"})
"""``engine`` keys applied only at the next engine restart (CORE-201).

``server.allow`` is the exception: it applies live, for connections made
afterwards, and is reported as the live key ``server.allow``.
"""

LIVE_SUBKEYS = {"server": frozenset({"allow"})}
"""Sub-keys of restart keys that apply live (CORE-201)."""

_TOP_SECTIONS = ("engine", "display", "layout", "pages", "backup")
_LAYOUT_KEYS = ("position", "positions", "pages", "appearance")


def unsubstituted(loaded: LoadedConfig) -> Any:
    """Return the loaded data with every ``!env`` value replaced by its reference.

    Args:
        loaded: Loader output.

    Returns:
        A copy of the resolved data where each ``!env`` node holds the
        reference as written (``!env NAME``), so secrets never take part in
        comparisons or history views (CORE-216, CORE-218).
    """
    data = copy.deepcopy(loaded.data)
    for ptr, ref in loaded.env_refs.items():
        parts = [p.replace("~1", "/").replace("~0", "~") for p in ptr.split("/")[1:]]
        if not parts:
            return ref.as_written()
        node: Any = data
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        last = parts[-1]
        if isinstance(node, list):
            node[int(last)] = ref.as_written()
        elif isinstance(node, dict):
            node[last] = ref.as_written()
    return data


def _instance_raw(data: Any, inst: ResolvedInstance) -> Any:
    if isinstance(data, Mapping) and isinstance(data.get("modules"), list):
        return data["modules"][inst.index]
    return None


def changed_sections(old: ConfigResult, new: ConfigResult) -> list[str]:
    """List the sections that differ between two configurations (CORE-216).

    Values are compared after includes are resolved and before ``!env``
    substitution, so a changed secret or formatting alone changes no section.

    Args:
        old: The configuration that was applied.
        new: The new configuration.

    Returns:
        Changed sections in a stable order: ``engine``, ``display``,
        ``layout``, ``pages``, ``backup``, then ``modules/<id>`` in file order (removed
        ones last), then ``modules/_order``, ``custom_css`` and
        ``widget_templates/<name>`` in name order.

    Implements:
        CORE-216: Core spec 0.11, §6.8 Configuration history, undo and redo.
    """
    a, b = unsubstituted(old.loaded), unsubstituted(new.loaded)
    am = a if isinstance(a, Mapping) else {}
    bm = b if isinstance(b, Mapping) else {}
    out = [s for s in _TOP_SECTIONS if am.get(s) != bm.get(s)]
    old_by_id = {i.id: i for i in old.instances}
    new_by_id = {i.id: i for i in new.instances}
    for iid, inst in new_by_id.items():
        before = old_by_id.get(iid)
        if before is None or _instance_raw(a, before) != _instance_raw(b, inst):
            out.append(f"modules/{iid}")
    out.extend(f"modules/{iid}" for iid in old_by_id if iid not in new_by_id)
    common = [i for i in old_by_id if i in new_by_id]
    if common != [i for i in new_by_id if i in old_by_id]:
        out.append("modules/_order")
    if old.assets.custom_css != new.assets.custom_css:
        out.append("custom_css")
    ta, tb = old.assets.templates, new.assets.templates
    out.extend(
        f"widget_templates/{n}" for n in sorted(ta.keys() | tb.keys()) if ta.get(n) != tb.get(n)
    )
    return out


@dataclass
class ReloadPlan:
    """What a hot reload does (core spec 6.3).

    Attributes:
        stop: Ids to stop and forget: removed, or now ``disabled``.
        replace: Ids whose module changed: stopped, then started as new.
        reconfigure: Ids whose module ``config`` changed, with the new raw config.
        restart: Ids to restart: grants changed.
        start: Ids to start: added, or no longer ``disabled``.
        locale: Ids whose instance ``locale`` changed, with the new value.
        relayout: Ids whose position, pages or appearance changed.
        engine_live: ``engine`` keys that changed and apply live.
        engine_restart: ``engine`` keys that changed and apply at the next restart.
        display: Whether the ``display`` block changed.
        layout: Whether the ``layout`` block changed.
        pages: Whether the ``pages`` block changed.
        custom_css: Whether custom.css changed (CORE-262).
        sections: Changed sections (CORE-216).
    """

    stop: list[str] = field(default_factory=list)
    replace: list[str] = field(default_factory=list)
    reconfigure: dict[str, dict[str, Any]] = field(default_factory=dict)
    restart: list[str] = field(default_factory=list)
    start: list[str] = field(default_factory=list)
    locale: dict[str, str | None] = field(default_factory=dict)
    relayout: list[str] = field(default_factory=list)
    engine_live: set[str] = field(default_factory=set)
    engine_restart: set[str] = field(default_factory=set)
    display: bool = False
    layout: bool = False
    pages: bool = False
    custom_css: bool = False
    sections: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """True when applying the plan would change nothing."""
        return not (
            self.stop
            or self.replace
            or self.reconfigure
            or self.restart
            or self.start
            or self.locale
            or self.relayout
            or self.engine_live
            or self.engine_restart
            or self.display
            or self.layout
            or self.pages
            or self.custom_css
            or self.sections
        )


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


def plan_reload(old: ConfigResult, new: ConfigResult) -> ReloadPlan:
    """Compute the hot reload plan between two valid configurations.

    Args:
        old: The configuration that was applied.
        new: The new configuration; both must be valid.

    Returns:
        The plan.

    Raises:
        ValueError: One of the configurations has errors.

    Implements:
        -: Core spec 0.9, §6.3 Hot reload.
        CORE-201: Core spec 0.11, §6.2 Rules.
    """
    if old.config is None or new.config is None:
        raise ValueError("both configurations must be valid")
    plan = ReloadPlan(sections=changed_sections(old, new))
    old_engine = old.config.engine.model_dump(mode="json")
    new_engine = new.config.engine.model_dump(mode="json")
    for key in old_engine.keys() | new_engine.keys():
        before, after = old_engine.get(key), new_engine.get(key)
        if before == after:
            continue
        if key not in RESTART_KEYS:
            plan.engine_live.add(key)
            continue
        live = LIVE_SUBKEYS.get(key, frozenset())
        was = before if isinstance(before, dict) else {}
        now = after if isinstance(after, dict) else {}
        changed = {k for k in was.keys() | now.keys() if was.get(k) != now.get(k)}
        plan.engine_live.update(f"{key}.{k}" for k in changed & live)
        if changed - live:
            plan.engine_restart.add(key)
    plan.display = _dump(old.config.display) != _dump(new.config.display)
    plan.layout = _dump(old.config.layout) != _dump(new.config.layout)
    plan.pages = _dump(old.config.pages) != _dump(new.config.pages)
    plan.custom_css = old.assets.custom_css != new.assets.custom_css

    old_by_id = {i.id: i for i in old.instances}
    for inst in new.instances:
        before = old_by_id.get(inst.id)
        if before is None or not before.enabled:
            if inst.enabled:
                plan.start.append(inst.id)
            continue
        if not inst.enabled:
            plan.stop.append(inst.id)
            continue
        if before.module != inst.module:
            plan.replace.append(inst.id)
            continue
        a, b = before.spec, inst.spec
        if a.grants != b.grants:
            plan.restart.append(inst.id)
        elif a.config != b.config or before.config != inst.config:
            plan.reconfigure[inst.id] = dict(b.config)
        if a.locale != b.locale:
            plan.locale[inst.id] = b.locale
        if any(getattr(a, k) != getattr(b, k) for k in _LAYOUT_KEYS):
            plan.relayout.append(inst.id)
    new_ids = {i.id for i in new.instances}
    plan.stop.extend(i.id for i in old.instances if i.enabled and i.id not in new_ids)
    return plan
