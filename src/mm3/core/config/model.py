"""The configuration file model: top-level keys and module instances.

This module checks each field on its own: types, ranges, patterns and
allowed values. Rules that relate fields to each other or to the installed
modules (ids, missing modules) live in :mod:`mm3.core.config.validate`; the
geometry rules of screen layout, display and pages (core spec 6.5, 6.7,
6.9) are added with the layout work of milestone 2.

Implements:
    CORE-201: Core spec 0.21, §6.2 Rules.
    CORE-202: Core spec 0.8, §6.2 Rules.
    CORE-244: Core spec 0.8, §10.8 Backup and restore.
    CORE-91: Core spec 0.8, §6.5 Screen layout.
    -: Core spec 0.21, §6.7 Display output.
    -: Core spec 0.8, §6.9 Pages and visibility.
    CORE-213: Core spec 0.9, §6.5 Screen layout.
    CORE-230: Core spec 0.9, §6.9 Pages and visibility.
    CORE-333, CORE-334, CORE-335: Core spec 0.34, §6.9 Pages and visibility.
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
    "AudioConfig",
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

    col: int = Field(
        ge=1, le=48, title="CFG_MODULES_POSITION_COL", description="CFG_MODULES_POSITION_COL_HELP"
    )
    row: int = Field(
        ge=1, le=48, title="CFG_MODULES_POSITION_ROW", description="CFG_MODULES_POSITION_ROW_HELP"
    )
    width: int = Field(
        ge=1,
        le=48,
        title="CFG_MODULES_POSITION_WIDTH",
        description="CFG_MODULES_POSITION_WIDTH_HELP",
    )
    height: int = Field(
        ge=1,
        le=48,
        title="CFG_MODULES_POSITION_HEIGHT",
        description="CFG_MODULES_POSITION_HEIGHT_HELP",
    )
    z: int = Field(
        default=0, title="CFG_MODULES_POSITION_Z", description="CFG_MODULES_POSITION_Z_HELP"
    )


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

    scale: float = Field(
        default=1.0,
        ge=0.5,
        le=3.0,
        title="CFG_MODULES_APPEARANCE_SCALE",
        description="CFG_MODULES_APPEARANCE_SCALE_HELP",
    )
    align: Literal["start", "center", "end"] = Field(
        default="start",
        title="CFG_MODULES_APPEARANCE_ALIGN",
        description="CFG_MODULES_APPEARANCE_ALIGN_HELP",
    )
    valign: Literal["start", "center", "end"] = Field(
        default="start",
        title="CFG_MODULES_APPEARANCE_VALIGN",
        description="CFG_MODULES_APPEARANCE_VALIGN_HELP",
    )
    opacity: float = Field(
        default=1.0,
        ge=0,
        le=1,
        title="CFG_MODULES_APPEARANCE_OPACITY",
        description="CFG_MODULES_APPEARANCE_OPACITY_HELP",
    )
    overflow: Literal["clip", "fit", "scroll"] = Field(
        default="clip",
        title="CFG_MODULES_APPEARANCE_OVERFLOW",
        description="CFG_MODULES_APPEARANCE_OVERFLOW_HELP",
    )
    header: Annotated[
        Annotated[StrictBool, Tag("flag")] | Annotated[LocalizedText, Tag("text")],
        Discriminator(_header_kind),
    ] = Field(
        default=False,
        title="CFG_MODULES_APPEARANCE_HEADER",
        description="CFG_MODULES_APPEARANCE_HEADER_HELP",
    )
    classes: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")]] = Field(
        default_factory=list,
        title="CFG_MODULES_APPEARANCE_CLASSES",
        description="CFG_MODULES_APPEARANCE_CLASSES_HELP",
    )
    css: str | None = Field(
        default=None,
        title="CFG_MODULES_APPEARANCE_CSS",
        description="CFG_MODULES_APPEARANCE_CSS_HELP",
    )


class MqttGrants(_Model):
    """MQTT topic globs an instance may use (Contract section 8)."""

    subscribe: list[str] = Field(
        default_factory=list,
        title="CFG_MODULES_GRANTS_MQTT_SUBSCRIBE",
        description="CFG_MODULES_GRANTS_MQTT_SUBSCRIBE_HELP",
    )
    publish: list[str] = Field(
        default_factory=list,
        title="CFG_MODULES_GRANTS_MQTT_PUBLISH",
        description="CFG_MODULES_GRANTS_MQTT_PUBLISH_HELP",
    )


class HttpGrant(_Model):
    """An ``http`` grant entry with its own CA file (CORE-284).

    Attributes:
        prefix: URL prefix the grant covers.
        ca: PEM file, relative to the directory of the main configuration
            file, that replaces the CA store for requests matching ``prefix``.

    Implements:
        CORE-284: Core spec 0.19, §3.4 HTTP transport.
    """

    prefix: str = Field(
        min_length=1,
        title="CFG_MODULES_GRANTS_HTTP_PREFIX",
        description="CFG_MODULES_GRANTS_HTTP_PREFIX_HELP",
    )
    ca: str = Field(
        min_length=1,
        title="CFG_MODULES_GRANTS_HTTP_CA",
        description="CFG_MODULES_GRANTS_HTTP_CA_HELP",
    )


class Grants(_Model):
    """Scopes granted to an instance (Contract section 8)."""

    mqtt: MqttGrants = Field(
        default=MqttGrants(),
        title="CFG_MODULES_GRANTS_MQTT",
        description="CFG_MODULES_GRANTS_MQTT_HELP",
    )
    http: list[str | HttpGrant] = Field(
        default_factory=list,
        title="CFG_MODULES_GRANTS_HTTP",
        description="CFG_MODULES_GRANTS_HTTP_HELP",
    )


class InstanceConfig(_Model):
    """One entry of ``modules`` (CORE-202, CORE-231).

    The ``config`` block is kept raw here and validated against the
    module's ``config_model``.

    Implements:
        CORE-202: Core spec 0.8, §6.2 Rules.
    """

    module: str = Field(
        min_length=1, title="CFG_MODULES_MODULE", description="CFG_MODULES_MODULE_HELP"
    )
    id: str | None = Field(
        default=None,
        pattern=INSTANCE_ID_PATTERN,
        title="CFG_MODULES_ID",
        description="CFG_MODULES_ID_HELP",
    )
    position: Position | None = Field(
        default=None, title="CFG_MODULES_POSITION", description="CFG_MODULES_POSITION_HELP"
    )
    positions: dict[str, Position] = Field(
        default_factory=dict,
        title="CFG_MODULES_POSITIONS",
        description="CFG_MODULES_POSITIONS_HELP",
    )
    pages: Literal["all"] | list[str] = Field(
        default="all", title="CFG_MODULES_PAGES", description="CFG_MODULES_PAGES_HELP"
    )
    appearance: Appearance = Field(
        default=Appearance(),
        title="CFG_MODULES_APPEARANCE",
        description="CFG_MODULES_APPEARANCE_HELP",
    )
    config: dict[str, Any] = Field(
        default_factory=dict, title="CFG_MODULES_CONFIG", description="CFG_MODULES_CONFIG_HELP"
    )
    disabled: bool = Field(
        default=False, title="CFG_MODULES_DISABLED", description="CFG_MODULES_DISABLED_HELP"
    )
    locale: LocaleTag | None = Field(
        default=None, title="CFG_MODULES_LOCALE", description="CFG_MODULES_LOCALE_HELP"
    )
    grants: Grants = Field(
        default=Grants(), title="CFG_MODULES_GRANTS", description="CFG_MODULES_GRANTS_HELP"
    )


class GridSize(_Model):
    """Grid size (``layout.grid``), each 1 to 48."""

    columns: int = Field(
        default=12,
        ge=1,
        le=48,
        title="CFG_LAYOUT_GRID_COLUMNS",
        description="CFG_LAYOUT_GRID_COLUMNS_HELP",
    )
    rows: int = Field(
        default=12,
        ge=1,
        le=48,
        title="CFG_LAYOUT_GRID_ROWS",
        description="CFG_LAYOUT_GRID_ROWS_HELP",
    )


class LayoutConfig(_Model):
    """The ``layout`` block (core spec 6.5); gap 16px and padding 32px by default.

    Implements:
        CORE-213: Core spec 0.9, §6.5 Screen layout.
    """

    grid: GridSize = Field(
        default=GridSize(), title="CFG_LAYOUT_GRID", description="CFG_LAYOUT_GRID_HELP"
    )
    gap: Pixels = Field(default=16, title="CFG_LAYOUT_GAP", description="CFG_LAYOUT_GAP_HELP")
    padding: Pixels = Field(
        default=32, title="CFG_LAYOUT_PADDING", description="CFG_LAYOUT_PADDING_HELP"
    )


class DisplayConfig(_Model):
    """The ``display`` block (core spec 6.7)."""

    output: str | None = Field(
        default=None, title="CFG_DISPLAY_OUTPUT", description="CFG_DISPLAY_OUTPUT_HELP"
    )
    width: int | None = Field(
        default=None,
        ge=320,
        le=7680,
        title="CFG_DISPLAY_WIDTH",
        description="CFG_DISPLAY_WIDTH_HELP",
    )
    height: int | None = Field(
        default=None,
        ge=320,
        le=7680,
        title="CFG_DISPLAY_HEIGHT",
        description="CFG_DISPLAY_HEIGHT_HELP",
    )
    refresh: int | None = Field(
        default=None, ge=1, title="CFG_DISPLAY_REFRESH", description="CFG_DISPLAY_REFRESH_HELP"
    )
    rotation: Literal[0, 90, 180, 270] = Field(
        default=0, title="CFG_DISPLAY_ROTATION", description="CFG_DISPLAY_ROTATION_HELP"
    )
    scale: float = Field(
        default=1.0, ge=0.5, le=3.0, title="CFG_DISPLAY_SCALE", description="CFG_DISPLAY_SCALE_HELP"
    )


class AudioConfig(_Model):
    """The ``audio`` block: output, volume and mute of the local kiosk (core spec 6.7).

    The block is optional and, unlike ``display``, has no default instance:
    without it the kiosk keeps PipeWire's default output and volume. An output
    name that the kiosk agent does not report is not a configuration error;
    the mismatch is shown as for a display mode (CORE-296).

    Attributes:
        sink: ``auto`` (WirePlumber's default output), ``hdmi`` or ``analog``
            (the first available output of that kind), or the exact name of
            an output reported by the agent.
        volume: Volume of the chosen output in percent; ``None`` leaves the
            volume as it is.
        mute: Mutes the chosen output.

    Implements:
        -: Core spec 0.21, §6.7 Display output.
    """

    sink: str = Field(
        default="auto", min_length=1, title="CFG_AUDIO_SINK", description="CFG_AUDIO_SINK_HELP"
    )
    volume: int | None = Field(
        default=None, ge=0, le=100, title="CFG_AUDIO_VOLUME", description="CFG_AUDIO_VOLUME_HELP"
    )
    mute: bool = Field(default=False, title="CFG_AUDIO_MUTE", description="CFG_AUDIO_MUTE_HELP")


class PageAfter(_Model):
    """The ``after`` key of a page: a timed jump (CORE-334).

    Attributes:
        time: How long the page stays current before the jump; above 0.
        page: The page to jump to; ``None`` means ``pages.default``. It must
            be a listed page other than the entry's own (checked with the
            layout rules, because it relates two fields).
    """

    time: timedelta = Field(
        gt=timedelta(0),
        title="CFG_PAGES_LIST_AFTER_TIME",
        description="CFG_PAGES_LIST_AFTER_TIME_HELP",
    )
    page: str | None = Field(
        default=None,
        pattern=PAGE_ID_PATTERN,
        title="CFG_PAGES_LIST_AFTER_PAGE",
        description="CFG_PAGES_LIST_AFTER_PAGE_HELP",
    )


class PageConfig(_Model):
    """One entry of ``pages.list`` (CORE-230, CORE-333, CORE-334).

    Attributes:
        id: Page id.
        title: Title shown in the admin and in Home Assistant.
        rotate: Rotation time of this page, overriding ``pages.rotate``.
        cycle: ``False`` takes the page out of the cycle: rotation,
            ``PAGE_NEXT``, ``PAGE_PREV`` and swipes never reach it (CORE-333).
        after: Jump to another page once this one has been current for a
            time (CORE-334).
    """

    id: str = Field(
        pattern=PAGE_ID_PATTERN, title="CFG_PAGES_LIST_ID", description="CFG_PAGES_LIST_ID_HELP"
    )
    title: LocalizedText | None = Field(
        default=None, title="CFG_PAGES_LIST_TITLE", description="CFG_PAGES_LIST_TITLE_HELP"
    )
    rotate: timedelta | None = Field(
        default=None, title="CFG_PAGES_LIST_ROTATE", description="CFG_PAGES_LIST_ROTATE_HELP"
    )
    cycle: bool = Field(
        default=True, title="CFG_PAGES_LIST_CYCLE", description="CFG_PAGES_LIST_CYCLE_HELP"
    )
    after: PageAfter | None = Field(
        default=None, title="CFG_PAGES_LIST_AFTER", description="CFG_PAGES_LIST_AFTER_HELP"
    )

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

    default: str | None = Field(
        default=None, title="CFG_PAGES_DEFAULT", description="CFG_PAGES_DEFAULT_HELP"
    )
    rotate: timedelta = Field(
        default=timedelta(0), title="CFG_PAGES_ROTATE", description="CFG_PAGES_ROTATE_HELP"
    )
    pause_on_interaction: timedelta = Field(
        default=timedelta(minutes=2),
        title="CFG_PAGES_PAUSE_ON_INTERACTION",
        description="CFG_PAGES_PAUSE_ON_INTERACTION_HELP",
    )
    reset_on_resume: bool = Field(
        default=True,
        title="CFG_PAGES_RESET_ON_RESUME",
        description="CFG_PAGES_RESET_ON_RESUME_HELP",
    )
    return_after: timedelta = Field(
        default=timedelta(0),
        ge=timedelta(0),
        title="CFG_PAGES_RETURN_AFTER",
        description="CFG_PAGES_RETURN_AFTER_HELP",
    )
    transition: Literal["fade", "none"] = Field(
        default="fade", title="CFG_PAGES_TRANSITION", description="CFG_PAGES_TRANSITION_HELP"
    )
    pages: list[PageConfig] = Field(
        alias="list",
        min_length=1,
        max_length=32,
        title="CFG_PAGES_LIST",
        description="CFG_PAGES_LIST_HELP",
    )


class BackupKeep(_Model):
    """Backup retention counts (CORE-244)."""

    daily: int = Field(
        default=7,
        ge=0,
        le=1000,
        title="CFG_BACKUP_KEEP_DAILY",
        description="CFG_BACKUP_KEEP_DAILY_HELP",
    )
    weekly: int = Field(
        default=4,
        ge=0,
        le=1000,
        title="CFG_BACKUP_KEEP_WEEKLY",
        description="CFG_BACKUP_KEEP_WEEKLY_HELP",
    )
    monthly: int = Field(
        default=6,
        ge=0,
        le=1000,
        title="CFG_BACKUP_KEEP_MONTHLY",
        description="CFG_BACKUP_KEEP_MONTHLY_HELP",
    )

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

    name: str = Field(
        pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$",
        title="CFG_BACKUP_REMOTE_NAME",
        description="CFG_BACKUP_REMOTE_NAME_HELP",
    )
    path: str = Field(
        default="", title="CFG_BACKUP_REMOTE_PATH", description="CFG_BACKUP_REMOTE_PATH_HELP"
    )


BackupPart = Literal["config", "data", "modules", "secrets"]


class BackupConfig(_Model):
    """The ``backup`` block (CORE-244); backups themselves run in milestone 7.

    Implements:
        CORE-244: Core spec 0.8, §10.8 Backup and restore.
    """

    schedule: str = Field(
        default="03:30",
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
        title="CFG_BACKUP_SCHEDULE",
        description="CFG_BACKUP_SCHEDULE_HELP",
    )
    encrypt_to: str | None = Field(
        default=None,
        pattern=r"^age1[02-9ac-hj-np-z]{58}$",
        title="CFG_BACKUP_ENCRYPT_TO",
        description="CFG_BACKUP_ENCRYPT_TO_HELP",
    )
    parts: list[BackupPart] | None = Field(
        default=None, min_length=1, title="CFG_BACKUP_PARTS", description="CFG_BACKUP_PARTS_HELP"
    )
    keep: BackupKeep = Field(
        default=BackupKeep(), title="CFG_BACKUP_KEEP", description="CFG_BACKUP_KEEP_HELP"
    )
    remote: BackupRemote | None = Field(
        default=None, title="CFG_BACKUP_REMOTE", description="CFG_BACKUP_REMOTE_HELP"
    )

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

    ``audio`` is ``None`` when the block is absent (CORE-296).

    Implements:
        CORE-201: Core spec 0.21, §6.2 Rules.
    """

    config_version: Annotated[int, Field(ge=1)] = Field(
        default=1, title="CFG_CONFIG_VERSION", description="CFG_CONFIG_VERSION_HELP"
    )
    engine: EngineSettings = Field(
        default_factory=EngineSettings, title="CFG_ENGINE", description="CFG_ENGINE_HELP"
    )
    display: DisplayConfig = Field(
        default=DisplayConfig(), title="CFG_DISPLAY", description="CFG_DISPLAY_HELP"
    )
    audio: AudioConfig | None = Field(default=None, title="CFG_AUDIO", description="CFG_AUDIO_HELP")
    layout: LayoutConfig = Field(
        default=LayoutConfig(), title="CFG_LAYOUT", description="CFG_LAYOUT_HELP"
    )
    pages: PagesConfig | None = Field(default=None, title="CFG_PAGES", description="CFG_PAGES_HELP")
    modules: list[InstanceConfig] = Field(
        default_factory=list, title="CFG_MODULES", description="CFG_MODULES_HELP"
    )
    backup: BackupConfig = Field(
        default=BackupConfig(), title="CFG_BACKUP", description="CFG_BACKUP_HELP"
    )

    @field_validator("config_version")
    @classmethod
    def _version(cls, value: int) -> int:
        return _check_version(value)
