"""mm3-media in a browser: every kind, the messages, the slideshow, suspend and touch.

The element is mounted in the kit's stand-alone shell with local fixture files as media
sources (MOD-103); states are the ones the backend sends, built by hand.

Implements:
    MED-02, MED-03, MED-04, MED-05, MED-06, MED-07, MED-08: Core spec 0.57,
        §9.4 Reference modules.
    MOD-102: Module Contract 1.0 rev 16, §4.5 Media.
    MOD-36: Module Contract 1.0 rev 16, §5.3 Rules.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mm3_media import MediaModule
from mm3_media.config import MediaConfig

pytest.importorskip("playwright.async_api")

from mm3.testing.browser import BROWSER_CHECKS, BrowserHarness, catalog_for, run_browser_checks

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from module_projects import chromium_available

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium cannot start (set MM3_CHROMIUM)"),
]

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "src" / "mm3_media" / "static"
FIXTURES = HERE / "fixtures"
LANGS = ("en", "it", "fr", "es", "de")
FILES = "/media/files/files/"
MEDIA = {
    f"{FILES}red.png": FIXTURES / "red.png",
    f"{FILES}blue.png": FIXTURES / "blue.png",
    f"{FILES}clip.webm": FIXTURES / "clip.webm",
    f"{FILES}tone.wav": FIXTURES / "tone.wav",
}
REASONS = {
    "unavailable": "MEDIA_ERROR_UNAVAILABLE",
    "forbidden": "MEDIA_ERROR_FORBIDDEN",
    "network": "MEDIA_ERROR_NETWORK",
    "decode": "MEDIA_ERROR_DECODE",
    "unsupported": "MEDIA_ERROR_UNSUPPORTED",
}
IMAGE = {"kind": "image", "src": f"{FILES}red.png", "delivery": "file"}
VIDEO = {"kind": "video", "src": f"{FILES}clip.webm", "delivery": "file"}
AUDIO = {"kind": "audio", "src": f"{FILES}tone.wav", "delivery": "file"}
YOUTUBE = {"kind": "youtube"}
CONFIGS: dict[str, dict[str, Any]] = {
    "image": {"kind": "image", "source": "photos", "file": "red.png"},
    "video": {"kind": "video", "source": "clip"},
    "audio": {"kind": "audio", "source": "tone"},
    "youtube": {"kind": "youtube", "video_id": "dQw4w9WgXcQ"},
    "slideshow": {"kind": "slideshow", "source": "photos"},
}


def slide(n: int, transition: str = "fade") -> dict[str, Any]:
    """The state of a slideshow at its ``n``-th image (red and blue alternate)."""
    srcs = [f"{FILES}red.png", f"{FILES}blue.png"]
    state: dict[str, Any] = {"kind": "slideshow", "src": srcs[(n - 1) % 2], "seq": n}
    state["previous"] = srcs[(n - 2) % 2] if n > 1 and transition == "fade" else None
    return state


@asynccontextmanager
async def element(
    config: dict[str, Any] | None = None, **kwargs: Any
) -> AsyncIterator[BrowserHarness]:
    # the engine hands the element the validated configuration, defaults included
    full = MediaConfig.model_validate(config or CONFIGS["image"]).model_dump(mode="json")
    async with BrowserHarness(MediaModule, config=full, media=MEDIA, **kwargs) as h:
        yield h


async def probe(h: BrowserHarness, expression: str) -> Any:
    """Evaluate ``expression`` with ``el`` the module element."""
    return await h.page.evaluate(
        f"() => {{ const el = document.querySelector('mm3-media'); return ({expression}); }}"
    )


async def wait_for(h: BrowserHarness, expression: str, seconds: float = 5.0) -> Any:
    """Poll until ``expression`` is truthy, and return it (or the last value)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    value = await probe(h, expression)
    while not value and loop.time() < deadline:
        await asyncio.sleep(0.05)
        value = await probe(h, expression)
    return value


NOTICE = "(el.shadow.querySelector('.notice') || {}).textContent"
PLAYER = "el.shadow.querySelector('mm3-player')"


async def notice(h: BrowserHarness) -> str | None:
    value = await probe(h, f"({NOTICE} ?? null)")
    return None if value is None else " ".join(str(value).split())


