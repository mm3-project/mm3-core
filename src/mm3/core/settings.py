"""Engine settings used by the bus, supervisor and scheduler.

These are the fields of the ``engine`` configuration block that this
milestone needs. Loading them from ``config.yaml`` and the full engine model
come with the configuration milestone (core spec 6).

Implements:
    -: Core spec 0.4, §3.2 Concurrency rules.
    CORE-27: Core spec 0.4, §5.3 Routing rules.
    CORE-197: Core spec 0.4, §7 Lifecycle and error handling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["BusSettings", "EngineSettings", "Timeouts"]


class Timeouts(BaseModel):
    """Time budgets in seconds (``engine.timeouts``, core spec 3.2).

    Implements:
        -: Core spec 0.4, §3.2 Concurrency rules.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: float = Field(default=15.0, gt=0)
    stop: float = Field(default=10.0, gt=0)
    suspend: float = Field(default=5.0, gt=0)
    reconfigure: float = Field(default=5.0, gt=0)
    handler: float = Field(default=5.0, gt=0)
    job: float = Field(default=60.0, gt=0)


class BusSettings(BaseModel):
    """Bus settings (``engine.bus``).

    Implements:
        CORE-27: Core spec 0.4, §5.3 Routing rules.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_size: int = Field(default=1000, ge=1)


class EngineSettings(BaseModel):
    """Engine settings needed by this milestone.

    Attributes:
        timezone: IANA zone for wall-clock jobs (``engine.timezone``).
        locale: Active locale (``engine.locale``).
        units: Unit system (``engine.units``).
        threads: Workers of the shared ``run_blocking`` pool (CORE-197).
        log_level: Default log level (CORE-110).
        state_dir: Override of the state directory (CORE-198).
        timeouts: Time budgets.
        bus: Bus settings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = "UTC"
    locale: str = "en"
    units: Literal["metric", "imperial"] = "metric"
    threads: int = Field(default=8, ge=1)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    state_dir: Path | None = None
    timeouts: Timeouts = Timeouts()
    bus: BusSettings = BusSettings()

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value
