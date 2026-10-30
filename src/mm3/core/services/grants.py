"""The scopes granted to one instance, as the engine services read them.

Built from the instance's ``grants`` block (``mm3.core.config.model.Grants``);
default is nothing granted (deny).

Implements:
    -: Module Contract 1.0 rev 12, §8 Capabilities and grants.
    CORE-284: Core spec 0.19, §3.4 HTTP transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["GrantSet"]


@dataclass(frozen=True)
class GrantSet:
    """Granted scopes of an instance.

    Attributes:
        http: URL prefixes for ``ctx.http`` (plain and ``{prefix, ca}`` entries).
        http_ca: ``(prefix, absolute PEM path)`` of the entries with a ``ca``.
        mqtt_publish: Topic globs for ``ctx.mqtt.publish``.
        mqtt_subscribe: Topic globs for ``ctx.mqtt.subscribe``.
    """

    http: tuple[str, ...] = ()
    http_ca: tuple[tuple[str, str], ...] = ()
    mqtt_publish: tuple[str, ...] = ()
    mqtt_subscribe: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, grants: Any, base_dir: Path | None = None) -> GrantSet:
        """Build from a ``Grants`` model or the same shape as plain data.

        Args:
            grants: ``Grants`` model, a mapping like
                ``{"http": [...], "mqtt": {"publish": [...], "subscribe": [...]}}``
                or ``None``.
            base_dir: Directory of the main configuration file, against which
                a relative ``ca`` path is resolved (CORE-284).

        Returns:
            The grant set.
        """
        if grants is None:
            return cls()
        if not isinstance(grants, Mapping):
            grants = grants.model_dump()
        mqtt = grants.get("mqtt") or {}
        prefixes: list[str] = []
        ca: list[tuple[str, str]] = []
        for entry in grants.get("http") or ():
            if isinstance(entry, str):
                prefixes.append(entry)
                continue
            prefix = str(entry["prefix"])
            prefixes.append(prefix)
            if entry.get("ca"):
                path = Path(str(entry["ca"]))
                ca.append((prefix, str(path if base_dir is None else base_dir / path)))
        return cls(
            http=tuple(prefixes),
            http_ca=tuple(ca),
            mqtt_publish=tuple(mqtt.get("publish") or ()),
            mqtt_subscribe=tuple(mqtt.get("subscribe") or ()),
        )
