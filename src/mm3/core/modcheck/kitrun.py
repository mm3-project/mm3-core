"""Run the test kit on an installed module (inside the check's temporary venv).

``python -m mm3.core.modcheck.kitrun <dist> <options.json> <out.json>``
loads the module through its ``mm3.modules`` entry point, builds a
configuration from its ``config_model``, runs
:func:`mm3.testing.run_kit_checks` and writes the results as JSON.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 10, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

__all__ = ["main", "sample_config"]

GROUP = "mm3.modules"
OPEN_GRANTS: dict[str, Any] = {
    "http.outbound": {"http": ["http://", "https://"]},
    "mqtt.publish": {"mqtt": {"publish": ["*"]}},
    "mqtt.subscribe": {"mqtt": {"subscribe": ["*"]}},
}


def sample_config(module: Any) -> dict[str, Any] | None:
    """Build a configuration that validates against the module's ``config_model``.

    Args:
        module: The module class.

    Returns:
        A configuration with the required fields filled, or ``None`` when no
        valid sample could be built.
    """
    from mm3.testing import sample_payload

    model = module.config_model
    full = sample_payload(model)
    if full is None:
        return None
    required = {
        (info.alias or name) for name, info in model.model_fields.items() if info.is_required()
    }
    lean = {k: v for k, v in full.items() if k in required}
    try:
        model.model_validate(lean)
    except Exception:
        return full
    return lean


def _open_grants(module: Any) -> dict[str, Any]:
    grants: dict[str, Any] = {}
    for cap in sorted(module.capabilities):
        for key, value in OPEN_GRANTS.get(cap, {}).items():
            if isinstance(value, dict):
                grants.setdefault(key, {}).update(value)
            else:
                grants[key] = value
    return grants


async def _run(module: Any, options: dict[str, Any]) -> dict[str, Any]:
    from mm3.testing import ModuleHarness, run_kit_checks

    config = options.get("config")
    if config is None:
        config = sample_config(module)
    if config is None:
        return {
            "error": "cannot build a configuration from config_model; "
            "add [tool.mm3.check] config = {...} to pyproject.toml"
        }
    grants = options.get("grants")
    if grants is None:
        grants = _open_grants(module)
    async with ModuleHarness(module, config=config, grants=grants) as h:
        results = await run_kit_checks(h)
    notes = [f for r in results for f in r.findings if f.startswith("note: ")]
    return {
        "checks": [
            {
                "id": r.id,
                "level": r.level,
                "status": r.status,
                "findings": [f for f in r.findings if not f.startswith("note: ")],
            }
            for r in results
        ],
        "notes": [n.removeprefix("note: ") for n in notes],
    }


def main(argv: list[str]) -> int:
    """Run the kit; the exit code is 0 whenever a result file was written.

    Args:
        argv: ``[dist, options.json, out.json]``.

    Returns:
        0 on success, 2 when the kit itself could not run.
    """
    dist, options_file, out_file = argv
    options = json.loads(Path(options_file).read_text(encoding="utf-8"))
    out: dict[str, Any]
    try:
        eps = [ep for ep in metadata.distribution(dist).entry_points if ep.group == GROUP]
        if len(eps) != 1:
            out = {"load_error": f"expected one {GROUP} entry point, found {len(eps)}"}
        else:
            try:
                module = eps[0].load()
            except Exception:
                out = {"load_error": traceback.format_exc(limit=5).strip()}
            else:
                out = asyncio.run(_run(module, options))
    except Exception:
        Path(out_file).write_text(
            json.dumps({"crash": traceback.format_exc(limit=8).strip()}), encoding="utf-8"
        )
        return 2
    Path(out_file).write_text(json.dumps(out), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
