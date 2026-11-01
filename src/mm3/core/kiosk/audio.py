"""Outputs, cards and volume through ``pactl`` (CORE-295, CORE-296).

The agent lists outputs and cards with ``pactl --format=json list sinks`` and
``pactl --format=json list cards``, classifies every output as ``hdmi``,
``analog``, ``usb``, ``bluetooth`` or ``other`` from its card and port, and
works out which output (and which card profile) the ``audio`` block asks for.
Nothing here runs a process: the functions read JSON text and return plain
data, so the agent can be tested with canned ``pactl`` output.

Implements:
    CORE-295: Core spec 0.21, §6.7 Display output.
    CORE-296: Core spec 0.21, §6.7 Display output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "KINDS",
    "Card",
    "Profile",
    "Resolution",
    "Sink",
    "mute_command",
    "parse_cards",
    "parse_sinks",
    "profile_command",
    "resolve_sink",
    "set_default_command",
    "volume_command",
]

KINDS = ("hdmi", "analog", "usb", "bluetooth", "other")
"""The ``kind`` of an output in the ``sinks`` frame (CORE-277)."""

_ALSA_CARD = "alsa_card."
_ALSA_OUTPUT = "alsa_output."


@dataclass(frozen=True)
class Profile:
    """A card profile that has outputs.

    Attributes:
        name: Profile name, e.g. ``output:hdmi-stereo``.
        available: Whether the profile is usable (a connected HDMI sink, say).
        sinks: Number of outputs the profile provides.
    """

    name: str
    available: bool
    sinks: int

    @property
    def output_part(self) -> str:
        """The ``output:`` half of the name without its prefix (``analog-stereo``)."""
        for part in self.name.split("+"):
            if part.startswith("output:"):
                return part[len("output:") :]
        return ""

    @property
    def kind(self) -> str:
        """``hdmi``, ``analog`` or ``other``, from the profile name."""
        part = self.output_part
        if "hdmi" in part or "displayport" in part:
            return "hdmi"
        if "analog" in part:
            return "analog"
        return "other"


@dataclass(frozen=True)
class Card:
    """A sound card as ``pactl list cards`` reports it.

    Attributes:
        name: Card name, e.g. ``alsa_card.pci-0000_00_1f.3``.
        alsa_card: The ``alsa.card`` property, which outputs carry too.
        active_profile: The profile in use, or ``None``.
        profiles: The profiles that have outputs.
    """

    name: str
    alsa_card: str | None = None
    active_profile: str | None = None
    profiles: tuple[Profile, ...] = ()

    def sink_name_for(self, profile: Profile) -> str | None:
        """Return the name PulseAudio and PipeWire give the output of a profile.

        Args:
            profile: One of this card's profiles.

        Returns:
            ``alsa_output.<card id>.<profile>`` for ALSA cards, ``None`` for
            others (Bluetooth cards name their outputs differently).
        """
        if not self.name.startswith(_ALSA_CARD) or not profile.output_part:
            return None
        return f"{_ALSA_OUTPUT}{self.name[len(_ALSA_CARD) :]}.{profile.output_part}"


@dataclass(frozen=True)
class Sink:
    """An output as ``pactl list sinks`` reports it.

    Attributes:
        name: The name ``pactl set-default-sink`` takes.
        description: Human-readable name.
        kind: One of :data:`KINDS`.
        available: False when the active port says nothing is plugged in.
        volume: Volume in percent of the first channel, or ``None``.
        mute: Whether the output is muted.
        card: The name of its card, when it has one.
    """

    name: str
    description: str
    kind: str
    available: bool
    volume: int | None = None
    mute: bool = False
    card: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return ``{name, description, kind, available}`` for the ``sinks`` frame."""
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "available": self.available,
        }


@dataclass(frozen=True)
class Resolution:
    """What the ``audio`` block's output means right now.

    Attributes:
        sink: The output to use; ``None`` for ``auto`` (keep the default) or
            when nothing matches.
        switch: A ``(card, profile)`` to activate first so the output appears.
        matched: False when the requested output was not found or is unavailable.
    """

    sink: Sink | None = None
    switch: tuple[str, str] | None = None
    matched: bool = True


def _json_list(text: str) -> list[dict[str, Any]]:
    """Parse ``pactl --format=json`` output into a list of objects.

    Raises:
        ValueError: The text is not a JSON array.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("pactl did not print a JSON array")
    return [item for item in data if isinstance(item, dict)]


def _flag(value: Any, default: bool = True) -> bool:
    """Read a ``pactl`` availability flag: booleans or ``yes``/``no`` text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("yes", "true", "available"):
            return True
        if low in ("no", "false", "not available"):
            return False
    return default


def parse_cards(text: str) -> list[Card]:
    """Read ``pactl --format=json list cards``.

    Args:
        text: The command's output.

    Returns:
        The cards; profiles without outputs are left out.

    Raises:
        ValueError: The text is not a JSON array.
    """
    cards: list[Card] = []
    for raw in _json_list(text):
        name = raw.get("name")
        if not isinstance(name, str):
            continue
        props = raw.get("properties")
        alsa = props.get("alsa.card") if isinstance(props, dict) else None
        profiles: list[Profile] = []
        raw_profiles = raw.get("profiles")
        if isinstance(raw_profiles, dict):
            for pname, info in raw_profiles.items():
                if not isinstance(info, dict):
                    continue
                sinks = info.get("sinks")
                if not isinstance(sinks, int) or sinks < 1:
                    continue
                profiles.append(Profile(str(pname), _flag(info.get("available")), sinks))
        active = raw.get("active_profile")
        cards.append(
            Card(
                name=name,
                alsa_card=str(alsa) if alsa is not None else None,
                active_profile=active if isinstance(active, str) else None,
                profiles=tuple(profiles),
            )
        )
    return cards


