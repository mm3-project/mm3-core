"""Milestone 2, part A: YAML loading, validation records and ``mm3 check-config``."""

from __future__ import annotations

import io
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field, SecretStr

from mm3.cli.main import check_config, run
from mm3.config import LocalizedText, ModuleConfig, locale_chain, parse_duration
from mm3.core.config import ConfigResult, load_config
from mm3.core.config.model import BackupConfig, MirrorConfig
from mm3.core.config.modelcheck import check_config_model
from mm3.core.config.records import CODES, MESSAGES, WARNING_CODES
from mm3.core.publicconfig import public_config
from mm3.core.registry import Registry
from mm3.core.settings import EngineSettings, system_timezone
from mm3.module import MirrorModule

SPEC = Path(__file__).resolve().parents[1] / "docs" / "spec" / "core.md"


class WeatherConfig(ModuleConfig):
    api_key: SecretStr = Field(title="API_KEY", json_schema_extra={"secret": True})
    lat: float = Field(default=0.0, ge=-90, le=90, title="LAT")
    lon: float = Field(default=0.0, ge=-180, le=180, title="LON")
    every: timedelta = Field(default=timedelta(minutes=10), title="EVERY")
    label: LocalizedText | None = Field(default=None, title="LABEL")


class Weather(MirrorModule):
    name = "weather"
    config_model = WeatherConfig
    element = "mm3-weather"


class Clock(MirrorModule):
    name = "clock"
    element = "mm3-clock"


class Future(MirrorModule):
    name = "future"
    api_version = "9.0"


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    for cls in (Weather, Clock, Future):
        reg.add(cls)
    return reg


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def load(
    tmp_path: Path,
    text: str,
    registry: Registry,
    *,
    env: str | None = None,
    environ: dict[str, str] | None = None,
) -> ConfigResult:
    cfg = write(tmp_path / "etc" / "config.yaml", text)
    env_path = tmp_path / "etc" / "mm3.env"
    if env is not None:
        write(env_path, env)
        env_path.chmod(0o600)
    return load_config(cfg, registry=registry, env_file=env_path, environ=environ or {})


def codes(result: ConfigResult) -> list[str]:
    return [r.code for r in result.records]


def by_code(result: ConfigResult, code: str) -> list[Any]:
    return [r for r in result.records if r.code == code]


# --- CORE-02, CORE-30: Config Manager and startup -------------------------------------


def test_core_02_load_config_validates_engine_and_module_schemas(
    tmp_path: Path, registry: Registry
) -> None:
    result = load(
        tmp_path,
        "engine:\n  locale: it\n  timezone: Europe/Rome\n"
        "modules:\n  - module: clock\n    position: top_left\n"
        "  - module: weather\n    position: top_right\n    config:\n"
        "      api_key: k\n      lat: 42.1\n",
        registry,
    )
    assert result.ok, result.records
    assert result.config is not None
    assert result.config.engine.locale == "it"
    weather = result.instances[1]
    assert isinstance(weather.config, WeatherConfig)
    assert weather.config.lat == 42.1
    assert weather.config.every == timedelta(minutes=10)
    assert [s.instance_id for s in result.instance_specs()] == ["clock", "weather"]


def test_core_30_run_aborts_with_exit_2_and_line(tmp_path: Path, registry: Registry) -> None:
    cfg = write(tmp_path / "config.yaml", "engine:\n  units: kelvin\n")
    err = io.StringIO()
    code = run(cfg, tmp_path / "none.env", registry=registry, err=err)
    assert code == 2
    line = err.getvalue().strip()
    assert line.startswith(f"{cfg}:2:10 error /engine/units ")


def test_core_30_empty_file_uses_defaults(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "", registry)
    assert result.ok
    assert result.config == MirrorConfig()


# --- CORE-31: !include ----------------------------------------------------------------


def test_core_31_include_replaces_node(tmp_path: Path, registry: Registry) -> None:
    write(tmp_path / "etc" / "parts" / "modules.yaml", "- module: clock\n  position: top_bar\n")
    result = load(tmp_path, "modules: !include parts/modules.yaml\n", registry)
    assert result.ok, result.records
    assert result.instances[0].module == "clock"
    assert tmp_path / "etc" / "parts" / "modules.yaml" in result.loaded.files


