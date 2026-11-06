"""Schedule windows: when the display is off by the clock (PRS-05).

A window ``{off, on, days}`` starts at ``off`` on each listed day and ends at
``on``, in the engine time zone; an ``on`` not after ``off`` falls on the next
day. Windows that overlap or touch act as one. A time that does not exist on a
day because of a clock change is taken as the next minute that does; one that
exists twice is taken at its first occurrence.

Implements:
    PRS-05: Core spec 0.34, §9.2 Presence and power: module boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import DAYS, Window

__all__ = ["Interval", "in_window", "intervals", "next_boundary", "resolve"]

Interval = tuple[datetime, datetime]
"""A window as two aware UTC instants: start (included) and end (excluded)."""

_LOOKBACK_DAYS = 2
_LOOKAHEAD_DAYS = 9


def resolve(day: date, clock: time, tz: ZoneInfo) -> datetime:
    """Return the instant of a local ``HH:MM`` on a day, as aware UTC.

    Args:
        day: The local date.
        clock: The local time of day.
        tz: The time zone.

    Returns:
        The first occurrence of the time, or, when it does not exist that day,
        the next minute that does.
    """
    naive = datetime.combine(day, clock)
    for _ in range(24 * 60):
        local = naive.replace(tzinfo=tz, fold=0)
        instant = local.astimezone(UTC)
        if instant.astimezone(tz).replace(tzinfo=None) == naive:
            return instant
        naive += timedelta(minutes=1)
    return naive.replace(tzinfo=tz).astimezone(UTC)  # unreachable: gaps are shorter than a day


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def intervals(windows: Sequence[Window], tz: ZoneInfo, around: datetime) -> list[Interval]:
    """Return the merged windows near an instant, sorted.

    Covers the days that can matter for "inside a window now" and for the
    next boundary within a week.

    Args:
        windows: The configured windows.
        tz: The engine time zone.
        around: The instant, aware.

    Returns:
        Disjoint ``(start, end)`` intervals in UTC.
    """
    today = around.astimezone(tz).date()
    raw: list[Interval] = []
    for offset in range(-_LOOKBACK_DAYS, _LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        for window in windows:
            if window.days is not None and DAYS[day.weekday()] not in window.days:
                continue
            off, on = _clock(window.off), _clock(window.on)
            end_day = day if on > off else day + timedelta(days=1)
            start, end = resolve(day, off, tz), resolve(end_day, on, tz)
            if end > start:
                raw.append((start, end))
    raw.sort()
    merged: list[Interval] = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def in_window(windows: Sequence[Window], tz: ZoneInfo, now: datetime) -> Interval | None:
    """Return the window that contains ``now``, if any.

    Args:
        windows: The configured windows.
        tz: The engine time zone.
        now: The instant, aware.

    Returns:
        The merged ``(start, end)`` or ``None``.
    """
    instant = now.astimezone(UTC)
    for start, end in intervals(windows, tz, now):
        if start <= instant < end:
            return (start, end)
    return None


def next_boundary(windows: Sequence[Window], tz: ZoneInfo, now: datetime) -> datetime | None:
    """Return the next instant a window starts or ends after ``now``.

    Args:
        windows: The configured windows.
        tz: The engine time zone.
        now: The instant, aware.

    Returns:
        An aware UTC instant, or ``None`` without windows.
    """
    instant = now.astimezone(UTC)
    later = [edge for pair in intervals(windows, tz, now) for edge in pair if edge > instant]
    return min(later) if later else None
