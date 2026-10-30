"""The scopes granted to one instance, as the engine services read them.

Built from the instance's ``grants`` block (``mm3.core.config.model.Grants``);
default is nothing granted (deny).

Implements:
    -: Module Contract 1.0 rev 9, §8 Capabilities and grants.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["GrantSet"]


@dataclass(frozen=True)
class GrantSet:
    """Granted scopes of an instance.

    Attributes:
        http: URL prefixes for ``ctx.http``.
        mqtt_publish: Topic globs for ``ctx.mqtt.publish``.
        mqtt_subscribe: Topic globs for ``ctx.mqtt.subscribe``.
    """

    http: tuple[str, ...] = ()
    mqtt_publish: tuple[str, ...] = ()
    mqtt_subscribe: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, grants: Any) -> GrantSet:
        """Build from a ``Grants`` model or the same shape as plain data.

        Args:
            grants: ``Grants`` model, a mapping like
                ``{"http": [...], "mqtt": {"publish": [...], "subscribe": [...]}}``
                or ``None``.

        Returns:
            The grant set.
        """
        if grants is None:
            return cls()
        if not isinstance(grants, Mapping):
            grants = grants.model_dump()
        mqtt = grants.get("mqtt") or {}
        return cls(
            http=tuple(grants.get("http") or ()),
            mqtt_publish=tuple(mqtt.get("publish") or ()),
            mqtt_subscribe=tuple(mqtt.get("subscribe") or ()),
        )
