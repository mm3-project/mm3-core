"""What a shell needs to know about the mirror: layout, pages, instances.

The engine sends this in ``welcome`` (CORE-246): the ``layout`` object of
CORE-274, the pages, the engine locale and time zone, the version of
``/custom.css``, and every instance that has a front-end element, with its
public configuration resolved for the element's locale (CORE-13,
CORE-101), the version of its catalog URL, its script and style URLs, its
appearance and its status.

Implements:
    CORE-246: Core spec 0.15, §5.6 WebSocket protocol.
    CORE-274: Core spec 0.14, §5.6 WebSocket protocol.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mm3.core.config.layout import IMPLICIT_PAGE, LayoutResult, header_text
from mm3.core.config.validate import ConfigResult
from mm3.core.i18n.catalogs import I18n, locale_chain
from mm3.core.publicconfig import public_config
from mm3.core.registry import Registry
from mm3.module import MirrorModule

__all__ = [
    "ModuleFiles",
    "ShellInstance",
    "ShellView",
    "cached_view",
    "content_version",
    "layout_object",
    "module_files",
    "shell_view",
]


def content_version(data: bytes | str) -> str:
    """Return the version used in ``?v=`` URLs: 12 hex digits of the SHA-256.

    Args:
        data: The content.

    Returns:
        The version.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()[:12]


@dataclass(frozen=True)
class ModuleFiles:
    """Where a module's static files are and under which version they are served.

    Attributes:
        version: Distribution version, part of the URL (CORE-252).
        static: The package's ``static/`` directory, or ``None``.
    """

    version: str
    static: Path | None


def module_files(cls: type[MirrorModule], distribution: str | None) -> ModuleFiles:
    """Find a module's version and static directory (Contract MOD-05).

    Args:
        cls: The module class.
        distribution: Its distribution name, if known.

    Returns:
        The version (``0`` when unknown) and the package's ``static/`` directory.
    """
    version = "0"
    if distribution:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "0"
    static: Path | None = None
    package = cls.__module__.split(".", 1)[0]
    try:
        candidate = Path(str(importlib.resources.files(package) / "static"))
    except (ModuleNotFoundError, TypeError, ValueError):
        candidate = None
    if candidate is not None and candidate.is_dir():
        static = candidate
    return ModuleFiles(version, static)


@dataclass(frozen=True)
class ShellInstance:
    """One instance as the shells see it.

    Attributes:
        id: Instance id.
        module: Module name.
        element: Custom element tag.
        config: Public configuration, resolved for ``locale``.
        locale: The element's locale.
        version: Module version, part of its static URLs.
        scripts: Script URLs.
        styles: Style URLs.
        appearance: ``{scale, align, valign, opacity, overflow, header, classes, css}``.
        appearance_css: The instance's ``appearance.css``, served at ``appearance["css"]``.
        catalog: Version of the instance's catalog (``/i18n/<id>.json?v=``).
        subscribes: The module's ``subscribes`` globs, ``None`` for all names.
        chain: The instance's locale chain (core spec 8.2).
        touch: The element takes touch gestures itself (CORE-235).
    """

    id: str
    module: str
    element: str
    config: Mapping[str, Any]
    locale: str
    version: str = "0"
    scripts: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    appearance: Mapping[str, Any] = field(default_factory=dict)
    appearance_css: str | None = None
    catalog: str | None = None
    subscribes: tuple[str, ...] | None = None
    chain: tuple[str, ...] = ("en",)
    touch: bool = False

    def welcome_entry(self, status: str = "ok") -> dict[str, Any]:
        """Return the instance's entry in ``welcome.instances`` (CORE-246).

        Args:
            status: ``ok`` or ``unavailable`` (CORE-117).

        Returns:
            The entry.
        """
        return {
            "id": self.id,
            "module": self.module,
            "element": self.element,
            "config": dict(self.config),
            "locale": self.locale,
            "catalog": self.catalog,
            "scripts": list(self.scripts),
            "styles": list(self.styles),
            "appearance": dict(self.appearance),
            "status": status,
            "touch": self.touch,
        }


@dataclass(frozen=True)
class ShellView:
    """The mirror as a shell is told about it in ``welcome``.

    Attributes:
        layout: The ``layout`` object (CORE-274).
        page: The default page (the current page comes from the page service).
        pages: ``[{id, title}]`` in page order, titles resolved for ``locale``.
        locale: The engine locale.
        timezone: The engine time zone (CORE-266).
        touch: ``engine.input.touch`` (CORE-103).
        custom_css: Version of ``/custom.css``, or ``None`` without one (CORE-262).
        instances: Instances with a front-end element, in configuration order.
        files: Module name to its version and static directory.
    """

    layout: Mapping[str, Any]
    page: str
    pages: list[dict[str, str]]
    locale: str
    timezone: str = "UTC"
    touch: bool = False
    custom_css: str | None = None
    instances: list[ShellInstance] = field(default_factory=list)
    files: Mapping[str, ModuleFiles] = field(default_factory=dict)

    def instance(self, instance_id: str) -> ShellInstance | None:
        """Return the instance with this id, if the shells show it."""
        for inst in self.instances:
            if inst.id == instance_id:
                return inst
        return None


