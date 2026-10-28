"""Smoke tests for the package skeleton."""

import pytest

import mm3
from mm3.cli.main import main


def test_core_73_version_is_semver() -> None:
    parts = mm3.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_core_03_module_api_version_format() -> None:
    major, minor = mm3.MODULE_API_VERSION.split(".")
    assert major.isdigit()
    assert minor.isdigit()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert mm3.__version__ in capsys.readouterr().out
