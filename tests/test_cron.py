"""Cron parsing and next-run computation (CORE-181, CORE-182, CORE-187, CORE-188)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ValidationError

from mm3.config import CronExpression
from mm3.core.services.cron import next_fire, parse_cron, resolve_local

ROME = ZoneInfo("Europe/Rome")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_core_181_fields_lists_ranges_steps_names() -> None:
    spec = parse_cron("*/15 8-18/2 1,15 jan-mar mon-fri")
    assert spec.minutes == {0, 15, 30, 45}
    assert spec.hours == {8, 10, 12, 14, 16, 18}
    assert spec.days == {1, 15}
    assert spec.months == {1, 2, 3}
    assert spec.weekdays == {1, 2, 3, 4, 5}


def test_core_181_sunday_is_zero_or_seven() -> None:
    assert parse_cron("0 0 * * 7").weekdays == {0}
    assert parse_cron("0 0 * * SUN").weekdays == {0}
    assert parse_cron("0 0 * * 5-7").weekdays == {0, 5, 6}


@pytest.mark.parametrize(
    ("macro", "expr"),
    [
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("@yearly", "0 0 1 1 *"),
    ],
)
def test_core_181_macros(macro: str, expr: str) -> None:
    a, b = parse_cron(macro), parse_cron(expr)
    assert (a.minutes, a.hours, a.days, a.months, a.weekdays) == (
        b.minutes,
        b.hours,
        b.days,
        b.months,
        b.weekdays,
    )


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "5-1 * * * *",
        "*/0 * * * *",
        "a * * * *",
        "1,,2 * * * *",
        "@reboot",
        "0 0 30 2 *",
        "0 0 31 4,6,9,11 *",
    ],
)
def test_core_181_invalid_expressions_raise(expr: str) -> None:
    with pytest.raises(ValueError, match=r"cron|field|macro"):
        parse_cron(expr)


def test_core_181_feb_29_is_valid_and_found() -> None:
    spec = parse_cron("0 12 29 2 *")
    assert next_fire(spec, utc(2026, 3, 1), UTC) == utc(2028, 2, 29, 12)


def test_core_181_both_day_fields_restricted_match_either() -> None:
    # 2026-11-01 is a Sunday; the 15th is a Sunday too, 2026-11-02 a Monday.
    spec = parse_cron("0 9 15 * mon")
    first = next_fire(spec, utc(2026, 11, 1), UTC)
    assert first == utc(2026, 11, 2, 9)
    assert next_fire(spec, first, UTC) == utc(2026, 11, 9, 9)


def test_core_181_star_step_day_field_is_unrestricted() -> None:
    spec = parse_cron("0 0 */2 * mon")
    assert not spec.days_restricted
    # Only odd days that are Mondays: 2026-11-09 is a Monday and odd.
    assert next_fire(spec, utc(2026, 11, 1), UTC) == utc(2026, 11, 9)


def test_core_188_cron_expression_type_validates() -> None:
    class Cfg(BaseModel):
        when: CronExpression

    assert Cfg(when="@daily").when == "@daily"
    with pytest.raises(ValidationError):
        Cfg(when="0 0 30 2 *")


def test_core_182_spring_forward_runs_once_at_next_valid_instant() -> None:
    # Europe/Rome 2026-03-29: 02:00 CET -> 03:00 CEST (01:00 UTC).
    spec = parse_cron("30 2 * * *")
    run = next_fire(spec, utc(2026, 3, 28, 23), ROME)
    assert run == utc(2026, 3, 29, 1, 0)
    assert run.astimezone(ROME).hour == 3
    assert next_fire(spec, run, ROME) == utc(2026, 3, 30, 0, 30)


def test_core_182_spring_forward_every_quarter_runs_once_in_gap() -> None:
    spec = parse_cron("*/15 * * * *")
    runs = [utc(2026, 3, 29, 0, 45)]
    for _ in range(3):
        runs.append(next_fire(spec, runs[-1], ROME))
    local = [r.astimezone(ROME).strftime("%H:%M") for r in runs]
    assert local == ["01:45", "03:00", "03:15", "03:30"]


def test_core_182_fall_back_runs_once_at_first_occurrence() -> None:
    # Europe/Rome 2026-10-25: 03:00 CEST -> 02:00 CET (01:00 UTC).
    spec = parse_cron("30 2 * * *")
    run = next_fire(spec, utc(2026, 10, 24, 22), ROME)
    assert run == utc(2026, 10, 25, 0, 30)  # 02:30 CEST, first occurrence
    assert next_fire(spec, run, ROME) == utc(2026, 10, 26, 1, 30)


def test_core_182_fall_back_repeated_hour_skipped_for_frequent_jobs() -> None:
    spec = parse_cron("0,30 * * * *")
    runs = [utc(2026, 10, 25, 0, 0)]  # 02:00 CEST
    for _ in range(3):
        runs.append(next_fire(spec, runs[-1], ROME))
    assert runs[1:] == [utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 2, 0), utc(2026, 10, 25, 2, 30)]


def test_core_182_started_in_second_occurrence_skips_to_after() -> None:
    spec = parse_cron("*/15 * * * *")
    # 02:10 CET, the second 02:10 of the night (01:10 UTC).
    assert next_fire(spec, utc(2026, 10, 25, 1, 10), ROME) == utc(2026, 10, 25, 2, 0)


def test_core_187_resolve_local_rules() -> None:
    assert resolve_local(datetime(2026, 3, 29, 2, 30), ROME) == utc(2026, 3, 29, 1, 0)
    assert resolve_local(datetime(2026, 10, 25, 2, 30), ROME) == utc(2026, 10, 25, 0, 30)
    assert resolve_local(datetime(2026, 7, 1, 12, 0), ROME) == utc(2026, 7, 1, 10, 0)