def test_core_31_nested_include_is_relative_to_including_file(
    tmp_path: Path, registry: Registry
) -> None:
    write(tmp_path / "etc" / "a" / "engine.yaml", "locale: !include ../b/locale.yaml\n")
    write(tmp_path / "etc" / "b" / "locale.yaml", "de\n")
    result = load(tmp_path, "engine: !include a/engine.yaml\n", registry)
    assert result.ok, result.records
    assert result.config is not None
    assert result.config.engine.locale == "de"


def test_core_31_include_outside_config_dir(tmp_path: Path, registry: Registry) -> None:
    write(tmp_path / "secret.yaml", "x: 1\n")
    result = load(tmp_path, "engine: !include ../secret.yaml\n", registry)
    (rec,) = by_code(result, "CORE_CFG_INCLUDE_OUTSIDE")
    assert rec.path == "/engine"
    assert rec.line == 1


def test_core_31_include_symlink_escaping_is_refused(tmp_path: Path, registry: Registry) -> None:
    outside = write(tmp_path / "outside.yaml", "locale: it\n")
    (tmp_path / "etc").mkdir()
    os.symlink(outside, tmp_path / "etc" / "link.yaml")
    result = load(tmp_path, "engine: !include link.yaml\n", registry)
    assert codes(result) == ["CORE_CFG_INCLUDE_OUTSIDE"]


def test_core_31_include_cycle(tmp_path: Path, registry: Registry) -> None:
    write(tmp_path / "etc" / "a.yaml", "x: !include b.yaml\n")
    write(tmp_path / "etc" / "b.yaml", "y: !include a.yaml\n")
    result = load(tmp_path, "engine: !include a.yaml\n", registry)
    assert "CORE_CFG_INCLUDE_CYCLE" in codes(result)


def test_core_31_include_depth_limited_to_8(tmp_path: Path, registry: Registry) -> None:
    for n in range(1, 10):
        write(tmp_path / "etc" / f"l{n}.yaml", f"!include l{n + 1}.yaml\n")
    write(tmp_path / "etc" / "l10.yaml", "en\n")
    result = load(tmp_path, "engine:\n  locale: !include l1.yaml\n", registry)
    (rec,) = by_code(result, "CORE_CFG_INCLUDE_DEPTH")
    assert rec.params == {"limit": 8}
    ok = load(tmp_path, "engine:\n  locale: !include l3.yaml\n", registry)
    assert ok.ok, ok.records


def test_core_31_errors_cite_included_file_and_line(tmp_path: Path, registry: Registry) -> None:
    inc = write(tmp_path / "etc" / "engine.yaml", "locale: it\nthreads: 0\n")
    result = load(tmp_path, "engine: !include engine.yaml\n", registry)
    (rec,) = result.errors
    assert (rec.file, rec.line, rec.col, rec.path) == (str(inc), 2, 10, "/engine/threads")


def test_core_31_missing_include_and_no_code_execution(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "engine: !include nope.yaml\n", registry)
    assert codes(result) == ["CORE_CFG_FILE_UNREADABLE"]
    tagged = load(tmp_path, "engine: !!python/object:os.system {}\n", registry)
    assert tagged.errors
    assert tagged.config is None


# --- CORE-200: env file and !env ------------------------------------------------------


def test_core_200_env_file_before_process_environment(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "engine:\n  admin:\n    token: !env MM3_TOKEN\n    port: !env PORT\n"
        "  locale: !env [LANG_TAG, fr]\n",
        registry,
        env="# comment\n\nMM3_TOKEN='s3cr=t'\nPORT=\"9000\"\n",
        environ={"MM3_TOKEN": "other", "PORT": "1"},
    )
    assert result.ok, result.records
    assert result.config is not None
    admin = result.config.engine.admin
    assert admin.token is not None
    assert admin.token.get_secret_value() == "s3cr=t"
    assert admin.port == 9000
    assert result.config.engine.locale == "fr"
    assert result.loaded.env_refs["/engine/admin/token"].as_written() == "!env MM3_TOKEN"


