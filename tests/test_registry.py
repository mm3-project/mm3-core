"""Module discovery and API version checks (CORE-03, MOD-90)."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from mm3.core.registry import Registry, api_compatible
from mm3.module import MirrorModule


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("1.0", True),
        ("1.1", False),
        ("2.0", False),
        ("0.9", False),
        ("1", False),
        (1.0, False),
        ("x.y", False),
    ],
)
def test_core_03_api_version_check(version: object, ok: bool) -> None:
    assert (api_compatible(version, "1.0") is None) is ok


def test_core_03_minor_lower_or_equal_is_accepted() -> None:
    assert api_compatible("1.0", "1.3") is None
    assert api_compatible("1.3", "1.3") is None
    assert "newer" in (api_compatible("1.4", "1.3") or "")
    assert "major" in (api_compatible("2.0", "1.3") or "")


class Good(MirrorModule):
    name = "good"


class Future(MirrorModule):
    name = "future"
    api_version = "1.99"


def test_core_03_registry_add_refuses_bad_modules() -> None:
    reg = Registry()
    reg.add(Good)
    reg.add(Future)
    reg.add(object, name="notamodule")
    reg.add(Good)  # duplicate
    good, future, bad = reg.get("good"), reg.get("future"), reg.get("notamodule")
    assert good is not None
    assert good.cls is Good
    assert good.error is None
    assert future is not None
    assert future.cls is None
    assert "newer" in (future.error or "")
    assert bad is not None
    assert bad.cls is None
    assert [e.name for e in reg.entries()] == ["future", "good", "notamodule"]
    assert "good" in reg
    assert reg.get("missing") is None


@pytest.fixture
def test_package(tmp_path: Path) -> Iterator[Path]:
    """Install a throw-away distribution with three entry points on sys.path."""
    pkg = tmp_path / "mm3_testpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            """
            from mm3 import MirrorModule

            class Echo(MirrorModule):
                name = "echo"

            class Old(MirrorModule):
                name = "old"
                api_version = "0.9"
            """
        )
    )
    (pkg / "broken.py").write_text("raise ImportError('boom')\n")
    dist = tmp_path / "mm3_testpkg-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: mm3-testpkg\nVersion: 0.1.0\n")
    (dist / "entry_points.txt").write_text(
        "[mm3.modules]\necho = mm3_testpkg:Echo\nold = mm3_testpkg:Old\n"
        "broken = mm3_testpkg.broken:Thing\n"
    )
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("mm3_testpkg")]:
        del sys.modules[name]


def test_core_03_discover_from_entry_points(test_package: Path) -> None:
    reg = Registry()
    reg.discover()
    echo, old, broken = reg.get("echo"), reg.get("old"), reg.get("broken")
    assert echo is not None
    assert echo.cls is not None
    assert echo.distribution == "mm3-testpkg"
    assert old is not None
    assert old.cls is None
    assert "major" in (old.error or "")
    assert broken is not None
    assert broken.cls is None
    assert "import failed" in (broken.error or "")
