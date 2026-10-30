"""The test kit ships behind the extra mm3-core[testing] (CORE-279)."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from mm3.core.extras import TESTING_INSTALL, missing_testing_modules

ROOT = Path(__file__).resolve().parents[1]

BLOCK = """
import importlib.util as u
real = u.find_spec
u.find_spec = lambda name, *a: None if name == {name!r} else real(name, *a)
import mm3.testing
"""


def project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]  # type: ignore[no-any-return]


def test_core_279_extra_adds_pytest_asyncio_and_playwright() -> None:
    meta = project()
    extras = meta["optional-dependencies"]
    assert isinstance(extras, dict)
    names = {req.split(">")[0].split("[")[0] for req in extras["testing"]}
    assert names == {"pytest", "pytest-asyncio", "playwright"}


def test_core_279_mirrors_install_without_the_test_tools() -> None:
    deps = project()["dependencies"]
    assert isinstance(deps, list)
    assert not [d for d in deps if d.startswith(("pytest", "playwright"))]


def test_core_279_extra_is_complete_in_the_dev_environment() -> None:
    assert missing_testing_modules() == []


def test_core_279_import_without_the_extra_names_the_install_command() -> None:
    for module, dist in (("pytest", "pytest"), ("playwright", "playwright")):
        done = subprocess.run(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", BLOCK.format(name=module)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode != 0
        assert "MissingExtraError" in done.stderr
        assert dist in done.stderr
        assert TESTING_INSTALL in done.stderr
