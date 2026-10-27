"""Configuration types for modules.

``ModuleConfig`` is the base of every module's ``config_model``;
``CronExpression`` is a cron string validated when the configuration is
loaded. ``LocalizedText`` (CORE-101) is added with the configuration
milestone.

Implements:
    MOD-45: Module Contract 1.0 rev 4, §6.2 Configuration.
    CORE-188: Core spec 0.4, §3.3 Scheduler.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from mm3.core.services.cron import parse_cron

__all__ = ["CronExpression", "ModuleConfig"]


class ModuleConfig(BaseModel):
    """Base class of a module's configuration model.

    Unknown keys are errors (``extra="forbid"``, CORE-32).

    Implements:
        MOD-45: Module Contract 1.0 rev 4, §6.2 Configuration.
    """

    model_config = ConfigDict(extra="forbid")


def _validate_cron(value: str) -> str:
    parse_cron(value)
    return value


CronExpression = Annotated[str, AfterValidator(_validate_cron)]
"""A five-field cron string or macro; invalid or never-matching strings fail validation."""
