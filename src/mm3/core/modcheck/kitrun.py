"""Run the test kit or the browser harness on an installed module (inside the check's venv).

``python -m mm3.core.modcheck.kitrun <dist> <options.json> <out.json> [kit|harness]``
loads the module through its ``mm3.modules`` entry point, builds a
configuration from its ``config_model`` and writes the results as JSON:
:func:`mm3.testing.run_kit_checks` for ``kit``, and for ``harness`` the
states the module sends to its front-end, fed to
:func:`mm3.testing.run_browser_checks`.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 11, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import asyncio
import contextlib
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


def _configuration(module: Any, options: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    config = options.get("config")
    if config is None:
        config = sample_config(module)
    grants = options.get("grants")
    if grants is None:
        grants = _open_grants(module)
    return config, grants


NO_CONFIG = (
    "cannot build a configuration from config_model; "
    "add [tool.mm3.check] config = {...} to pyproject.toml"
)


def _public(module: Any, config: dict[str, Any]) -> dict[str, Any]:
    """The configuration a front-end sees: secrets removed (core spec 6.4)."""
    secret = {
        (info.alias or name)
        for name, info in module.config_model.model_fields.items()
        if "SecretStr" in str(info.annotation)
    }
    return {k: v for k, v in config.items() if k not in secret}


def _serialise(results: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "level": r.level,
            "status": r.status,
            "findings": [f for f in r.findings if not f.startswith("note: ")],
        }
        for r in results
    ]


async def _run_harness(module: Any, options: dict[str, Any]) -> dict[str, Any]:
    from mm3.testing import (
        BROWSER_CHECKS,
        BrowserHarness,
        BrowserUnavailableError,
        ModuleHarness,
        run_browser_checks,
    )

    if module.element is None:
        return {"checks": [{"id": i, "level": "MUST", "status": "pass", "findings": []}
                           for i in BROWSER_CHECKS]}  # fmt: skip
    config, grants = _configuration(module, options)
    if config is None:
        return {"error": NO_CONFIG}
    states: list[dict[str, Any]] = []
    async with ModuleHarness(module, config=config, grants=grants) as h:
        with contextlib.suppress(Exception):  # the states so far are enough
            await h.advance(minutes=1)
        for state in [*h.state_history, dict(h.state)]:
            if state and state not in states:
                states.append(state)
    try:
        async with BrowserHarness(module, config=_public(module, config)) as browser:
            results = await run_browser_checks(browser, states)
    except BrowserUnavailableError as exc:
        return {"error": str(exc)}
    return {"checks": _serialise(results), "notes": []}


async def _run(module: Any, options: dict[str, Any]) -> dict[str, Any]:
    from mm3.testing import ModuleHarness, run_kit_checks

    config, grants = _configuration(module, options)
    if config is None:
        return {"error": NO_CONFIG}
    async with ModuleHarness(module, config=config, grants=grants) as h:
        results = await run_kit_checks(h)
    notes = [f for r in results for f in r.findings if f.startswith("note: ")]
    return {"checks": _serialise(results), "notes": [n.removeprefix("note: ") for n in notes]}


def main(argv: list[str]) -> int:
    """Run the kit; the exit code is 0 whenever a result file was written.

    Args:
        argv: ``[dist, options.json, out.json]`` and optionally the stage, ``kit`` (default)
            or ``harness``.

    Returns:
        0 on success, 2 when the kit itself could not run.
    """
    dist, options_file, out_file = argv[:3]
    stage = argv[3] if len(argv) > 3 else "kit"
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
                out = asyncio.run(
                    _run_harness(module, options) if stage == "harness" else _run(module, options)
                )
    except Exception:
        Path(out_file).write_text(
            json.dumps({"crash": traceback.format_exc(limit=8).strip()}), encoding="utf-8"
        )
        return 2
    Path(out_file).write_text(json.dumps(out), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