def test_core_200_process_environment_and_missing_variable(
    tmp_path: Path, registry: Registry
) -> None:
    result = load(
        tmp_path,
        "engine:\n  locale: !env A\n  timezone: !env B\n",
        registry,
        environ={"A": "it"},
    )
    (rec,) = result.errors
    assert rec.code == "CORE_CFG_ENV_MISSING"
    assert rec.params == {"name": "B"}
    assert (rec.line, rec.path) == (3, "/engine/timezone")


def test_core_200_env_file_syntax_never_shows_value(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "{}\n", registry, env="GOOD=1\nthis is hunter2\n")
    (rec,) = result.errors
    assert rec.code == "CORE_CFG_ENV_FILE_SYNTAX"
    assert rec.line == 2
    assert "hunter2" not in json.dumps(rec.as_dict())


def test_core_200_world_readable_env_file_warns(tmp_path: Path, registry: Registry) -> None:
    cfg = write(tmp_path / "config.yaml", "{}\n")
    env = write(tmp_path / "mm3.env", "A=1\n")
    env.chmod(0o644)
    result = load_config(cfg, registry=registry, env_file=env, environ={})
    assert result.ok
    assert codes(result) == ["CORE_CFG_ENV_FILE_MODE"]


# --- CORE-201: top-level and engine keys ----------------------------------------------


def test_core_201_unknown_top_level_and_engine_keys(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "regions: {}\nengine:\n  colour: red\n", registry)
    recs = by_code(result, "CORE_CFG_UNKNOWN_KEY")
    assert {(r.path, r.line, r.col) for r in recs} == {("/regions", 1, 1), ("/engine/colour", 3, 3)}


def test_core_201_engine_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Rome")
    s = EngineSettings()
    assert (s.locale, s.timezone, s.units) == ("en", "Europe/Rome", "metric")
    assert (s.server.host, s.server.port, s.admin.port, s.admin.allow) == (
        "127.0.0.1",
        8080,
        8081,
        [],
    )
    assert s.mqtt is None
    assert s.admin.token is None
    assert (s.bus.queue_size, s.threads, s.log_level) == (1000, 8, "info")
    assert (s.debug_ttl, s.history.keep, s.input.touch) == (timedelta(minutes=30), 200, False)


def test_core_201_system_timezone_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    assert system_timezone(tmp_path) == "UTC"
    write(tmp_path / "etc" / "timezone", "Europe/Rome\n")
    assert system_timezone(tmp_path) == "Europe/Rome"


def test_core_201_config_version_newer_than_engine(tmp_path: Path, registry: Registry) -> None:
    result = load(tmp_path, "config_version: 2\n", registry)
    (rec,) = result.errors
    assert rec.code == "CORE_CFG_VERSION"
    assert rec.params == {"value": 2, "max": 1}


def test_core_201_admin_allow_and_mqtt(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "engine:\n  admin:\n    allow: ['192.168.1.0/24', 'nonsense']\n"
        "  mqtt:\n    url: http://x\n    base_topic: mm3/a\n",
        registry,
    )
    paths = {r.path: r.code for r in result.errors}
    assert paths == {
        "/engine/admin/allow/1": "CORE_CFG_TYPE",
        "/engine/mqtt/url": "CORE_CFG_PATTERN",
    }


# --- CORE-202..204: instances ---------------------------------------------------------


def test_core_202_default_id_and_required_ids(tmp_path: Path, registry: Registry) -> None:
    single = load(tmp_path, "modules:\n  - module: clock\n    position: top_left\n", registry)
    assert single.instances[0].id == "clock"
    twice = load(
        tmp_path,
        "modules:\n  - module: clock\n    id: clock_a\n    position: top_left\n"
        "  - module: clock\n    position: top_right\n",
        registry,
    )
    (rec,) = twice.errors
    assert (rec.code, rec.path, rec.line) == ("CORE_CFG_ID_REQUIRED", "/modules/1", 5)


def test_core_202_duplicate_and_invalid_ids(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: clock\n    id: main\n    position: top_left\n"
        "  - module: weather\n    id: main\n    position: top_right\n    config: {api_key: k}\n"
        "  - module: weather\n    id: Bad-Id\n    position: top_bar\n    config: {api_key: k}\n",
        registry,
    )
    assert {(r.code, r.path) for r in result.errors} == {
        ("CORE_CFG_ID_DUPLICATE", "/modules/1/id"),
        ("CORE_CFG_PATTERN", "/modules/2/id"),
    }