async def shown_image(h: BrowserHarness) -> None:
    """Show the image state and wait until the real player has decoded it (so it is quiet)."""
    await h.state(IMAGE)
    assert await wait_for(h, f"(({PLAYER}._root.querySelector('img') || {{}}).naturalWidth)")
    await asyncio.sleep(0.1)


async def raise_event(h: BrowserHarness, name: str, detail: dict[str, Any] | None = None) -> None:
    """Dispatch what a player dispatches: a bubbling, composed event from inside it."""
    await h.page.evaluate(
        """([name, detail, sel]) => {
            const el = document.querySelector('mm3-media');
            const target = el.shadow.querySelector(sel);
            target.dispatchEvent(new CustomEvent(name, { bubbles: true, composed: true, detail }));
        }""",
        [name, detail or {}, "mm3-player, mm3-youtube"],
    )
    await asyncio.sleep(0.05)


# --- MOD-102: media only through the engine's elements ---------------------------------------


@pytest.mark.parametrize(
    ("config", "state", "tag"),
    [
        ("image", IMAGE, "mm3-player"),
        ("video", VIDEO, "mm3-player"),
        ("audio", AUDIO, "mm3-player"),
        ("slideshow", slide(1), "mm3-player"),
        ("youtube", YOUTUBE, "mm3-youtube"),
    ],
)
async def test_mod_102_every_kind_is_shown_by_an_engine_element_and_nothing_else(
    config: str, state: dict[str, Any], tag: str
) -> None:
    async with element(CONFIGS[config]) as h:
        r = await h.state(state)
        assert not r.failed, r.error
        assert await probe(h, f"el.shadow.querySelectorAll('{tag}').length") >= 1
        own = await probe(
            h,
            "el.shadow.querySelectorAll('img, video, audio, iframe, source, embed, object').length",
        )
        assert own == 0  # the module's own shadow root holds no media element of its own


def test_mod_102_the_element_never_sets_a_media_url_of_its_own() -> None:
    source = (STATIC / "media.js").read_text(encoding="utf-8")
    assert not re.search(r"<(img|video|audio|iframe|source|embed|object)\b", source)
    assert not re.search(r"\.src\s*=", source)
    assert not re.search(r"setAttribute\(\s*[\"'](src|href|data)", source)
    assert not re.search(r"\bnew\s+(Image|Audio)\b|\bfetch\s*\(|\bXMLHttpRequest\b", source)
    assert 'from "/core/mm3.js"' in source
    assert re.search(r"src=\$\{state\.src\}|src=\$\{src\}", source)  # from the backend's state


async def test_mod_102_a_player_gets_the_backends_url_and_the_configured_options() -> None:
    cfg = CONFIGS["video"] | {"fit": "cover", "loop": False, "muted": True}
    async with element(cfg) as h:
        await h.state(VIDEO)
        assert await probe(h, f"{PLAYER}.getAttribute('src')") == f"{FILES}clip.webm"
        assert await probe(h, f"{PLAYER}.getAttribute('fit')") == "cover"
        assert await probe(h, f"{PLAYER}.hasAttribute('loop')") is False
        assert await probe(h, f"{PLAYER}.hasAttribute('muted')") is True


# --- MED-02: what each kind plays ----------------------------------------------------------------


async def test_med_02_an_image_is_decoded_and_shown() -> None:
    async with element() as h:
        await h.state(IMAGE)
        size = await wait_for(
            h,
            f"(({PLAYER}.shadow || {PLAYER}._root).querySelector('img') || {{}}).naturalWidth",
        )
        assert size == 64
        assert await notice(h) is None


async def test_med_02_a_video_plays_muted_here_and_loops_by_default() -> None:
    async with element(CONFIGS["video"]) as h:
        await h.state(VIDEO)
        assert await wait_for(h, f"{PLAYER}._root.querySelector('video')")
        assert await probe(h, f"{PLAYER}._root.querySelector('video').loop") is True
        assert await probe(h, f"{PLAYER}._root.querySelector('video').muted") is True


async def test_med_02_audio_has_a_player_without_a_picture() -> None:
    async with element(CONFIGS["audio"]) as h:
        await h.state(AUDIO)
        assert await wait_for(h, f"{PLAYER}._root.querySelector('audio')")
        assert await notice(h) is None


