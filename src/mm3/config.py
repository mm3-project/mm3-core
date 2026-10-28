"""Configuration types for modules.

``ModuleConfig`` is the base of every module's ``config_model``.
``LocalizedText`` holds user-typed display text in one or more languages,
``LocaleTag`` is a BCP 47 language tag, and ``CronExpression`` is a cron
string validated when the configuration is loaded. ``timedelta`` fields of
every ``ModuleConfig`` accept seconds, ISO 8601 durations and the compact
form ``1h30m``.

Implements:
    MOD-45, MOD-48: Module Contract 1.0 rev 6, §6.2 Configuration.
    CORE-101: Core spec 0.8, §11.1 Engine additions.
    CORE-205: Core spec 0.8, §6.2 Rules.
    CORE-188: Core spec 0.4, §3.3 Scheduler.
"""

from __future__ import annotations

import re
import types
from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError, core_schema

from mm3.core.services.cron import parse_cron

__all__ = [
    "CronExpression",
    "LocaleTag",
    "LocalizedText",
    "ModuleConfig",
    "is_locale_tag",
    "locale_chain",
    "parse_duration",
]

# BCP 47 language tag: language, optional script, region, variants and
# extensions (RFC 5646, without grandfathered and private-use-only tags).
_TAG = re.compile(
    r"^[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}"
    r"(?:-[A-Za-z]{4})?"
    r"(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-x(?:-[A-Za-z0-9]{1,8})+)?$"
)
_COMPACT = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+)s)?(?:(\d+)ms)?$")


def is_locale_tag(value: object) -> bool:
    """Tell whether a value is a well-formed BCP 47 language tag.

    Args:
        value: Candidate.

    Returns:
        True for tags such as ``en``, ``de-CH`` or ``zh-Hant-TW``.
    """
    return isinstance(value, str) and bool(_TAG.match(value))


def _check_tag(value: str) -> str:
    if not is_locale_tag(value):
        raise PydanticCustomError("CORE_CFG_LOCALE", "invalid language tag", {"value": value})
    return value


LocaleTag = Annotated[str, AfterValidator(_check_tag)]
"""A BCP 47 language tag; invalid tags give ``CORE_CFG_LOCALE``."""


def locale_chain(locale: str | Sequence[str]) -> list[str]:
    """Build the fallback chain for a locale (core spec 8.2).

    Args:
        locale: One tag, or a chain already starting with the most specific tag.

    Returns:
        The tags to try in order, lower-cased, each followed by its base
        languages, ending with ``en``; no duplicates.
    """
    first = [locale] if isinstance(locale, str) else list(locale)
    out: list[str] = []
    for tag in [*first, "en"]:
        parts = tag.lower().split("-")
        for n in range(len(parts), 0, -1):
            candidate = "-".join(parts[:n])
            if candidate not in out:
                out.append(candidate)
    return out


def parse_duration(value: str) -> timedelta | None:
    """Parse the compact duration form ``30s``, ``10m``, ``1h30m``, ``2d``.

    Units are ``d``, ``h``, ``m``, ``s`` and ``ms``, each at most once and
    from the largest to the smallest.

    Args:
        value: The text.

    Returns:
        The duration, or ``None`` when the text is not in the compact form.

    Implements:
        CORE-205: Core spec 0.8, §6.2 Rules.
    """
    m = _COMPACT.match(value.strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s, ms = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=s, milliseconds=ms)


