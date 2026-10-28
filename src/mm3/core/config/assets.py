"""Files of the config set that are not YAML: custom.css and widget templates.

Both live next to the main configuration file: ``custom.css`` and the
``widget-templates`` directory. They belong to the config set, so they are
stored in the history (CORE-153), compared as sections (CORE-216),
restorable alone (CORE-155) and watched for hot reload (CORE-208).
custom.css is also validated: it follows the limits of CORE-214 except
that it may be up to 64 KiB (CORE-262); a file that breaks them is a
``CORE_CFG_CSS`` error with path ``/custom_css``.

Implements:
    CORE-153: Core spec 0.11, §6.8 Configuration history, undo and redo.
    CORE-216: Core spec 0.11, §6.8 Configuration history, undo and redo.
    CORE-262: Core spec 0.11, §6.10 Theme and custom CSS.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mm3.core.config.layout import check_css
from mm3.core.config.records import ValidationRecord, make_record

__all__ = [
    "CUSTOM_CSS",
    "CUSTOM_CSS_MAX_BYTES",
    "CUSTOM_CSS_POINTER",
    "TEMPLATE_DIR",
    "ConfigAssets",
    "read_assets",
]

CUSTOM_CSS = "custom.css"
"""File name of the custom stylesheet, next to the main file (CORE-262)."""

CUSTOM_CSS_POINTER = "/custom_css"
"""Path of records about custom.css (CORE-262)."""

CUSTOM_CSS_MAX_BYTES = 64 * 1024
"""Largest custom.css (CORE-262)."""

TEMPLATE_DIR = "widget-templates"
"""Directory of widget templates, next to the main file (core spec 11)."""


@dataclass(frozen=True)
class ConfigAssets:
    """custom.css and the widget templates of one configuration.

    Attributes:
        custom_css: Content of custom.css; ``None`` when there is none.
        templates: Template name (path below ``widget-templates``, with
            ``/`` separators) to content.
    """

    custom_css: bytes | None = None
    templates: Mapping[str, bytes] = field(default_factory=dict)

    def custom_css_text(self) -> str | None:
        """Return custom.css as text, or ``None`` when there is none or it is not UTF-8."""
        if self.custom_css is None:
            return None
        try:
            return self.custom_css.decode("utf-8")
        except UnicodeDecodeError:
            return None


def check_custom_css(data: bytes) -> str | None:
    """Check custom.css against the limits of CORE-262.

    Args:
        data: The file content.

    Returns:
        ``None`` when allowed, otherwise the reason.

    Implements:
        CORE-262: Core spec 0.11, §6.10 Theme and custom CSS.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    return check_css(text, max_bytes=CUSTOM_CSS_MAX_BYTES, relative_to="files next to custom.css")


def read_assets(config_dir: Path) -> tuple[ConfigAssets, list[ValidationRecord]]:
    """Read and check custom.css and the widget templates of a config directory.

    Args:
        config_dir: Directory of the main file.

    Returns:
        ``(assets, records)``; the records are ``CORE_CFG_FILE_UNREADABLE``
        or ``CORE_CFG_CSS`` errors about custom.css.

    Implements:
        CORE-216: Core spec 0.11, §6.8 Configuration history, undo and redo.
        CORE-262: Core spec 0.11, §6.10 Theme and custom CSS.
    """
    records: list[ValidationRecord] = []
    css_path = config_dir / CUSTOM_CSS
    css: bytes | None = None
    if css_path.exists():
        try:
            css = css_path.read_bytes()
        except OSError:
            records.append(
                make_record(
                    "CORE_CFG_FILE_UNREADABLE",
                    CUSTOM_CSS_POINTER,
                    {"file": str(css_path)},
                    file=str(css_path),
                )
            )
        else:
            if reason := check_custom_css(css):
                records.append(
                    make_record(
                        "CORE_CFG_CSS", CUSTOM_CSS_POINTER, {"reason": reason}, file=str(css_path)
                    )
                )
    templates: dict[str, bytes] = {}
    tdir = config_dir / TEMPLATE_DIR
    if tdir.is_dir():
        for path in sorted(tdir.rglob("*")):
            if path.is_file():
                try:
                    templates[path.relative_to(tdir).as_posix()] = path.read_bytes()
                except OSError:
                    continue
    return ConfigAssets(css, templates), records
