"""Cron expressions: parsing and next-run computation in a time zone.

Wall-clock rules across DST changes: a local time that does not exist
(spring forward) runs once at the next valid instant; a local time that
occurs twice (fall back) runs once, at its first occurrence.

Implements:
    CORE-181, CORE-182, CORE-187, CORE-188: Core spec 0.4, §3.3 Scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

__all__ = ["CronSpec", "next_fire", "parse_cron", "resolve_local"]

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}
_MONTHS = {
    n: i
    for i, n in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_DAYS = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
# Largest day count of each month in any year (February 29 in leap years).
_MAX_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
# Upper bound of the search: a matching day exists within eight years
# whenever the expression can match at all (leap days included).
_SEARCH_LIMIT = timedelta(days=366 * 8)


@dataclass(frozen=True)
class CronSpec:
    """A parsed five-field cron expression.

    Attributes:
        expr: The expression as written.
        minutes: Matching minutes (0-59).
        hours: Matching hours (0-23).
        days: Matching days of the month (1-31).
        months: Matching months (1-12).
        weekdays: Matching days of the week (0-6, Sunday = 0).
        days_restricted: The day-of-month field does not start with ``*``.
        weekdays_restricted: The day-of-week field does not start with ``*``.
    """

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_restricted: bool
    weekdays_restricted: bool

    def day_matches(self, day: datetime) -> bool:
        """Tell whether a calendar day matches the two day fields.

        When both day fields are restricted, a match on either one is
        enough (classic cron behaviour).

        Args:
            day: Any time on that day (only the date is used).

        Returns:
            True when the day matches.
        """
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return dom or dow
        return dom and dow


def _value(token: str, names: dict[str, int], low: int, high: int, field: str) -> int:
    key = token.lower()
    if key in names:
        return names[key]
    if not token.isdigit():
        raise ValueError(f"cron field {field}: {token!r} is not a number or name")
    number = int(token)
    if not low <= number <= high:
        raise ValueError(f"cron field {field}: {number} is outside {low}-{high}")
    return number


def _field(text: str, low: int, high: int, field: str, names: dict[str, int]) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"cron field {field}: empty list item")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"cron field {field}: bad step {step_text!r}")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _value(first, names, low, high, field)
            end = _value(last, names, low, high, field)
            if start > end:
                raise ValueError(f"cron field {field}: range {base!r} is reversed")
        else:
            start = _value(base, names, low, high, field)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> CronSpec:
    """Parse a cron expression.

    Five fields (minute hour day-of-month month day-of-week) with ``*``,
    lists, ranges, steps and names (``jan``-``dec``, ``sun``-``sat``;
    Sunday is 0 or 7), or one of the macros ``@hourly``, ``@daily``,
    ``@weekly``, ``@monthly``, ``@yearly``.

    Args:
        expr: The expression.

    Returns:
        The parsed expression.

    Raises:
        ValueError: The expression is malformed, or can never match
            (for example ``0 0 30 2 *``).

    Implements:
        CORE-181, CORE-188: Core spec 0.4, §3.3 Scheduler.
    """
    text = expr.strip()
    text = _MACROS.get(text.lower(), text)
    if text.startswith("@"):
        raise ValueError(f"unknown cron macro {expr!r}")
    fields = text.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression {expr!r} needs 5 fields, has {len(fields)}")
    minute, hour, dom, month, dow = fields
    weekdays = _field(dow, 0, 7, "day-of-week", _DAYS)
    if 7 in weekdays:
        weekdays = (weekdays - {7}) | {0}
    spec = CronSpec(
        expr=expr,
        minutes=_field(minute, 0, 59, "minute", {}),
        hours=_field(hour, 0, 23, "hour", {}),
        days=_field(dom, 1, 31, "day-of-month", {}),
        months=_field(month, 1, 12, "month", _MONTHS),
        weekdays=frozenset(weekdays),
        days_restricted=not dom.startswith("*"),
        weekdays_restricted=not dow.startswith("*"),
    )
    if (
        spec.days_restricted
        and not spec.weekdays_restricted
        and not any(day <= _MAX_DAYS[m] for m in spec.months for day in spec.days)
    ):
        raise ValueError(f"cron expression {expr!r} can never match")
    return spec


def resolve_local(naive: datetime, tz: tzinfo) -> datetime:
    """Turn a local wall-clock time into a UTC instant.

    A time that occurs twice resolves to its first occurrence; a time that
    does not exist resolves to the next valid instant (the transition).

    Args:
        naive: Local time without tzinfo.
        tz: The time zone.

    Returns:
        Aware UTC datetime.

    Implements:
        CORE-182, CORE-187: Core spec 0.4, §3.3 Scheduler.
    """
    first = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if first.astimezone(tz).replace(tzinfo=None) == naive:
        return first
    # Non-existent: the earliest instant whose local time is later than ``naive``
    # lies between the two fold readings; find it to the second.
    low = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    high = first
    while (high - low) > timedelta(seconds=1):
        mid = low + (high - low) / 2
        if mid.astimezone(tz).replace(tzinfo=None) > naive:
            high = mid
        else:
            low = mid
    return high.replace(microsecond=0)


def next_fire(spec: CronSpec, after: datetime, tz: tzinfo) -> datetime:
    """Compute the first run strictly after a given instant.

    Args:
        spec: The parsed expression.
        after: Aware instant; the result is later than this.
        tz: Time zone the expression is read in.

    Returns:
        Aware UTC datetime of the next run.

    Implements:
        CORE-181, CORE-182: Core spec 0.4, §3.3 Scheduler.
    """
    local = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
    local += timedelta(minutes=1)
    limit = local + _SEARCH_LIMIT
    while local < limit:
        if local.month not in spec.months:
            year, month = (
                (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
            )
            local = datetime(year, month, 1)
            continue
        if not spec.day_matches(local):
            local = datetime(local.year, local.month, local.day) + timedelta(days=1)
            continue
        if local.hour not in spec.hours:
            local = local.replace(minute=0) + timedelta(hours=1)
            continue
        if local.minute not in spec.minutes:
            local += timedelta(minutes=1)
            continue
        instant = resolve_local(local, tz)
        if instant > after:
            return instant
        local += timedelta(minutes=1)
    raise ValueError(f"cron expression {spec.expr!r} has no run before {limit:%Y-%m-%d}")
