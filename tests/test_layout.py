"""Milestone 2, part C: screen layout, display viewport and pages."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from mm3.cli.main import check_config
from mm3.config import LocalizedText
from mm3.core.config import ConfigResult, load_config
from mm3.core.config.layout import (
    Cells,
    cell_size,
    check_css,
    header_text,
    preset_cells,
    viewport,
)
from mm3.core.config.model import Appearance, DisplayConfig, LayoutConfig, PageConfig
from mm3.core.registry import Registry
from mm3.module import MirrorModule


class Clock(MirrorModule):
    name = "clock"
    element = "mm3-clock"


class Big(MirrorModule):
    name = "big"
    element = "mm3-big"
    min_size_px = (400, 300)


class Sensor(MirrorModule):
    name = "sensor"

    async def start(self) -> None:
        return None


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    for cls in (Clock, Big, Sensor):
        reg.add(cls)
    return reg


def load(tmp_path: Path, text: str, registry: Registry) -> ConfigResult:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load_config(path, registry=registry, env_file=tmp_path / "none.env", environ={})


def errs(result: ConfigResult) -> list[tuple[str, str]]:
    return [(r.code, r.path) for r in result.errors]


def modules(*entries: str) -> str:
    return "modules:\n" + "".join(entries)


def clock(iid: str, position: str, extra: str = "") -> str:
    return f"  - module: clock\n    id: {iid}\n    position: {position}\n{extra}"


# --- CORE-33: position required or forbidden ------------------------------------------


def test_core_33_position_required_for_front_ends(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "modules:\n  - module: clock\n", registry)
    assert errs(result) == [("CORE_CFG_POSITION_REQUIRED", "/modules/0")]


def test_core_33_position_forbidden_for_backend_only(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: sensor\n    position: top_bar\n    pages: [main]\n"
        "    positions: {main: top_left}\n",
        registry,
    )
    assert sorted(errs(result)) == [
        ("CORE_CFG_POSITION_FORBIDDEN", "/modules/0/pages"),
        ("CORE_CFG_POSITION_FORBIDDEN", "/modules/0/position"),
        ("CORE_CFG_POSITION_FORBIDDEN", "/modules/0/positions"),
    ]
    assert load(tmp_path, "modules:\n  - module: sensor\n", registry).ok


# --- CORE-90..94: grid, presets, overlap ----------------------------------------------


def test_core_92_preset_table_on_default_grid() -> None:
    expected = {
        "top_bar": Cells(1, 1, 12, 1),
        "top_left": Cells(1, 2, 4, 3),
        "top_center": Cells(5, 2, 4, 3),
        "top_right": Cells(9, 2, 4, 3),
        "upper_third": Cells(1, 5, 12, 1),
        "middle_center": Cells(1, 6, 12, 2),
        "lower_third": Cells(1, 8, 12, 1),
        "bottom_left": Cells(1, 9, 4, 3),
        "bottom_center": Cells(5, 9, 4, 3),
        "bottom_right": Cells(9, 9, 4, 3),
        "bottom_bar": Cells(1, 12, 12, 1),
        "fullscreen_below": Cells(1, 1, 12, 12, -100),
        "fullscreen_above": Cells(1, 1, 12, 12, 100),
    }
    assert {p: preset_cells(p, 12, 12) for p in expected} == expected


def test_core_90_presets_follow_the_grid_size() -> None:
    assert preset_cells("top_right", 24, 24) == Cells(17, 3, 8, 6)
    assert preset_cells("top_bar", 5, 6) == Cells(1, 1, 5, 1)
    assert preset_cells("lower_third", 5, 6) is None


def test_core_92_used_empty_preset_is_an_error(tmp_path: Path, registry: Registry) -> None:
    text = "layout:\n  grid: {columns: 6, rows: 6}\n" + modules(clock("c", "lower_third"))
    result = load(tmp_path, text, registry)
    assert errs(result) == [("CORE_CFG_PRESET_EMPTY", "/modules/0/position")]
    assert result.errors[0].params == {"preset": "lower_third"}


def test_core_91_rectangle_must_fit(tmp_path: Path, registry: Registry) -> None:
    ok = load(tmp_path, modules(clock("c", "{col: 9, row: 11, width: 4, height: 2}")), registry)
    assert ok.ok, ok.records
    bad = load(tmp_path, modules(clock("c", "{col: 10, row: 11, width: 4, height: 2}")), registry)
    assert errs(bad) == [("CORE_CFG_OUT_OF_GRID", "/modules/0/position")]
    assert bad.errors[0].params == {"columns": 12, "rows": 12}
    zero = load(tmp_path, modules(clock("c", "{col: 0, row: 1, width: 1, height: 1}")), registry)
    assert errs(zero) == [("CORE_CFG_RANGE", "/modules/0/position/col")]


def test_core_94_smaller_grid_fails_instead_of_moving(tmp_path: Path, registry: Registry) -> None:
    text = "layout:\n  grid: {columns: 8, rows: 8}\n" + modules(
        clock("c", "{col: 7, row: 1, width: 4, height: 2}")
    )
    assert errs(load(tmp_path, text, registry)) == [("CORE_CFG_OUT_OF_GRID", "/modules/0/position")]


def test_core_93_overlap_rules(tmp_path: Path, registry: Registry) -> None:
    overlap = load(
        tmp_path,
        modules(
            clock("a", "{col: 1, row: 1, width: 4, height: 4}"),
            clock("b", "{col: 4, row: 4, width: 2, height: 2}"),
        ),
        registry,
    )
    assert errs(overlap) == [("CORE_CFG_OVERLAP", "/modules/1/position")]
    assert overlap.errors[0].params == {"other": "a"}
    layered = load(
        tmp_path,
        modules(
            clock("a", "{col: 1, row: 1, width: 4, height: 4}"),
            clock("b", "{col: 4, row: 4, width: 2, height: 2, z: 1}"),
            clock("bg", "fullscreen_below"),
        ),
        registry,
    )
    assert layered.ok, layered.records
    against_stack = load(
        tmp_path,
        modules(clock("a", "top_left"), clock("b", "{col: 2, row: 3, width: 1, height: 1}")),
        registry,
    )
    assert errs(against_stack) == [("CORE_CFG_OVERLAP", "/modules/1/position")]
    disabled = load(
        tmp_path,
        modules(
            clock("a", "{col: 1, row: 1, width: 4, height: 4}"),
            clock("b", "{col: 4, row: 4, width: 2, height: 2}", "    disabled: true\n"),
        ),
        registry,
    )
    assert disabled.ok


# --- CORE-212: stacks -----------------------------------------------------------------


def test_core_212_same_preset_forms_a_stack(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        modules(clock("a", "top_left"), clock("b", "top_left"), clock("c", "top_right")),
        registry,
    )
    assert result.ok, result.records
    assert result.layout is not None
    (page,) = result.layout.pages
    stack = [(p.instance, p.preset, p.stack_index, p.stack_size) for p in page.placements]
    assert stack == [("a", "top_left", 0, 2), ("b", "top_left", 1, 2), ("c", "top_right", 0, 1)]
    a, _b, c = page.placements
    assert a.area.height == pytest.approx(c.area.height / 2)
    assert a.area.width == pytest.approx(c.area.width)


# --- CORE-96, CORE-140..142, CORE-213, CORE-215: sizes --------------------------------


def test_core_141_viewport_rotation_and_scale() -> None:
    v270 = viewport(DisplayConfig(width=1920, height=1080, rotation=270))
    assert (v270.width, v270.height) == (1080, 1920)
    v = viewport(DisplayConfig(width=1920, height=1080, rotation=90))
    assert (v.width, v.height) == (1080, 1920)
    v4k = viewport(DisplayConfig(width=3840, height=2160, scale=2.0))
    assert (v4k.width, v4k.height) == (1920, 1080)


def test_core_215_rounding_and_assumed_viewport() -> None:
    v = viewport(DisplayConfig(width=1366, height=768, scale=1.5))
    assert (v.width, v.height) == (910, 512)
    assert v.assumed
    default = viewport(DisplayConfig(rotation=90))
    assert (default.width, default.height) == (1080, 1920)
    reported = viewport(DisplayConfig(width=1920, height=1080), applied=(1280, 720))
    assert (reported.width, reported.height, reported.assumed) == (1280, 720, False)


def test_core_215_mode_needs_width_and_height(tmp_path: Path, registry: Registry) -> None:
    for display in ("{width: 1920}", "{refresh: 60}"):
        result = load(tmp_path, f"display: {display}\n", registry)
        assert errs(result) == [("CORE_CFG_DISPLAY_MODE", "/display")]


def test_core_215_check_config_states_assumed_viewport(tmp_path: Path, registry: Registry) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("display: {width: 1920, height: 1080, rotation: 90}\n")
    out = io.StringIO()
    assert check_config(cfg, tmp_path / "e", registry=registry, out=out) == 0
    assert out.getvalue() == "viewport 1080x1920 CSS px (assumed, no kiosk report)\n"
    out = io.StringIO()
    check_config(cfg, tmp_path / "e", registry=registry, out=out, as_json=True)
    assert json.loads(out.getvalue())["viewport"] == {
        "width": 1080,
        "height": 1920,
        "assumed": True,
    }


def test_core_142_cell_size() -> None:
    view = viewport(DisplayConfig())
    w, h = cell_size(view, LayoutConfig())
    assert w == pytest.approx((1920 - 64 - 11 * 16) / 12)
    assert h == pytest.approx((1080 - 64 - 11 * 16) / 12)


def test_core_213_defaults_and_too_small_cells(tmp_path: Path, registry: Registry) -> None:
    layout = LayoutConfig()
    assert (layout.gap, layout.padding) == (16, 32)
    assert LayoutConfig.model_validate({"gap": "8px", "padding": 0}).gap == 8
    result = load(tmp_path, "layout:\n  grid: {columns: 48, rows: 48}\n  gap: 40px\n", registry)
    assert errs(result) == [("CORE_CFG_CELL_TOO_SMALL", "/layout")]
    big = load(tmp_path, "layout:\n  gap: 501\n", registry)
    assert errs(big) == [("CORE_CFG_RANGE", "/layout/gap")]


def test_core_96_min_size_warns(tmp_path: Path, registry: Registry) -> None:
    text = modules(
        "  - module: big\n    id: small\n    position: {col: 1, row: 1, width: 2, height: 2}\n",
        "  - module: big\n    id: wide\n    position: {col: 1, row: 4, width: 12, height: 8}\n",
    )
    result = load(tmp_path, text, registry)
    assert result.ok
    (rec,) = result.warnings
    assert (rec.code, rec.path) == ("CORE_CFG_MIN_SIZE", "/modules/0/position")
    assert (rec.params["min_width"], rec.params["min_height"]) == (400, 300)


def test_core_96_stack_shares_height_for_min_size(tmp_path: Path, registry: Registry) -> None:
    tall = "display: {width: 1920, height: 2400}\n"
    one = load(tmp_path, tall + modules("  - module: big\n    position: middle_center\n"), registry)
    assert one.warnings == []
    two = load(
        tmp_path,
        tall
        + modules(
            "  - module: big\n    id: a\n    position: middle_center\n",
            "  - module: big\n    id: b\n    position: middle_center\n",
        ),
        registry,
    )
    assert [r.code for r in two.warnings] == ["CORE_CFG_MIN_SIZE"] * 2


# --- CORE-214, MOD-67: appearance -----------------------------------------------------


@pytest.mark.parametrize(
    ("css", "ok"),
    [
        (":host { color: red; }", True),
        ("div { background: url(img/bg.png); }", True),
        ("div { background: url('data:image/png;base64,AAAA'); }", True),
        ("@import 'x.css';", False),
        ("div { background: url(https://evil.example/x.png); }", False),
        ("div { background: url(/etc/passwd); }", False),
        ("div { background: url(../other/x.png); }", False),
        ("a{}" * 6000, False),
    ],
)
def test_core_214_css_limits(css: str, ok: bool) -> None:
    assert (check_css(css) is None) is ok


def test_core_214_css_and_classes_in_config(tmp_path: Path, registry: Registry) -> None:
    css = modules(clock("a", "top_left", "    appearance:\n      css: \"@import 'x';\"\n"))
    assert errs(load(tmp_path, css, registry)) == [("CORE_CFG_CSS", "/modules/0/appearance/css")]
    classes = modules(clock("a", "top_left", "    appearance:\n      classes: [ok, 'bad class']\n"))
    assert errs(load(tmp_path, classes, registry)) == [
        ("CORE_CFG_PATTERN", "/modules/0/appearance/classes/1")
    ]


def test_mod_67_header_text() -> None:
    keys = {"HEADER": "Meteo"}
    assert header_text(Appearance(), "weather", "it", keys.get) is None
    assert header_text(Appearance(header=True), "weather", "it", keys.get) == "Meteo"
    assert header_text(Appearance(header=True), "weather", "it", {}.get) == "weather"
    custom = Appearance(header=LocalizedText({"en": "Outside", "it": "Fuori"}))
    assert header_text(custom, "weather", "it", keys.get) == "Fuori"


# --- CORE-230..232: pages -------------------------------------------------------------

PAGES = (
    "pages:\n  default: home\n  list:\n    - id: home\n"
    "    - id: energy\n      title: {en: Energy}\n"
)


def test_core_230_pages_block(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, PAGES, registry)
    assert result.ok
    assert result.layout is not None
    assert [p.page for p in result.layout.pages] == ["home", "energy"]
    assert PageConfig(id="home").resolved_title("it") == "home"
    bad = load(
        tmp_path,
        "pages:\n  default: nope\n  list:\n    - id: a\n    - id: a\n    - id: Bad\n",
        registry,
    )
    assert sorted(errs(bad)) == [
        ("CORE_CFG_PATTERN", "/pages/list/2/id"),
    ]
    dupes = load(tmp_path, "pages:\n  default: nope\n  list:\n    - id: a\n    - id: a\n", registry)
    assert sorted(errs(dupes)) == [
        ("CORE_CFG_ID_DUPLICATE", "/pages/list/1/id"),
        ("CORE_CFG_PAGE_UNKNOWN", "/pages/default"),
    ]
    many = "pages:\n  list:\n" + "".join(f"    - id: p{i}\n" for i in range(33))
    assert errs(load(tmp_path, many, registry)) == [("CORE_CFG_LENGTH", "/pages/list")]


def test_core_230_implicit_main_page(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, modules(clock("a", "top_left", "    pages: [main]\n")), registry)
    assert result.ok, result.records
    assert result.layout is not None
    assert [p.page for p in result.layout.pages] == ["main"]


def test_core_231_instance_pages_and_positions(tmp_path: Path, registry: Registry) -> None:
    text = PAGES + modules(
        clock(
            "a",
            "top_left",
            "    pages: [home, energy, garden]\n    positions:\n"
            "      energy: {col: 1, row: 2, width: 6, height: 5}\n      shed: top_bar\n",
        )
    )
    result = load(tmp_path, text, registry)
    assert sorted(errs(result)) == [
        ("CORE_CFG_PAGE_UNKNOWN", "/modules/0/pages/2"),
        ("CORE_CFG_PAGE_UNKNOWN", "/modules/0/positions/shed"),
    ]
    good = load(
        tmp_path, text.replace(", garden", "").replace("      shed: top_bar\n", ""), registry
    )
    assert good.layout is not None
    home, energy = good.layout.pages
    assert home.placements[0].preset == "top_left"
    assert energy.placements[0].cells == Cells(1, 2, 6, 5)


def test_core_232_layout_checked_per_page_and_named(tmp_path: Path, registry: Registry) -> None:
    text = PAGES + modules(
        clock("a", "{col: 1, row: 1, width: 6, height: 6}"),
        clock(
            "b",
            "{col: 7, row: 1, width: 6, height: 6}",
            "    positions:\n      energy: {col: 3, row: 3, width: 2, height: 2}\n",
        ),
        clock("c", "{col: 1, row: 1, width: 2, height: 2}", "    pages: [energy]\n"),
    )
    result = load(tmp_path, text, registry)
    found: list[Any] = [(r.code, r.path, r.params["page"]) for r in result.errors]
    assert sorted(found) == [
        ("CORE_CFG_OVERLAP", "/modules/1/positions/energy", "energy"),
        ("CORE_CFG_OVERLAP", "/modules/2/position", "energy"),
    ]
    no_pages = load(tmp_path, modules(clock("a", "top_left"), clock("b", "top_bar")), registry)
    assert no_pages.ok
    overlap = load(
        tmp_path,
        modules(
            clock("a", "{col: 1, row: 1, width: 2, height: 2}"),
            clock("b", "{col: 1, row: 1, width: 2, height: 2}"),
        ),
        registry,
    )
    assert "page" not in overlap.errors[0].params


def test_core_140_display_block_is_optional(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "{}\n", registry)
    assert result.config is not None
    assert result.config.display == DisplayConfig()
    assert result.layout is not None
    assert (result.layout.viewport.width, result.layout.viewport.height) == (1920, 1080)
    rotated = load(
        tmp_path,
        "display: {output: HDMI-A-1, width: 2560, height: 1440, "
        "refresh: 60, rotation: 180, scale: 2}\n",
        registry,
    )
    assert rotated.layout is not None
    assert (rotated.layout.viewport.width, rotated.layout.viewport.height) == (1280, 720)
