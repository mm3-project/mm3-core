"""mm3-clock in a browser: time zone, formats, hands, ticks, sun and moon, languages.

Implements:
    CLK-01, CLK-02, CLK-03, CLK-04, CLK-05, CLK-06, CLK-07, CLK-08: Core spec 0.19,
        §9.4 Reference modules.
    MOD-36: Module Contract 1.0 rev 12, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mm3_clock import ClockModule

pytest.importorskip("playwright.async_api")

from mm3.testing.browser import BROWSER_CHECKS, BrowserHarness, catalog_for, run_browser_checks

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from module_projects import chromium_available

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium cannot start (set MM3_CHROMIUM)"),
]

HERE = Path(__file__).resolve().parent
LOCALES = HERE.parent / "src" / "mm3_clock" / "locales"
LANGS = ("en", "it", "fr", "es", "de")
ROME = {"lat": 41.9028, "lon": 12.4964, "name": "Rome"}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC)


@asynccontextmanager
async def clock(
    iso: str,
    config: dict[str, Any] | None = None,
    *,
    paused: bool = False,
    **kwargs: Any,
) -> AsyncIterator[BrowserHarness]:
    """A mounted clock whose browser time is ``iso``.

    Fake time keeps flowing after ``install``; with ``paused`` it stops exactly at ``iso`` once
    the element is mounted, and only ``run_for`` moves it, so tick tests are deterministic.
    """
    async with BrowserHarness(ClockModule, config=config or {}, **kwargs) as h:
        start = at(iso)
        await h.page.clock.install(time=start - timedelta(seconds=30) if paused else start)
        assert await h.mount(), h.mount_failures
        if paused:
            await h.page.clock.pause_at(start)
        await h.page.clock.run_for(0)
        yield h


async def text(h: BrowserHarness, selector: str = ".clock") -> str:
    """Whitespace-normalised text of the first match in the element's shadow tree."""
    return str(
        await h.page.evaluate(
            """(sel) => {
                const node = document.querySelector("mm3-clock").shadow.querySelector(sel);
                return node ? node.textContent.replace(/\\s+/g, " ").trim() : null;
            }""",
            selector,
        )
    )


async def count(h: BrowserHarness, selector: str) -> int:
    return int(
        await h.page.evaluate(
            "(s) => document.querySelector('mm3-clock').shadow.querySelectorAll(s).length", selector
        )
    )


async def rotations(h: BrowserHarness) -> dict[str, float]:
    found = await h.page.evaluate(
        """() => Object.fromEntries([...document.querySelector("mm3-clock").shadow
            .querySelectorAll(".hand")]
            .map((n) => [n.classList[1], n.getAttribute("transform")]))"""
    )
    return {k: float(re.search(r"rotate\(([-\d.]+)\)", v).group(1)) for k, v in found.items()}  # type: ignore[union-attr]


# --- CLK-01, CLK-02: the mirror's time zone, formats ---------------------------------


async def test_clk_01_time_is_shown_in_the_mirror_time_zone_not_the_browser() -> None:
    cfg = {"time_format": "24", "show_date": False}
    async with clock("2026-06-01T10:30:15+00:00", cfg, timezone="Asia/Tokyo") as h:
        assert await text(h, ".time") == "19:30:15"
    async with clock("2026-06-01T10:30:15+00:00", cfg, timezone="America/New_York") as h:
        assert await text(h, ".time") == "06:30:15"


async def test_clk_02_instance_timezone_replaces_the_mirror_one() -> None:
    cfg = {"time_format": "24", "timezone": "Europe/Rome", "show_date": False}
    async with clock("2026-06-01T10:30:15+00:00", cfg, timezone="Asia/Tokyo") as h:
        assert await text(h, ".time") == "12:30:15"


@pytest.mark.parametrize(
    ("fmt", "locale", "expected"),
    [
        ("24", "en", "15:04:05"),
        ("12", "en", "3:04:05 PM"),
        ("locale", "en", "3:04:05 PM"),
        ("locale", "de", "15:04:05"),
        ("12", "de", "3:04:05 PM"),
    ],
)
async def test_clk_02_time_format(fmt: str, locale: str, expected: str) -> None:
    cfg = {"time_format": fmt, "show_date": False}
    async with clock("2026-06-01T15:04:05+00:00", cfg, locale=locale) as h:
        assert (await text(h, ".time")).replace("\N{NARROW NO-BREAK SPACE}", " ") == expected


