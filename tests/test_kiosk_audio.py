"""Milestone 3, part F: kiosk audio, pactl parsing and output choice (CORE-295, CORE-296)."""

from __future__ import annotations

import json

import pytest

import pactl_fake as pa
from mm3.core.kiosk.audio import (
    mute_command,
    parse_cards,
    parse_sinks,
    profile_command,
    resolve_sink,
    set_default_command,
    volume_command,
)
from pactl_fake import FakePactl


def listed(fake: FakePactl):  # type: ignore[no-untyped-def]
    cards = parse_cards(json.dumps(fake.cards()))
    sinks = parse_sinks(json.dumps(fake.sinks()), cards)
    return sinks, cards


def test_core_296_classifies_each_output_from_card_and_port() -> None:
    fake = FakePactl()
    fake.usb = True
    fake.bluetooth = True
    sinks, _ = listed(fake)
    assert {s.name: s.kind for s in sinks} == {
        pa.HDA_ANALOG: "analog",
        pa.USB_SINK: "usb",  # a USB card's name says analog; the bus wins
        pa.BLUEZ_SINK: "bluetooth",
    }
    fake.profile = "output:hdmi-stereo"
    sinks, _ = listed(fake)
    hdmi = next(s for s in sinks if s.name == pa.HDA_HDMI)
    assert (hdmi.kind, hdmi.available) == ("hdmi", True)
    assert hdmi.card == pa.HDA
    assert (hdmi.volume, hdmi.mute) == (80, False)


def test_core_296_unplugged_hdmi_is_unavailable() -> None:
    fake = FakePactl()
    fake.profile = "output:hdmi-stereo"
    fake.hdmi_plugged = False
    sinks, _ = listed(fake)
    assert [(s.kind, s.available) for s in sinks] == [("hdmi", False)]


def test_core_296_unknown_output_is_other() -> None:
    raw = [{"name": "null-sink", "description": "Null Output", "mute": False, "volume": {}}]
    (sink,) = parse_sinks(json.dumps(raw))
    assert (sink.kind, sink.available, sink.volume, sink.card) == ("other", True, None, None)


def test_core_296_cards_keep_only_profiles_with_outputs() -> None:
    (card, *_) = parse_cards(json.dumps(FakePactl().cards()))
    assert [p.name for p in card.profiles] == [
        "output:analog-stereo",
        "output:hdmi-stereo",
        "output:hdmi-stereo-extra1",
    ]
    assert card.active_profile == "output:analog-stereo"
    assert [p.kind for p in card.profiles] == ["analog", "hdmi", "hdmi"]
    assert [p.available for p in card.profiles] == [True, True, False]


@pytest.mark.parametrize("text", ["{}", "not json", '"x"'])
def test_core_296_bad_pactl_output_is_a_value_error(text: str) -> None:
    with pytest.raises(ValueError, match=r"JSON|Expecting"):
        parse_sinks(text)
    with pytest.raises(ValueError, match=r"JSON|Expecting"):
        parse_cards(text)


def test_core_296_auto_keeps_the_default() -> None:
    sinks, cards = listed(FakePactl())
    r = resolve_sink("auto", sinks, cards)
    assert (r.sink, r.switch, r.matched) == (None, None, True)


def test_core_296_kind_takes_the_first_available_output_of_that_kind() -> None:
    fake = FakePactl()
    fake.usb = True
    sinks, cards = listed(fake)
    r = resolve_sink("analog", sinks, cards)
    assert r.sink is not None
    assert r.sink.name == pa.HDA_ANALOG
    assert r.matched


def test_core_296_inactive_card_profile_is_switched_first() -> None:
    sinks, cards = listed(FakePactl())
    r = resolve_sink("hdmi", sinks, cards)
    assert r.sink is None
    assert r.matched
    assert r.switch == (pa.HDA, "output:hdmi-stereo")  # the extra1 profile is not available
    # The exact name of an output that only a profile provides does the same.
    r = resolve_sink(pa.HDA_HDMI, sinks, cards)
    assert r.switch == (pa.HDA, "output:hdmi-stereo")
    assert resolve_sink(pa.HDA_HDMI2, sinks, cards).matched is False  # not available


def test_core_296_missing_or_unavailable_output_is_not_matched() -> None:
    fake = FakePactl()
    fake.hdmi_plugged = False
    sinks, cards = listed(fake)
    assert resolve_sink("hdmi", sinks, cards).matched is False
    assert resolve_sink("no-such-output", sinks, cards).matched is False
    fake.profile = "output:hdmi-stereo"
    sinks, cards = listed(fake)
    r = resolve_sink(pa.HDA_HDMI, sinks, cards)  # listed, but nothing plugged in
    assert (r.sink, r.switch, r.matched) == (None, None, False)


def test_core_296_commands() -> None:
    assert set_default_command("pactl", "s") == ["pactl", "set-default-sink", "s"]
    assert volume_command("pactl", "s", 70) == ["pactl", "set-sink-volume", "s", "70%"]
    assert mute_command("pactl", "s", True) == ["pactl", "set-sink-mute", "s", "1"]
    assert mute_command("pactl", "s", False) == ["pactl", "set-sink-mute", "s", "0"]
    assert profile_command("pactl", "c", "p") == ["pactl", "set-card-profile", "c", "p"]
