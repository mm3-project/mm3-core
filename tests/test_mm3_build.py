"""mm3-build and its ``mm3-i18n`` hatchling build hook (CORE-280, CORE-263)."""

from __future__ import annotations

import json
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

import mm3.core.i18n.compiler as core_compiler
import mm3_build.catalogs as catalogs
from mm3_build.catalogs import CatalogError, check_locales, compiled
from mm3_build.plugin import catalog_dist_paths

PO = """\
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"

msgid "HELLO"
msgstr "{hello}"

msgid "ITEMS"
msgid_plural "ITEMS"
msgstr[0] "{one}"
msgstr[1] "{other}"
"""

TEXTS = {
    "en": ("Hello", "{count} item", "{count} items"),
    "it": ("Ciao", "{count} elemento", "{count} elementi"),
}


def write_po(root: Path, lang: str, hello: str, one: str, other: str) -> Path:
    po = root / lang / "LC_MESSAGES" / "messages.po"
    po.parent.mkdir(parents=True, exist_ok=True)
    po.write_text(PO.format(hello=hello, one=one, other=other), encoding="utf-8")
    return po


def module_project(tmp_path: Path, *, hook: bool = True) -> Path:
    root = tmp_path / "mm3-demo"
    pkg = root / "src" / "mm3_demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""Demo."""\n')
    for lang, (hello, one, other) in TEXTS.items():
        write_po(pkg / "locales", lang, hello, one, other)
    hook_table = "[tool.hatch.build.hooks.mm3-i18n]\n" if hook else ""
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [build-system]
            requires = ["hatchling", "mm3-build>=1.0,<2"]
            build-backend = "hatchling.build"

            [project]
            name = "mm3-demo"
            version = "0.1.0"

            [tool.hatch.build.targets.wheel]
            packages = ["src/mm3_demo"]

            """
        )
        + hook_table
    )
    return root


def build_wheel(root: Path, out: Path) -> zipfile.ZipFile:
    [wheel] = WheelBuilder(str(root)).build(directory=str(out), versions=["standard"])
    return zipfile.ZipFile(wheel)


def test_core_280_hook_compiles_every_catalog_into_the_wheel(tmp_path: Path) -> None:
    root = module_project(tmp_path)
    wheel = build_wheel(root, tmp_path / "dist")
    names = set(wheel.namelist())
    for lang in TEXTS:
        po = root / "src/mm3_demo/locales" / lang / "LC_MESSAGES/messages.po"
        mo_bytes, json_bytes, entries = compiled(po)
        assert wheel.read(f"mm3_demo/locales/{lang}/LC_MESSAGES/messages.mo") == mo_bytes
        assert wheel.read(f"mm3_demo/locales/{lang}/messages.json") == json_bytes
        assert f"mm3_demo/locales/{lang}/LC_MESSAGES/messages.po" in names
        assert entries["HELLO"] == TEXTS[lang][0]
    data = json.loads(wheel.read("mm3_demo/locales/it/messages.json"))
    assert data["ITEMS"]["one"] == "{count} elemento"
    assert data["ITEMS"]["other"] == "{count} elementi"
    # Nothing is written into the source tree.
    assert not list((root / "src").rglob("*.mo"))
    assert not list((root / "src").rglob("messages.json"))


def test_core_280_wheel_without_the_hook_has_no_compiled_catalogs(tmp_path: Path) -> None:
    wheel = build_wheel(module_project(tmp_path, hook=False), tmp_path / "dist")
    assert not [n for n in wheel.namelist() if n.endswith((".mo", "messages.json"))]


def test_core_280_stale_committed_files_are_replaced(tmp_path: Path) -> None:
    root = module_project(tmp_path)
    lang_dir = root / "src/mm3_demo/locales/en"
    (lang_dir / "messages.json").write_text('{"HELLO": "stale"}\n')
    (lang_dir / "LC_MESSAGES/messages.mo").write_bytes(b"stale")
    wheel = build_wheel(root, tmp_path / "dist")
    names = wheel.namelist()
    assert names.count("mm3_demo/locales/en/messages.json") == 1
    assert json.loads(wheel.read("mm3_demo/locales/en/messages.json"))["HELLO"] == "Hello"
    assert wheel.read("mm3_demo/locales/en/LC_MESSAGES/messages.mo") != b"stale"


def test_core_280_catalog_error_fails_the_build(tmp_path: Path) -> None:
    root = module_project(tmp_path)
    po = root / "src/mm3_demo/locales/it/LC_MESSAGES/messages.po"
    po.write_text('msgid "HELLO"\nnot a po line\nmsgstr "Ciao"\n', encoding="utf-8")
    with pytest.raises((SystemExit, CatalogError)):
        build_wheel(root, tmp_path / "dist")
    assert not list((tmp_path / "dist").glob("*.whl"))


def test_core_280_sdist_keeps_sources_only(tmp_path: Path) -> None:
    root = module_project(tmp_path)
    [sdist] = SdistBuilder(str(root)).build(directory=str(tmp_path / "dist"))
    with tarfile.open(sdist) as tar:
        names = tar.getnames()
    assert any(n.endswith("it/LC_MESSAGES/messages.po") for n in names)
    assert not [n for n in names if n.endswith((".mo", "messages.json"))]


@pytest.mark.parametrize(
    ("dist_path", "expected"),
    [
        (
            "mm3_demo/locales/it/LC_MESSAGES/messages.po",
            ("mm3_demo/locales/it/LC_MESSAGES/messages.mo", "mm3_demo/locales/it/messages.json"),
        ),
        (
            "mm3/core/locales/pt-BR/LC_MESSAGES/messages.po",
            (
                "mm3/core/locales/pt-BR/LC_MESSAGES/messages.mo",
                "mm3/core/locales/pt-BR/messages.json",
            ),
        ),
        ("mm3_demo/locales/messages.pot", None),
        ("mm3_demo/it/LC_MESSAGES/messages.po", None),
        ("mm3_demo/locales/it/LC_MESSAGES/other.po", None),
    ],
)
def test_core_280_catalog_paths_inside_the_wheel(
    dist_path: str, expected: tuple[str, str] | None
) -> None:
    assert catalog_dist_paths(dist_path) == expected


def test_core_280_one_compiler_for_core_and_modules() -> None:
    # mm3 i18n compile and check use mm3-build's code (CORE-280).
    for name in ("compiled", "compile_locales", "check_locales", "po_files", "placeholders"):
        assert getattr(core_compiler, name) is getattr(catalogs, name)


def test_core_280_check_reports_unreadable_catalog(tmp_path: Path) -> None:
    root = tmp_path / "locales"
    write_po(root, "en", "Hello", "{count} item", "{count} items")
    bad = root / "it" / "LC_MESSAGES" / "messages.po"
    bad.parent.mkdir(parents=True)
    bad.write_text('msgid "HELLO"\nnot a po line\nmsgstr "Ciao"\n', encoding="utf-8")
    problems = check_locales(root, required=["en", "it"])
    assert any(p.path == bad for p in problems)