async def test_clk_02_seconds_and_date_options() -> None:
    cfg = {"time_format": "24", "show_seconds": False, "date_style": "full"}
    async with clock("2026-06-01T15:04:05+00:00", cfg) as h:
        assert await text(h, ".time") == "15:04"
        assert await text(h, ".date") == "Monday, June 1, 2026"
    async with clock("2026-06-01T15:04:05+00:00", {"show_date": False}) as h:
        assert await count(h, ".date") == 0


async def test_clk_02_week_number_is_iso_8601() -> None:
    cases = [
        ("2026-12-31T12:00:00+00:00", "Week 53"),  # a 53-week year
        ("2027-01-01T12:00:00+00:00", "Week 53"),  # still last year's week
        ("2027-01-04T12:00:00+00:00", "Week 1"),
        ("2024-12-30T12:00:00+00:00", "Week 1"),  # belongs to 2025
        ("2026-06-01T12:00:00+00:00", "Week 23"),
    ]
    for iso, expected in cases:
        async with clock(iso, {"show_week": True}) as h:
            assert await text(h, ".week") == expected, iso
    async with clock("2026-06-01T12:00:00+00:00", {}) as h:
        assert await count(h, ".week") == 0


# --- CLK-03: other time zones -------------------------------------------------------


async def test_clk_03_zones_show_time_label_and_day_offset() -> None:
    cfg = {
        "time_format": "24",
        "show_seconds": False,
        "zones": [
            {"timezone": "Asia/Tokyo"},
            {"timezone": "America/Los_Angeles", "label": {"en": "Home", "it": "Casa"}},
            {"timezone": "America/New_York"},
        ],
    }
    # New York is 19:30 on 1 June; Tokyo is already 2 June
    async with clock("2026-06-01T23:30:00+00:00", cfg, timezone="America/New_York") as h:
        rows = await h.page.evaluate(
            """() => [...document.querySelector("mm3-clock").shadow.querySelectorAll(".zones li")]
                .map((li) => li.textContent.replace(/\\s+/g, " ").trim())"""
        )
        assert rows == ["Tokyo 08:30 +1 d", "Home 16:30", "New York 19:30"]
    async with clock("2026-06-01T23:30:00+00:00", cfg, timezone="Asia/Tokyo", locale="it") as h:
        rows = await h.page.evaluate(
            """() => [...document.querySelector("mm3-clock").shadow.querySelectorAll(".zones li")]
                .map((li) => li.textContent.replace(/\\s+/g, " ").trim())"""
        )
        assert rows == [
            "Tokyo 08:30",
            "Casa 16:30 \N{MINUS SIGN}1 g",
            "New York 19:30 \N{MINUS SIGN}1 g",
        ]


# --- CLK-04: the analog face --------------------------------------------------------


async def test_clk_04_hands_follow_the_time() -> None:
    async with clock("2026-06-01T06:30:30+00:00", {"display": "analog"}) as h:
        hands = await rotations(h)
        assert hands["hour"] == pytest.approx(195.25)
        assert hands["minute"] == pytest.approx(183.0)
        assert hands["second"] == pytest.approx(180.0)
        assert await count(h, ".time") == 0


async def test_clk_04_no_second_hand_without_seconds() -> None:
    async with clock(
        "2026-06-01T03:00:00+00:00", {"display": "analog", "show_seconds": False}
    ) as h:
        assert set(await rotations(h)) == {"hour", "minute"}


@pytest.mark.parametrize(
    ("face", "ticks", "numbers"), [("ticks", 60, 0), ("numbers", 0, 12), ("none", 0, 0)]
)
async def test_clk_04_face_option(face: str, ticks: int, numbers: int) -> None:
    async with clock("2026-06-01T03:00:00+00:00", {"display": "analog", "face": face}) as h:
        assert await count(h, ".tick") == ticks
        assert await count(h, ".number") == numbers


