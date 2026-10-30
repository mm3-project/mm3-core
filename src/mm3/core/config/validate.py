"""Validation of a whole configuration against the engine and the installed modules.

``load_config`` is what startup, ``mm3 check-config`` and (from milestone 6)
the admin all call, so they report exactly the same records. Validation
collects every problem instead of stopping at the first.

Implements:
    CORE-02: Core spec 0.8, §3 Engine components.
    CORE-30: Core spec 0.8, §6 Configuration.
    CORE-32: Core spec 0.8, §6.2 Rules.
    CORE-202, CORE-203, CORE-204, CORE-206: Core spec 0.8, §6.2 Rules.
    CORE-262: Core spec 0.11, §6.10 Theme and custom CSS.
    MOD-49: Module Contract 1.0 rev 6, §6.2 Configuration.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from mm3.config import ModuleConfig, iter_localized
from mm3.core.config.assets import ConfigAssets, read_assets
from mm3.core.config.envfile import DEFAULT_ENV_FILE, EnvFile, read_env_file
from mm3.core.config.layout import LayoutResult, check_layout
from mm3.core.config.loader import LoadedConfig, load_yaml
from mm3.core.config.model import INSTANCE_ID_PATTERN, InstanceConfig, MirrorConfig
from mm3.core.config.records import (
    SourceMap,
    ValidationRecord,
    make_record,
    pointer,
    records_from_pydantic,
)
from mm3.core.registry import Registry
from mm3.core.services.grants import GrantSet
from mm3.core.supervisor import InstanceSpec

__all__ = ["DEFAULT_CONFIG", "ConfigResult", "ResolvedInstance", "load_config", "validate"]

DEFAULT_CONFIG = Path("/etc/mm3/config.yaml")
_ID_RE = re.compile(INSTANCE_ID_PATTERN)


@dataclass(frozen=True)
class ResolvedInstance:
    """One module instance after validation.

    Attributes:
        index: Position in the ``modules`` list.
        id: Instance id, explicit or defaulted to the module name.
        module: Module name.
        spec: The instance keys, validated.
        config: The module configuration; ``None`` when it was not
            validated (module missing, unavailable, or disabled and not installed).
        unavailable: Why the module cannot be used (CORE-203); the instance
            is then DISABLED with this reason.
    """

    index: int
    id: str
    module: str
    spec: InstanceConfig
    config: ModuleConfig | None
    unavailable: str | None = None

    @property
    def enabled(self) -> bool:
        """True unless the instance has ``disabled: true``."""
        return not self.spec.disabled


@dataclass
class ConfigResult:
    """Outcome of loading and validating a configuration.

    Attributes:
        path: Main file.
        env: The env file that was read.
        loaded: Loader output (data, source map, files).
        records: Every problem, errors and warnings.
        config: The validated model; ``None`` when there are errors.
        instances: Instances with their validated configuration.
        layout: Viewport, cell size and placements per page (core spec 6.5,
            6.7, 6.9); ``None`` when there are errors.
        assets: custom.css and the widget templates (CORE-216, CORE-262).
    """

    path: Path
    env: EnvFile
    loaded: LoadedConfig
    records: list[ValidationRecord] = field(default_factory=list)
    config: MirrorConfig | None = None
    instances: list[ResolvedInstance] = field(default_factory=list)
    layout: LayoutResult | None = None
    assets: ConfigAssets = field(default_factory=ConfigAssets)

    @property
    def errors(self) -> list[ValidationRecord]:
        """Records with severity ``error``."""
        return [r for r in self.records if r.severity == "error"]

    @property
    def warnings(self) -> list[ValidationRecord]:
        """Records with severity ``warning``."""
        return [r for r in self.records if r.severity == "warning"]

    @property
    def ok(self) -> bool:
        """True when there is no error; warnings never block loading."""
        return not self.errors

    def instance_specs(self) -> list[InstanceSpec]:
        """Return what the supervisor runs: every enabled instance, in file order.

        Instances of unavailable modules are included; the supervisor
        disables them with the registry's reason (CORE-203).

        Returns:
            The specs; empty when the configuration has errors.
        """
        if not self.ok:
            return []
        return [
            InstanceSpec(
                instance_id=i.id,
                module=i.module,
                config=i.spec.config,
                locale=i.spec.locale,
                grants=GrantSet.from_config(i.spec.grants),
            )
            for i in self.instances
            if i.enabled
        ]


def _skipper(reported: set[str]) -> Callable[[str], bool]:
    def skip(ptr: str) -> bool:
        return any(ptr == r or ptr.startswith(r + "/") for r in reported)

    return skip


def _localized_warnings(
    value: BaseModel, prefix: tuple[str | int, ...], sources: SourceMap
) -> list[ValidationRecord]:
    return [
        make_record("CORE_CFG_LOCALIZED_NO_EN", pointer((*prefix, *path)), {}, sources=sources)
        for path, text in iter_localized(value)
        if text.missing_en
    ]


def _validate_module_config(
    cls_model: type[ModuleConfig],
    raw: Any,
    index: int,
    sources: SourceMap,
    skip: Callable[[str], bool],
) -> tuple[ModuleConfig | None, list[ValidationRecord]]:
    prefix: tuple[str | int, ...] = ("modules", index, "config")
    data = {} if raw is None else raw
    try:
        model = cls_model.model_validate(data)
    except ValidationError as exc:
        return None, records_from_pydantic(exc, data, prefix=prefix, sources=sources, skip=skip)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        rec = make_record("CORE_CFG_INVALID", pointer(prefix), {"reason": reason}, sources=sources)
        return None, [rec]
    return model, _localized_warnings(model, prefix, sources)


def validate(
    loaded: LoadedConfig, registry: Registry
) -> tuple[MirrorConfig | None, list[ResolvedInstance], list[ValidationRecord]]:
    """Validate loaded data against the engine model and the installed modules.

    Args:
        loaded: Output of the loader.
        registry: Installed modules.

    Returns:
        ``(config, instances, records)``; ``config`` is ``None`` when the
        file breaks the engine model.

    Implements:
        CORE-202, CORE-203, CORE-204, CORE-206: Core spec 0.8, §6.2 Rules.
        MOD-49: Module Contract 1.0 rev 6, §6.2 Configuration.
    """
    data = loaded.data
    sources = loaded.sources
    skip = _skipper(loaded.reported)
    records: list[ValidationRecord] = []
    config: MirrorConfig | None = None
    try:
        config = MirrorConfig.model_validate({} if data is None else data)
    except ValidationError as exc:
        records.extend(records_from_pydantic(exc, data, sources=sources, skip=skip))
    if config is not None:
        records.extend(_localized_warnings(config, (), sources))

    items = data.get("modules") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        return config, [], records

    counts = Counter(
        it["module"]
        for it in items
        if isinstance(it, Mapping) and isinstance(it.get("module"), str)
    )
    seen: dict[str, int] = {}
    instances: list[ResolvedInstance] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping) or not isinstance(item.get("module"), str):
            continue
        module: str = item["module"]
        base = ("modules", i)
        explicit = item.get("id")
        if isinstance(explicit, str):
            inst_id = explicit
        elif explicit is None and counts[module] > 1:
            records.append(
                make_record(
                    "CORE_CFG_ID_REQUIRED", pointer(base), {"module": module}, sources=sources
                )
            )
            continue
        elif explicit is None and not _ID_RE.match(module):
            records.append(
                make_record(
                    "CORE_CFG_PATTERN",
                    pointer((*base, "id")),
                    {"pattern": INSTANCE_ID_PATTERN},
                    sources=sources,
                )
            )
            continue
        else:
            inst_id = module if explicit is None else str(explicit)
        if inst_id in seen:
            where = (*base, "id") if explicit is not None else base
            records.append(
                make_record(
                    "CORE_CFG_ID_DUPLICATE", pointer(where), {"id": inst_id}, sources=sources
                )
            )
        seen.setdefault(inst_id, i)

        disabled = item.get("disabled") is True
        entry = registry.get(module)
        model: ModuleConfig | None = None
        unavailable: str | None = None
        if entry is None:
            if not disabled:
                records.append(
                    make_record(
                        "CORE_CFG_MODULE_MISSING",
                        pointer((*base, "module")),
                        {"module": module},
                        sources=sources,
                    )
                )
        elif entry.cls is None:
            unavailable = entry.error or "unavailable"
            records.append(
                make_record(
                    "CORE_CFG_MODULE_UNAVAILABLE",
                    pointer((*base, "module")),
                    {"module": module, "reason": unavailable},
                    sources=sources,
                )
            )
        elif isinstance(item.get("config", {}), Mapping | None):
            model, recs = _validate_module_config(
                entry.cls.config_model, item.get("config"), i, sources, skip
            )
            records.extend(recs)
        if config is not None:
            instances.append(
                ResolvedInstance(i, inst_id, module, config.modules[i], model, unavailable)
            )
    return config, instances, records


def load_config(
    path: Path = DEFAULT_CONFIG,
    *,
    registry: Registry,
    env_file: Path = DEFAULT_ENV_FILE,
    environ: Mapping[str, str] | None = None,
    applied: tuple[int, int] | None = None,
) -> ConfigResult:
    """Load and validate a configuration, collecting every problem.

    Args:
        path: Main configuration file (``--config``).
        registry: Installed modules.
        env_file: Env file (``--env-file``).
        environ: Process environment; ``os.environ`` by default.
        applied: The mode the kiosk reported as applied; the layout uses it
            instead of the configured or assumed mode (CORE-145, CORE-215).

    Returns:
        The result. ``result.ok`` is False when there is any error; then
        ``config`` is ``None`` and nothing may be applied.

    Implements:
        CORE-02: Core spec 0.8, §3 Engine components.
        CORE-30: Core spec 0.8, §6 Configuration.
        CORE-232: Core spec 0.9, §6.9 Pages and visibility.
        CORE-262: Core spec 0.11, §6.10 Theme and custom CSS.
    """
    env = read_env_file(env_file)
    loaded = load_yaml(
        path, env_file=env.values, environ=os.environ if environ is None else environ
    )
    result = ConfigResult(path=path, env=env, loaded=loaded)
    result.records.extend(env.records)
    result.records.extend(loaded.records)
    result.assets, asset_records = read_assets(path.parent)
    result.records.extend(asset_records)
    if loaded.data is None and loaded.records:
        return result
    config, instances, records = validate(loaded, registry)
    result.records.extend(records)
    layout = None
    if config is not None:
        layout = check_layout(config, instances, registry, loaded.sources, applied=applied)
        result.records.extend(layout.records)
    if result.ok:
        result.config = config
        result.instances = instances
        result.layout = layout
    return result
