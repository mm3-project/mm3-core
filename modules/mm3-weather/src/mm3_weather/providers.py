"""Weather providers: the entry point group ``mm3_weather.providers``.

Implements:
    WTH-03: Core spec 0.19, §9.4 Reference modules.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from mm3 import ConfigurationError, ModuleContext

from .models import WeatherData, WeatherRequest

GROUP = "mm3_weather.providers"


class Provider(Protocol):
    """What a provider class offers (WTH-03).

    Attributes:
        name: The value of the ``provider`` config key.
        options_model: Pydantic model of ``provider_options`` (or ``None``).
        url_prefix: The URL prefix it contacts, which the ``http`` grant must cover.
    """

    name: ClassVar[str]
    options_model: ClassVar[type[BaseModel] | None]
    url_prefix: ClassVar[str]

    async def fetch(self, http: Any, request: WeatherRequest) -> WeatherData:
        """Fetch and normalise the weather for ``request``."""
        ...


def builtin() -> dict[str, type[Provider]]:
    """The providers that ship with the module."""
    from .open_meteo import OpenMeteo

    return {OpenMeteo.name: OpenMeteo}


def load_provider(name: str, ctx: ModuleContext) -> type[Provider]:
    """Find a provider by name: built in first, then the entry point group.

    Args:
        name: The ``provider`` config value.
        ctx: The module context (for logging).

    Returns:
        The provider class.

    Raises:
        ConfigurationError: ``CFG_PROVIDER_UNKNOWN``.
    """
    found = builtin()
    if name in found:
        return found[name]
    for point in entry_points(group=GROUP):
        if point.name != name:
            continue
        try:
            loaded: type[Provider] = point.load()
        except ImportError as exc:
            ctx.log.warning("provider_import_failed", provider=name, error=str(exc))
            break
        return loaded
    raise ConfigurationError("provider", "CFG_PROVIDER_UNKNOWN")