def _coerce_durations(annotation: Any, value: Any) -> Any:
    """Convert compact duration strings wherever the annotation expects ``timedelta``."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _coerce_durations(get_args(annotation)[0], value)
    if annotation is timedelta:
        if isinstance(value, str) and (parsed := parse_duration(value)) is not None:
            return parsed
        return value
    if origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            converted = _coerce_durations(arg, value)
            if converted is not value:
                return converted
        return value
    if origin in (list, tuple, set, frozenset, Sequence) and isinstance(value, list):
        args = get_args(annotation)
        if args:
            return [_coerce_durations(args[0], v) for v in value]
    if origin in (dict, Mapping) and isinstance(value, dict):
        args = get_args(annotation)
        if len(args) == 2:
            return {k: _coerce_durations(args[1], v) for k, v in value.items()}
    return value


class ModuleConfig(BaseModel):
    """Base class of a module's configuration model.

    Unknown keys are errors (``extra="forbid"``, CORE-32). ``timedelta``
    fields, also inside ``Optional``, lists and dicts, accept the forms of
    CORE-205; nested models should subclass ``ModuleConfig`` too.

    Implements:
        MOD-45: Module Contract 1.0 rev 6, §6.2 Configuration.
        CORE-32: Core spec 0.8, §6.2 Rules.
        CORE-205: Core spec 0.8, §6.2 Rules.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _durations(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name is None:
            return value
        field = cls.model_fields.get(info.field_name)
        return value if field is None else _coerce_durations(field.annotation, value)


class LocalizedText:
    """User-typed display text in one or more languages.

    Accepts a string, the same in every language, or a non-empty mapping
    from BCP 47 tags to strings (``{en: Home, it: Casa}``).

    Args:
        value: The string or the mapping, as written.

    Implements:
        CORE-101: Core spec 0.8, §11.1 Engine additions.
        MOD-48: Module Contract 1.0 rev 6, §6.2 Configuration.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | Mapping[str, str]) -> None:
        self._value: str | dict[str, str] = value if isinstance(value, str) else dict(value)

    @property
    def value(self) -> str | dict[str, str]:
        """The text as written: a string or a mapping."""
        return self._value if isinstance(self._value, str) else dict(self._value)

    @property
    def missing_en(self) -> bool:
        """True for a mapping without an ``en`` entry (a validation warning)."""
        return isinstance(self._value, dict) and not any(k.lower() == "en" for k in self._value)

    def languages(self) -> list[str]:
        """Return the tags of a mapping in file order (empty for a plain string).

        Returns:
            The tags.
        """
        return [] if isinstance(self._value, str) else list(self._value)

    def resolve(self, locale: str | Sequence[str]) -> str:
        """Pick the text for a locale.

        The locale chain (core spec 8.2) is tried in order; when none of its
        tags is present the first entry in file order is used.

        Args:
            locale: The active locale (``ctx.locale``) or a full chain.

        Returns:
            The text.
        """
        if isinstance(self._value, str):
            return self._value
        lowered = {k.lower(): v for k, v in self._value.items()}
        for tag in locale_chain(locale):
            if tag in lowered:
                return lowered[tag]
        return next(iter(self._value.values()))

    def __str__(self) -> str:
        return self.resolve("en")

    def __repr__(self) -> str:
        return f"LocalizedText({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LocalizedText):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        v = self._value
        return hash(v if isinstance(v, str) else tuple(v.items()))

    @classmethod
    def _validate(cls, value: Any) -> LocalizedText:
        if isinstance(value, LocalizedText):
            return value
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            if not value:
                raise PydanticCustomError(
                    "CORE_CFG_LENGTH", "at least one language", {"min": 1, "max": None}
                )
            for tag, text in value.items():
                if not is_locale_tag(tag):
                    raise PydanticCustomError(
                        "CORE_CFG_LOCALE", "invalid language tag", {"value": tag}
                    )
                if not isinstance(text, str):
                    raise PydanticCustomError(
                        "CORE_CFG_TYPE", "expected a string", {"expected": "a string"}
                    )
            return cls({str(k): str(v) for k, v in value.items()})
        raise PydanticCustomError(
            "CORE_CFG_TYPE",
            "expected a string or a mapping of languages",
            {"expected": "a string or a mapping of languages"},
        )

    @staticmethod
    def _serialize(value: LocalizedText, info: core_schema.SerializationInfo) -> Any:
        ctx = info.context
        if isinstance(ctx, Mapping) and isinstance(ctx.get("locale"), str | list | tuple):
            return value.resolve(ctx["locale"])
        return value.value

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                cls._serialize, info_arg=True
            ),
            json_schema_input_schema=core_schema.union_schema(
                [
                    core_schema.str_schema(),
                    core_schema.dict_schema(core_schema.str_schema(), core_schema.str_schema()),
                ]
            ),
        )


def iter_localized(
    value: Any, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], LocalizedText]]:
    """Find every ``LocalizedText`` in a validated configuration.

    Args:
        value: A model, container or value.
        path: Location of ``value``.

    Yields:
        ``(path, text)`` pairs, in field order.
    """
    if isinstance(value, LocalizedText):
        yield path, value
    elif isinstance(value, BaseModel):
        for name, info in type(value).model_fields.items():
            yield from iter_localized(getattr(value, name), (*path, info.alias or name))
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield from iter_localized(v, (*path, str(k)))
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from iter_localized(v, (*path, i))


def _validate_cron(value: str) -> str:
    try:
        parse_cron(value)
    except ValueError as exc:
        raise PydanticCustomError("CORE_CFG_CRON", "invalid cron", {"reason": str(exc)}) from exc
    return value


CronExpression = Annotated[str, AfterValidator(_validate_cron)]
"""A five-field cron string or macro; invalid or never-matching strings fail validation."""
