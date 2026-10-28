"""The public part of a module configuration: what may leave the engine.

Fields marked ``secret`` (``Field(json_schema_extra={"secret": True})``,
MOD-47) and ``SecretStr`` / ``SecretBytes`` values are removed, at any
depth, before a configuration is sent to browsers (``this.config``) or
returned by the remote API. With a locale, every ``LocalizedText`` is
replaced by its text for that locale (CORE-101).

Implements:
    CORE-13: Core spec 0.7, §4.4 Contract rules.
    CORE-101: Core spec 0.8, §11.1 Engine additions.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, SecretBytes, SecretStr
from pydantic.fields import FieldInfo

__all__ = ["is_secret_field", "public_config"]


def is_secret_field(info: FieldInfo) -> bool:
    """Tell whether a model field is marked ``secret``.

    Args:
        info: The field.

    Returns:
        True when ``json_schema_extra`` sets ``secret`` to true.

    Implements:
        CORE-13: Core spec 0.7, §4.4 Contract rules.
    """
    extra = info.json_schema_extra
    return isinstance(extra, dict) and extra.get("secret") is True


def _exclude(value: Any) -> Any:
    """Build a ``model_dump`` exclude spec for the secrets inside ``value``."""
    if isinstance(value, SecretStr | SecretBytes):
        return True
    if isinstance(value, BaseModel):
        spec: dict[Any, Any] = {}
        for name, info in type(value).model_fields.items():
            sub = True if is_secret_field(info) else _exclude(getattr(value, name))
            if sub:
                spec[name] = sub
        return spec or None
    if isinstance(value, dict):
        items: dict[Any, Any] = {k: s for k, v in value.items() if (s := _exclude(v))}
        return items or None
    if isinstance(value, list | tuple):
        items = {i: s for i, v in enumerate(value) if (s := _exclude(v))}
        return items or None
    return None


def public_config(config: BaseModel, locale: str | Sequence[str] | None = None) -> dict[str, Any]:
    """Return a configuration without its secrets, as JSON-ready data.

    Args:
        config: A validated configuration model.
        locale: The element's locale (or chain); when given, each
            ``LocalizedText`` becomes its resolved string, as browsers receive it.

    Returns:
        The configuration dumped in JSON mode, without secret fields or
        secret-typed values, nested models and containers included.

    Implements:
        CORE-13: Core spec 0.7, §4.4 Contract rules.
        CORE-101: Core spec 0.8, §11.1 Engine additions.
    """
    context = None if locale is None else {"locale": locale}
    return config.model_dump(mode="json", exclude=_exclude(config), context=context)
