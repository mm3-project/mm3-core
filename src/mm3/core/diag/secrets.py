"""Secret values of the running configuration, and masking them by value.

The secret values are the strings of fields marked ``secret`` (MOD-47) or
typed ``SecretStr`` in every instance's configuration, the values the
``!env`` references resolve to, and the kiosk agent key. A string equal to
one of them is replaced by ``!masked``, the marker of CORE-303, wherever it
appears; strings shorter than 4 characters are never matched.

Implements:
    CORE-116: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-319: Core spec 0.24, §10.5 Diagnostics and debugging.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, SecretBytes, SecretStr

from mm3.core.config.envfile import read_env_file
from mm3.core.publicconfig import is_secret_field

if TYPE_CHECKING:
    from pathlib import Path

    from mm3.core.config.validate import ConfigResult

__all__ = ["MASK", "MIN_SECRET_LENGTH", "Masker", "config_secrets"]

MASK = "!masked"
"""What a secret value becomes (CORE-303, CORE-319)."""

MIN_SECRET_LENGTH = 4
"""Shorter strings are never treated as secrets (CORE-319)."""


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, SecretStr):
        yield value.get_secret_value()
    elif isinstance(value, SecretBytes):
        yield value.get_secret_value().decode("utf-8", errors="replace")
    elif isinstance(value, str):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _strings(getattr(value, name))
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple | set | frozenset):
        for v in value:
            yield from _strings(v)


def _model_secrets(model: BaseModel) -> Iterable[str]:
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        if is_secret_field(info) or isinstance(value, SecretStr | SecretBytes):
            yield from _strings(value)
        elif isinstance(value, BaseModel):
            yield from _model_secrets(value)
        elif isinstance(value, Mapping):
            for v in value.values():
                if isinstance(v, BaseModel):
                    yield from _model_secrets(v)
        elif isinstance(value, list | tuple):
            for v in value:
                if isinstance(v, BaseModel):
                    yield from _model_secrets(v)


def config_secrets(
    result: ConfigResult,
    env_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    extra: Iterable[str] = (),
) -> frozenset[str]:
    """Collect the secret values of a configuration (CORE-319).

    Args:
        result: The configuration.
        env_file: The env file; its values for the referenced names count.
        environ: Process environment; ``os.environ`` by default.
        extra: Further secrets, such as the kiosk agent key.

    Returns:
        Every secret string of at least :data:`MIN_SECRET_LENGTH` characters.

    Implements:
        CORE-319: Core spec 0.24, §10.5 Diagnostics and debugging.
        CORE-338: Core spec 0.34, §9.3 MQTT bridge.
    """
    found: set[str] = set(extra)
    if result.config is not None:
        found.update(_model_secrets(result.config.engine))
    for inst in result.instances:
        if inst.config is not None:
            found.update(_model_secrets(inst.config))
    env = dict(environ if environ is not None else os.environ)
    if env_file is not None:
        env.update(read_env_file(env_file).values)
    for ref in result.loaded.env_refs.values():
        value = env.get(ref.name, ref.default)
        if value is not None:
            found.add(value)
    return frozenset(s for s in found if len(s) >= MIN_SECRET_LENGTH)


class Masker:
    """Replace secret values by ``!masked`` in JSON-like data (CORE-319).

    Args:
        secrets: The secret values.

    Implements:
        CORE-319: Core spec 0.24, §10.5 Diagnostics and debugging.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = frozenset(s for s in secrets if len(s) >= MIN_SECRET_LENGTH)

    def mask(self, value: Any) -> Any:
        """Return a copy of ``value`` with every secret string masked.

        Args:
            value: Dicts, lists and scalars.

        Returns:
            The masked copy; ``value`` itself is not changed.
        """
        if not self.secrets:
            return value
        if isinstance(value, str):
            return MASK if value in self.secrets else value
        if isinstance(value, Mapping):
            return {k: self.mask(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.mask(v) for v in value]
        return value

    def mask_text(self, text: str) -> str:
        """Replace every secret inside a text (log lines, journal output).

        Args:
            text: The text.

        Returns:
            The text with each secret replaced by ``!masked``.
        """
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, MASK)
        return text