def test_core_202_unknown_instance_key(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: clock\n    position: top_left\n    header: true\n",
        registry,
    )
    (rec,) = result.errors
    assert (rec.code, rec.path, rec.line, rec.col) == (
        "CORE_CFG_UNKNOWN_KEY",
        "/modules/0/header",
        4,
        5,
    )


def test_core_202_instance_locale_reaches_context(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: clock\n    position: top_left\n    locale: de-CH\n",
        registry,
    )
    assert result.instance_specs()[0].locale == "de-CH"


def test_core_203_missing_and_unavailable_modules(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: calendar\n    id: cal\n  - module: calendar\n    id: off_cal\n"
        "    disabled: true\n    config: {anything: 1}\n"
        "  - module: future\n    config: {nonsense: true}\n",
        registry,
    )
    assert [(r.code, r.severity, r.path) for r in result.records] == [
        ("CORE_CFG_MODULE_MISSING", "error", "/modules/0/module"),
        ("CORE_CFG_MODULE_UNAVAILABLE", "warning", "/modules/2/module"),
    ]
    without_missing = load(
        tmp_path,
        "modules:\n  - module: calendar\n    disabled: true\n  - module: future\n",
        registry,
    )
    assert without_missing.ok
    (spec,) = without_missing.instance_specs()
    assert spec.module == "future"
    assert without_missing.instances[1].unavailable is not None


def test_core_204_disabled_instances_are_validated(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: weather\n    disabled: true\n    position: top_left\n"
        "    config: {lat: 200}\n",
        registry,
    )
    assert {(r.code, r.path) for r in result.errors} == {
        ("CORE_CFG_REQUIRED", "/modules/0/config/api_key"),
        ("CORE_CFG_RANGE", "/modules/0/config/lat"),
    }
    assert result.instance_specs() == []


# --- CORE-205: durations --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (90, timedelta(seconds=90)),
        ("PT1H30M", timedelta(hours=1, minutes=30)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("30s", timedelta(seconds=30)),
        ("10m", timedelta(minutes=10)),
        ("2d", timedelta(days=2)),
        ("250ms", timedelta(milliseconds=250)),
    ],
)
def test_core_205_duration_forms(raw: Any, expected: timedelta) -> None:
    cfg = WeatherConfig.model_validate({"api_key": "k", "every": raw})
    assert cfg.every == expected
    assert EngineSettings.model_validate({"debug_ttl": raw}).debug_ttl == expected


def test_core_205_invalid_duration(tmp_path: Path, registry: Registry) -> None:
    assert parse_duration("1m1h") is None
    assert parse_duration("") is None
    result = load(tmp_path, "engine:\n  debug_ttl: soon\n", registry)
    (rec,) = result.errors
    assert (rec.code, rec.params) == ("CORE_CFG_DURATION", {"value": "soon"})


# --- CORE-206, CORE-243: records and codes --------------------------------------------


def test_core_206_collects_every_problem_with_records(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "engine:\n  threads: 0\n  units: kelvin\nmodules:\n  - module: weather\n"
        "    position: top_left\n    config:\n      lat: north\n",
        registry,
    )
    recs = {r.path: r for r in result.errors}
    assert set(recs) == {
        "/engine/threads",
        "/engine/units",
        "/modules/0/config/api_key",
        "/modules/0/config/lat",
    }
    lat = recs["/modules/0/config/lat"].as_dict()
    assert set(lat) == {"file", "line", "col", "severity", "path", "code", "params", "message"}
    assert (lat["line"], lat["col"], lat["code"]) == (8, 12, "CORE_CFG_TYPE")
    assert lat["message"] == "Expected a number"
    assert result.config is None


def test_core_206_warnings_never_block(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: weather\n    position: top_left\n"
        "    config: {api_key: k, label: {it: Ciao}}\n",
        registry,
    )
    assert result.ok
    assert result.config is not None
    (rec,) = result.warnings
    assert (rec.code, rec.path) == ("CORE_CFG_LOCALIZED_NO_EN", "/modules/0/config/label")


