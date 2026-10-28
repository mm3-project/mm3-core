"""The configuration file model: top-level keys and module instances.

This module checks each field on its own: types, ranges, patterns and
allowed values. Rules that relate fields to each other or to the installed
modules (ids, missing modules) live in :mod:`mm3.core.config.validate`; the
geometry rules of screen layout, display and pages (core spec 6.5, 6.7,
6.9) are added with the layout work of milestone 2.

Implements:
    CORE-201: Core spec 0.11, §6.2 Rules.
    CORE-202: Core spec 0.8, §6.2 Rules.
    CORE-244: Core spec 0.8, §10.8 Backup and restore.
    CORE-91: Core spec 0.8, §6.5 Screen layout.
    -: Core spec 0.8, §6.7 Display output.
    -: Core spec 0.8, §6.9 Pages and visibility.
    CORE-213: Core spec 0.9, §6.5 Screen layout.
    CORE-230: Core spec 0.9, §6.9 Pages and visibility.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    StrictBool,
    Tag,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

from mm3.config import LocaleTag, LocalizedText, ModuleConfig
from mm3.core.settings import EngineSettings

__all__ = [
    "CONFIG_VERSION",
    "INSTANCE_ID_PATTERN",
    "PAGE_ID_PATTERN",
    "PRESETS",
    "Appearance",
    "BackupConfig",
    "DisplayConfig",
    "Grants",
    "InstanceConfig",
    "LayoutConfig",
    "MirrorConfig",
    "PageConfig",
    "PagesConfig",
    "Rect",
]

CONFIG_VERSION = 1
INSTANCE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PAGE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
PRESETS = (
    "top_bar",
    "top_left",
    "top_center",
    "top_right",
    "upper_third",
    "middle_center",
    "lower_third",
    "bottom_left",
    "bottom_center",
    "bottom_right",
    "bottom_bar",
    "fullscreen_below",
    "fullscreen_above",
)
PresetName = Literal[
    "top_bar",
    "top_left",
    "top_center",
    "top_right",
    "upper_third",
    "middle_center",
    "lower_third",
    "bottom_left",
    "bottom_center",
    "bottom_right",
    "bottom_bar",
    "fullscreen_below",
    "fullscreen_above",
]


class _Model(ModuleConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _pixels(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        number = text.removesuffix("px").strip()
        try:
            return float(number)
        except ValueError:
            raise PydanticCustomError(
                "CORE_CFG_TYPE", "pixel length", {"expected": "a pixel length such as 16px"}
            ) from None
    return value


Pixels = Annotated[float, BeforeValidator(_pixels), Field(ge=0, le=500)]
"""A pixel length: ``16px`` or a number, 0 to 500 (CORE-213)."""


class Rect(_Model):
    """A grid rectangle; values are 1-based whole cells (CORE-91).

    Implements:
        CORE-91: Core spec 0.8, §6.5 Screen layout.
    """

    col: int = Field(ge=1, le=48)
    row: int = Field(ge=1, le=48)
    width: int = Field(ge=1, le=48)
    height: int = Field(ge=1, le=48)
    z: int = 0


def _position_kind(value: Any) -> str:
    return "preset" if isinstance(value, str) else "rect"


Position = Annotated[
    Annotated[PresetName, Tag("preset")] | Annotated[Rect, Tag("rect")],
    Discriminator(_position_kind),
]
"""A preset name or a grid rectangle (CORE-91)."""


def _header_kind(value: Any) -> str:
    return "flag" if isinstance(value, bool) else "text"


class Appearance(_Model):
    """Appearance common to every instance (core spec 6.5)."""

    scale: float = Field(default=1.0, ge=0.5, le=3.0)
    align: Literal["start", "center", "end"] = "start"
    valign: Literal["start", "center", "end"] = "start"
    opacity: float = Field(default=1.0, ge=0, le=1)
    overflow: Literal["clip", "fit", "scroll"] = "clip"
    header: Annotated[
        Annotated[StrictBool, Tag("flag")] | Annotated[LocalizedText, Tag("text")],
        Discriminator(_header_kind),
    ] = False
    classes: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")]] = Field(
        default_factory=list
    )
    css: str | None = None


class MqttGrants(_Model):
    """MQTT topic globs an instance may use (Contract section 8)."""

    subscribe: list[str] = Field(default_factory=list)
    publish: list[str] = Field(default_factory=list)


class Grants(_Model):
    """Scopes granted to an instance (Contract section 8)."""

    mqtt: MqttGrants = MqttGrants()
    http: list[str] = Field(default_factory=list)


class InstanceConfig(_Model):
    """One entry of ``modules`` (CORE-202, CORE-231).

    The ``config`` block is kept raw here and validated against the
    module's ``config_model``.

    Implements:
        CORE-202: Core spec 0.8, §6.2 Rules.
    """

    module: str = Field(min_length=1)
    id: str | None = Field(default=None, pattern=INSTANCE_ID_PATTERN)
    position: Position | None = None
    positions: dict[str, Position] = Field(default_factory=dict)
    pages: Literal["all"] | list[str] = "all"
    appearance: Appearance = Appearance()
    config: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False
    locale: LocaleTag | None = None
    grants: Grants = Grants()


class GridSize(_Model):
    """Grid size (``layout.grid``), each 1 to 48."""

    columns: int = Field(default=12, ge=1, le=48)
    rows: int = Field(default=12, ge=1, le=48)


class LayoutConfig(_Model):
    """The ``layout`` block (core spec 6.5); gap 16px and padding 32px by default.

    Implements:
        CORE-213: Core spec 0.9, §6.5 Screen layout.
    """

    grid: GridSize = GridSize()
    gap: Pixels = 16
    padding: Pixels = 32


class DisplayConfig(_Model):
    """The ``display`` block (core spec 6.7)."""

    output: str | None = None
    width: int | None = Field(default=None, ge=320, le=7680)
    height: int | None = Field(default=None, ge=320, le=7680)
    refresh: int | None = Field(default=None, ge=1)
    rotation: Literal[0, 90, 180, 270] = 0
    scale: float = Field(default=1.0, ge=0.5, le=3.0)


class PageConfig(_Model):
    """One entry of ``pages.list`` (CORE-230)."""

    id: str = Field(pattern=PAGE_ID_PATTERN)
    title: LocalizedText | None = None
    rotate: timedelta | None = None

    def resolved_title(self, locale: str) -> str:
        """Return the page title for a locale; the id when no title is set (CORE-230).

        Args:
            locale: The locale.

        Returns:
            The title.
        """
        return self.title.resolve(locale) if self.title is not None else self.id


class PagesConfig(_Model):
    """The ``pages`` block (CORE-230); ``list`` is read into ``pages``."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True)

    default: str | None = None
    rotate: timedelta = timedelta(0)
    pause_on_interaction: timedelta = timedelta(minutes=2)
    reset_on_resume: bool = True
    transition: Literal["fade", "none"] = "fade"
    pages: list[PageConfig] = Field(alias="list", min_length=1, max_length=32)


