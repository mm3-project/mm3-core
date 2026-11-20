"""mm3-media: configuration rules.

Implements:
    MED-01, MED-02, MED-03, MED-05: Core spec 0.57, §9.4 Reference modules.
"""

from __future__ import annotations

from typing import Any

import pytest
from mm3_media import MediaConfig, MediaModule
from pydantic import ValidationError


def config(**values: Any) -> MediaConfig:
    return MediaConfig.model_validate(values)


def test_med_01_is_a_full_module_with_an_element_and_the_media_capabilities() -> None:
    assert MediaModule.name == "media"
    assert MediaModule.element == "mm3-media"
    assert MediaModule.scripts == ["media.js"]
    assert {"media.play", "media.takeover", "media.youtube"} <= MediaModule.capabilities


def test_med_02_defaults_of_a_video() -> None:
    cfg = config(kind="video", source="door")
    assert (cfg.fit, cfg.loop, cfg.muted, cfg.opacity) == ("contain", True, False, 1.0)
    assert cfg.on_touch == "none"
    assert cfg.file is None


@pytest.mark.parametrize("kind", ["video", "image", "slideshow", "audio"])
def test_med_02_every_kind_but_youtube_needs_a_source(kind: str) -> None:
    config(kind=kind, source="x")
    with pytest.raises(ValidationError, match="CFG_SOURCE_REQUIRED"):
        config(kind=kind)


def test_med_02_kind_is_required_and_closed() -> None:
    with pytest.raises(ValidationError):
        config(source="x")
    with pytest.raises(ValidationError):
        config(kind="gif", source="x")


def test_med_02_youtube_needs_a_video_id_and_takes_no_source() -> None:
    cfg = config(kind="youtube", video_id="dQw4w9WgXcQ")
    assert cfg.source is None
    with pytest.raises(ValidationError, match="CFG_YOUTUBE_NEEDS_VIDEO_ID"):
        config(kind="youtube")
    with pytest.raises(ValidationError, match="CFG_YOUTUBE_NO_SOURCE"):
        config(kind="youtube", video_id="dQw4w9WgXcQ", source="x")
    with pytest.raises(ValidationError, match="CFG_YOUTUBE_NO_SOURCE"):
        config(kind="youtube", video_id="dQw4w9WgXcQ", file="a.mp4")


@pytest.mark.parametrize("bad", ["short", "dQw4w9WgXcQ1", "dQw4w9WgXc!"])
def test_med_02_a_video_id_is_eleven_characters_of_the_id_alphabet(bad: str) -> None:
    with pytest.raises(ValidationError):
        config(kind="youtube", video_id=bad)


def test_med_02_a_video_id_is_for_youtube_only() -> None:
    with pytest.raises(ValidationError, match="CFG_VIDEO_ID_YOUTUBE_ONLY"):
        config(kind="video", source="x", video_id="dQw4w9WgXcQ")


@pytest.mark.parametrize("opacity", [-0.1, 1.1])
def test_med_02_opacity_is_between_zero_and_one(opacity: float) -> None:
    with pytest.raises(ValidationError):
        config(kind="video", source="x", opacity=opacity)
    assert config(kind="video", source="x", opacity=0).opacity == 0
    assert config(kind="video", source="x", opacity=1).opacity == 1


def test_med_02_fit_is_contain_or_cover() -> None:
    assert config(kind="video", source="x", fit="cover").fit == "cover"
    with pytest.raises(ValidationError):
        config(kind="video", source="x", fit="fill")


def test_med_03_slideshow_defaults() -> None:
    cfg = config(kind="slideshow", source="photos")
    assert (cfg.interval, cfg.order, cfg.transition, cfg.recursive) == (15, "random", "fade", True)


def test_med_03_the_interval_is_at_least_three_seconds() -> None:
    assert config(kind="slideshow", source="p", interval=3).interval == 3
    with pytest.raises(ValidationError):
        config(kind="slideshow", source="p", interval=2.9)


def test_med_03_order_and_transition_are_closed() -> None:
    for order in ("name", "date", "random"):
        assert config(kind="slideshow", source="p", order=order).order == order
    with pytest.raises(ValidationError):
        config(kind="slideshow", source="p", order="size")
    with pytest.raises(ValidationError):
        config(kind="slideshow", source="p", transition="slide")


def test_med_03_a_slideshow_shows_a_folder_not_a_file() -> None:
    with pytest.raises(ValidationError, match="CFG_SLIDESHOW_NO_FILE"):
        config(kind="slideshow", source="p", file="a.jpg")


def test_med_05_on_touch_is_none_or_takeover_and_not_for_youtube() -> None:
    assert config(kind="video", source="x", on_touch="takeover").on_touch == "takeover"
    with pytest.raises(ValidationError):
        config(kind="video", source="x", on_touch="zoom")
    with pytest.raises(ValidationError, match="CFG_YOUTUBE_NO_TAKEOVER"):
        config(kind="youtube", video_id="dQw4w9WgXcQ", on_touch="takeover")


def test_med_02_unknown_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        config(kind="video", source="x", volume=3)