async def test_clk_04_the_face_stays_square_and_both_follows_the_shape() -> None:
    async with clock("2026-06-01T03:00:00+00:00", {"display": "both"}) as h:
        await h.resize(600, 200)
        await h.page.clock.run_for(0)
        box = await h.page.evaluate(
            """() => { const f = document.querySelector("mm3-clock").shadow.querySelector(".face")
                .getBoundingClientRect(); return [f.width, f.height]; }"""
        )
        assert abs(box[0] - box[1]) < 1
        assert "row" in await h.page.evaluate(
            "() => document.querySelector('mm3-clock').shadow.querySelector('.clock').className"
        )
        await h.resize(200, 600)
        await h.page.clock.run_for(0)
        assert "column" in await h.page.evaluate(
            "() => document.querySelector('mm3-clock').shadow.querySelector('.clock').className"
        )


# --- CLK-05: ticks at the next boundary ---------------------------------------------


async def test_clk_05_second_ticks_land_on_the_boundary() -> None:
    cfg = {"time_format": "24", "show_date": False}
    async with clock("2026-06-01T12:00:00.400+00:00", cfg, paused=True) as h:
        assert await text(h, ".time") == "12:00:00"
        await h.page.clock.run_for(599)
        assert await text(h, ".time") == "12:00:00"
        await h.page.clock.run_for(1)
        assert await text(h, ".time") == "12:00:01"
        await h.page.clock.run_for(1000)
        assert await text(h, ".time") == "12:00:02"


async def test_clk_05_nothing_runs_between_minute_boundaries() -> None:
    cfg = {"time_format": "24", "show_seconds": False, "show_date": False}
    async with clock("2026-06-01T12:00:30+00:00", cfg, paused=True) as h:
        await asyncio.sleep(0.3)  # let the harness finish its own resize render
        await h.page.evaluate(
            """() => { const el = document.querySelector("mm3-clock"); const render = el.render;
                window.renders = 0;
                el.render = function (...a) {
                    window.renders++;
                    return render.apply(this, a);
                }; }"""
        )
        await h.page.clock.run_for(29_999)
        assert await h.page.evaluate("() => window.renders") == 0
        assert await text(h, ".time") == "12:00"
        await h.page.clock.run_for(1)
        assert await text(h, ".time") == "12:01"
        assert await h.page.evaluate("() => window.renders") == 1
        await h.page.clock.run_for(60_000)
        assert await h.page.evaluate("() => window.renders") == 2


async def test_clk_05_a_late_timer_corrects_itself() -> None:
    cfg = {"time_format": "24", "show_date": False}
    async with clock("2026-06-01T12:00:00+00:00", cfg, paused=True) as h:
        await h.page.clock.fast_forward(90_000)  # a suspended tab wakes up late
        assert await text(h, ".time") == "12:01:30"


# --- CLK-06, CLK-07: sun and moon ---------------------------------------------------


async def test_clk_06_sun_and_moon_rows_for_rome() -> None:
    cfg = {"sun": True, "moon": True, "time_format": "24"}
    async with clock("2026-03-20T11:00:00+00:00", cfg, timezone="Europe/Rome", location=ROME) as h:
        rows = await h.page.evaluate(
            """() => Object.fromEntries([...document.querySelector("mm3-clock").shadow
                .querySelectorAll(".sky li")]
                .map((li) => [...li.children].map((c) => c.textContent)))"""
        )
        assert set(rows) == {
            "Sunrise",
            "Sunset",
            "Day length",
            "New moon",
            "Moonrise",
            "Moonset",
        }
        assert rows["Sunrise"] == "06:13"  # fixture: 05:13:20 UTC, CET
        assert rows["Sunset"] == "18:22"
        assert rows["Day length"] == "12 h 8 min"
        assert rows["New moon"] == "3%"


async def test_clk_06_instance_location_wins_over_the_mirror_location() -> None:
    cfg = {"sun": True, "time_format": "24", "location": {"lat": -33.8688, "lon": 151.2093}}
    async with clock(
        "2026-03-20T00:00:00+00:00", cfg, timezone="Australia/Sydney", location=ROME
    ) as h:
        sunrise = (await text(h, ".sky")).split("Sunrise")[1][:5]
        assert sunrise == "06:" + sunrise[3:]  # Sydney, autumn equinox: about 06:3x


async def test_clk_06_no_location_shows_a_hint_and_nothing_else_changes() -> None:
    cfg = {"sun": True, "moon": True, "time_format": "24", "show_date": False}
    async with clock("2026-03-20T11:00:00+00:00", cfg) as h:
        assert await text(h, ".hint") == "Set a location to see sun and moon times"
        assert await count(h, ".sky") == 0
        assert await text(h, ".time") == "11:00:00"