def _percent(volume: Any) -> int | None:
    """Return the volume of the first channel in percent."""
    if not isinstance(volume, dict) or not volume:
        return None
    channel = next(iter(volume.values()))
    pct = channel.get("value_percent") if isinstance(channel, dict) else None
    if isinstance(pct, str) and pct.rstrip("%").isdigit():
        return int(pct.rstrip("%"))
    return None


def _card_of(raw: Mapping[str, Any], cards: Sequence[Card]) -> Card | None:
    """Find the card an output belongs to: by ``alsa.card``, else by name."""
    props = raw.get("properties")
    alsa = props.get("alsa.card") if isinstance(props, dict) else None
    if alsa is not None:
        for card in cards:
            if card.alsa_card == str(alsa):
                return card
    name = raw.get("name")
    if isinstance(name, str) and name.startswith(_ALSA_OUTPUT):
        rest = name[len(_ALSA_OUTPUT) :]
        for card in cards:
            card_id = card.name[len(_ALSA_CARD) :]
            if card.name.startswith(_ALSA_CARD) and rest.startswith(card_id + "."):
                return card
    return None


def _kind(raw: Mapping[str, Any], card: Card | None) -> str:
    """Classify an output from its card and port (CORE-296)."""
    props = raw.get("properties")
    props = props if isinstance(props, dict) else {}
    name = str(raw.get("name") or "")
    if (
        name.startswith("bluez_")
        or props.get("device.api") == "bluez5"
        or (card and card.name.startswith("bluez_"))
    ):
        return "bluetooth"
    if props.get("device.bus") == "usb" or (card and card.name.startswith("alsa_card.usb-")):
        return "usb"
    port = str(raw.get("active_port") or "").lower()
    ports = raw.get("ports")
    port_type = ""
    if isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict) and p.get("name") == raw.get("active_port"):
                port_type = str(p.get("type") or "").lower()
    profile = (card.active_profile or "").lower() if card else ""
    for text in (port, port_type, profile, name.lower()):
        if "hdmi" in text or "displayport" in text:
            return "hdmi"
    for text in (port, profile, name.lower()):
        if "analog" in text:
            return "analog"
    if port_type in ("speaker", "headphones", "line", "headset"):
        return "analog"
    return "other"


def _available(raw: Mapping[str, Any]) -> bool:
    """False when the active port reports that nothing is plugged in."""
    ports = raw.get("ports")
    if not isinstance(ports, list):
        return True
    for p in ports:
        if isinstance(p, dict) and p.get("name") == raw.get("active_port"):
            return _flag(p.get("availability"), True)
    return True


def parse_sinks(text: str, cards: Sequence[Card] = ()) -> list[Sink]:
    """Read ``pactl --format=json list sinks``.

    Args:
        text: The command's output.
        cards: The cards from :func:`parse_cards`, to classify from.

    Returns:
        The outputs in the order ``pactl`` lists them.

    Raises:
        ValueError: The text is not a JSON array.
    """
    sinks: list[Sink] = []
    for raw in _json_list(text):
        name = raw.get("name")
        if not isinstance(name, str):
            continue
        card = _card_of(raw, cards)
        description = raw.get("description")
        sinks.append(
            Sink(
                name=name,
                description=description if isinstance(description, str) else name,
                kind=_kind(raw, card),
                available=_available(raw),
                volume=_percent(raw.get("volume")),
                mute=bool(raw.get("mute")),
                card=card.name if card else None,
            )
        )
    return sinks


def resolve_sink(want: str, sinks: Sequence[Sink], cards: Sequence[Card]) -> Resolution:
    """Work out the output the ``audio`` block asks for (CORE-296).

    ``auto`` keeps the current default. ``hdmi`` and ``analog`` take the first
    available output of that kind; ``<name>`` takes that exact output. When no
    listed output fits but an inactive profile of a card would provide it (the
    HDMI and analog outputs of one HDA card), the result carries the profile to
    switch to; the agent switches and resolves again.

    Args:
        want: ``audio.sink``.
        sinks: The outputs now listed.
        cards: The cards now listed.

    Returns:
        The resolution; ``matched`` is False when nothing fits.
    """
    if want == "auto":
        return Resolution()
    if want in ("hdmi", "analog"):
        for sink in sinks:
            if sink.kind == want and sink.available:
                return Resolution(sink=sink)
        for card in cards:
            for profile in card.profiles:
                if (
                    profile.kind == want
                    and profile.available
                    and profile.name != card.active_profile
                ):
                    return Resolution(switch=(card.name, profile.name))
        return Resolution(matched=False)
    for sink in sinks:
        if sink.name == want:
            return Resolution(sink=sink) if sink.available else Resolution(matched=False)
    for card in cards:
        for profile in card.profiles:
            if (
                profile.available
                and profile.name != card.active_profile
                and card.sink_name_for(profile) == want
            ):
                return Resolution(switch=(card.name, profile.name))
    return Resolution(matched=False)


def set_default_command(pactl: str, sink: str) -> list[str]:
    """Return the ``pactl set-default-sink`` command."""
    return [pactl, "set-default-sink", sink]


def volume_command(pactl: str, sink: str, percent: int) -> list[str]:
    """Return the ``pactl set-sink-volume`` command for a percentage."""
    return [pactl, "set-sink-volume", sink, f"{percent}%"]


def mute_command(pactl: str, sink: str, mute: bool) -> list[str]:
    """Return the ``pactl set-sink-mute`` command."""
    return [pactl, "set-sink-mute", sink, "1" if mute else "0"]


def profile_command(pactl: str, card: str, profile: str) -> list[str]:
    """Return the ``pactl set-card-profile`` command."""
    return [pactl, "set-card-profile", card, profile]
