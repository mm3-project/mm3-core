"""``mm3 new-module``: argument handling and the files it writes (MOD-95)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mm3.cli.main import main
from mm3.cli.new_module_cmd import scaffold


def test_mod_95_full_module_has_class_assets_catalogs_and_readme(tmp_path: Path) -> None:
    root = scaffold("clockwork", "full", tmp_path)
    assert root.name == "mm3-clockwork"
    pkg = root / "src" / "mm3_clockwork"
    assert (pkg / "static" / "clockwork.js").is_file()
    assert (pkg / "static" / "clockwork.css").is_file()
    for lang in ("en", "it", "fr", "es", "de"):
        assert (pkg / "locales" / lang / "LC_MESSAGES" / "messages.po").is_file()
    readme = (root / "README.md").read_text()
    assert "## Notifications" in readme
    assert "## Grants" in readme
    assert "mm3-build" in (root / "pyproject.toml").read_text()
    assert (root / "tests" / "test_clockwork.py").is_file()


def test_mod_95_backend_kind_has_no_front_end(tmp_path: Path) -> None:
    root = scaffold("feeder", "backend", tmp_path)
    pkg = root / "src" / "mm3_feeder"
    assert not (pkg / "static").exists()
    assert "element" not in (pkg / "__init__.py").read_text()


def test_mod_95_frontend_kind_has_no_hooks(tmp_path: Path) -> None:
    root = scaffold("banner", "frontend", tmp_path)
    source = (root / "src" / "mm3_banner" / "__init__.py").read_text()
    assert "async def" not in source
    assert "element: ClassVar" in source


@pytest.mark.parametrize("bad", ["Bad", "1abc", "with-dash", ""])
def test_mod_95_bad_names_are_refused(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must match"):
        scaffold(bad, "full", tmp_path)


def test_mod_95_existing_directory_is_not_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new-module", "twice", "--path", str(tmp_path)]) == 0
    assert main(["new-module", "twice", "--path", str(tmp_path)]) == 1
    assert "already exists" in capsys.readouterr().err
