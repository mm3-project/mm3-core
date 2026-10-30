"""Build the module's wheel with ``uv build`` and install it in a temporary venv.

The venv holds the wheel, the module's other dependencies and the running
``mm3-core`` with its testing extra, so the test kit runs against the engine
that runs the check (core CORE-281).

Implements:
    CORE-280, CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement

from mm3.__about__ import __version__
from mm3.core.modcheck.project import ToolError

__all__ = ["BuildResult", "build_wheel", "core_requirements", "make_venv", "venv_python"]

TIMEOUT_S = 900


@dataclass(frozen=True)
class BuildResult:
    """The outcome of ``uv build``.

    Attributes:
        wheel: The wheel, ``None`` when the build failed.
        log: What ``uv build`` printed.
    """

    wheel: Path | None
    log: str


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise ToolError("uv is not on PATH; install it from https://docs.astral.sh/uv/")
    return uv


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed argument list, no shell
            cmd, capture_output=True, text=True, check=False, timeout=TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"{what} did not run: {exc}") from exc


def build_wheel(root: Path, out_dir: Path) -> BuildResult:
    """Build the wheel from the source directory.

    Args:
        root: The module's project directory.
        out_dir: Directory for the wheel.

    Returns:
        The wheel, or ``None`` and the build log when the build failed.

    Raises:
        ToolError: ``uv`` is missing or cannot run.
    """
    done = _run([_uv(), "build", "--wheel", "--out-dir", str(out_dir), str(root)], "uv build")
    log = (done.stdout + done.stderr).strip()
    wheels = sorted(out_dir.glob("*.whl"))
    if done.returncode != 0 or not wheels:
        return BuildResult(None, log)
    return BuildResult(wheels[-1], log)


def core_requirements() -> list[str]:
    """Return the ``uv pip install`` arguments that install the running ``mm3-core``.

    A checkout installed from a local directory (a development install) is
    installed again from there, with ``mm3-build`` next to it; a published
    release is installed by version.

    Returns:
        The install arguments, with the ``testing`` extra.
    """
    try:
        dist = metadata.distribution("mm3-core")
    except metadata.PackageNotFoundError:
        return [f"mm3-core[testing]=={__version__}"]
    raw = dist.read_text("direct_url.json")
    if raw:
        info = json.loads(raw)
        url = urlparse(str(info.get("url", "")))
        if url.scheme == "file":
            path = Path(unquote(url.path))
            args = [f"mm3-core[testing] @ {path.as_uri()}"]
            if (path / "mm3-build" / "pyproject.toml").is_file():
                args.append(f"mm3-build @ {(path / 'mm3-build').as_uri()}")
            return args
    return [f"mm3-core[testing]=={dist.version}"]


def venv_python(venv: Path) -> Path:
    """Return the interpreter of a venv."""
    return venv / "bin" / "python"


def make_venv(venv: Path, wheel: Path, requires_dist: list[str]) -> Path:
    """Create a venv with the running ``mm3-core``, the module's dependencies and its wheel.

    Args:
        venv: Directory for the venv.
        wheel: The module's wheel.
        requires_dist: The wheel's ``Requires-Dist`` lines.

    Returns:
        The venv's interpreter.

    Raises:
        ToolError: The venv cannot be created or a package cannot be installed.
    """
    uv = _uv()
    done = _run([uv, "venv", "--python", sys.executable, str(venv)], "uv venv")
    if done.returncode != 0:
        raise ToolError(f"cannot create the temporary venv:\n{done.stderr.strip()}")
    python = venv_python(venv)
    others: list[str] = []
    for line in requires_dist:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if req.name.lower().replace("_", "-") != "mm3-core" and (
            req.marker is None or req.marker.evaluate({"extra": ""})
        ):
            others.append(line)
    # The module's own range on mm3-core is checked statically (MOD-03); the running
    # engine is what the kit must test, whatever its release number.
    packages = [*core_requirements(), *others]
    done = _run([uv, "pip", "install", "--python", str(python), *packages], "uv pip install")
    if done.returncode != 0:
        raise ToolError(f"cannot install the engine and dependencies:\n{done.stderr.strip()}")
    done = _run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)], "uv pip")
    if done.returncode != 0:
        raise ToolError(f"cannot install the module wheel:\n{done.stderr.strip()}")
    return python
