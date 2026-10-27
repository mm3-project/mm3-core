"""Module registry: entry-point discovery and API version checks.

A module whose ``api_version`` major differs from the engine's, whose minor
is higher, or that cannot be parsed is refused; so is one that fails to
import. A refused module is kept with its reason so the admin can show it.

Implements:
    CORE-03: Core spec 0.4, §4 Module contract.
    -: Core spec 0.4, §7.1 Engine startup and shutdown.
    MOD-90: Module Contract 1.0 rev 4, §10 API versioning and deprecation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points

from mm3.__about__ import MODULE_API_VERSION
from mm3.core.logging import get_logger
from mm3.module import MirrorModule

__all__ = ["ENTRY_POINT_GROUP", "ModuleEntry", "Registry", "api_compatible"]

ENTRY_POINT_GROUP = "mm3.modules"
_VERSION = re.compile(r"^(\d+)\.(\d+)$")
_log = get_logger(subsystem="registry")


def api_compatible(module_api: object, engine_api: str = MODULE_API_VERSION) -> str | None:
    """Check a module's API version against the engine's.

    Args:
        module_api: The module's ``api_version`` attribute.
        engine_api: The engine's Module API version.

    Returns:
        ``None`` when compatible, otherwise the reason it is refused.

    Implements:
        CORE-03: Core spec 0.4, §4 Module contract.
    """
    if not isinstance(module_api, str) or not (mod := _VERSION.match(module_api)):
        return f"api_version {module_api!r} cannot be parsed"
    eng = _VERSION.match(engine_api)
    assert eng is not None  # noqa: S101 - engine constant
    if mod[1] != eng[1]:
        return f"api_version {module_api} has major {mod[1]}, engine supports {eng[1]}.x"
    if int(mod[2]) > int(eng[2]):
        return f"api_version {module_api} is newer than the engine's {engine_api}"
    return None


@dataclass(frozen=True)
class ModuleEntry:
    """One discovered module.

    Attributes:
        name: Entry point key (the module name).
        cls: The backend class; ``None`` when refused.
        error: Why the module is refused (it is then DISABLED); ``None`` if usable.
        distribution: Name of the distribution that provides it, if known.
    """

    name: str
    cls: type[MirrorModule] | None
    error: str | None = None
    distribution: str | None = None


class Registry:
    """Known module classes, keyed by name.

    Implements:
        CORE-03: Core spec 0.4, §4 Module contract.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ModuleEntry] = {}

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def get(self, name: str) -> ModuleEntry | None:
        """Return the entry of a module.

        Args:
            name: Module name.

        Returns:
            The entry, or ``None`` if unknown.
        """
        return self._entries.get(name)

    def entries(self) -> list[ModuleEntry]:
        """Return all entries, sorted by name.

        Returns:
            The entries.
        """
        return [self._entries[k] for k in sorted(self._entries)]

    def add(self, cls: object, *, name: str | None = None, distribution: str | None = None) -> None:
        """Register a module class after checking it.

        Args:
            cls: The object the entry point loaded.
            name: Entry point key; defaults to ``cls.name``.
            distribution: Providing distribution, for diagnostics.
        """
        key = name or getattr(cls, "name", None) or repr(cls)
        module_cls: type[MirrorModule] | None = None
        if not (isinstance(cls, type) and issubclass(cls, MirrorModule)):
            error: str | None = "entry point is not a MirrorModule subclass"
        else:
            error = api_compatible(getattr(cls, "api_version", None))
            module_cls = cls if error is None else None
        if key in self._entries:
            error, module_cls = f"duplicate module name {key!r}", None
        else:
            self._entries[key] = ModuleEntry(key, module_cls, error, distribution)
        if error:
            _log.error("module_refused", module=key, reason=error, distribution=distribution)

    def _load(self, ep: EntryPoint) -> None:
        dist = ep.dist.name if ep.dist is not None else None
        try:
            obj = ep.load()
        except Exception as exc:
            reason = f"import failed: {type(exc).__name__}: {exc}"
            self._entries.setdefault(ep.name, ModuleEntry(ep.name, None, reason, dist))
            _log.exception("module_refused", module=ep.name, reason=reason, distribution=dist)
            return
        self.add(obj, name=ep.name, distribution=dist)

    def discover(self, group: str = ENTRY_POINT_GROUP) -> None:
        """Load every module declared in an entry point group.

        Args:
            group: Entry point group.
        """
        for ep in sorted(entry_points(group=group), key=lambda e: e.name):
            self._load(ep)
