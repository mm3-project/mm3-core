"""mm3-presence: schedule windows, midnight, days and clock changes.

Implements:
    PRS-05: Core spec 0.34, §9.2 Presence and power: module boundary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from mm3_presence.config import Window
from mm3_presence.schedule import in_window, intervals, next_boundary, resolve

ROME = ZoneInfo("Europe/Rome")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_prs_05_a_window_over_midnight_ends_the_next_day() -> None:
    windows = [Window(off="23:00", on="06:30")]
    assert in_window(windows, ROME, at("2026-10-30T23:30:00+01:00")) is not None
    assert in_window(windows, ROME, at("2026-10-31T06:29:00+01:00")) is not None
    assert in_window(windows, ROME, at("2026-10-31T06:30:00+01:00")) is None
    assert in_window(windows, ROME, at("2026-10-30T12:00:00+01:00")) is None


def test_prs_05_on_not_after_off_means_the_next_day() -> None:
    windows = [Window(off="08:00", on="08:00")]  # a full day, every day: always off
    assert len(intervals(windows, ROME, at("2026-10-30T12:00:00+01:00"))) == 1
    assert in_window(windows, ROME, at("2026-10-30T12:00:00+01:00")) is not None
    sat = [Window(off="08:00", on="08:00", days=["sat"])]
    start, end = intervals(sat, ROME, at("2026-10-30T12:00:00+01:00"))[0]
    assert (end - start).total_seconds() == 24 * 3600


def test_prs_05_days_are_the_days_a_window_starts() -> None:
    windows = [Window(off="23:00", on="06:00", days=["sat"])]
    assert in_window(windows, ROME, at("2026-10-30T23:30:00+01:00")) is None  # Friday
    assert in_window(windows, ROME, at("2026-10-31T23:30:00+01:00")) is not None  # Saturday
    assert in_window(windows, ROME, at("2026-11-01T05:00:00+01:00")) is not None  # Sunday morning
    assert in_window(windows, ROME, at("2026-11-01T23:30:00+01:00")) is None


def test_prs_05_overlapping_and_touching_windows_act_as_one() -> None:
    windows = [Window(off="22:00", on="01:00"), Window(off="00:30", on="06:00")]
    found = intervals(windows, ROME, at("2026-10-30T12:00:00+01:00"))
    assert len({start for start, _ in found}) == len(found)
    inside = in_window(windows, ROME, at("2026-10-31T00:45:00+01:00"))
    assert inside is not None
    assert inside[0] == at("2026-10-30T22:00:00+01:00").astimezone(UTC)
    assert inside[1] == at("2026-10-31T06:00:00+01:00").astimezone(UTC)


def test_prs_05_next_boundary() -> None:
    windows = [Window(off="23:00", on="06:30")]
    assert next_boundary(windows, ROME, at("2026-10-30T12:00:00+01:00")) == at(
        "2026-10-30T23:00:00+01:00"
    ).astimezone(UTC)
    assert next_boundary(windows, ROME, at("2026-10-30T23:10:00+01:00")) == at(
        "2026-10-31T06:30:00+01:00"
    ).astimezone(UTC)
    assert next_boundary([], ROME, at("2026-10-30T12:00:00+01:00")) is None


def test_prs_05_a_time_in_the_spring_gap_is_the_next_minute() -> None:
    # 2026-03-29: clocks go from 02:00 to 03:00 in Rome.
    assert resolve(date(2026, 3, 29), time(2, 30), ROME) == at(
        "2026-03-29T03:00:00+02:00"
    ).astimezone(UTC)


def test_prs_05_a_repeated_time_is_the_first_one() -> None:
    # 2026-10-25: 02:30 happens at 02:30+02:00 and again at 02:30+01:00.
    assert resolve(date(2026, 10, 25), time(2, 30), ROME) == at(
        "2026-10-25T02:30:00+02:00"
    ).astimezone(UTC)


def test_prs_05_the_window_length_follows_the_clock_change() -> None:
    windows = [Window(off="23:00", on="06:30")]
    start_at = at("2026-10-24T23:00:00+02:00").astimezone(UTC)
    start, end = next(i for i in intervals(windows, ROME, start_at) if i[0] == start_at)
    assert (end - start).total_seconds() == 8.5 * 3600  # one hour longer than the usual 7.5 h
