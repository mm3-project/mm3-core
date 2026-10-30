"""Run ``mm3 module check``: build, static stage, kit stage, report.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    CORE-279: Core spec 0.18, §10.2 Packaging and deployment.
    MOD-95: Module Contract 1.0 rev 10, §12.1 mm3 module check.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from mm3.__about__ import MODULE_API_VERSION, __version__
from mm3.core.extras import MissingExtraError, missing_testing_modules
from mm3.core.modcheck.build import build_wheel, make_venv
from mm3.core.modcheck.catalog import CATALOG
from mm3.core.modcheck.project import ModuleProject, ToolError, load_project
from mm3.core.modcheck.report import (
    Check,
    Finding,
    Report,
    checks_from_findings,
    review_checks,
)
from mm3.core.modcheck.static import run_static

__all__ = ["REPORT_PATH", "Stage", "run_check"]

Stage = Literal["static", "kit", "harness"]
REPORT_PATH = Path(".mm3") / "check-report.json"
KIT_TIMEOUT_S = 600


def run_check(path: Path, only: Stage | None = None) -> Report:
    """Check a module's source tree.

    Args:
        path: The module's project directory.
        only: Run just this stage.

    Returns:
        The report. The browser harness is not part of this version, so a run
        without ``only`` has the stages ``static`` and ``kit``.

    Raises:
        ToolError: The check cannot run (exit code 2): no ``uv``, no
            ``pyproject.toml``, the testing extra is missing, the engine
            cannot be installed in the temporary venv, or ``only`` names a
            stage this version does not have.
    """
    if only == "harness":
        raise ToolError("the browser harness is not available in this version of mm3 module check")
    stages: list[Stage] = [only] if only else ["static", "kit"]
    if "kit" in stages:
        missing = missing_testing_modules()
        if missing:
            raise ToolError(str(MissingExtraError(missing)))
    mod = load_project(path)
    report = Report(__version__, mod.dist, mod.version or "", MODULE_API_VERSION)
    with tempfile.TemporaryDirectory(prefix="mm3-check-") as tmp_name:
        tmp = Path(tmp_name)
        build_failure = _build(mod, tmp)
        report.version = mod.version or next(iter(mod.metadata.get("Version", [])), "")
        facts = None
        if "static" in stages:
            checks, facts = run_static(mod)
            if build_failure:
                _attach(checks, "MOD-05", Finding(build_failure))
            report.checks += checks
            report.stages.append("static")
        if facts is not None and facts.api_version:
            report.api_version = facts.api_version
        if "kit" in stages:
            report.checks += _kit(mod, tmp, build_failure, report)
            report.stages.append("kit")
    report.checks += review_checks()
    report.checks.sort(key=lambda c: (list(CATALOG).index(c.id), c.category != "static"))
    return report


def _build(mod: ModuleProject, tmp: Path) -> str | None:
    """Build and attach the wheel; return the failure text, or ``None`` on success."""
    out = tmp / "dist"
    out.mkdir()
    built = build_wheel(mod.root, out)
    if built.wheel is None:
        tail = "\n".join(built.log.splitlines()[-12:])
        return f"the wheel cannot be built with uv build:\n{tail}"
    extract = tmp / "wheel"
    extract.mkdir()
    mod.attach_wheel(built.wheel, extract)
    return None


def _attach(checks: list[Check], mod_id: str, finding: Finding) -> None:
    for c in checks:
        if c.id == mod_id:
            c.findings.append(finding)
            c.settle()


def _kit(mod: ModuleProject, tmp: Path, build_failure: str | None, report: Report) -> list[Check]:
    ids = [i for i, row in CATALOG.items() if "kit" in row.categories]
    if build_failure is not None or mod.wheel is None:
        return _all_failed(ids, "not run: the wheel could not be built")
    name = (mod.metadata.get("Name") or [mod.dist])[0]
    python = make_venv(tmp / "venv", mod.wheel, mod.metadata.get("Requires-Dist", []))
    options = tmp / "options.json"
    options.write_text(json.dumps(mod.check_options), encoding="utf-8")
    out = tmp / "kit.json"
    try:
        done = subprocess.run(  # noqa: S603 - the venv's own interpreter, fixed arguments
            [str(python), "-I", "-m", "mm3.core.modcheck.kitrun", name, str(options), str(out)],
            capture_output=True,
            text=True,
            check=False,
            timeout=KIT_TIMEOUT_S,
            cwd=tmp,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"the test kit did not run: {exc}") from exc
    if not out.is_file():
        raise ToolError(f"the test kit stopped without a result:\n{done.stderr.strip()[-2000:]}")
    data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    if "crash" in data:
        raise ToolError(f"the test kit crashed:\n{data['crash']}")
    if "error" in data:
        raise ToolError(str(data["error"]))
    if "load_error" in data:
        return _all_failed(ids, f"not run: the module cannot be loaded\n{data['load_error']}")
    found: dict[str, list[Finding]] = {}
    for item in data["checks"]:
        found[item["id"]] = [Finding(str(f)) for f in item["findings"]]
    report.notes += [str(n) for n in data.get("notes", [])]
    return checks_from_findings("kit", found, ids)


def _all_failed(ids: list[str], message: str) -> list[Check]:
    found = {i: [Finding(message)] for i in ids}
    return checks_from_findings("kit", found, ids)
