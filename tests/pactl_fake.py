"""A fake ``pactl`` for the kiosk audio tests (CORE-295, CORE-296).

``FakePactl`` answers the commands the agent runs from a small model of one
HDA card (analog and two HDMI profiles), one USB card and, optionally, a
Bluetooth card. Its JSON has the shape ``pactl --format=json`` prints; it is
written by hand from the documented output, so a run against real
PipeWire on Debian 13 stays part of the milestone's manual check.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

HDA = "alsa_card.pci-0000_00_1f.3"
HDA_ANALOG = "alsa_output.pci-0000_00_1f.3.analog-stereo"
HDA_HDMI = "alsa_output.pci-0000_00_1f.3.hdmi-stereo"
HDA_HDMI2 = "alsa_output.pci-0000_00_1f.3.hdmi-stereo-extra1"
USB = "alsa_card.usb-Generic_USB_Audio-00"
USB_SINK = "alsa_output.usb-Generic_USB_Audio-00.analog-stereo"
BLUEZ = "bluez_card.00_11_22_33_44_55"
BLUEZ_SINK = "bluez_output.00_11_22_33_44_55.1"


def _volume(percent: int) -> dict[str, Any]:
    raw = round(percent * 65536 / 100)
    chan = {"value": raw, "value_percent": f"{percent}%", "db": "0.00 dB"}
    return {"front-left": dict(chan), "front-right": dict(chan)}


def _profile(sinks: int, available: bool = True) -> dict[str, Any]:
    return {"description": "x", "sinks": sinks, "sources": 0, "priority": 1, "available": available}


class FakePactl:
    """State of the fake sound server; :meth:`run` answers pactl commands."""

    def __init__(self) -> None:
        self.profile = "output:analog-stereo"
        self.hdmi_plugged = True
        self.usb = False
        self.bluetooth = False
        self.default = HDA_ANALOG
        self.volume = {HDA_ANALOG: 55, HDA_HDMI: 80, USB_SINK: 40, BLUEZ_SINK: 30}
        self.mute = {HDA_ANALOG: False, HDA_HDMI: False, USB_SINK: False, BLUEZ_SINK: False}
        self.down = False
        self.fail: set[str] = set()
        self.commands: list[list[str]] = []

    # --- the JSON the server would print -----------------------------------------------

    def cards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = [
            {
                "index": 40,
                "name": HDA,
                "properties": {"alsa.card": "0", "device.bus": "pci"},
                "profiles": {
                    "off": _profile(0),
                    "output:analog-stereo": _profile(1),
                    "output:hdmi-stereo": _profile(1, self.hdmi_plugged),
                    "output:hdmi-stereo-extra1": _profile(1, False),
                    "input:analog-stereo": {**_profile(0), "sources": 1},
                },
                "active_profile": self.profile,
            }
        ]
        if self.usb:
            cards.append(
                {
                    "index": 41,
                    "name": USB,
                    "properties": {"alsa.card": "1", "device.bus": "usb"},
                    "profiles": {"output:analog-stereo": _profile(1)},
                    "active_profile": "output:analog-stereo",
                }
            )
        if self.bluetooth:
            cards.append(
                {
                    "index": 42,
                    "name": BLUEZ,
                    "properties": {"device.api": "bluez5"},
                    "profiles": {"a2dp-sink": _profile(1)},
                    "active_profile": "a2dp-sink",
                }
            )
        return cards

    def _sink(self, name: str, description: str, **extra: Any) -> dict[str, Any]:
        return {
            "index": 1,
            "state": "SUSPENDED",
            "name": name,
            "description": description,
            "mute": self.mute[name],
            "volume": _volume(self.volume[name]),
            **extra,
        }

    def sinks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.profile == "output:analog-stereo":
            out.append(
                self._sink(
                    HDA_ANALOG,
                    "Built-in Audio Analog Stereo",
                    active_port="analog-output-speaker",
                    ports=[
                        {
                            "name": "analog-output-speaker",
                            "description": "Speakers",
                            "type": "Speaker",
                            "priority": 10000,
                            "availability": "availability unknown",
                        }
                    ],
                    properties={"alsa.card": "0", "device.api": "alsa", "device.bus": "pci"},
                )
            )
        elif self.profile == "output:hdmi-stereo":
            out.append(
                self._sink(
                    HDA_HDMI,
                    "Built-in Audio Digital Stereo (HDMI)",
                    active_port="hdmi-output-0",
                    ports=[
                        {
                            "name": "hdmi-output-0",
                            "description": "HDMI / DisplayPort",
                            "type": "HDMI",
                            "priority": 5900,
                            "availability": "available" if self.hdmi_plugged else "not available",
                        }
                    ],
                    properties={"alsa.card": "0", "device.api": "alsa", "device.bus": "pci"},
                )
            )
        if self.usb:
            out.append(
                self._sink(
                    USB_SINK,
                    "USB Audio Analog Stereo",
                    active_port="analog-output",
                    ports=[
                        {
                            "name": "analog-output",
                            "description": "Analog Output",
                            "type": "Line",
                            "priority": 9900,
                            "availability": "availability unknown",
                        }
                    ],
                    properties={"alsa.card": "1", "device.api": "alsa", "device.bus": "usb"},
                )
            )
        if self.bluetooth:
            out.append(
                self._sink(
                    BLUEZ_SINK,
                    "Speaker",
                    properties={"device.api": "bluez5", "device.bus": "bluetooth"},
                )
            )
        return out

    # --- the commands --------------------------------------------------------------------

    async def run(self, cmd: Sequence[str]) -> tuple[int, str, str]:
        self.commands.append(list(cmd))
        if self.down:
            return 1, "", "Connection failure: Connection refused"
        args = list(cmd[1:])
        verb = " ".join(args[:3]) if args[:1] == ["--format=json"] else args[0]
        if verb in self.fail:
            return 1, "", f"Failure: {verb}"
        if args == ["--format=json", "list", "sinks"]:
            return 0, json.dumps(self.sinks()), ""
        if args == ["--format=json", "list", "cards"]:
            return 0, json.dumps(self.cards()), ""
        if args == ["get-default-sink"]:
            return 0, self.default + "\n", ""
        if args[0] == "set-default-sink":
            if args[1] not in {s["name"] for s in self.sinks()}:
                return 1, "", "Failure: No such entity"
            self.default = args[1]
            return 0, "", ""
        if args[0] == "set-card-profile":
            if args[1] != HDA or args[2] not in self.cards()[0]["profiles"]:
                return 1, "", "Failure: No such entity"
            self.profile = args[2]
            return 0, "", ""
        if args[0] == "set-sink-volume":
            self.volume[args[1]] = int(args[2].rstrip("%"))
            return 0, "", ""
        if args[0] == "set-sink-mute":
            self.mute[args[1]] = args[2] == "1"
            return 0, "", ""
        return 1, "", f"unexpected pactl command {args}"
