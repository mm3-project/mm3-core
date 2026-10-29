"""What a shell needs to know about the mirror: layout, pages, instances.

The engine sends this in ``welcome`` (CORE-246): the ``layout`` object of
CORE-274, the current page and the pages with their titles, the engine
locale, and every instance that has a front-end element, with its public
configuration resolved for the element's locale (CORE-13, CORE-101) and
the version of its catalog URL.

Implements:
    CORE-246: Core spec 0.13, §5.6 WebSocket protocol.
    CORE-274: Core spec 0.13, §5.6 WebSocket protocol.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mm3.core.config.layout import IMPLICIT_PAGE, LayoutResult
from mm3.core.config.validate import ConfigResult
from mm3.core.publicconfig import public_config
from mm3.core.registry import Registry

__all__ = [
    "CatalogVersion",
    "ShellInstance",
    "ShellView",
    "cached_view",
    "layout_object",
    "shell_view",
]

CatalogVersion = Callable[[str], str | None]
"""Returns the catalog version (the ``v`` of ``/i18n/<instance>.json``) of an instance."""


@dataclass(frozen=True)
class ShellInstance:
    """One instance as the shells see it.

    Attributes:
        id: Instance id.
        module: Module name.
        element: Custom element tag.
        config: Public configuration, resolved for ``locale``.
        locale: The element's locale.
        catalog: Version of the instance's catalog, ``None`` until known.
        subscribes: The module's ``subscribes`` globs, ``None`` for all names.
    """

    id: str
    module: str
    element: str
    config: Mapping[str, Any]
    locale: str
    catalog: str | None = None
    subscribes: tuple[str, ...] | None = None

    def welcome_entry(self) -> dict[str, Any]:
        """Return the instance's entry in ``welcome.instances`` (CORE-246)."""
        return {
            "id": self.id,
            "module": self.module,
            "element": self.element,
            "config": dict(self.config),
            "catalog": self.catalog,
        }


@dataclass(frozen=True)
class ShellView:
    """The mirror as a shell is told about it in ``welcome``.

    Attributes:
        layout: The ``layout`` object (CORE-274).
        page: Current page id.
        pages: ``[{id, title}]`` in page order, titles resolved for ``locale``.
        locale: The engine locale.
        instances: Instances with a front-end element, in configuration order.
    """

    layout: Mapping[str, Any]
    page: str
    pages: list[dict[str, str]]
    locale: str
    instances: list[ShellInstance] = field(default_factory=list)

    def instance(self, instance_id: str) -> ShellInstance | None:
        """Return the instance with this id, if the shells show it."""
        for inst in self.instances:
            if inst.id == instance_id:
                return inst
        return None


def layout_object(layout: LayoutResult) -> dict[str, Any]:
    """Convert the engine's layout to the ``layout`` object of CORE-274.

    Args:
        layout: The layout computed from a valid configuration.

    Returns:
        ``{viewport, cell, pages: {<page id>: [placement, …]}}``.

    Implements:
        CORE-274: Core spec 0.13, §5.6 WebSocket protocol.
    """
    return {
        "viewport": [layout.viewport.width, layout.viewport.height],
        "cell": [layout.cell[0], layout.cell[1]],
        "pages": {
            plan.page: [
                {
                    "instance": p.instance,
                    "col": p.cells.col,
                    "row": p.cells.row,
                    "width": p.cells.width,
                    "height": p.cells.height,
                    "z": p.cells.z,
                    "preset": p.preset,
                    "stack_index": p.stack_index,
                    "stack_size": p.stack_size,
                    "area": [p.area.width, p.area.height],
                }
                for p in plan.placements
            ]
            for plan in layout.pages
        },
    }


def shell_view(
    result: ConfigResult,
    registry: Registry,
    *,
    page: str | None = None,
    catalog: CatalogVersion | None = None,
) -> ShellView:
    """Build what shells are told about a valid configuration.

    Args:
        result: The running configuration.
        registry: Installed modules (for element names and ``subscribes``).
        page: Current page; default ``pages.default``, else the first page.
        catalog: Catalog versions; until the i18n service, ``None`` for all.

    Returns:
        The view.

    Raises:
        ValueError: The configuration is not valid.
    """
    config = result.config
    if config is None or result.layout is None:
        raise ValueError("the configuration is not valid")
    locale = config.engine.locale
    if config.pages is not None:
        pages = [{"id": p.id, "title": p.resolved_title(locale)} for p in config.pages.pages]
        current = page or config.pages.default or pages[0]["id"]
    else:
        pages = [{"id": IMPLICIT_PAGE, "title": IMPLICIT_PAGE}]
        current = page or IMPLICIT_PAGE
    instances: list[ShellInstance] = []
    for inst in result.instances:
        if not inst.enabled:
            continue
        entry = registry.get(inst.module)
        cls = entry.cls if entry is not None else None
        if cls is None or cls.element is None:
            continue
        inst_locale = inst.spec.locale or locale
        instances.append(
            ShellInstance(
                id=inst.id,
                module=inst.module,
                element=cls.element,
                config=public_config(inst.config, inst_locale) if inst.config is not None else {},
                locale=inst_locale,
                catalog=catalog(inst.id) if catalog is not None else None,
                subscribes=None if cls.subscribes is None else tuple(sorted(cls.subscribes)),
            )
        )
    return ShellView(layout_object(result.layout), current, pages, locale, instances)


def cached_view(
    current: Callable[[], ConfigResult],
    registry: Registry,
    *,
    catalog: CatalogVersion | None = None,
) -> Callable[[], ShellView]:
    """Return a view provider that rebuilds the view only when the configuration changes.

    Args:
        current: Returns the running configuration (it is replaced, never
            mutated, on reload).
        registry: Installed modules.
        catalog: Catalog versions.

    Returns:
        A callable returning the view of the current configuration.
    """
    cache: dict[str, Any] = {}

    def view() -> ShellView:
        result = current()
        if cache.get("result") is not result:
            cache["view"] = shell_view(result, registry, catalog=catalog)
            cache["result"] = result
        built: ShellView = cache["view"]
        return built

    return view
