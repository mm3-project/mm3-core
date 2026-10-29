"""Milestone 3, part D: catalogs, lookup, plurals and formatting (core spec 8)."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from babel.messages.catalog import Catalog
from babel.messages.pofile import write_po

from mm3.cli.i18n_cmd import catalog_roots, run_i18n
from mm3.cli.main import main
from mm3.core.config.records import MESSAGES
from mm3.core.i18n import (
    CORE_CATALOG,
    I18n,
    check_locales,
    compile_locales,
    format_date,
    format_number,
    locale_chain,
    plural_category,
    substitute,
)
from mm3.core.i18n.catalogs import Table
from mm3.core.web.pages import TEXT

LANGS = ("en", "it", "fr", "es", "de")


def write_catalog(
    root: Path,
    lang: str,
    entries: dict[str, str | tuple[str, ...]],
) -> Path:
    catalog = Catalog(locale=lang, domain="messages")
    for key, text in entries.items():
        if isinstance(text, tuple):
            catalog.add((key, key), text)
        else:
            catalog.add(key, text)
    po = root / lang / "LC_MESSAGES" / "messages.po"
    po.parent.mkdir(parents=True, exist_ok=True)
    with po.open("wb") as f:
        write_po(f, catalog)
    return po


def full_root(tmp_path: Path) -> Path:
    root = tmp_path / "locales"
    texts = {
        "en": ("Feels like {temp}", ("{count} file", "{count} files")),
        "it": ("Percepita {temp}", ("{count} file", "{count} file")),
        "fr": ("Ressenti {temp}", ("{count} fichier", "{count} fichiers")),
        "es": ("Sensación {temp}", ("{count} archivo", "{count} archivos")),
        "de": ("Gefühlt {temp}", ("{count} Datei", "{count} Dateien")),
    }
    for lang, (feels, files) in texts.items():
        write_catalog(root, lang, {"FEELS_LIKE": feels, "FILES": files})
    return root


# --- CORE-263: compiled at build ---------------------------------------------------------


def test_core_263_compile_writes_mo_and_json(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    written = compile_locales(root)
    assert len(written) == 2 * len(LANGS)
    assert (root / "fr" / "LC_MESSAGES" / "messages.mo").is_file()
    fr = json.loads((root / "fr" / "messages.json").read_text())
    assert fr["FEELS_LIKE"] == "Ressenti {temp}"
    assert fr["FILES"]["one"] == "{count} fichier"
    assert fr["FILES"]["other"] == "{count} fichiers"
    assert compile_locales(root) == []  # already up to date
    assert check_locales(root) == []


def test_core_263_check_flags_missing_and_stale_files(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    problems = [str(p) for p in check_locales(root)]
    assert sum("not compiled" in p for p in problems) == 2 * len(LANGS)
    compile_locales(root)
    write_catalog(root, "it", {"FEELS_LIKE": "Sembra {temp}", "FILES": ("{count} file",) * 2})
    problems = [str(p) for p in check_locales(root)]
    assert len(problems) == 2
    assert all("does not match its .po" in p for p in problems)


def test_core_263_core_catalog_holds_validation_messages() -> None:
    en = Table(CORE_CATALOG).get("en")
    assert MESSAGES
    assert all(en[k] == v for k, v in MESSAGES.items())
    assert "CORE_CFG_OVERLAP" in MESSAGES


# --- CORE-40, CORE-41, CORE-42: quality gate ----------------------------------------------


def test_core_40_core_catalog_ships_five_locales_and_passes() -> None:
    assert check_locales(CORE_CATALOG) == []
    for lang in LANGS:
        assert (CORE_CATALOG / lang / "messages.json").is_file()


def test_core_40_missing_locale(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    (root / "de" / "LC_MESSAGES" / "messages.po").unlink()
    compile_locales(root)
    assert any("locale missing" in str(p) for p in check_locales(root))


def test_core_41_missing_key(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    write_catalog(root, "es", {"FEELS_LIKE": "Sensación {temp}"})
    compile_locales(root)
    assert [str(p).split(": ", 1)[1] for p in check_locales(root)] == ["FILES is missing (CORE-41)"]


def test_core_42_placeholder_mismatch(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    write_catalog(
        root, "de", {"FEELS_LIKE": "Gefühlt {temperatur}", "FILES": ("{count} Datei",) * 2}
    )
    compile_locales(root)
    (problem,) = check_locales(root)
    assert "FEELS_LIKE has placeholders ['temperatur'], en has ['temp'] (CORE-42)" in str(problem)


def test_core_263_cli_compile_and_check(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    out = io.StringIO()
    args = argparse.Namespace(i18n_command="check", paths=[tmp_path])
    assert run_i18n(args, out) == 1
    args.i18n_command = "compile"
    assert run_i18n(args, out) == 0
    args.i18n_command = "check"
    out = io.StringIO()
    assert run_i18n(args, out) == 0
    assert out.getvalue().strip().endswith(": ok")
    assert catalog_roots([tmp_path]) == [root.resolve()]
    assert catalog_roots([]) == [CORE_CATALOG]
    assert run_i18n(argparse.Namespace(i18n_command="check", paths=[tmp_path / "x"])) == 1
    assert main(["i18n", "check"]) == 0


# --- CORE-264: lookup ----------------------------------------------------------------------


def test_core_264_locale_chain() -> None:
    assert locale_chain(None, "de-CH") == ["de-CH", "de", "en"]
    assert locale_chain("fr", "it") == ["fr", "it", "en"]
    assert locale_chain("pt-BR", "en") == ["pt-BR", "pt", "en"]


def json_root(tmp_path: Path, name: str, data: dict[str, dict[str, object]]) -> Path:
    root = tmp_path / name
    for lang, entries in data.items():
        (root / lang).mkdir(parents=True)
        (root / lang / "messages.json").write_text(json.dumps(entries))
    return root


def test_core_264_module_then_core_and_packages(tmp_path: Path) -> None:
    module = json_root(
        tmp_path,
        "mod",
        {"en": {"HELLO": "Hello", "ONLY_EN": "English"}, "it": {"HELLO": "Ciao"}},
    )
    core = json_root(tmp_path, "core", {"en": {"CORE_X": "Core"}, "it": {"CORE_X": "Nucleo"}})
    pack = json_root(
        tmp_path,
        "pack",
        {"pt": {"HELLO": "Olá"}, "it": {"HELLO": "Salve", "ONLY_EN": "Solo inglese"}},
    )
    core_pack = json_root(tmp_path, "core_pack", {"pt": {"CORE_X": "Núcleo"}})
    i18n = I18n(
        lambda m: module if m == "greet" else None,
        core_root=core,
        packages={"greet": [pack], "mm3": [core_pack]},
    )
    it = locale_chain(None, "it")
    assert i18n.translate("greet", it, "HELLO") == "Ciao"  # the module's own wins
    assert i18n.translate("greet", it, "ONLY_EN") == "Solo inglese"  # package adds a key
    assert i18n.translate("greet", ["pt", "en"], "HELLO") == "Olá"  # package adds a locale
    assert i18n.translate("greet", it, "CORE_X") == "Nucleo"
    assert i18n.translate("greet", ["pt", "en"], "CORE_X") == "Núcleo"
    assert i18n.translate("greet", it, "NOPE") == "NOPE"
    assert i18n.translate("other", it, "HELLO") == "HELLO"


def test_core_264_merged_catalog_and_version(tmp_path: Path) -> None:
    module = json_root(tmp_path, "mod", {"en": {"A": "a", "B": "b"}, "de": {"A": "ä"}})
    core = json_root(tmp_path, "core", {"en": {"CORE_X": "x"}})
    i18n = I18n(lambda m: module, core_root=core, packages={})
    version, raw = i18n.merged("m", ["de", "en"])
    assert json.loads(raw) == {"A": "ä", "B": "b", "CORE_X": "x"}
    assert len(version) == 12
    assert i18n.merged("m", ["en"])[0] != version


# --- CORE-265: plurals and placeholders --------------------------------------------------------


def test_core_265_plural_forms_and_count(tmp_path: Path) -> None:
    root = full_root(tmp_path)
    compile_locales(root)
    i18n = I18n(lambda m: root, core_root=tmp_path / "none", packages={})
    assert i18n.translate("m", ["fr", "en"], "FILES", count=0) == "0 fichier"
    assert i18n.translate("m", ["fr", "en"], "FILES", count=2) == "2 fichiers"
    assert i18n.translate("m", ["en"], "FILES", count=1) == "1 file"
    assert i18n.translate("m", ["de", "en"], "FILES", count=5) == "5 Dateien"
    assert (
        i18n.translate("m", ["it", "en"], "FEELS_LIKE", values={"temp": "21 °C"})
        == "Percepita 21 °C"
    )
    assert plural_category("it", 1_000_000) == "many"
    assert plural_category("en", 1) == "one"


def test_core_265_missing_variable_stays() -> None:
    assert substitute("{a} and {b}", {"a": 1}, key="K") == "1 and {b}"


# --- CORE-266: formatting -----------------------------------------------------------------------


def test_core_266_format_date_uses_engine_timezone() -> None:
    moment = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)
    rome = ZoneInfo("Europe/Rome")
    assert format_date(moment, "short", "it", rome) == "30/10/26"
    assert format_date(moment, "time", "it", rome) == "00:30"
    assert format_date(moment, "medium", "en", ZoneInfo("UTC")) == "Oct 29, 2026"
    assert format_date(moment.replace(tzinfo=None), "long", "de", rome) == "30. Oktober 2026"
    with pytest.raises(ValueError, match="unknown date style"):
        format_date(moment, "weekday", "en", rome)


def test_core_266_format_number_with_units() -> None:
    assert format_number(21.5, "it", "celsius") == "21,5 °C"
    assert format_number(12, "de", "kilometer-per-hour") == "12 km/h"
    assert format_number(1234.5, "fr") == "1\u202f234,5"
    assert format_number(3, "en", "no-such-unit") == "3 no-such-unit"


# --- CORE-11: engine text from catalogs --------------------------------------------------


def test_core_11_engine_pages_use_the_core_catalog() -> None:
    en = Table(CORE_CATALOG).get("en")
    assert TEXT
    assert all(en[k] == v for k, v in TEXT.items())
    it = Table(CORE_CATALOG).get("it")
    assert it["CORE_SHELL_UNAVAILABLE"] == "Non disponibile"
    assert set(it) == set(en)


def test_core_272_error_and_timeout_keys_are_translated() -> None:
    from mm3.core.web.ws import ERROR_KEYS, REQUEST_TIMEOUT_KEY

    for lang in LANGS:
        table = Table(CORE_CATALOG).get(lang)
        for key in [*ERROR_KEYS.values(), REQUEST_TIMEOUT_KEY]:
            assert isinstance(table.get(key), str), (lang, key)
