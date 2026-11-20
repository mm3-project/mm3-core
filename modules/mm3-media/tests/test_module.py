"""mm3-media: what the backend shows, the slideshow, the grant messages and touch.

The tests run the module under ``ModuleHarness`` with its fake media service (MOD-103)
and virtual time: nothing plays and no folder is read from disk.

Implements:
    MED-01, MED-02, MED-03, MED-04, MED-05, MED-06, MED-08: Core spec 0.57,
        §9.4 Reference modules.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from helpers import GRANTS, PHOTOS, media, shown
from mm3_media import MediaModule, order_key

from mm3.errors import TransientError
from mm3.media import MediaFile
from mm3.testing import ModuleHarness

SLIDESHOW = {"kind": "slideshow", "source": "photos", "order": "name", "interval": 15}
ORDER = [
    f"{PHOTOS}a.jpg",
    f"{PHOTOS}b.jpg",
    f"{PHOTOS}trip/d.png",
    f"{PHOTOS}trip/E.png",
]


async def seen(h: ModuleHarness, count: int, step: float = 15) -> list[str | None]:
    """The URL now shown and the ones after ``count - 1`` more intervals."""
    urls = [shown(h)]
    for _ in range(count - 1):
        await h.advance(step)
        urls.append(shown(h))
    return urls


# --- MED-02: one source or file ---------------------------------------------------------------


async def test_med_02_a_video_source_gives_its_url_and_delivery() -> None:
    async with media({"kind": "video", "source": "door"}) as h:
        assert h.state_history[-1] == {
            "kind": "video",
            "src": "/media/door/stream.mjpg",
            "delivery": "mjpeg",
        }


async def test_med_02_a_file_of_a_files_source() -> None:
    async with media({"kind": "image", "source": "photos", "file": "a.jpg"}) as h:
        state = h.state_history[-1]
        assert state["src"] == f"{PHOTOS}a.jpg"
        assert state["delivery"] == "file"


async def test_med_02_a_source_that_is_one_file_needs_no_file() -> None:
    async with media({"kind": "video", "source": "clip"}) as h:
        assert shown(h) == "/media/files/files/clip.webm"


async def test_med_02_audio_from_a_stream_source() -> None:
    async with media({"kind": "audio", "source": "radio"}) as h:
        assert h.state_history[-1] == {
            "kind": "audio",
            "src": "/media/radio/hls/index.m3u8",
            "delivery": "hls",
        }


async def test_med_02_youtube_needs_nothing_from_the_media_service() -> None:
    async with media({"kind": "youtube", "video_id": "dQw4w9WgXcQ"}, grants={}) as h:
        assert h.state_history[-1] == {"kind": "youtube"}
        assert h.instance_state.name == "RUNNING"
        assert not h.denied


async def test_med_02_the_backend_never_builds_a_url_of_its_own() -> None:
    async with media({"kind": "image", "source": "photos", "file": "a.jpg"}) as h:
        assert h.instance is not None
        assert shown(h) == h.instance.ctx.media.url("photos", file="a.jpg").url


# --- MED-04: grants, missing sources and the instance that stays alive -------------------------


@pytest.mark.parametrize(
    "config",
    [
        {"kind": "video", "source": "door"},
        {"kind": "slideshow", "source": "photos"},
    ],
)
async def test_med_04_without_the_media_grant_the_state_names_the_source_and_nothing_crashes(
    config: dict[str, Any],
) -> None:
    async with media(config, grants={}) as h:
        assert h.state_history[-1]["error"] == "grant"
        assert h.state_history[-1]["source"] == config["source"]
        assert h.instance_state.name == "RUNNING"
        assert "media_grant_missing" in h.logs.events("warning")


async def test_med_04_an_unknown_file_is_unavailable_and_logged() -> None:
    async with media({"kind": "image", "source": "photos", "file": "nope.jpg"}) as h:
        assert h.state_history[-1]["error"] == "unavailable"
        assert h.instance_state.name == "RUNNING"
        assert "media_unavailable" in h.logs.events("warning")


async def test_med_04_an_unknown_source_with_a_wildcard_grant_is_unavailable() -> None:
    async with media({"kind": "video", "source": "gone"}, grants={"media": ["*"]}) as h:
        assert h.state_history[-1]["error"] == "unavailable"
        assert h.instance_state.name == "RUNNING"


async def test_med_04_a_source_added_later_is_found_when_the_configuration_changes() -> None:
    async with media({"kind": "video", "source": "late"}, grants={"media": ["*"]}) as h:
        assert h.state_history[-1]["error"] == "unavailable"
        h.media.add_source("late", "rtsp", delivery="mjpeg")
        await h.notify("CORE_CONFIG_CHANGED", {"version": 2, "sections": ["media"]})
        assert h.state_history[-1]["src"] == "/media/late/stream.mjpg"
        assert "error" not in h.state_history[-1]


async def test_med_04_a_folder_that_is_not_one_is_unavailable() -> None:
    async with media({"kind": "slideshow", "source": "clip"}) as h:
        assert h.state_history[-1]["error"] == "unavailable"


# --- MED-03: the slideshow ---------------------------------------------------------------------


async def test_med_03_name_order_walks_the_subfolders_skips_videos_and_wraps() -> None:
    async with media(SLIDESHOW) as h:
        assert await seen(h, 6) == [*ORDER, ORDER[0], ORDER[1]]


async def test_med_03_the_interval_sets_the_pace() -> None:
    async with media(SLIDESHOW | {"interval": 20}) as h:
        await h.advance(19)
        assert shown(h) == ORDER[0]
        await h.advance(1)
        assert shown(h) == ORDER[1]


async def test_med_03_without_recursion_only_the_folder_itself_is_shown() -> None:
    async with media(SLIDESHOW | {"recursive": False}) as h:
        assert await seen(h, 4) == [ORDER[0], ORDER[1], ORDER[0], ORDER[1]]


async def test_med_03_random_shows_every_image_once_before_any_repeats() -> None:
    async with media(SLIDESHOW | {"order": "random"}) as h:
        urls = await seen(h, 12)
        for round_ in range(3):
            assert sorted(u for u in urls[round_ * 4 : round_ * 4 + 4] if u) == sorted(ORDER)
        assert all(a != b for a, b in itertools.pairwise(urls))  # never twice running


async def test_med_03_random_is_the_default_order() -> None:
    async with media({"kind": "slideshow", "source": "photos"}) as h:
        urls = await seen(h, 4)
        assert sorted(u for u in urls if u) == sorted(ORDER)


def test_med_03_date_order_is_oldest_first_and_a_missing_date_comes_first() -> None:
    def entry(path: str, modified: datetime | None) -> MediaFile:
        return MediaFile(path.rpartition("/")[2], path, "image", 1, modified)

    old = datetime(2020, 1, 1, tzinfo=UTC)
    new = old + timedelta(days=1)
    files = [entry("a.jpg", new), entry("b.jpg", old), entry("c.jpg", None), entry("d.jpg", old)]
    assert [f.path for f in sorted(files, key=lambda f: order_key("date", f))] == [
        "c.jpg",
        "b.jpg",
        "d.jpg",
        "a.jpg",
    ]


def test_med_03_name_order_ignores_case_and_uses_the_whole_path() -> None:
    def entry(path: str) -> MediaFile:
        return MediaFile(path, path, "image", 1, None)

    paths = ["b.jpg", "A.jpg", "trip/c.jpg", "a2.jpg"]
    ordered = sorted((entry(p) for p in paths), key=lambda f: order_key("name", f))
    assert [f.path for f in ordered] == ["A.jpg", "a2.jpg", "b.jpg", "trip/c.jpg"]


async def test_med_03_the_folder_is_read_again_every_ten_minutes() -> None:
    async with media(SLIDESHOW) as h:
        h.media.add_files("photos", ["0first.jpg"])
        await h.advance(minutes=9)
        assert f"{PHOTOS}0first.jpg" not in await seen(h, 8)  # not read yet: 9 min < 10 min
        await h.advance(minutes=1)
        assert f"{PHOTOS}0first.jpg" in await seen(h, 8)


async def test_med_03_the_folder_is_read_again_when_the_configuration_changes() -> None:
    async with media(SLIDESHOW) as h:
        h.media.add_files("photos", ["0first.jpg"])
        await h.notify("CORE_CONFIG_CHANGED", {"version": 2, "sections": ["media"]})
        assert f"{PHOTOS}0first.jpg" in await seen(h, 6)


async def test_med_03_a_file_that_is_gone_is_skipped() -> None:
    async with media(SLIDESHOW) as h:
        # the fake still lists it, but the engine no longer finds it: url() says ValueError
        h.media._single.discard("files/photos/b.jpg")
        entries = h.media._folders["files/photos"]
        h.media._folders["files/photos"] = [e for e in entries if e["name"] != "b.jpg"]
        await h.notify("CORE_CONFIG_CHANGED", {"version": 2, "sections": ["media"]})
        urls = await seen(h, 6)
        assert f"{PHOTOS}b.jpg" not in urls
        assert set(urls) >= {ORDER[0], ORDER[2], ORDER[3]}


async def test_med_03_a_removed_file_that_is_being_shown_stays_until_the_next_image() -> None:
    async with media(SLIDESHOW) as h:
        h.media._folders["files/photos"] = [
            e for e in h.media._folders["files/photos"] if e["name"] != "a.jpg"
        ]
        await h.notify("CORE_CONFIG_CHANGED", {"version": 2, "sections": ["media"]})
        assert shown(h) == ORDER[0]
        await h.advance(15)
        assert shown(h) == ORDER[1]


async def test_med_03_an_empty_folder_says_so_and_fills_in_when_images_arrive() -> None:
    async with media({"kind": "slideshow", "source": "empty", "order": "name"}) as h:
        assert h.state_history[-1] == {"kind": "slideshow", "error": "empty"}
        assert h.instance_state.name == "RUNNING"
        h.media.add_files("empty", ["x.jpg"])
        await h.advance(minutes=10)
        assert shown(h) == "/media/files/files/empty/x.jpg"
        assert "error" not in h.state_history[-1]


async def test_med_03_a_hung_folder_keeps_what_is_shown_and_the_old_list() -> None:
    async with media(SLIDESHOW) as h:
        h.media.list_error = TransientError("MEDIA_FOLDER_SLOW")
        await h.advance(minutes=10)
        assert "media_folder_slow" in h.logs.events("warning")
        assert h.instance_state.name == "RUNNING"
        urls = await seen(h, 4)
        assert set(urls) == set(ORDER)  # the old list still plays
        h.media.list_error = None


async def test_med_03_the_fade_hands_the_element_the_previous_image() -> None:
    async with media(SLIDESHOW) as h:
        assert h.state_history[-1]["previous"] is None
        await h.advance(15)
        assert h.state_history[-1]["previous"] == ORDER[0]
        assert h.state_history[-1]["seq"] == 2


async def test_med_03_without_a_transition_there_is_no_previous_image() -> None:
    async with media(SLIDESHOW | {"transition": "none"}) as h:
        await h.advance(15)
        assert h.state_history[-1]["previous"] is None


# --- MED-06: hidden or suspended ----------------------------------------------------------------


async def test_med_06_a_hidden_slideshow_does_not_advance() -> None:
    async with media(SLIDESHOW) as h:
        await h.suspend()
        await h.advance(minutes=2)
        assert shown(h) == ORDER[0]
        await h.resume()
        await h.advance(30)
        assert shown(h) != ORDER[0]


# --- MED-05: touch ---------------------------------------------------------------------------


async def test_med_05_a_touch_starts_a_takeover_of_the_same_source() -> None:
    async with media({"kind": "video", "source": "door", "on_touch": "takeover"}) as h:
        await h.client_message("touch")
        assert [(t.source, t.file) for t in h.media.takeovers] == [("door", None)]


async def test_med_05_a_touch_takes_over_the_file_of_an_image() -> None:
    cfg = {"kind": "image", "source": "photos", "file": "a.jpg", "on_touch": "takeover"}
    async with media(cfg) as h:
        await h.client_message("touch")
        assert [(t.source, t.file) for t in h.media.takeovers] == [("photos", "a.jpg")]


async def test_med_05_a_touch_takes_over_the_image_the_slideshow_shows() -> None:
    async with media(SLIDESHOW | {"on_touch": "takeover"}) as h:
        await h.advance(15)
        await h.client_message("touch")
        assert [(t.source, t.file) for t in h.media.takeovers] == [("photos", "b.jpg")]


async def test_med_05_nothing_happens_when_on_touch_is_none() -> None:
    async with media({"kind": "video", "source": "door"}) as h:
        await h.client_message("touch")
        assert h.media.takeovers == []


async def test_med_05_other_client_messages_are_ignored() -> None:
    async with media({"kind": "video", "source": "door", "on_touch": "takeover"}) as h:
        await h.client_message("zoom")
        assert h.media.takeovers == []


async def test_med_05_a_refused_takeover_is_logged_and_the_instance_goes_on() -> None:
    cfg = {"kind": "image", "source": "photos", "file": "nope.jpg", "on_touch": "takeover"}
    async with media(cfg) as h:
        await h.client_message("touch")  # the file does not exist: ctx.media raises ValueError
        assert h.media.takeovers == []
        assert h.instance_state.name == "RUNNING"
        assert "media_takeover_refused" in h.logs.events("warning")


# --- MED-08: the kit's own contract checks -----------------------------------------------------


async def test_med_08_check_contract_for_a_stream_and_a_slideshow() -> None:
    for cfg in ({"kind": "video", "source": "door"}, SLIDESHOW):
        async with media(cfg) as h:
            await h.check_contract()


def test_med_01_the_grants_the_tests_use_are_what_the_readme_lists() -> None:
    assert GRANTS["media"]
    assert MediaModule.consumes.keys() == {"CORE_CONFIG_CHANGED"}
    assert not MediaModule.emits