async def test_med_02_opacity_comes_from_the_configuration() -> None:
    async with element(CONFIGS["image"] | {"opacity": 0.4}) as h:
        await h.state(IMAGE)
        assert await probe(h, "getComputedStyle(el).opacity") == "0.4"


async def test_med_02_youtube_without_the_grant_shows_the_grant_message_and_no_frame() -> None:
    async with element(CONFIGS["youtube"]) as h:
        await h.state(YOUTUBE)
        shown = await wait_for(h, NOTICE)
        assert " ".join(shown.split()) == catalog_for(MediaModule, "en")["MEDIA_GRANT_YOUTUBE"]
        assert (
            await probe(h, "el.shadow.querySelector('mm3-youtube')._root.querySelector('iframe')")
            is None
        )


# --- MED-04: messages, and the player that stays to try again --------------------------------


@pytest.mark.parametrize("reason", sorted(REASONS))
async def test_med_04_every_player_error_shows_its_message_in_the_cell(reason: str) -> None:
    async with element() as h:
        await shown_image(h)
        await raise_event(h, "mm3-media-error", {"reason": reason})
        assert await notice(h) == catalog_for(MediaModule, "en")[REASONS[reason]]
        assert (await h.report())["errors"] == []


async def test_med_04_an_unknown_reason_is_shown_as_unavailable() -> None:
    async with element() as h:
        await shown_image(h)
        await raise_event(h, "mm3-media-error", {"reason": "weird"})
        assert await notice(h) == catalog_for(MediaModule, "en")["MEDIA_ERROR_UNAVAILABLE"]


async def test_med_04_the_message_goes_when_the_player_plays_again() -> None:
    async with element() as h:
        await shown_image(h)
        await raise_event(h, "mm3-media-error", {"reason": "network"})
        assert await notice(h) is not None
        await raise_event(h, "mm3-media-playing")
        assert await notice(h) is None


async def test_med_04_while_a_message_shows_the_player_stays_hidden_in_place() -> None:
    async with element() as h:
        await shown_image(h)
        player = await probe(h, f"{PLAYER} !== null")
        await raise_event(h, "mm3-media-error", {"reason": "unavailable"})
        assert player
        assert await probe(h, f"{PLAYER} !== null")  # it tries again by itself
        assert (
            await probe(h, "getComputedStyle(el.shadow.querySelector('.layers')).visibility")
            == "hidden"
        )


async def test_med_04_a_youtube_forbidden_reason_names_the_youtube_grant() -> None:
    async with element(CONFIGS["youtube"]) as h:
        await h.state(YOUTUBE)
        await raise_event(h, "mm3-media-error", {"reason": "forbidden"})
        assert await notice(h) == catalog_for(MediaModule, "en")["MEDIA_GRANT_YOUTUBE"]


async def test_med_04_a_missing_source_shows_a_message_and_the_instance_does_not_crash() -> None:
    async with element() as h:
        r = await h.state(IMAGE | {"src": f"{FILES}missing.png"})
        assert not r.failed, r.error
        shown = await wait_for(h, NOTICE, seconds=10)
        # the stand-alone shell refuses the player's own probe (no fetch), so it says "network"
        assert " ".join(shown.split()) in {
            catalog_for(MediaModule, "en")["MEDIA_ERROR_UNAVAILABLE"],
            catalog_for(MediaModule, "en")["MEDIA_ERROR_NETWORK"],
        }
        assert any(path.endswith("missing.png") for path in h.stray_requests)


async def test_med_04_the_player_tries_again_every_thirty_seconds() -> None:
    async with element() as h:
        await h.page.clock.install()
        assert await h.mount()
        await h.state(IMAGE | {"src": f"{FILES}missing.png"})
        await h.page.clock.run_for(1000)
        await wait_for(h, NOTICE)
        before = len([p for p in h.stray_requests if p.endswith("missing.png")])
        await h.page.clock.run_for(31000)
        await asyncio.sleep(0.3)
        after = len([p for p in h.stray_requests if p.endswith("missing.png")])
        assert after > before


