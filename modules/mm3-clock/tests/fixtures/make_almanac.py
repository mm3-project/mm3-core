"""Regenerate ``almanac.json``: reference sun and moon events for the clock's accuracy tests.

The values come from PyEphem (VSOP87 and ELP2000 based), an independent
implementation of the astronomy, not from the clock's own approximations
(NOAA equations, Meeus low-precision series). Events are those of the local
calendar day of each place; the illumination and the elongation are taken at
local noon. Regenerate with::

    uv run --with ephem python tests/fixtures/make_almanac.py

Implements:
    CLK-07: Core spec 0.19, §9.4 Reference modules.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ephem

PLACES = [
    ("Rome", 41.9028, 12.4964, "Europe/Rome"),
    ("New York", 40.7128, -74.0060, "America/New_York"),
    ("Sydney", -33.8688, 151.2093, "Australia/Sydney"),
    ("Cape Town", -33.9249, 18.4241, "Africa/Johannesburg"),
    ("Singapore", 1.3521, 103.8198, "Asia/Singapore"),
    ("Reykjavik", 64.1466, -21.9426, "Atlantic/Reykjavik"),
    ("Tromso", 69.6492, 18.9553, "Europe/Oslo"),
]
DATES = ["2026-03-20", "2026-06-21", "2026-09-23", "2026-12-21"]


def iso(value: ephem.Date) -> str:
    return value.datetime().replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def event(obs: ephem.Observer, body: ephem.Body, rising: bool, start: datetime, end: datetime):
    obs.date = ephem.Date(start)
    try:
        t = obs.next_rising(body) if rising else obs.next_setting(body)
    except ephem.AlwaysUpError:
        return None, "up"
    except ephem.NeverUpError:
        return None, "down"
    when = t.datetime().replace(tzinfo=UTC)
    return (iso(t) if when < end else None), None


rows = []
for name, lat, lon, tz in PLACES:
    for day in DATES:
        zone = ZoneInfo(tz)
        y, m, d = map(int, day.split("-"))
        start = datetime(y, m, d, tzinfo=zone).astimezone(UTC)
        end = datetime(y, m, d, tzinfo=zone).astimezone(UTC) + timedelta(days=1)
        end = (datetime(y, m, d, tzinfo=zone) + timedelta(days=1)).astimezone(UTC)
        noon = datetime(y, m, d, 12, tzinfo=zone).astimezone(UTC)
        obs = ephem.Observer()
        obs.lat, obs.lon, obs.elevation = str(lat), str(lon), 0
        sun, moon = ephem.Sun(), ephem.Moon()
        sunrise, always = event(obs, sun, True, start, end)
        sunset, _ = event(obs, sun, False, start, end)
        moonrise, _ = event(obs, moon, True, start, end)
        moonset, _ = event(obs, moon, False, start, end)
        obs.date = ephem.Date(noon)
        moon.compute(obs)
        sun.compute(obs)
        elong = (
            (float(ephem.Ecliptic(moon).lon) - float(ephem.Ecliptic(sun).lon))
            * 180
            / 3.141592653589793
        )
        rows.append(
            {
                "place": name,
                "lat": lat,
                "lon": lon,
                "tz": tz,
                "date": day,
                "sunrise": sunrise,
                "sunset": sunset,
                "sun_always": always,
                "moonrise": moonrise,
                "moonset": moonset,
                "noon": noon.isoformat().replace("+00:00", "Z"),
                "illumination": round(moon.phase, 2),
                "elongation": round(elong % 360, 2),
            }
        )
out = Path(__file__).with_name("almanac.json")
out.write_text(
    json.dumps({"source": "PyEphem " + ephem.__version__, "rows": rows}, indent=1) + "\n"
)
print(len(rows), "rows")
