"""Static checks of a module's ``config_model``.

These are the checks ``mm3 module check`` (milestone 4) runs for the
configuration rules of the Module Contract; they need no running engine.

Implements:
    MOD-45, MOD-46, MOD-47: Module Contract 1.0 rev 6, §6.2 Configuration.
"""

from __future__ import annotations

import re
import types
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, SecretBytes, SecretStr

from mm3.config import ModuleConfig
from mm3.core.publicconfig import is_secret_field

__all__ = ["ModelProblem", "check_config_model"]

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_NAME = re.compile(r"(?:^|_)(?:key|token|password|passwd|secret|apikey)(?:$|_)", re.I)


@dataclass(frozen=True)
class ModelProblem:
    """One finding about a configuration model.

    Attributes:
        rule: The Contract rule, ``MOD-45`` to ``MOD-47``.
        severity: ``error`` breaks a MUST; ``warning`` needs review.
        field: Dotted field path, ``""`` for the model itself.
        message: English description.
    """

    rule: str
    severity: Literal["error", "warning"]
    field: str
    message: str


def _types(annotation: Any) -> list[Any]:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return [t for arg in get_args(annotation) for t in _types(arg)]
    if origin is not None:
        return [annotation, *[t for arg in get_args(annotation) for t in _types(arg)]]
    return [annotation]


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    return [t for t in _types(annotation) if isinstance(t, type) and issubclass(t, BaseModel)]


def check_config_model(model: type[BaseModel], *, _prefix: str = "") -> list[ModelProblem]:
    """Check a ``config_model`` against MOD-45 to MOD-47.

    MOD-45: the model subclasses ``ModuleConfig`` and forbids unknown keys,
    and every field has a type. MOD-46: every field has a ``title`` that is
    an UPPER_SNAKE catalog key, and its ``description``, if any, is one too.
    MOD-47: ``SecretStr`` fields are marked ``secret: True`` and only they
    are; a field whose name looks like a secret but is not one is a warning.
    Nested models are checked the same way.

    Args:
        model: The class to check.

    Returns:
        The problems found, empty when the model complies.

    Implements:
        MOD-45, MOD-46, MOD-47: Module Contract 1.0 rev 6, §6.2 Configuration.
    """
    out: list[ModelProblem] = []
    if not _prefix and not issubclass(model, ModuleConfig):
        out.append(ModelProblem("MOD-45", "error", "", "config_model must subclass ModuleConfig"))
    if model.model_config.get("extra") != "forbid":
        out.append(
            ModelProblem("MOD-45", "error", _prefix, 'unknown keys must be errors (extra="forbid")')
        )
    for name, info in model.model_fields.items():
        path = f"{_prefix}.{name}" if _prefix else name
        if info.annotation in (None, Any) or info.annotation is object:
            out.append(ModelProblem("MOD-45", "error", path, "field has no type"))
        if not (isinstance(info.title, str) and _KEY.match(info.title)):
            out.append(ModelProblem("MOD-46", "error", path, "title must be a catalog key"))
        if info.description is not None and not _KEY.match(info.description):
            out.append(ModelProblem("MOD-46", "error", path, "description must be a catalog key"))
        found = _types(info.annotation)
        secret_type = any(t in (SecretStr, SecretBytes) for t in found)
        marked = is_secret_field(info)
        if secret_type and not marked:
            out.append(ModelProblem("MOD-47", "error", path, "SecretStr field needs secret: True"))
        if marked and not secret_type:
            out.append(ModelProblem("MOD-47", "error", path, "secret field must be a SecretStr"))
        if not secret_type and not marked and _SECRET_NAME.search(name):
            out.append(
                ModelProblem("MOD-47", "warning", path, "name looks like a secret; use SecretStr")
            )
        for sub in _nested_models(info.annotation):
            if sub is not model:
                out.extend(check_config_model(sub, _prefix=path))
    return out
