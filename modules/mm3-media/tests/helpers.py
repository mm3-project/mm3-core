"""Shared test helpers: a fake media world and a configured harness."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mm3_media import MediaModule

from mm3.testing import ModuleHarness

GRANTS = {"media": ["door", "radio", "photos", "clip", "empty"]}
FIXTURES = "tests/fixtures"


def world(h: ModuleHarness) -> None:
    """The sources of the user's ``media`` key, as the fake media service holds them."""
    m = h.media
    m.add_source("door", "rtsp", delivery="mjpeg", audio=False)
    m.add_source("radio", "stream")
    m.add_source("photos", "files", path="files/photos")
    m.add_source("clip", "files", path="files/clip.webm")
    m.add_source("empty", "files", path="files/empty")
    m.add_files("photos", ["b.jpg", "a.jpg", "c.mp4", "trip/"])
    m.add_files("photos", ["d.png", "E.png"], path="trip")
    m.add_files("empty", [])


def media(
    config: Mapping[str, Any],
    *,
    grants: Mapping[str, Any] | None = GRANTS,
    **kwargs: Any,
) -> ModuleHarness:
    """A harness with the fake world set up before the instance starts."""
    h = ModuleHarness(MediaModule, config=config, grants=grants, **kwargs)
    world(h)
    return h


def shown(h: ModuleHarness) -> str | None:
    """The URL the instance last gave its element."""
    return h.state_history[-1].get("src") if h.state_history else None


PHOTOS = "/media/files/files/photos/"
