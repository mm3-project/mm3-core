"""mm3-clock: sun and moon values against an independent ephemeris.

Implements:
    CLK-06, CLK-07: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "src" / "mm3_clock" / "static"
FIXTURE = json.loads((HERE / "fixtures" / "almanac.json").read_text())
ROWS = FIXTURE["rows"]

SUN_MINUTES = 2
MOON_MINUTES = 10
ILLUMINATION_POINTS = 1

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def minutes_apart(a: str, b: str) -> float:
    """Distance in minutes between two ISO instants."""
    fa, fb = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b))
    return abs((fa - fb).total_seconds()) / 60


@pytest.fixture(scope="module")
def computed(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, object]]:
    """Run the module's own astro.js in Node over every fixture row."""
    work = tmp_path_factory.mktemp("astro")
    astro = work / "astro.mjs"  # Node reads .js as CommonJS here; the browser reads it as a module
    shutil.copy(STATIC / "astro.js", astro)
    node = shutil.which("node")
    assert node is not None
    run = subprocess.run(  # noqa: S603
        [
            node,
            str(HERE / "astro_check.mjs"),
            str(astro),
            str(HERE / "fixtures" / "almanac.json"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(run.stdout)  # type: ignore[no-any-return]


def test_clk_07_fixture_covers_places_and_dates() -> None:
    places = {r["place"]: r["lat"] for r in ROWS}
    dates = {r["date"] for r in ROWS}
    assert len(places) >= 5
    assert len(dates) >= 4
    assert any(lat > 60 for lat in places.values())
    assert any(lat < 0 for lat in places.values())


@pytest.mark.parametrize("index", range(len(ROWS)))
def test_clk_07_sun_events_within_two_minutes(
    index: int, computed: list[dict[str, object]]
) -> None:
    want, got = ROWS[index], computed[index]
    assert got["sun_always"] == want["sun_always"], (want["place"], want["date"])
    for event in ("sunrise", "sunset"):
        if want[event] is None:
            assert got[event] is None
        else:
            assert got[event] is not None, (want["place"], want["date"], event)
            assert minutes_apart(str(got[event]), want[event]) <= SUN_MINUTES, (
                want["place"],
                want["date"],
                event,
            )


@pytest.mark.parametrize("index", range(len(ROWS)))
def test_clk_07_moon_events_within_ten_minutes(
    index: int, computed: list[dict[str, object]]
) -> None:
    want, got = ROWS[index], computed[index]
    for event in ("moonrise", "moonset"):
        if want[event] is None:
            assert got[event] is None, (want["place"], want["date"], event)
        else:
            assert got[event] is not None, (want["place"], want["date"], event)
            assert minutes_apart(str(got[event]), want[event]) <= MOON_MINUTES, (
                want["place"],
                want["date"],
                event,
            )


@pytest.mark.parametrize("index", range(len(ROWS)))
def test_clk_07_illumination_within_one_point(
    index: int, computed: list[dict[str, object]]
) -> None:
    want, got = ROWS[index], computed[index]
    assert abs(float(got["illumination"]) - want["illumination"]) <= ILLUMINATION_POINTS  # type: ignore[arg-type]