def layout_object(layout: LayoutResult, grid: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the engine's layout to the ``layout`` object of CORE-274.

    Args:
        layout: The layout computed from a valid configuration.
        grid: ``{columns, rows, gap, padding}`` in CSS pixels.

    Returns:
        ``{viewport, cell, grid, pages: {<page id>: [placement, …]}}``.

    Implements:
        CORE-274: Core spec 0.14, §5.6 WebSocket protocol.
    """
    return {
        "viewport": [layout.viewport.width, layout.viewport.height],
        "cell": [layout.cell[0], layout.cell[1]],
        "grid": dict(grid),
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
    i18n: I18n | None = None,
) -> ShellView:
    """Build what shells are told about a valid configuration.

    Args:
        result: The running configuration.
        registry: Installed modules (element names, files, ``subscribes``).
        i18n: Catalogs (versions and ``HEADER``); without it catalogs are
            ``None`` and headers show the module name (MOD-67).

    Returns:
        The view.

    Raises:
        ValueError: The configuration is not valid.

    Implements:
        CORE-246: Core spec 0.15, §5.6 WebSocket protocol.
    """
    config = result.config
    if config is None or result.layout is None:
        raise ValueError("the configuration is not valid")
    locale = config.engine.locale
    if config.pages is not None:
        pages = [{"id": p.id, "title": p.resolved_title(locale)} for p in config.pages.pages]
        default = config.pages.default or pages[0]["id"]
    else:
        pages = [{"id": IMPLICIT_PAGE, "title": IMPLICIT_PAGE}]
        default = IMPLICIT_PAGE
    touch = config.engine.input.touch
    files: dict[str, ModuleFiles] = {}
    instances: list[ShellInstance] = []
    for inst in result.instances:
        if not inst.enabled:
            continue
        entry = registry.get(inst.module)
        cls = entry.cls if entry is not None else None
        if cls is None or cls.element is None:
            continue
        mf = files.get(inst.module)
        if mf is None:
            mf = files[inst.module] = module_files(cls, entry.distribution if entry else None)
        base = f"/modules/{inst.module}/{mf.version}/"
        inst_locale = inst.spec.locale or locale
        chain = tuple(locale_chain(inst.spec.locale, locale))
        app = inst.spec.appearance
        css = app.css
        css_url = f"/appearance/{inst.id}.css?v={content_version(css)}" if css else None

        def lookup(key: str, module: str = inst.module, ch: tuple[str, ...] = chain) -> str | None:
            entry = i18n.lookup(module, ch, key) if i18n is not None else None
            return entry if isinstance(entry, str) else None

        appearance = {
            "scale": app.scale,
            "align": app.align,
            "valign": app.valign,
            "opacity": app.opacity,
            "overflow": app.overflow,
            "header": header_text(app, inst.module, list(chain), lookup),
            "classes": list(app.classes),
            "css": css_url,
        }
        instances.append(
            ShellInstance(
                id=inst.id,
                module=inst.module,
                element=cls.element,
                config=public_config(inst.config, inst_locale) if inst.config is not None else {},
                locale=inst_locale,
                version=mf.version,
                scripts=tuple(base + s.lstrip("/") for s in cls.scripts),
                styles=tuple(base + s.lstrip("/") for s in cls.styles),
                appearance=appearance,
                appearance_css=css,
                catalog=i18n.merged(inst.module, chain)[0] if i18n is not None else None,
                subscribes=None if cls.subscribes is None else tuple(sorted(cls.subscribes)),
                chain=chain,
                touch=touch and "input.touch" in cls.capabilities,
            )
        )
    lc = config.layout
    grid = {
        "columns": lc.grid.columns,
        "rows": lc.grid.rows,
        "gap": float(lc.gap),
        "padding": float(lc.padding),
    }
    custom = result.assets.custom_css
    return ShellView(
        layout_object(result.layout, grid),
        default,
        pages,
        locale,
        timezone=config.engine.timezone,
        touch=config.engine.input.touch,
        custom_css=content_version(custom) if custom is not None else None,
        instances=instances,
        files=files,
    )


def cached_view(
    current: Callable[[], ConfigResult],
    registry: Registry,
    *,
    i18n: I18n | None = None,
) -> Callable[[], ShellView]:
    """Return a view provider that rebuilds the view only when the configuration changes.

    Args:
        current: Returns the running configuration (it is replaced, never
            mutated, on reload).
        registry: Installed modules.
        i18n: Catalogs.

    Returns:
        A callable returning the view of the current configuration.
    """
    cache: dict[str, Any] = {}

    def view() -> ShellView:
        result = current()
        if cache.get("result") is not result:
            cache["view"] = shell_view(result, registry, i18n=i18n)
            cache["result"] = result
        built: ShellView = cache["view"]
        return built

    return view
