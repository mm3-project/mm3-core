"""Shared test helpers: a configured harness and a driver that records its calls."""

from __future__ import annotations

from typing import Any

import mm3_presence
import pytest
from mm3_presence import PresenceModule

from mm3.testing import ModuleHarness

NOW = "2026-10-30T12:00:00+01:00"
GRANTS = {"mqtt": {"subscribe": ["home/*"]}}


class RecordingDriver:
    """A display driver with brightness that records what it is asked."""

    name = "recording"
    supports_brightness = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail = False
        self.reason: str | None = None

    async def set_power(self, on: bool) -> None:
        self._call("power", on)

    async def set_brightness(self, percent: int) -> None:
        self._call("brightness", percent)

    async def check(self) -> str | None:
        return self.reason

    def _call(self, kind: str, value: Any) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((kind, value))


def presence(
    config: dict[str, Any] | None = None,
    *,
    driver: RecordingDriver | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
    **kwargs: Any,
) -> ModuleHarness:
    """A harness; with ``driver`` the module uses it instead of the kiosk driver."""
    kwargs.setdefault("grants", GRANTS)
    kwargs.setdefault("timezone", "Europe/Rome")
    kwargs.setdefault("now", NOW)
    cfg = dict(config or {})
    if driver is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(mm3_presence, "load_driver", lambda *_a: driver)
        cfg.setdefault("display", "recording")
    return ModuleHarness(PresenceModule, config=cfg, **kwargs)


def states(h: ModuleHarness) -> list[tuple[str, str]]:
    """The ``PRESENCE_STATE`` broadcasts as ``(state, reason)``."""
    return [
        (n.payload["state"], n.payload["reason"])
        for n in h.notifications
        if n.name == "PRESENCE_STATE"
    ]
