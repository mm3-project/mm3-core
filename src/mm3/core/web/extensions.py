"""Admin extensions: one admin page per module that brings its own element (CORE-329).

A module that declares ``admin_element`` and the capability
``admin.extension`` and has at least one instance in the running
configuration (enabled or disabled) gets the page ``/admin/ext/<module>`` on
the admin listener. Its ``admin_scripts`` and the other files of its
``static/`` directory are served under ``/admin/ext/<module>/<version>/``
there, and nowhere else (the shell's ``/modules/`` route leaves the admin
scripts out). A module that declares ``admin_element`` without the capability
gets no page; the engine logs a warning once.

Implements:
    CORE-329, CORE-330: Core spec 0.29, §11.1 Engine additions.
    CORE-102: Core spec 0.1, §11.1 Engine additions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mm3.core.i18n.catalogs import I18n
from mm3.core.logging import get_logger
from mm3.core.registry import Registry
from mm3.core.web.view import ModuleFiles, module_files

if TYPE_CHECKING:
    from mm3.core.config.validate import ConfigResult
    from mm3.core.supervisor import Supervisor

__all__ = ["Extension", "admin_extensions", "extension_page_data", "is_admin_script"]

CAPABILITY = "admin.extension"

_log = get_logger(subsystem="web.extensions")
_warned: set[str] = set()


@dataclass(frozen=True)
class Extension:
    """One module's admin page.

    Attributes:
        module: Module name.
        element: Its ``admin_element`` tag.
        scripts: Its ``admin_scripts``, relative to ``static/``.
        files: Its version and static directory.
        instances: Ids of its instances in the running configuration.
    """

    module: str
    element: str
    scripts: tuple[str, ...]
    files: ModuleFiles
    instances: tuple[str, ...]

    @property
    def base(self) -> str:
        """URL prefix of the module's files on the admin page."""
        return f"/admin/ext/{self.module}/{self.files.version}/"


def admin_extensions(registry: Registry, result: ConfigResult) -> list[Extension]:
    """List the admin pages of the running configuration, by module name.

    Args:
        registry: Installed modules.
        result: The running configuration.

    Returns:
        The extensions, sorted by module name.
    """
    by_module: dict[str, list[str]] = {}
    for inst in result.instances:
        by_module.setdefault(inst.module, []).append(inst.id)
    out: list[Extension] = []
    for name in sorted(by_module):
        entry = registry.get(name)
        cls = entry.cls if entry is not None else None
        if cls is None or not cls.admin_element:
            continue
        if CAPABILITY not in cls.capabilities:
            if name not in _warned:
                _warned.add(name)
                _log.warning("admin_extension_undeclared", module=name, capability=CAPABILITY)
            continue
        files = module_files(cls, entry.distribution if entry else None)
        if files.static is None:
            continue
        out.append(
            Extension(
                name, cls.admin_element, tuple(cls.admin_scripts), files, tuple(by_module[name])
            )
        )
    return out


def is_admin_script(registry: Registry, module: str, path: str) -> bool:
    """Whether ``path`` under the module's ``static/`` is one of its admin scripts."""
    entry = registry.get(module)
    cls = entry.cls if entry is not None else None
    return cls is not None and path.lstrip("/") in {s.lstrip("/") for s in cls.admin_scripts}


def header_label(i18n: I18n | None, module: str, chain: Sequence[str]) -> str:
    """The link text: the module's ``HEADER`` text (MOD-67), else its name."""
    if i18n is not None:
        text = i18n.lookup(module, tuple(chain), "HEADER")
        if isinstance(text, str) and text:
            return text
    return module


def extension_page_data(
    ext: Extension, supervisor: Supervisor | None, enabled: Callable[[str], bool]
) -> dict[str, Any]:
    """What the page hands the element, as data attributes (CORE-330).

    Args:
        ext: The extension.
        supervisor: For the instances' states.
        enabled: Whether an instance is enabled in the running configuration.

    Returns:
        ``{module, element, base, scripts, instances: [{id, enabled, state}]}``.
    """
    instances = []
    for iid in ext.instances:
        state = "LOADED"
        if supervisor is not None:
            try:
                state = str(supervisor.get(iid).state)
            except KeyError:
                state = "STOPPED"
        instances.append({"id": iid, "enabled": enabled(iid), "state": state})
    return {
        "module": ext.module,
        "element": ext.element,
        "base": ext.base,
        "scripts": list(ext.scripts),
        "instances": instances,
    }


def data_attributes(data: dict[str, Any]) -> str:
    """Render the page data as one ``data-ext`` attribute (no inline script, CORE-63)."""
    from html import escape

    return f'data-ext="{escape(json.dumps(data), quote=True)}"'


def names(extensions: Iterable[Extension]) -> list[str]:
    """Module names of the extensions."""
    return [e.module for e in extensions]
