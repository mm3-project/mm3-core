"""Screen layout, display viewport and pages: geometry and validation.

From a valid configuration this module derives the logical viewport, the
pixel size of a grid cell, and, for every page, where each instance is
placed: a rectangle of cells, or a place in a stack of instances on the
same preset. The same computation checks positions, overlaps, cell size,
``min_size_px``, appearance CSS and page references.

Implements:
    CORE-33: Core spec 0.9, §6.2 Rules.
    CORE-90, CORE-91, CORE-92, CORE-93, CORE-94, CORE-96: Core spec 0.9, §6.5 Screen layout.
    CORE-212, CORE-213, CORE-214: Core spec 0.9, §6.5 Screen layout.
    CORE-140, CORE-141, CORE-142, CORE-215: Core spec 0.9, §6.7 Display output.
    CORE-230, CORE-231, CORE-232: Core spec 0.9, §6.9 Pages and visibility.
    MOD-67: Module Contract 1.0 rev 6, §7 Internationalization.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from mm3.config import LocalizedText
from mm3.core.config.model import (
    Appearance,
    DisplayConfig,
    InstanceConfig,
    LayoutConfig,
    MirrorConfig,
    Rect,
)
from mm3.core.config.records import SourceMap, ValidationRecord, make_record, pointer
from mm3.core.registry import Registry

__all__ = [
    "ASSUMED_MODE",
    "CSS_MAX_BYTES",
    "IMPLICIT_PAGE",
    "Area",
    "Cells",
    "LayoutResult",
    "PagePlan",
    "Placement",
    "Viewport",
    "cell_size",
    "check_css",
    "check_layout",
    "header_text",
    "preset_cells",
    "viewport",
]

ASSUMED_MODE = (1920, 1080)
CSS_MAX_BYTES = 16 * 1024
IMPLICIT_PAGE = "main"

_F = Fraction
_ROWS: dict[str, tuple[Fraction, Fraction]] = {
    "top_bar": (_F(0), _F(1, 12)),
    "top": (_F(1, 12), _F(1, 3)),
    "upper_third": (_F(1, 3), _F(5, 12)),
    "middle_center": (_F(5, 12), _F(7, 12)),
    "lower_third": (_F(7, 12), _F(2, 3)),
    "bottom": (_F(2, 3), _F(11, 12)),
    "bottom_bar": (_F(11, 12), _F(1)),
    "fullscreen": (_F(0), _F(1)),
}
_THIRDS: dict[str, tuple[Fraction, Fraction]] = {
    "left": (_F(0), _F(1, 3)),
    "center": (_F(1, 3), _F(2, 3)),
    "right": (_F(2, 3), _F(1)),
}
_ALL = (_F(0), _F(1))


@dataclass(frozen=True)
class Cells:
    """A rectangle of grid cells, 1-based, with its stacking level.

    Attributes:
        col: First column.
        row: First row.
        width: Columns spanned.
        height: Rows spanned.
        z: Stacking level.
    """

    col: int
    row: int
    width: int
    height: int
    z: int = 0

    def overlaps(self, other: Cells) -> bool:
        """Tell whether two rectangles share at least one cell.

        Args:
            other: The other rectangle.

        Returns:
            True when they overlap.
        """
        return not (
            self.col + self.width <= other.col
            or other.col + other.width <= self.col
            or self.row + self.height <= other.row
            or other.row + other.height <= self.row
        )


def _boundary(f: Fraction, n: int) -> int:
    """round(f x n) with halves rounded up (CORE-92)."""
    return math.floor(f * n + _F(1, 2))


def preset_cells(preset: str, columns: int, rows: int) -> Cells | None:
    """Resolve a preset to grid cells (CORE-92).

    Args:
        preset: Preset name.
        columns: Grid columns.
        rows: Grid rows.

    Returns:
        The rectangle, or ``None`` when the preset is empty on this grid.

    Raises:
        KeyError: Unknown preset.

    Implements:
        CORE-90, CORE-92: Core spec 0.9, §6.5 Screen layout.
    """
    z = 0
    if preset.startswith("fullscreen_"):
        z = -100 if preset == "fullscreen_below" else 100
        band, cols = _ROWS["fullscreen"], _ALL
    elif preset in _ROWS:
        band, cols = _ROWS[preset], _ALL
    else:
        vertical, horizontal = preset.split("_", 1)
        band, cols = _ROWS[vertical], _THIRDS[horizontal]
    r0, r1 = _boundary(band[0], rows), _boundary(band[1], rows)
    c0, c1 = _boundary(cols[0], columns), _boundary(cols[1], columns)
    if r1 <= r0 or c1 <= c0:
        return None
    return Cells(c0 + 1, r0 + 1, c1 - c0, r1 - r0, z)


@dataclass(frozen=True)
class Viewport:
    """The logical viewport in CSS pixels (CORE-141).

    Attributes:
        width: Width in CSS pixels.
        height: Height in CSS pixels.
        scale: Device pixel ratio.
        rotation: Clockwise rotation.
        assumed: True until a kiosk reports its applied mode, and always in
            ``mm3 check-config`` (CORE-215).
    """

    width: int
    height: int
    scale: float
    rotation: int
    assumed: bool


def viewport(display: DisplayConfig, applied: tuple[int, int] | None = None) -> Viewport:
    """Derive the logical viewport from the ``display`` block.

    Args:
        display: The display block.
        applied: Mode the kiosk reported as applied (CORE-144, milestone 3).

    Returns:
        The mode, width and height swapped for rotation 90 or 270, divided
        by ``scale`` and rounded down; the configured mode or 1920 x 1080 is
        used while no kiosk has reported one.

    Implements:
        CORE-140, CORE-141, CORE-215: Core spec 0.9, §6.7 Display output.
    """
    assumed = applied is None
    if applied is not None:
        w, h = applied
    elif display.width is not None and display.height is not None:
        w, h = display.width, display.height
    else:
        w, h = ASSUMED_MODE
    if display.rotation in (90, 270):
        w, h = h, w
    return Viewport(
        math.floor(w / display.scale),
        math.floor(h / display.scale),
        display.scale,
        display.rotation,
        assumed,
    )


def cell_size(view: Viewport, layout: LayoutConfig) -> tuple[float, float]:
    """Compute one grid cell's size in CSS pixels (CORE-142, CORE-213).

    Args:
        view: The viewport.
        layout: The layout block.

    Returns:
        ``(width, height)``, fractions of a pixel allowed.

    Implements:
        CORE-142, CORE-213: Core spec 0.9, §6.5 Screen layout.
    """
    cols, rows = layout.grid.columns, layout.grid.rows
    w = (view.width - 2 * layout.padding - (cols - 1) * layout.gap) / cols
    h = (view.height - 2 * layout.padding - (rows - 1) * layout.gap) / rows
    return (w, h)


@dataclass(frozen=True)
class Area:
    """An instance's area in CSS pixels.

    Attributes:
        width: Width.
        height: Height.
    """

    width: float
    height: float


@dataclass(frozen=True)
class Placement:
    """Where an instance sits on one page.

    Attributes:
        instance: Instance id.
        index: Position in ``modules``.
        cells: The rectangle; for a stack, the preset's rectangle.
        preset: Preset name when placed on a preset.
        stack_index: Place in the preset's stack, 0 for the first.
        stack_size: Number of instances in that stack.
        area: Area used for ``min_size_px`` (CORE-212).
    """

    instance: str
    index: int
    cells: Cells
    preset: str | None
    stack_index: int
    stack_size: int
    area: Area


@dataclass
class PagePlan:
    """The placements of one page."""

    page: str
    placements: list[Placement] = field(default_factory=list)


@dataclass
class LayoutResult:
    """Geometry derived from a configuration.

    Attributes:
        viewport: The logical viewport.
        cell: Cell size in CSS pixels.
        pages: One plan per page, in page order.
        records: Problems found.
    """

    viewport: Viewport
    cell: tuple[float, float]
    pages: list[PagePlan]
    records: list[ValidationRecord]


_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_IMPORT = re.compile(r"@import\b", re.I)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def check_css(
    css: str, *, max_bytes: int = CSS_MAX_BYTES, relative_to: str = "the module's files"
) -> str | None:
    """Check CSS against the limits of CORE-214 (and CORE-262 for custom.css).

    Args:
        css: The CSS.
        max_bytes: Size limit in bytes: 16 KiB for ``appearance.css``,
            64 KiB for custom.css.
        relative_to: What relative ``url()`` paths point into, for the reason.

    Returns:
        ``None`` when allowed, otherwise the reason.

    Implements:
        CORE-214: Core spec 0.9, §6.5 Screen layout.
        CORE-262: Core spec 0.11, §6.10 Theme and custom CSS.
    """
    if len(css.encode("utf-8")) > max_bytes:
        return f"larger than {max_bytes // 1024} KiB"
    if _IMPORT.search(css):
        return "@import is not allowed"
    for match in _URL.finditer(css):
        target = match.group(2).strip()
        if target.lower().startswith("data:"):
            continue
        parts = target.replace("\\", "/").split("/")
        if _SCHEME.match(target) or target.startswith("/") or ".." in parts or not target:
            return f"url({target}) must be a data: URI or a relative path to {relative_to}"
    return None


def header_text(
    appearance: Appearance,
    module: str,
    locale: str | Sequence[str],
    translate: Callable[[str], str | None],
) -> str | None:
    """Resolve the header shown above an instance (CORE-214, MOD-67).

    Args:
        appearance: The instance's appearance.
        module: Module name, shown when the module has no ``HEADER`` key.
        locale: Locale (or chain) for a ``LocalizedText`` header.
        translate: Looks up a key in the module's catalog for the locale;
            returns ``None`` when the key is missing.

    Returns:
        The header text, or ``None`` when ``header`` is false.

    Implements:
        CORE-214: Core spec 0.9, §6.5 Screen layout.
        MOD-67: Module Contract 1.0 rev 6, §7 Internationalization.
    """
    header = appearance.header
    if header is False:
        return None
    if isinstance(header, LocalizedText):
        return header.resolve(locale)
    return translate("HEADER") or module


@dataclass
class _Ctx:
    config: MirrorConfig
    sources: SourceMap
    paged: bool
    records: list[ValidationRecord] = field(default_factory=list)

    def add(
        self, code: str, parts: Sequence[str | int], params: dict[str, Any], page: str | None = None
    ) -> None:
        full = dict(params)
        if self.paged and page is not None:
            full["page"] = page
        self.records.append(make_record(code, pointer(parts), full, sources=self.sources))


def _pages(ctx: _Ctx) -> list[str]:
    pages = ctx.config.pages
    if pages is None:
        return [IMPLICIT_PAGE]
    ids: list[str] = []
    for i, page in enumerate(pages.pages):
        if page.id in ids:
            ctx.add("CORE_CFG_ID_DUPLICATE", ("pages", "list", i, "id"), {"id": page.id})
        else:
            ids.append(page.id)
    if pages.default is not None and pages.default not in ids:
        ctx.add("CORE_CFG_PAGE_UNKNOWN", ("pages", "default"), {"page": pages.default})
    return ids


def _cells(pos: Any, layout: LayoutConfig) -> tuple[Cells | None, str | None]:
    cols, rows = layout.grid.columns, layout.grid.rows
    if isinstance(pos, Rect):
        return Cells(pos.col, pos.row, pos.width, pos.height, pos.z), None
    return preset_cells(pos, cols, rows), pos


def _area(cells: Cells, cell: tuple[float, float], gap: float, share: int = 1) -> Area:
    w = cells.width * cell[0] + (cells.width - 1) * gap
    h = cells.height * cell[1] + (cells.height - 1) * gap
    if share > 1:
        h = h / share
    return Area(w, h)


def check_layout(
    config: MirrorConfig,
    instances: Sequence[Any],
    registry: Registry,
    sources: SourceMap,
    *,
    applied: tuple[int, int] | None = None,
) -> LayoutResult:
    """Compute the layout of every page and check it.

    Args:
        config: The validated configuration.
        instances: ``ResolvedInstance`` objects, in file order.
        registry: Installed modules (front-end or backend-only, ``min_size_px``).
        sources: Source positions for the records.
        applied: Mode reported by the kiosk, if any.

    Returns:
        The geometry and the records found.

    Implements:
        CORE-33: Core spec 0.9, §6.2 Rules.
        CORE-93, CORE-94, CORE-96, CORE-212: Core spec 0.9, §6.5 Screen layout.
        CORE-230, CORE-231, CORE-232: Core spec 0.9, §6.9 Pages and visibility.
    """
    ctx = _Ctx(config, sources, paged=config.pages is not None)
    disp = config.display
    if (disp.width is None) != (disp.height is None) or (
        disp.refresh is not None and disp.width is None
    ):
        ctx.add("CORE_CFG_DISPLAY_MODE", ("display",), {})
    view = viewport(disp, applied)
    layout = config.layout
    cell = cell_size(view, layout)
    if cell[0] < 1 or cell[1] < 1:
        ctx.add(
            "CORE_CFG_CELL_TOO_SMALL",
            ("layout",),
            {"width": round(cell[0], 2), "height": round(cell[1], 2)},
        )
    page_ids = _pages(ctx)
    known = set(page_ids)
    cols, rows = layout.grid.columns, layout.grid.rows

    shown: list[tuple[Any, InstanceConfig, type[Any] | None]] = []
    for inst in instances:
        spec: InstanceConfig = inst.spec
        base = ("modules", inst.index)
        entry = registry.get(inst.module)
        cls = entry.cls if entry is not None else None
        if spec.appearance.css is not None and (reason := check_css(spec.appearance.css)):
            ctx.add("CORE_CFG_CSS", (*base, "appearance", "css"), {"reason": reason})
        if isinstance(spec.pages, list):
            for j, page in enumerate(spec.pages):
                if page not in known:
                    ctx.add("CORE_CFG_PAGE_UNKNOWN", (*base, "pages", j), {"page": page})
        for page in spec.positions:
            if page not in known:
                ctx.add("CORE_CFG_PAGE_UNKNOWN", (*base, "positions", page), {"page": page})
        if cls is None:
            continue  # missing or unavailable: not known whether it has a front-end
        if cls.element is None:
            for key in ("position", "positions", "pages"):
                if key in spec.model_fields_set:
                    ctx.add("CORE_CFG_POSITION_FORBIDDEN", (*base, key), {"key": key})
            continue
        if spec.position is None:
            ctx.add("CORE_CFG_POSITION_REQUIRED", base, {"module": inst.module})
            continue
        shown.append((inst, spec, cls))

    plans: list[PagePlan] = []
    for page in page_ids:
        plans.append(_plan_page(ctx, page, shown, layout, cols, rows, cell))
    return LayoutResult(view, cell, plans, ctx.records)


def _plan_page(
    ctx: _Ctx,
    page: str,
    shown: Sequence[tuple[Any, InstanceConfig, type[Any] | None]],
    layout: LayoutConfig,
    cols: int,
    rows: int,
    cell: tuple[float, float],
) -> PagePlan:
    plan = PagePlan(page)
    stacks: dict[str, list[tuple[Any, Cells]]] = {}
    rects: list[tuple[Any, Cells, str | None, tuple[str | int, ...]]] = []
    for inst, spec, _cls in shown:
        if isinstance(spec.pages, list) and page not in spec.pages:
            continue
        use_page = page in spec.positions
        pos = spec.positions[page] if use_page else spec.position
        where: tuple[str | int, ...] = (
            ("modules", inst.index, "positions", page)
            if use_page
            else ("modules", inst.index, "position")
        )
        cells, preset = _cells(pos, layout)
        if cells is None:
            ctx.add("CORE_CFG_PRESET_EMPTY", where, {"preset": preset}, page)
            continue
        if preset is None and (
            cells.col + cells.width - 1 > cols or cells.row + cells.height - 1 > rows
        ):
            ctx.add("CORE_CFG_OUT_OF_GRID", where, {"columns": cols, "rows": rows}, page)
            continue
        if not inst.enabled:
            continue  # validated in full, ignored by overlap and stacking (CORE-204)
        if preset is not None:
            stacks.setdefault(preset, []).append((inst, cells))
            if len(stacks[preset]) == 1:
                rects.append((inst, cells, preset, where))
        else:
            rects.append((inst, cells, None, where))

    # Overlaps on the same z; a stack counts as one rectangle (CORE-93).
    for i, (_inst, cells, _preset, where) in enumerate(rects):
        for other, ocells, _op, _ow in rects[:i]:
            if cells.z == ocells.z and cells.overlaps(ocells):
                ctx.add("CORE_CFG_OVERLAP", where, {"other": other.id}, page)
                break

    for inst, cells, preset, _where in rects:
        if preset is None:
            plan.placements.append(
                Placement(inst.id, inst.index, cells, None, 0, 1, _area(cells, cell, layout.gap))
            )
            continue
        members = stacks[preset]
        for k, (member, mcells) in enumerate(members):
            plan.placements.append(
                Placement(
                    member.id,
                    member.index,
                    mcells,
                    preset,
                    k,
                    len(members),
                    _area(mcells, cell, layout.gap, len(members)),
                )
            )
    plan.placements.sort(key=lambda p: p.index)

    # min_size_px warnings (CORE-96), with the stack share of CORE-212.
    by_id = {inst.id: (inst, cls) for inst, _s, cls in shown}
    for p in plan.placements:
        inst, cls = by_id[p.instance]
        minimum = getattr(cls, "min_size_px", None)
        if minimum and (p.area.width < minimum[0] or p.area.height < minimum[1]):
            spec = inst.spec
            key = ("positions", page) if page in spec.positions else ("position",)
            ctx.add(
                "CORE_CFG_MIN_SIZE",
                ("modules", inst.index, *key),
                {
                    "width": math.floor(p.area.width),
                    "height": math.floor(p.area.height),
                    "min_width": minimum[0],
                    "min_height": minimum[1],
                },
                page,
            )
    return plan
