"""mm3-greeting: the part of the day follows the mirror's clock and time zone.

Implements:
    CORE-289: Core spec 0.19, §9.4 Reference modules.
    MOD-29: Module Contract 1.0 rev 12, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import pytest
from mm3_greeting import GreetingModule

from mm3.testing import ModuleHarness


@pytest.mark.parametrize(
    ("now", "part"),
    [
        ("2026-06-01T05:59:00+00:00", "morning"),
        ("2026-06-01T11:59:00+00:00", "morning"),
        ("2026-06-01T12:00:00+00:00", "afternoon"),
        ("2026-06-01T17:59:00+00:00", "afternoon"),
        ("2026-06-01T18:00:00+00:00", "evening"),
        ("2026-06-01T23:30:00+00:00", "evening"),
    ],
)
async def test_greeting_part_of_day(now: str, part: str) -> None:
    async with ModuleHarness(GreetingModule, now=now) as h:
        assert h.state == {"part": part}


async def test_greeting_uses_the_mirror_time_zone() -> None:
    # 10:30 UTC is 12:30 in Rome in summer: the afternoon there
    async with ModuleHarness(
        GreetingModule, now="2026-06-01T10:30:00+00:00", timezone="Europe/Rome"
    ) as h:
        assert h.state == {"part": "afternoon"}


async def test_greeting_updates_every_minute() -> None:
    async with ModuleHarness(GreetingModule, now="2026-06-01T11:59:30+00:00") as h:
        assert h.state == {"part": "morning"}
        await h.advance(seconds=60)
        assert h.state == {"part": "afternoon"}