async def test_clk_06_nothing_extra_without_sun_or_moon() -> None:
    async with clock("2026-03-20T11:00:00+00:00", {}, location=ROME) as h:
        assert await count(h, ".sky") == 0
        assert await count(h, ".hint") == 0


@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("2026-06-21T12:00:00+00:00", "Polar day: the sun does not set"),
        ("2026-12-21T12:00:00+00:00", "Polar night: the sun does not rise"),
    ],
)
async def test_clk_06_polar_day_and_night_show_text(iso: str, expected: str) -> None:
    place = {"lat": 78.22, "lon": 15.65, "name": "Longyearbyen"}
    async with clock(iso, {"sun": True}, timezone="Arctic/Longyearbyen", location=place) as h:
        assert await text(h, ".sky") == expected


# --- CLK-08: five languages ---------------------------------------------------------


def po_keys(lang: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pairs = re.findall(
        r'msgid "(.+?)"\nmsgstr "(.*?)"\n',
        (LOCALES / lang / "LC_MESSAGES" / "messages.po").read_text(),
    )
    for key, value in pairs:
        out[key] = value
    return out


def test_clk_08_every_language_has_the_same_keys() -> None:
    keys = {lang: set(po_keys(lang)) for lang in LANGS}
    assert len({frozenset(k) for k in keys.values()}) == 1
    for lang in LANGS:
        assert all(po_keys(lang).values()), lang


def test_clk_08_every_key_the_code_uses_is_in_the_catalog() -> None:
    source = (HERE.parent / "src" / "mm3_clock" / "static" / "clock.js").read_text()
    used = set(re.findall(r'this\.t\("([A-Z_]+)"', source))
    used |= {f"MOON_{i}" for i in range(8)} if "`MOON_${" in source else set()
    config = (HERE.parent / "src" / "mm3_clock" / "config.py").read_text()
    used |= set(re.findall(r'"(CFG_[A-Z_]+)"', config))
    used.add("HEADER")
    for lang in LANGS:
        assert used <= set(po_keys(lang)), (lang, sorted(used - set(po_keys(lang))))


def test_clk_08_moon_phase_names_differ_in_every_language() -> None:
    for lang in LANGS:
        names = [po_keys(lang)[f"MOON_{i}"] for i in range(8)]
        assert len(set(names)) == 8, lang


@pytest.mark.parametrize("lang", LANGS)
async def test_clk_08_catalog_reaches_the_element(lang: str) -> None:
    cfg = catalog_for(ClockModule, lang)
    assert cfg["MOON_4"] == po_keys(lang)["MOON_4"]


FULL = {
    "display": "both",
    "sun": True,
    "moon": True,
    "show_week": True,
    "zones": [{"timezone": "Asia/Tokyo"}],
}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize(
    ("size", "config"),
    [((180, 80), {}), ((320, 240), {"display": "analog"}), ((480, 520), FULL)],
)
async def test_clk_07_five_languages_three_sizes(
    lang: str, size: tuple[int, int], config: dict[str, Any]
) -> None:
    async with clock(
        "2026-03-20T11:00:00+00:00", config, locale=lang, timezone="Europe/Rome", location=ROME
    ) as h:
        await h.resize(*size)
        await h.page.clock.run_for(0)
        found = await h.overflow()
        assert not found["content"], (lang, size, found)
        assert not found["page"], (lang, size, found)
        report = await h.report()
        assert report["errors"] == []
        assert report["csp"] == []
        assert report["net"] == []
        warnings = [r for r in report["logs"] if r.get("level") in ("warn", "error")]
        assert warnings == [], (lang, size, warnings)


# --- MOD-36: the contract's browser checks ---------------------------------------------


async def test_mod_36_clock_passes_every_harness_row() -> None:
    # seconds off: the text changes once a minute, so two renders in a row cannot differ
    config = {"show_seconds": False}
    async with clock("2026-03-20T11:00:00+00:00", config, location=ROME) as h:
        results = await run_browser_checks(h, [{}])
    status = {r.id: r.status for r in results}
    assert set(status) == set(BROWSER_CHECKS)
    assert all(s in ("pass", "skip") for s in status.values()), json.dumps(status)
