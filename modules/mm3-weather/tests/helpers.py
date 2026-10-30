"""Shared test helpers: fixtures and a configured harness."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from mm3_weather import WeatherModule

from mm3.testing import ModuleHarness

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://api.open-meteo.com/v1/forecast"
GRANTS = {"http": ["https://api.open-meteo.com/"]}
ROME = {"lat": 41.9028, "lon": 12.4964, "name": "Rome"}
NOW = "2026-10-30T14:00:00+01:00"


def fixture(name: str = "full") -> dict[str, Any]:
    """A fresh copy of an Open-Meteo response."""
    return copy.deepcopy(json.loads((FIXTURES / f"open_meteo_{name}.json").read_text()))


def weather(
    config: dict[str, Any] | None = None,
    *,
    body: dict[str, Any] | None = None,
    status: int = 200,
    **kwargs: Any,
) -> ModuleHarness:
    """A harness with Open-Meteo mocked (set up before the first refresh at start)."""
    kwargs.setdefault("grants", GRANTS)
    kwargs.setdefault("timezone", "Europe/Rome")
    kwargs.setdefault("location", ROME)
    kwargs.setdefault("now", NOW)
    h = ModuleHarness(WeatherModule, config=config or {}, **kwargs)
    h.http.mock("GET", URL, status=status, json=body if body is not None else fixture())
    return h
