"""Notification and request declarations on the module class (MOD-29)."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel

from mm3 import MirrorModule
from mm3.notifications import PresenceEvent, ShowAlert, declaration_problems


class Forecast(BaseModel):
    temp_c: float


class Ask(BaseModel):
    city: str


class Answer(BaseModel):
    temp_c: float


def test_mod_29_defaults_are_empty_and_read_only() -> None:
    for attr in ("emits", "consumes", "answers"):
        value = getattr(MirrorModule, attr)
        assert isinstance(value, MappingProxyType)
        assert dict(value) == {}
    assert declaration_problems(MirrorModule) == []


def test_mod_29_sound_declarations_pass() -> None:
    class Weather(MirrorModule):
        name = "weather"
        emits: ClassVar[Any] = {"WEATHER_UPDATED": Forecast, "SHOW_ALERT": ShowAlert}
        consumes: ClassVar[Any] = {"PRESENCE_DETECTED": PresenceEvent}
        answers: ClassVar[Any] = {"WEATHER_NOW": (Ask, Answer)}

    assert declaration_problems(Weather) == []


def test_mod_29_registered_name_must_use_registry_model() -> None:
    class Wrong(MirrorModule):
        name = "wrong"
        emits: ClassVar[Any] = {"SHOW_ALERT": Forecast}

    [problem] = declaration_problems(Wrong)
    assert "SHOW_ALERT is registered" in problem
    assert "ShowAlert" in problem


def test_mod_29_bad_shapes_are_reported() -> None:
    class Bad(MirrorModule):
        name = "bad"
        emits: ClassVar[Any] = {"lower_name": Forecast, "NOT_A_MODEL": dict}
        consumes: ClassVar[Any] = ["X"]
        answers: ClassVar[Any] = {"ONE_MODEL": Ask}

    problems = declaration_problems(Bad)
    assert any("'lower_name'" in p and "UPPER_SNAKE" in p for p in problems)
    assert any("NOT_A_MODEL" in p and "Pydantic model" in p for p in problems)
    assert any(p.startswith("consumes is not a mapping") for p in problems)
    assert any("ONE_MODEL" in p and "request model, reply model" in p for p in problems)


def test_mod_29_engine_only_names_cannot_be_emitted_or_answered() -> None:
    class Rogue(MirrorModule):
        name = "rogue"
        emits: ClassVar[Any] = {"CORE_FOO": Forecast}
        answers: ClassVar[Any] = {"CORE_BAR": (Ask, Answer)}

    problems = declaration_problems(Rogue)
    assert len(problems) == 2
    assert all("reserved for the engine" in p for p in problems)


def test_mod_29_engine_events_may_be_consumed() -> None:
    from mm3.notifications import LocaleChanged

    class Listener(MirrorModule):
        name = "listener"
        consumes: ClassVar[Any] = {"CORE_LOCALE_CHANGED": LocaleChanged}

    assert declaration_problems(Listener) == []