@pytest.mark.parametrize(
    ("text", "code", "line"),
    [
        ("engine: [unclosed\n", "CORE_CFG_YAML_SYNTAX", 2),
        ("engine: {}\nengine: {}\n", "CORE_CFG_DUPLICATE_KEY", 2),
        ("engine: !secret x\n", "CORE_CFG_YAML_TAG", 1),
    ],
)
def test_core_206_yaml_problems(
    tmp_path: Path, registry: Registry, text: str, code: str, line: int
) -> None:
    result = load(tmp_path, text, registry)
    (rec,) = result.errors
    assert (rec.code, rec.line) == (code, line)


def _spec_code_table() -> dict[str, str]:
    text = SPEC.read_text()
    start = text.index("- CORE-243:")
    rows = re.findall(r"^\| `(CORE_CFG_[A-Z0-9_]+)` \| (error|warning) \|", text[start:], re.M)
    return dict(rows)


def test_core_243_codes_match_spec_table() -> None:
    table = _spec_code_table()
    assert set(table) == CODES == set(MESSAGES)
    assert {c for c, sev in table.items() if sev == "warning"} == WARNING_CODES


@pytest.mark.parametrize(
    ("data", "code", "params"),
    [
        ({"engine": {"threads": "x"}}, "CORE_CFG_TYPE", {"expected": "an integer"}),
        ({"engine": {"threads": 0}}, "CORE_CFG_RANGE", {"ge": 1}),
        ({"engine": {"locale": "not a tag"}}, "CORE_CFG_LOCALE", {"value": "not a tag"}),
        ({"engine": {"timezone": "Mars/Base"}}, "CORE_CFG_TIMEZONE", {"value": "Mars/Base"}),
        ({"engine": {"units": "k"}}, "CORE_CFG_CHOICE", {"allowed": "'metric' or 'imperial'"}),
        ({"engine": {"mqtt": {"url": "mqtt://b"}}}, "CORE_CFG_REQUIRED", {}),
        (
            {"engine": {"mqtt": {"url": "mqtt://b", "base_topic": ""}}},
            "CORE_CFG_LENGTH",
            {"min": 1, "max": None},
        ),
        (
            {"modules": [{"module": "x", "id": "1"}]},
            "CORE_CFG_PATTERN",
            {"pattern": "^[a-z][a-z0-9_]{0,63}$"},
        ),
    ],
)
def test_core_243_pydantic_errors_map_to_codes(
    tmp_path: Path, registry: Registry, data: dict[str, Any], code: str, params: dict[str, Any]
) -> None:
    result = load(tmp_path, json.dumps(data), registry)
    assert any(r.code == code and dict(r.params) == params for r in result.errors), result.records


def test_core_243_module_value_error_is_invalid_with_reason(
    tmp_path: Path, registry: Registry
) -> None:
    from pydantic import field_validator

    class Strict(ModuleConfig):
        word: str = Field(default="a", title="WORD")

        @field_validator("word")
        @classmethod
        def _no_b(cls, v: str) -> str:
            if v == "b":
                raise ValueError("b is not allowed")
            return v

    class StrictModule(MirrorModule):
        name = "strict"
        config_model = Strict

    registry.add(StrictModule)
    result = load(tmp_path, "modules:\n  - module: strict\n    config: {word: b}\n", registry)
    (rec,) = result.errors
    assert (rec.code, rec.params) == ("CORE_CFG_INVALID", {"reason": "b is not allowed"})


# --- CORE-207: mm3 check-config -------------------------------------------------------


def test_core_207_check_config_exit_codes_and_lines(tmp_path: Path, registry: Registry) -> None:
    good = write(tmp_path / "good.yaml", "modules:\n  - module: future\n")
    bad = write(tmp_path / "bad.yaml", "engine:\n  threads: -1\n")
    env = tmp_path / "none.env"
    out = io.StringIO()
    assert check_config(good, env, registry=registry, out=out) == 0
    assert "warning /modules/0/module" in out.getvalue()
    assert check_config(good, env, strict=True, registry=registry, out=io.StringIO()) == 2
    out = io.StringIO()
    assert check_config(bad, env, registry=registry, out=out) == 2
    assert out.getvalue() == f"{bad}:2:12 error /engine/threads Value out of range (ge 1)\n"