class BackupKeep(_Model):
    """Backup retention counts (CORE-244)."""

    daily: int = Field(default=7, ge=0, le=1000)
    weekly: int = Field(default=4, ge=0, le=1000)
    monthly: int = Field(default=6, ge=0, le=1000)

    @field_validator("monthly")
    @classmethod
    def _not_all_zero(cls, value: int, info: ValidationInfo) -> int:
        if value == 0 and info.data.get("daily") == 0 and info.data.get("weekly") == 0:
            raise PydanticCustomError(
                "CORE_CFG_INVALID",
                "keeps nothing",
                {"reason": "keep must keep at least one backup"},
            )
        return value


class BackupRemote(_Model):
    """Remote copy target (CORE-244)."""

    name: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
    path: str = ""


BackupPart = Literal["config", "data", "modules", "secrets"]


class BackupConfig(_Model):
    """The ``backup`` block (CORE-244); backups themselves run in milestone 7.

    Implements:
        CORE-244: Core spec 0.8, §10.8 Backup and restore.
    """

    schedule: str = Field(default="03:30", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    encrypt_to: str | None = Field(default=None, pattern=r"^age1[02-9ac-hj-np-z]{58}$")
    parts: list[BackupPart] | None = Field(default=None, min_length=1)
    keep: BackupKeep = BackupKeep()
    remote: BackupRemote | None = None

    @field_validator("parts")
    @classmethod
    def _check_parts(
        cls, value: list[BackupPart] | None, info: ValidationInfo
    ) -> list[BackupPart] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise PydanticCustomError(
                "CORE_CFG_INVALID", "duplicate part", {"reason": "a part is listed twice"}
            )
        if "secrets" in value and info.data.get("encrypt_to") is None:
            raise PydanticCustomError("CORE_CFG_SECRETS_UNENCRYPTED", "secrets unencrypted", {})
        return value

    @property
    def effective_parts(self) -> list[BackupPart]:
        """Parts backed up: as configured, else the default of CORE-244."""
        if self.parts is not None:
            return list(self.parts)
        base: list[BackupPart] = ["config", "data", "modules"]
        return [*base, "secrets"] if self.encrypt_to else base


def _check_version(value: int) -> int:
    if value > CONFIG_VERSION:
        raise PydanticCustomError(
            "CORE_CFG_VERSION", "config_version too new", {"value": value, "max": CONFIG_VERSION}
        )
    return value


class MirrorConfig(_Model):
    """The whole configuration file (CORE-201, CORE-230).

    Implements:
        CORE-201: Core spec 0.11, §6.2 Rules.
    """

    config_version: Annotated[int, Field(ge=1)] = 1
    engine: EngineSettings = Field(default_factory=EngineSettings)
    display: DisplayConfig = DisplayConfig()
    layout: LayoutConfig = LayoutConfig()
    pages: PagesConfig | None = None
    modules: list[InstanceConfig] = Field(default_factory=list)
    backup: BackupConfig = BackupConfig()

    @field_validator("config_version")
    @classmethod
    def _version(cls, value: int) -> int:
        return _check_version(value)