@pytest.mark.parametrize(
    ("state", "key"),
    [
        ({"kind": "video", "error": "grant", "source": "door"}, "MEDIA_GRANT_MISSING"),
        ({"kind": "video", "error": "unavailable"}, "MEDIA_ERROR_UNAVAILABLE"),
        ({"kind": "slideshow", "error": "empty"}, "MEDIA_NO_IMAGES"),
    ],
)
async def test_med_04_the_backends_error_states_show_a_message(
    state: dict[str, Any], key: str
) -> None:
    async with element() as h:
        await h.state(state)
        text = catalog_for(MediaModule, "en")[key].replace("{source}", "door")
        assert await notice(h) == text
        assert await probe(h, "el.shadow.querySelectorAll('mm3-player, mm3-youtube').length") == 0


async def test_med_04_the_empty_state_says_it_is_loading() -> None:
    async with element() as h:
        await h.state({})
        assert await notice(h) == catalog_for(MediaModule, "en")["MEDIA_LOADING"]


# --- MED-03: the slideshow fades from one image to the next -----------------------------------


async def test_med_03_the_first_image_fades_in_alone() -> None:
    async with element(CONFIGS["slideshow"]) as h:
        await h.state(slide(1))
        assert await probe(h, "el.shadow.querySelectorAll('.slide').length") == 1
        assert await probe(h, "el.shadow.querySelectorAll('.slide.current').length") == 1


async def test_med_03_the_next_image_fades_in_over_the_previous_one() -> None:
    async with element(CONFIGS["slideshow"]) as h:
        await h.state(slide(1))
        await h.state(slide(2))
        assert await probe(h, "el.shadow.querySelectorAll('.slide').length") == 2
        names = await probe(
            h,
            "[...el.shadow.querySelectorAll('.slide')].map(s => "
            "s.className.split(' ').includes('current') + ':' + getComputedStyle(s).animationName)",
        )
        assert names == ["false:mm3-media-out", "true:mm3-media-in"]
        assert (
            await probe(h, "getComputedStyle(el.shadow.querySelector('.slide')).animationDuration")
            == "1s"
        )
        srcs = await probe(
            h,
            "[...el.shadow.querySelectorAll('.slide mm3-player')].map(p => p.getAttribute('src'))",
        )
        assert srcs == [f"{FILES}red.png", f"{FILES}blue.png"]


async def test_med_03_every_image_is_a_new_node_so_the_fade_runs_each_time() -> None:
    async with element(CONFIGS["slideshow"]) as h:
        await h.state(slide(1))
        await h.state(slide(2))
        await probe(h, "(el.shadow.querySelector('.slide.current').dataset.mark = 'two', 0)")
        await h.state(slide(3))
        assert await probe(h, "el.shadow.querySelector('.slide.current').dataset.mark") is None


async def test_med_03_without_a_transition_there_is_no_animation() -> None:
    async with element(CONFIGS["slideshow"] | {"transition": "none"}) as h:
        await h.state(slide(2, "none"))
        assert await probe(h, "el.shadow.querySelectorAll('.slide').length") == 1
        name = await probe(h, "getComputedStyle(el.shadow.querySelector('.slide')).animationName")
        assert name == "none"


# --- MED-06: nothing plays while the instance is hidden ---------------------------------------


async def test_med_06_suspend_stops_the_players_and_resume_starts_them() -> None:
    async with element(CONFIGS["video"]) as h:
        await h.state(VIDEO)
        assert await wait_for(h, f"{PLAYER}._root.querySelector('video')")
        await probe(h, "el.onSuspend()")
        assert await probe(h, f"{PLAYER}._root.children.length") == 0
        await probe(h, "el.onResume()")
        assert await wait_for(h, f"{PLAYER}._root.querySelector('video')")


async def test_med_06_a_player_made_while_suspended_is_stopped_at_once() -> None:
    async with element(CONFIGS["slideshow"]) as h:
        await h.state(slide(1))
        await probe(h, "el.onSuspend()")
        await h.state(slide(2))
        await asyncio.sleep(0.2)
        assert await probe(
            h,
            "[...el.shadow.querySelectorAll('mm3-player')]"
            ".every(p => p._root.children.length === 0)",
        )