def test_core_207_check_config_json(tmp_path: Path, registry: Registry) -> None:
    bad = write(tmp_path / "bad.yaml", "engine:\n  threads: -1\n")
    out = io.StringIO()
    assert check_config(bad, tmp_path / "e", as_json=True, registry=registry, out=out) == 2
    doc = json.loads(out.getvalue())
    assert (doc["ok"], doc["errors"], doc["warnings"]) == (False, 1, 0)
    assert doc["records"][0]["code"] == "CORE_CFG_RANGE"


def test_core_207_cli_entry(tmp_path: Path) -> None:
    from mm3.cli.main import main

    cfg = write(tmp_path / "c.yaml", "{}\n")
    assert main(["check-config", "--config", str(cfg), "--env-file", str(tmp_path / "e")]) == 0


# --- CORE-244: backup block -----------------------------------------------------------


def test_core_244_backup_defaults() -> None:
    b = BackupConfig()
    assert (b.schedule, b.keep.daily, b.keep.weekly, b.keep.monthly) == ("03:30", 7, 4, 6)
    assert b.effective_parts == ["config", "data", "modules"]
    key = "age1" + "q" * 58
    assert BackupConfig(encrypt_to=key).effective_parts == ["config", "data", "modules", "secrets"]


@pytest.mark.parametrize(
    ("block", "code", "path"),
    [
        ({"parts": ["config", "secrets"]}, "CORE_CFG_SECRETS_UNENCRYPTED", "/backup/parts"),
        ({"parts": ["config", "config"]}, "CORE_CFG_INVALID", "/backup/parts"),
        ({"parts": []}, "CORE_CFG_LENGTH", "/backup/parts"),
        (
            {"keep": {"daily": 0, "weekly": 0, "monthly": 0}},
            "CORE_CFG_INVALID",
            "/backup/keep/monthly",
        ),
        ({"schedule": "25:00"}, "CORE_CFG_PATTERN", "/backup/schedule"),
        ({"encrypt_to": "age1short"}, "CORE_CFG_PATTERN", "/backup/encrypt_to"),
        ({"remote": {"path": "x"}}, "CORE_CFG_REQUIRED", "/backup/remote/name"),
        ({"when": "daily"}, "CORE_CFG_UNKNOWN_KEY", "/backup/when"),
    ],
)
def test_core_244_backup_checks(
    tmp_path: Path, registry: Registry, block: dict[str, Any], code: str, path: str
) -> None:
    result = load(tmp_path, json.dumps({"backup": block}), registry)
    assert [(r.code, r.path) for r in result.errors] == [(code, path)]


# --- CORE-101, MOD-48: LocalizedText --------------------------------------------------


def test_core_101_localized_text_resolution() -> None:
    plain = LocalizedText("Hi")
    assert plain.resolve("it") == "Hi"
    assert not plain.missing_en
    text = LocalizedText({"it": "Casa", "en": "Home", "de-CH": "Hei"})
    assert text.resolve("it") == "Casa"
    assert text.resolve("de-CH") == "Hei"
    assert text.resolve("fr") == "Home"
    assert LocalizedText({"it": "Casa", "de": "Haus"}).resolve("fr") == "Casa"
    assert LocalizedText({"de": "Haus"}).resolve("de-AT") == "Haus"
    assert locale_chain("de-CH") == ["de-ch", "de", "en"]


def test_core_101_invalid_tags_and_empty_map(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: weather\n    position: top_left\n"
        "    config: {api_key: k, label: {'not a tag': x}}\n",
        registry,
    )
    assert [r.code for r in result.errors] == ["CORE_CFG_LOCALE"]
    empty = load(
        tmp_path,
        "modules:\n  - module: weather\n    position: top_left\n"
        "    config: {api_key: k, label: {}}\n",
        registry,
    )
    assert [r.code for r in empty.errors] == ["CORE_CFG_LENGTH"]


def test_core_101_public_config_resolves_text_and_hides_secrets() -> None:
    cfg = WeatherConfig.model_validate({"api_key": "k", "label": {"en": "Out", "it": "Fuori"}})
    assert public_config(cfg)["label"] == {"en": "Out", "it": "Fuori"}
    sent = public_config(cfg, "it")
    assert sent["label"] == "Fuori"
    assert "api_key" not in sent


def test_mod_48_localized_text_in_module_config_resolves_in_backend() -> None:
    cfg = WeatherConfig.model_validate({"api_key": "k", "label": {"en": "Out", "it": "Fuori"}})
    assert cfg.label is not None
    assert cfg.label.resolve("it-IT") == "Fuori"


