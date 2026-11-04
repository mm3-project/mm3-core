"""One JSON Schema for the whole configuration, for the admin's three views.

The schema is generated from the engine model and every installed module's
``config_model`` (core spec 6.4). Each ``modules`` item is one of the
installed modules, its ``config`` that module's schema. Every property also
accepts an ``!env`` reference and every object an ``!include``, in the JSON
form of CORE-320, and region presets are an enum. A module's
``json_schema_extra={"ui": {...}}`` hints pass through unchanged (CORE-37).

Implements:
    CORE-37: Core spec 0.22, §6.4 Web configuration editor.
    CORE-320: Core spec 0.25, §6.4 Web configuration editor.
    -: Core spec 0.22, §6.4 Web configuration editor.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from mm3.core.config.model import MirrorConfig
from mm3.core.registry import Registry

__all__ = ["ENV_REF", "INCLUDE_REF", "config_schema"]

ENV_REF = {"$ref": "#/$defs/mm3_env"}
INCLUDE_REF = {"$ref": "#/$defs/mm3_include"}

_ENV = {
    "title": "!env",
    "description": "An !env reference: the value of an environment variable (CORE-320).",
    "type": "object",
    "properties": {
        "!env": {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
            ]
        }
    },
    "required": ["!env"],
    "additionalProperties": False,
}
_INCLUDE = {
    "title": "!include",
    "description": "An !include: this node is the content of another file (CORE-320).",
    "type": "object",
    "properties": {"!include": {"type": "string", "minLength": 1}},
    "required": ["!include"],
    "additionalProperties": False,
}


def _allow_tags(node: Any) -> None:
    """Let every property take ``!env`` and every object-valued property ``!include``."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for name, prop in list(props.items()):
                _allow_tags(prop)
                props[name] = {"anyOf": [prop, ENV_REF, INCLUDE_REF]}
        for key, value in node.items():
            if key != "properties":
                _allow_tags(value)
    elif isinstance(node, list):
        for item in node:
            _allow_tags(item)


def config_schema(registry: Registry) -> dict[str, Any]:
    """Build the JSON Schema of ``GET /api/v1/config/schema``.

    Args:
        registry: Installed modules.

    Returns:
        A JSON Schema (draft 2020-12) with ``$defs``; ``x-mm3-modules`` maps each
        installed module to the definition of its ``config``.

    Implements:
        CORE-37: Core spec 0.22, §6.4 Web configuration editor.
        CORE-320: Core spec 0.25, §6.4 Web configuration editor.
    """
    models: list[tuple[type[BaseModel], Literal["validation", "serialization"]]] = [
        (MirrorConfig, "validation")
    ]
    modules: dict[str, type[BaseModel]] = {}
    for entry in sorted(registry.entries(), key=lambda e: e.name):
        if entry.cls is not None:
            modules[entry.name] = entry.cls.config_model
            if (entry.cls.config_model, "validation") not in models:
                models.append((entry.cls.config_model, "validation"))
    keys, schema = models_json_schema(models, ref_template="#/$defs/{model}")
    defs: dict[str, Any] = schema.setdefault("$defs", {})
    instance = defs["InstanceConfig"]
    variants = []
    refs: dict[str, str] = {}
    for name, model in modules.items():
        ref = keys[(model, "validation")]["$ref"]
        refs[name] = ref
        item = copy.deepcopy(instance)
        item["title"] = name
        item["properties"]["module"] = {"const": name}
        item["properties"]["config"] = ref
        variants.append(item)
    root = copy.deepcopy(defs["MirrorConfig"])
    modules_prop = root["properties"]["modules"]
    modules_prop["items"] = {"oneOf": variants} if variants else {"$ref": "#/$defs/InstanceConfig"}
    del defs["MirrorConfig"]
    _allow_tags(root)
    _allow_tags(defs)
    defs["mm3_env"] = _ENV
    defs["mm3_include"] = _INCLUDE
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MM3 configuration",
        **root,
        "$defs": defs,
        "x-mm3-modules": refs,
    }