async def test_med_06_youtube_has_no_frame_while_suspended() -> None:
    async with element(CONFIGS["youtube"]) as h:
        await h.state(YOUTUBE)
        await probe(h, "el.onSuspend()")
        assert await probe(h, "el.shadow.querySelector('mm3-youtube')._root.children.length") == 0


# --- MED-05: touch ----------------------------------------------------------------------------


async def test_med_05_a_click_on_the_cell_sends_touch_when_on_touch_is_takeover() -> None:
    async with element(CONFIGS["image"] | {"on_touch": "takeover"}) as h:
        await h.state(IMAGE)
        await h.page.evaluate(
            "() => document.querySelector('mm3-media').shadow.querySelector('.box').click()"
        )
        sent = [s for s in (await h.report())["sent"] if s["kind"] == "client"]
        assert [s["data"]["type"] for s in sent] == ["touch"]


async def test_med_05_a_click_does_nothing_when_on_touch_is_none() -> None:
    async with element() as h:
        await h.state(IMAGE)
        await h.page.evaluate(
            "() => document.querySelector('mm3-media').shadow.querySelector('.box').click()"
        )
        assert [s for s in (await h.report())["sent"] if s["kind"] == "client"] == []


# --- MED-07: five languages ---------------------------------------------------------------------


def test_med_07_every_language_has_every_key() -> None:
    keys = {lang: set(catalog_for(MediaModule, lang)) for lang in LANGS}
    assert keys["en"] >= {"HEADER", "MEDIA_LOADING", "MEDIA_NO_IMAGES", "MEDIA_GRANT_MISSING"}
    assert keys["en"] >= set(REASONS.values()) | {"MEDIA_GRANT_YOUTUBE"}
    for lang in LANGS:
        assert keys[lang] == keys["en"], (lang, keys["en"] ^ keys[lang])
        assert all(catalog_for(MediaModule, lang)[k] for k in keys[lang])


@pytest.mark.parametrize("lang", LANGS)
async def test_med_07_every_text_of_the_element_is_translated_and_no_key_leaks(lang: str) -> None:
    cat = catalog_for(MediaModule, lang)
    async with element(locale=lang) as h:
        await h.state({})
        assert await notice(h) == cat["MEDIA_LOADING"]
        await h.state({"kind": "video", "error": "grant", "source": "door"})
        assert await notice(h) == cat["MEDIA_GRANT_MISSING"].replace("{source}", "door")
        await h.state({"kind": "slideshow", "error": "empty"})
        assert await notice(h) == cat["MEDIA_NO_IMAGES"]
        await shown_image(h)
        for reason, key in REASONS.items():
            await raise_event(h, "mm3-media-error", {"reason": reason})
            shown = await notice(h)
            assert shown == cat[key], (lang, reason)
            assert not re.search(r"\bMEDIA_[A-Z_]+\b", shown or ""), shown
        report = await h.report()
        assert not report["csp"], report["csp"]


async def test_med_07_switching_the_language_rerenders_the_message() -> None:
    async with element() as h:
        await h.state({"kind": "slideshow", "error": "empty"})
        await h.set_locale("it")
        assert await notice(h) == catalog_for(MediaModule, "it")["MEDIA_NO_IMAGES"]


# --- MED-08, MOD-36: the kit's own browser checks ----------------------------------------------


async def test_med_08_browser_checks_pass_for_every_kind() -> None:
    states = [
        IMAGE,
        VIDEO,
        slide(2),
        YOUTUBE,
        {"kind": "video", "error": "grant", "source": "door"},
        {"kind": "slideshow", "error": "empty"},
    ]
    async with element(CONFIGS["slideshow"]) as h:
        results = await run_browser_checks(h, states)
    status = {r.id: r.status for r in results}
    assert set(status) == set(BROWSER_CHECKS)
    assert not {i for i, s in status.items() if s == "fail"}, status


@pytest.mark.parametrize("size", [(160, 90), (400, 300), (1080, 1920)])
async def test_med_08_nothing_overflows_the_cell(size: tuple[int, int]) -> None:
    async with element(CONFIGS["slideshow"]) as h:
        await h.resize(*size)
        await h.state(slide(2), repeat=1)
        await h.resize(*size)
        found = await h.overflow()
        assert not found["content"], (size, found["content"])
        assert not found["page"], size