# --- MOD-45..47, MOD-49: config models ------------------------------------------------


def test_mod_45_module_config_forbids_unknown_keys(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: weather\n    position: top_left\n"
        "    config: {api_key: k, lang: it}\n",
        registry,
    )
    (rec,) = result.errors
    assert (rec.code, rec.path) == ("CORE_CFG_UNKNOWN_KEY", "/modules/0/config/lang")
    assert check_config_model(WeatherConfig) == []


def test_mod_45_model_check_rules() -> None:
    from pydantic import BaseModel

    class NotModuleConfig(BaseModel):
        x: int = Field(default=1, title="X")

    problems = check_config_model(NotModuleConfig)
    assert {(p.rule, p.field) for p in problems} == {("MOD-45", "")} | {("MOD-45", "")}
    assert len(problems) == 2  # not a ModuleConfig, extra not forbidden


def test_mod_46_titles_and_descriptions_are_catalog_keys() -> None:
    class Sub(ModuleConfig):
        z: int = Field(default=0, title="Z", description="not a key")

    class Bad(ModuleConfig):
        a: int = 1
        b: int = Field(default=1, title="Lowercase title")
        sub: Sub = Sub()

    found = {(p.rule, p.field) for p in check_config_model(Bad)}
    assert found == {("MOD-46", "a"), ("MOD-46", "b"), ("MOD-46", "sub"), ("MOD-46", "sub.z")}


def test_mod_47_secret_rules() -> None:
    class Secrets(ModuleConfig):
        token: SecretStr = Field(title="TOKEN")
        flagged: str = Field(default="", title="FLAGGED", json_schema_extra={"secret": True})
        api_key: str = Field(default="", title="API_KEY")

    found = {(p.field, p.severity) for p in check_config_model(Secrets) if p.rule == "MOD-47"}
    assert found == {("token", "error"), ("flagged", "error"), ("api_key", "warning")}


def test_mod_49_model_constraints_reported_while_editing(
    tmp_path: Path, registry: Registry
) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: weather\n    position: top_left\n"
        "    config:\n      api_key: k\n      lon: 500\n",
        registry,
    )
    (rec,) = result.errors
    assert (rec.code, rec.path, rec.line) == ("CORE_CFG_RANGE", "/modules/0/config/lon", 6)
    assert rec.params == {"le": 180.0}


def test_core_206_union_labels_never_reach_pointers(tmp_path: Path, registry: Registry) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: clock\n    position: {col: 1, row: 1, width: 0}\n"
        "    appearance:\n      header: {'bad tag': x}\n",
        registry,
    )
    assert {(r.code, r.path) for r in result.errors} == {
        ("CORE_CFG_RANGE", "/modules/0/position/width"),
        ("CORE_CFG_REQUIRED", "/modules/0/position/height"),
        ("CORE_CFG_LOCALE", "/modules/0/appearance/header"),
    }


def test_core_30_run_starts_valid_config_until_sigterm(
    tmp_path: Path, registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    import signal
    import threading

    cfg = write(tmp_path / "config.yaml", "modules:\n  - module: future\n")
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        assert run(cfg, tmp_path / "none.env", registry=registry) == 0
    finally:
        timer.cancel()
    out = capsys.readouterr().out
    assert "config_warning" in out
    assert "CORE_CFG_MODULE_UNAVAILABLE" in out


def test_core_32_module_defaults_then_yaml_and_unknown_keys(
    tmp_path: Path, registry: Registry
) -> None:
    result = load(
        tmp_path,
        "modules:\n  - module: weather\n    position: top_left\n"
        "    config: {api_key: k, lon: 12}\n",
        registry,
    )
    cfg = result.instances[0].config
    assert isinstance(cfg, WeatherConfig)
    assert (cfg.lat, cfg.lon, cfg.every) == (0.0, 12.0, timedelta(minutes=10))
    bad = load(
        tmp_path,
        "modules:\n  - module: clock\n    position: top_left\n    config: {size: 3}\n",
        registry,
    )
    assert [(r.code, r.path) for r in bad.errors] == [
        ("CORE_CFG_UNKNOWN_KEY", "/modules/0/config/size")
    ]
