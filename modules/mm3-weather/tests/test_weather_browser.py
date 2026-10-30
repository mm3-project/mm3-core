"""mm3-weather in a browser: icons, the three views, stale marking, grant message, languages.

Implements:
    WTH-06, WTH-08, WTH-09, WTH-10, WTH-12: Core spec 0.19, §9.4 Reference modules.
    MOD-36: Module Contract 1.0 rev 12, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mm3_weather import WeatherModule
from mm3_weather.models import CONDITIONS

pytest.importorskip("playwright.async_api")

from mm3.testing.browser import BROWSER_CHECKS, BrowserHarness, catalog_for, run_browser_checks

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from module_projects import chromium_available

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium cannot start (set MM3_CHROMIUM)"),
]

STATIC = Path(__file__).resolve().parents[1] / "src" / "mm3_weather" / "static"
LANGS = ("en", "it", "fr", "es", "de")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
PLACE = {"lat": 41.9, "lon": 12.5, "name": "Rome"}


def current(**extra: Any) -> dict[str, Any]:
    data = {
        "observed_at": "2026-06-01T14:00:00+02:00",
        "condition": "partly_cloudy",
        "is_day": True,
        "temperature": 21.6,
        "feels_like": 20.2,
        "humidity": 55,
        "wind_speed": 12.4,
        "precipitation": 0.3,
    }
    return {
        "type": "current",
        "units": "metric",
        "location": PLACE,
        "fetched_at": (NOW - timedelta(minutes=2)).isoformat(),
        "data": data | extra,
    }


def daily(n: int = 3) -> dict[str, Any]:
    conditions = ["clear", "rain", "snow", "thunderstorm"]
    days = [
        {
            "date": f"2026-06-{1 + i:02d}",
            "condition": conditions[i % 4],
            "temp_min": 11.2 + i,
            "temp_max": 22.8 + i,
            "precipitation_probability": 30 + i,
            "sunrise": f"2026-06-{1 + i:02d}T05:40:00+02:00",
            "sunset": f"2026-06-{1 + i:02d}T20:45:00+02:00",
        }
        for i in range(n)
    ]
    return current() | {"type": "daily", "data": days}


def hourly(n: int = 4) -> dict[str, Any]:
    hours = [
        {
            "time": f"2026-06-01T{14 + i:02d}:00:00+02:00",
            "condition": "clear" if i % 2 else "cloudy",
            "is_day": i < 3,
            "temperature": 20.0 + i,
            "precipitation_probability": 10 * i,
        }
        for i in range(n)
    ]
    return current() | {"type": "hourly", "data": hours}


@asynccontextmanager
async def weather(
    config: dict[str, Any] | None = None, *, paused: bool = False, **kwargs: Any
) -> AsyncIterator[BrowserHarness]:
    kwargs.setdefault("timezone", "Europe/Rome")
    cfg = {"type": "current", "refresh": "PT10M"} | (config or {})
    async with BrowserHarness(WeatherModule, config=cfg, **kwargs) as h:
        await h.page.clock.install(time=NOW - timedelta(seconds=30) if paused else NOW)
        assert await h.mount(), h.mount_failures
        if paused:
            await h.page.clock.pause_at(NOW)
        yield h


async def text(h: BrowserHarness, selector: str = ".weather") -> str:
    return str(
        await h.page.evaluate(
            """(sel) => {
                const node = document.querySelector("mm3-weather").shadow.querySelector(sel);
                return node ? node.textContent.replace(/\\s+/g, " ").trim() : null;
            }""",
            selector,
        )
    )


async def count(h: BrowserHarness, selector: str) -> int:
    return int(
        await h.page.evaluate(
            "(s) => document.querySelector('mm3-weather').shadow.querySelectorAll(s).length",
            selector,
        )
    )


def plain(value: str) -> str:
    return value.replace("\N{NARROW NO-BREAK SPACE}", " ").replace("\N{NO-BREAK SPACE}", " ")


# --- WTH-06: icons ------------------------------------------------------------------------


def test_wth_06_every_condition_has_a_day_and_night_icon_file_in_step_with_the_script() -> None:
    script = (STATIC / "icons.js").read_text(encoding="utf-8")
    icons = STATIC / "icons"
    names = [*CONDITIONS, "clear-night", "partly_cloudy-night"]
    for name in names:
        svg_text = (icons / f"{name}.svg").read_text(encoding="utf-8")
        path = re.search(r' d="([^"]+)"', svg_text)
        assert path, name
        assert f'"{name}": "{path.group(1)}"' in script, name
    assert "Apache" in (icons / "LICENSE").read_text(encoding="utf-8")


async def test_wth_06_icons_are_svg_paths_in_theme_colours() -> None:
    async with weather() as h:
        await h.state(current())
        assert await count(h, "svg.icon path") == 1
        fill = await h.page.evaluate(
            "() => getComputedStyle(document.querySelector('mm3-weather').shadow"
            ".querySelector('svg.icon')).fill"
        )
        assert fill != "rgb(0, 0, 0)"  # the theme colour, not the default black
        await h.state(current(condition="clear", is_day=False))
        night_clear = await h.page.evaluate(
            "() => document.querySelector('mm3-weather').shadow.querySelector('svg.icon path')"
            ".getAttribute('d')"
        )
        await h.state(current(condition="clear", is_day=True))
        day_clear = await h.page.evaluate(
            "() => document.querySelector('mm3-weather').shadow.querySelector('svg.icon path')"
            ".getAttribute('d')"
        )
        assert night_clear != day_clear


# --- WTH-10: views --------------------------------------------------------------------------


async def test_wth_10_current_shows_icon_temperature_condition_and_optional_fields() -> None:
    cfg = {"show_humidity": True, "show_wind": True, "show_precipitation": True}
    async with weather(cfg) as h:
        await h.state(current())
        assert plain(await text(h, ".temp")) == "22°C"
        assert await text(h, ".text") == "Partly cloudy"
        assert await text(h, ".place") == "Rome"
        details = plain(await text(h, ".details"))
        assert "Feels like 20°C" in details
        assert "Humidity 55%" in details
        assert "Wind 12 km/h" in details
        assert "Precipitation 0.3 mm" in details


async def test_wth_10_current_hides_optional_fields_by_default_and_by_option() -> None:
    async with weather() as h:
        await h.state(current())
        details = plain(await text(h, ".details"))
        assert "Feels like" in details
        assert "Humidity" not in details
        assert "Wind" not in details
    async with weather({"show_feels_like": False}) as h:
        await h.state(current())
        assert await count(h, ".details") == 0
    async with weather() as h:
        await h.state(current(feels_like=None, humidity=None))
        assert await count(h, ".details") == 0


async def test_wth_10_imperial_units() -> None:
    async with weather({"show_wind": True}) as h:
        await h.state(current() | {"units": "imperial"})
        assert plain(await text(h, ".temp")) == "22°F"
        assert "mph" in plain(await text(h, ".details"))


async def test_wth_10_night_variants_of_the_condition_text() -> None:
    async with weather() as h:
        await h.state(current(condition="clear", is_day=False))
        assert await text(h, ".text") == "Clear night"
        await h.state(current(condition="rain", is_day=False))
        assert await text(h, ".text") == "Rain"


async def test_wth_10_daily_lists_days_with_weekday_range_and_probability() -> None:
    async with weather({"type": "daily"}) as h:
        await h.state(daily(3))
        assert await count(h, ".list li") == 3
        first = plain(await text(h, ".list li"))
        assert first.startswith("Mon")  # 2026-06-01 is a Monday, in any time zone
        assert "23°C" in first
        assert "11°C" in first
        assert "30%" in first
        assert "5:40" not in first  # show_sun is off


async def test_wth_10_daily_weekday_does_not_depend_on_the_time_zone() -> None:
    for zone in ("Pacific/Honolulu", "Pacific/Auckland"):
        async with weather({"type": "daily"}, timezone=zone) as h:
            await h.state(daily(1))
            assert plain(await text(h, ".list li")).startswith("Mon"), zone


async def test_wth_10_daily_sun_option() -> None:
    async with weather({"type": "daily", "show_sun": True}) as h:
        await h.state(daily(1))
        assert "5:40 AM\N{EN DASH}8:45 PM" in plain(await text(h, ".sun"))


async def test_wth_10_hourly_lists_hours() -> None:
    async with weather({"type": "hourly"}) as h:
        await h.state(hourly(4))
        assert await count(h, ".list li") == 4
        first = plain(await text(h, ".list li"))
        assert first.startswith("2 PM")
        assert "20°C" in first
        assert "0%" in first


async def test_wth_10_empty_and_error_states_say_what_is_going_on() -> None:
    async with weather() as h:
        await h.state({})
        assert await text(h, ".notice") == "Loading weather…"
        await h.state({"error": "unavailable"})
        assert await text(h, ".notice") == catalog_for(WeatherModule, "en")["WEATHER_UNAVAILABLE"]


# --- WTH-09: grant message ---------------------------------------------------------------


async def test_wth_09_grant_message_names_the_grant_in_every_language() -> None:
    state = {"error": "grant", "grant": "https://api.open-meteo.com/"}
    for lang in LANGS:
        async with weather(locale=lang) as h:
            await h.state(state)
            message = await text(h, ".notice")
            assert 'http: ["https://api.open-meteo.com/"]' in message, lang
            assert message == catalog_for(WeatherModule, lang)["GRANT_MISSING"].replace(
                "{grant}", "https://api.open-meteo.com/"
            )


# --- WTH-08: stale data ------------------------------------------------------------------


async def test_wth_08_data_older_than_twice_the_refresh_is_dimmed_with_its_age() -> None:
    fresh = current() | {"fetched_at": (NOW - timedelta(minutes=19)).isoformat()}
    old = current() | {"fetched_at": (NOW - timedelta(minutes=45)).isoformat()}
    async with weather() as h:
        await h.state(fresh)
        assert await count(h, ".weather.stale") == 0
        assert await count(h, ".age") == 0
        await h.state(old)
        assert await count(h, ".weather.stale") == 1
        assert plain(await text(h, ".age")) == "Last updated 45 min ago"
        opacity = await h.page.evaluate(
            "() => getComputedStyle(document.querySelector('mm3-weather').shadow"
            ".querySelector('.now')).opacity"
        )
        assert float(opacity) < 1


async def test_wth_08_age_in_hours_and_the_refresh_option_sets_the_limit() -> None:
    async with weather({"refresh": "PT1H"}) as h:
        await h.state(current() | {"fetched_at": (NOW - timedelta(minutes=90)).isoformat()})
        assert await count(h, ".weather.stale") == 0  # limit is 2 h
        await h.state(current() | {"fetched_at": (NOW - timedelta(hours=3)).isoformat()})
        assert plain(await text(h, ".age")) == "Last updated 3 hr ago"


async def test_wth_08_data_becomes_stale_as_time_passes_without_a_new_state() -> None:
    state = current() | {"fetched_at": (NOW - timedelta(minutes=15)).isoformat()}
    async with weather(paused=True) as h:
        await h.state(state)
        await h.page.clock.run_for(0)
        assert await count(h, ".weather.stale") == 0
        await h.page.clock.run_for(
            6 * 60_000
        )  # 21 minutes old; the element looks again each minute
        assert await count(h, ".weather.stale") == 1


# --- WTH-10, WTH-12: five languages, three views -------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("view", ["current", "daily", "hourly"])
async def test_wth_12_three_views_render_in_five_languages(lang: str, view: str) -> None:
    states = {"current": current(), "daily": daily(5), "hourly": hourly(6)}
    cfg = {"type": view, "show_humidity": True, "show_wind": True, "show_precipitation": True}
    cat = catalog_for(WeatherModule, lang)
    async with weather(cfg, locale=lang) as h:
        r = await h.state(states[view])
        assert not r.failed, r.error
        shown = plain(await text(h))
        assert not re.search(r"\b[A-Z][A-Z_]{4,}\b", shown), shown  # no catalog key leaked
        if view == "current":
            assert cat["WEATHER_PARTLY_CLOUDY"] in shown
            for key in ("FEELS_LIKE", "HUMIDITY", "WIND", "PRECIPITATION"):
                assert cat[key] in shown
        else:
            assert await count(h, ".list li") == (5 if view == "daily" else 6)
        report = await h.report()
        assert not report["csp"], report["csp"]


async def test_wth_10_all_condition_texts_exist_in_every_language() -> None:
    for lang in LANGS:
        cat = catalog_for(WeatherModule, lang)
        for c in CONDITIONS:
            assert cat[f"WEATHER_{c.upper()}"], (lang, c)
        assert cat["WEATHER_CLEAR_NIGHT"]
        assert cat["WEATHER_PARTLY_CLOUDY_NIGHT"]
        assert cat["HEADER"]


async def test_wth_10_switching_the_language_rerenders_the_text() -> None:
    async with weather() as h:
        await h.state(current())
        await h.set_locale("it")
        assert await text(h, ".text") == "Parzialmente nuvoloso"


# --- MOD-36: the kit's own browser checks -------------------------------------------------


async def test_mod_36_browser_checks_pass_for_the_three_views() -> None:
    states = [current(), daily(5), hourly(8), {"error": "grant", "grant": "https://x/"}]
    async with weather() as h:
        results = await run_browser_checks(h, states)
    status = {r.id: r.status for r in results}
    assert set(status) == set(BROWSER_CHECKS)
    assert not {i for i, s in status.items() if s == "fail"}, status


@pytest.mark.parametrize("view", ["current", "daily", "hourly"])
@pytest.mark.parametrize("size", [(200, 100), (320, 240), (180, 80), (600, 400)])
async def test_wth_10_nothing_overflows_with_every_option_a_stale_age_and_a_long_name(
    view: str, size: tuple[int, int]
) -> None:
    cfg = {
        "type": view,
        "show_humidity": True,
        "show_wind": True,
        "show_precipitation": True,
        "show_sun": True,
    }
    old = {"fetched_at": (NOW - timedelta(hours=2)).isoformat()}
    long_place = {"lat": 1.0, "lon": 2.0, "name": "Sant'Angelo in Pontano di Sopra"}
    states = {"current": current(), "daily": daily(7), "hourly": hourly(12)}
    async with weather(cfg, locale="de") as h:
        await h.resize(*size)
        await h.state(states[view] | old | {"location": long_place}, repeat=1)
        await h.resize(*size)
        found = await h.overflow()
        assert not found["content"], (view, size, found["content"])
        assert not found["page"], (view, size)
