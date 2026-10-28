"""Milestone 2, part D: configuration history, undo, redo and restore."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import Field, SecretStr

from conftest import EngineFactory
from mm3.cli.config_cmd import run_config
from mm3.cli.main import build_parser
from mm3.config import ModuleConfig
from mm3.core.config import load_config
from mm3.core.config.history import MASK, ConfigHistory, HistoryRecorder
from mm3.core.config.manager import ConfigManager
from mm3.core.registry import Registry
from mm3.module import MirrorModule


class SecretConfig(ModuleConfig):
    api_key: SecretStr = Field(
        default=SecretStr(""), title="API_KEY", json_schema_extra={"secret": True}
    )
    lat: float = Field(default=0.0, title="LAT")


class Clock(MirrorModule):
    name = "clock"
    element = "mm3-clock"


class Weather(MirrorModule):
    name = "weather"
    element = "mm3-weather"
    config_model = SecretConfig


class Now:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


def registry(*classes: type[MirrorModule]) -> Registry:
    reg = Registry()
    for cls in classes or (Clock, Weather):
        reg.add(cls)
    return reg


class Setup:
    def __init__(self, root: Path, reg: Registry | None = None) -> None:
        self.etc = root / "etc"
        self.etc.mkdir(exist_ok=True)
        self.state = root / "state"
        self.config = self.etc / "config.yaml"
        self.env = self.etc / "mm3.env"
        self.now = Now()
        self.registry = reg or registry()
        self.history = self.make()

    def make(self, reg: Registry | None = None) -> ConfigHistory:
        return ConfigHistory(
            self.config,
            env_file=self.env,
            registry=reg or self.registry,
            state_dir=self.state,
            environ={},
            now=self.now,
        )

    def save(self, text: str, rel: str = "config.yaml") -> int:
        (self.etc / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.etc / rel).write_text(text)
        with self.history.store.lock():
            meta = self.history.record_disk()
        assert meta is not None
        return meta.version

    def text(self, rel: str = "config.yaml") -> str:
        return (self.etc / rel).read_text()


def cfg(locale: str, extra: str = "") -> str:
    return f"engine:\n  locale: {locale}  # keep me\n{extra}"


@pytest.fixture
def s(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


# --- CORE-153: versions ---------------------------------------------------------------


def test_core_153_initial_version_holds_the_whole_set(s: Setup) -> None:
    (s.etc / "widget-templates").mkdir()
    (s.etc / "widget-templates" / "room.yaml").write_text("x: 1\n")
    (s.etc / "mods.yaml").write_text("- module: clock\n  position: top_left\n")
    s.env.write_text("SECRET=1\n")
    v = s.save("engine: {locale: it}\nmodules: !include mods.yaml\n")
    assert v == 1
    meta = s.history.store.meta(1)
    assert (meta.origin, meta.author, meta.parent, meta.sections) == ("initial", "file", None, [])
    assert meta.time == "2026-10-28T12:00:00Z"
    files = s.history.store.read_set(1).files
    assert set(files) == {"config.yaml", "mods.yaml", "widget-templates/room.yaml"}
    assert len(meta.sha256) == 64
    vdir = s.state / "config-history" / "000001"
    assert stat.S_IMODE(vdir.stat().st_mode) == 0o750
    assert stat.S_IMODE((vdir / "meta.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((vdir / "files" / "config.yaml").stat().st_mode) == 0o640


def test_core_153_append_only_with_parents_and_sections(s: Setup) -> None:
    s.save(cfg("it"))
    first = s.history.store.read_set(1)
    assert s.save(cfg("de")) == 2
    meta = s.history.store.meta(2)
    assert (meta.origin, meta.parent, meta.sections) == ("file", 1, ["engine"])
    assert s.history.store.read_set(1) == first


def test_core_153_invalid_files_are_never_recorded(s: Setup) -> None:
    s.save(cfg("it"))
    s.config.write_text("engine: {threads: 0}\n")
    with s.history.store.lock():
        assert s.history.record_disk() is None
    assert s.history.store.versions() == [1]


# --- CORE-154, CORE-150: undo and redo --------------------------------------------------


def test_core_154_undo_redo_example_of_the_spec(s: Setup) -> None:
    for loc in ("it", "de", "fr"):
        s.save(cfg(loc))

    def step(op: str) -> tuple[int | None, str, int | None, int | None]:
        res = getattr(s.history, op)("cli:test")
        assert res.code == 0, res.message
        meta = s.history.store.meta(res.version)
        return res.version, s.text().split()[2], meta.parent, meta.restores

    assert step("undo") == (4, "de", 1, 2)
    assert step("undo") == (5, "it", None, 1)
    assert step("redo") == (6, "de", 1, 2)
    assert step("redo") == (7, "fr", 2, 3)
    assert s.history.redo("cli:test").code == 1
    assert [s.history.store.meta(v).origin for v in range(4, 8)] == ["undo", "undo", "redo", "redo"]


def test_core_154_any_other_save_ends_the_redo_chain(s: Setup) -> None:
    s.save(cfg("it"))
    s.save(cfg("de"))
    assert s.history.undo("cli:t").code == 0
    s.save(cfg("es"))
    assert s.history.redo("cli:t").code == 1


def test_core_154_nothing_to_undo(s: Setup) -> None:
    s.save(cfg("it"))
    res = s.history.undo("cli:t")
    assert (res.code, res.version) == (1, None)


def test_core_150_undo_after_saving_and_comments_survive(s: Setup) -> None:
    s.save(cfg("it"))
    s.save(cfg("de"))
    assert s.history.undo("cli:t").code == 0
    assert s.text() == cfg("it")


# --- CORE-155: section restore --------------------------------------------------------


def test_core_155_section_restore_into_the_file_holding_it(s: Setup) -> None:
    (s.etc / "engine.yaml").write_text("locale: it\n")
    s.save("# main comment\nengine: !include engine.yaml\nlayout: {gap: 8}\n")
    (s.etc / "engine.yaml").write_text("locale: de\n")
    s.save("# main comment\nengine: !include engine.yaml\nlayout: {gap: 4}\n")
    res = s.history.restore(1, "cli:t", section="engine")
    assert res.code == 0, res.message
    assert s.text("engine.yaml") == "locale: it\n"
    assert "gap: 4" in s.text()
    assert s.text().startswith("# main comment\n")
    assert s.history.store.meta(res.version).sections == ["engine"]
    assert s.history.store.meta(res.version).origin == "restore"


def test_core_155_instance_restore_and_append(s: Setup) -> None:
    base = "modules:\n  - module: clock\n    position: top_left\n"
    weather = "  - module: weather\n    position: top_right\n    config: {lat: 1}\n"
    s.save(base + weather)
    s.save(base + weather.replace("lat: 1", "lat: 2"))
    assert s.history.restore(1, "cli:t", section="modules/weather").code == 0
    assert "lat: 1" in s.text()
    s.save(base)
    assert s.history.restore(1, "cli:t", section="modules/weather").code == 0
    result = load_config(s.config, registry=s.registry, env_file=s.env, environ={})
    assert [i.id for i in result.instances] == ["clock", "weather"]
    assert s.history.restore(1, "cli:t", section="modules/nope").code == 2


def test_core_155_pages_and_order(s: Setup) -> None:
    mods = (
        "modules:\n  - module: clock\n    position: top_left\n"
        "  - module: weather\n    position: top_right\n"
    )
    s.save("pages:\n  list: [{id: home}]\n" + mods)
    swapped = (
        "modules:\n  - module: weather\n    position: top_right\n"
        "  - module: clock\n    position: top_left\n"
    )
    s.save("pages:\n  list: [{id: home}, {id: energy}]\n" + swapped)
    assert s.history.store.meta(2).sections == ["pages", "modules/_order"]
    assert s.history.restore(1, "cli:t", section="pages").code == 0
    assert "energy" not in s.text()
    assert s.history.restore(1, "cli:t", section="modules/_order").code == 0
    result = load_config(s.config, registry=s.registry, env_file=s.env, environ={})
    assert [i.id for i in result.instances] == ["clock", "weather"]


def test_core_155_restore_all_and_templates(s: Setup) -> None:
    (s.etc / "widget-templates").mkdir()
    (s.etc / "widget-templates" / "a.yaml").write_text("a: 1\n")
    s.save(cfg("it"))
    (s.etc / "widget-templates" / "a.yaml").write_text("a: 2\n")
    (s.etc / "widget-templates" / "b.yaml").write_text("b: 1\n")
    s.save(cfg("de"))
    assert s.history.store.meta(2).sections[0] == "engine"
    assert s.history.restore(1, "cli:t", section="widget_templates/a.yaml").code == 0
    assert s.text("widget-templates/a.yaml") == "a: 1\n"
    assert s.history.restore(1, "cli:t").code == 0
    assert s.text() == cfg("it")
    assert not (s.etc / "widget-templates" / "b.yaml").exists()


# --- CORE-156: validation before applying ---------------------------------------------


def test_core_156_invalid_restore_applies_nothing(s: Setup) -> None:
    s.save("modules:\n  - module: weather\n    position: top_left\n")
    s.save(cfg("it"))
    before = s.text()
    without_weather = s.make(registry(Clock))
    res = without_weather.restore(1, "cli:t")
    assert res.code == 2
    assert [r.code for r in res.records] == ["CORE_CFG_MODULE_MISSING"]
    assert str(s.etc) in (res.records[0].file or "")
    assert s.text() == before
    assert without_weather.store.versions() == [1, 2]


# --- CORE-216: sections ---------------------------------------------------------------


def test_core_216_formatting_only_is_a_version_without_sections(s: Setup) -> None:
    s.save(cfg("it"))
    assert s.save("# new comment\n" + cfg("it")) == 2
    assert s.history.store.meta(2).sections == []


# --- CORE-217: lock, atomic writes, same hash ------------------------------------------


def test_core_217_same_set_no_new_version_and_mode_kept(s: Setup) -> None:
    s.save(cfg("it"))
    s.save(cfg("de"))
    with s.history.store.lock():
        assert s.history.record_disk() is not None
    assert s.history.store.versions() == [1, 2]
    assert (s.state / "config-history" / ".lock").exists()
    os.chmod(s.config, 0o600)
    assert s.history.undo("cli:t").code == 0
    assert stat.S_IMODE(s.config.stat().st_mode) == 0o600
    assert not [p for p in s.etc.iterdir() if p.name.startswith(".config.yaml.")]


async def test_core_217_engine_applies_cli_result_without_new_version(
    make_engine: EngineFactory, tmp_path: Path
) -> None:
    s = Setup(tmp_path, registry(Clock, Weather))
    s.config.write_text(cfg("it"))
    engine = make_engine(Clock, Weather)
    result = load_config(s.config, registry=engine.registry, env_file=s.env, environ={})
    history = s.make(engine.registry)
    manager = ConfigManager(
        engine,
        result,
        registry=engine.registry,
        env_file=s.env,
        recorder=HistoryRecorder(history),
        environ={},
    )
    await engine.start(result.instance_specs())
    await manager.start(watch=False)
    assert manager.version == 1
    s.config.write_text(cfg("de"))
    assert await manager.reload("file")
    assert manager.version == 2
    assert history.undo("cli:t").version == 3
    assert await manager.reload("file")
    assert manager.version == 3
    assert engine.supervisor.settings.locale == "it"
    assert history.store.versions() == [1, 2, 3]
    s.config.write_text("# only a comment\n" + cfg("it"))
    assert await manager.reload("file") is False
    assert manager.version == 4
    await manager.close()


def test_core_217_refuses_invalid_disk_unless_forced(s: Setup) -> None:
    s.save(cfg("it"))
    s.save(cfg("de"))
    s.config.write_text("engine: {threads: 0}\n")
    res = s.history.undo("cli:t")
    assert res.code == 3
    assert s.text() == "engine: {threads: 0}\n"
    forced = s.history.undo("cli:t", force=True)
    assert forced.code == 0
    assert s.text() == cfg("it")


# --- CORE-218: retention and masking --------------------------------------------------


def test_core_218_retention(s: Setup) -> None:
    s.now.t = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(12):
        s.save(cfg("it", f"  threads: {i + 1}\n  history: {{keep: 10}}\n"))
    s.now.t += timedelta(days=200)
    s.save(cfg("de", "  history: {keep: 10}\n"))
    versions = s.history.store.versions()
    assert versions == list(range(4, 14))
    s2 = s.history.store
    s.now.t += timedelta(days=1)
    s2.prune(10)
    assert s2.versions()[-1] == 13


def test_core_218_young_versions_are_kept(s: Setup) -> None:
    for i in range(12):
        s.save(cfg("it", f"  threads: {i + 1}\n  history: {{keep: 10}}\n"))
    assert len(s.history.store.versions()) == 12


def test_core_218_diff_masks_secrets_and_shows_env_as_written(s: Setup) -> None:
    s.save(
        "engine:\n  locale: it\nmodules:\n  - module: weather\n"
        "    position: top_left\n    config: {api_key: !env [KEY, k1], lat: 1}\n"
    )
    s.save(
        "engine:\n  locale: de\nmodules:\n  - module: weather\n"
        "    position: top_left\n    config: {api_key: hard-coded, lat: 2}\n"
    )
    changes = s.history.diff(1, 2)
    text = json.dumps(changes)
    assert "hard-coded" not in text
    assert MASK in changes["modules/weather"][1]
    assert "!env [KEY, k1]" in changes["modules/weather"][0]
    assert set(changes) == {"engine", "modules/weather"}
    assert set(s.history.diff(1, 2, section="engine")) == {"engine"}


# --- CORE-219: CLI --------------------------------------------------------------------


def cli(s: Setup, *argv: str) -> tuple[int, str, str]:
    args: argparse.Namespace = build_parser().parse_args(
        ["config", argv[0], "--config", str(s.config), "--env-file", str(s.env), *argv[1:]]
    )
    out, err = io.StringIO(), io.StringIO()
    code = run_config(args, registry=s.registry, state_dir=s.state, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_core_219_commands_and_exit_codes(s: Setup) -> None:
    s.save(cfg("it"))
    assert cli(s, "undo")[0] == 1
    s.save(cfg("de"))
    assert cli(s, "undo") == (0, "3\n", "")
    assert cli(s, "redo") == (0, "4\n", "")
    code, out, _ = cli(s, "history", "--limit", "2")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("4\t")
    assert "\tredo\tcli:" in lines[0]
    assert len(lines) == 2
    code, out, _ = cli(s, "history", "--json")
    assert [v["version"] for v in json.loads(out)] == [4, 3, 2, 1]
    code, out, _ = cli(s, "diff", "1", "2")
    assert code == 0
    assert "-locale: it" in out
    assert "+locale: de" in out
    code, out, _ = cli(s, "diff", "1", "2", "--json")
    assert set(json.loads(out)["sections"]) == {"engine"}
    assert cli(s, "diff", "1", "99")[0] == 2
    assert cli(s, "restore", "1", "--section", "engine")[1] == "5\n"
    s.config.write_text("engine: {threads: 0}\n")
    code, _, err = cli(s, "undo")
    assert code == 3
    assert "--force" in err
    assert cli(s, "undo", "--force")[0] == 0


def test_core_219_validation_failure_exit_2(s: Setup) -> None:
    s.save("modules:\n  - module: weather\n    position: top_left\n")
    s.save(cfg("it"))
    s.registry = registry(Clock)
    code, _, err = cli(s, "restore", "1")
    assert code == 2
    assert "CORE_CFG_MODULE_MISSING" not in err  # message text, not codes
    assert "not installed" in err


# --- custom.css in the config set (CORE-153, CORE-155, CORE-216) ----------------------


def test_core_153_custom_css_is_part_of_the_set(s: Setup) -> None:
    (s.etc / "custom.css").write_text("body { --mm3-bg: #000; }\n")
    v = s.save(cfg("it"))
    stored = s.history.store.read_set(v)
    assert stored.files["custom.css"] == b"body { --mm3-bg: #000; }\n"


def test_core_216_custom_css_is_a_section(s: Setup) -> None:
    s.save(cfg("it"))
    (s.etc / "custom.css").write_text("body { color: red; }\n")
    v = s.save(cfg("it"))
    assert s.history.store.meta(v).sections == ["custom_css"]
    changes = s.history.diff(1, v)
    assert set(changes) == {"custom_css"}
    assert "color: red" in changes["custom_css"][1]


def test_core_155_restore_custom_css_alone(s: Setup) -> None:
    s.save(cfg("it"))
    (s.etc / "custom.css").write_text("body { color: red; }\n")
    s.save(cfg("de"))
    (s.etc / "custom.css").write_text("body { color: blue; }\n")
    s.save(cfg("de"))
    res = s.history.restore(2, "cli:t", section="custom_css")
    assert res.code == 0, res.message
    assert s.text("custom.css") == "body { color: red; }\n"
    assert "locale: de" in s.text()
    assert s.history.store.meta(res.version).sections == ["custom_css"]
    # Restoring from a version without custom.css removes the file.
    res = s.history.restore(1, "cli:t", section="custom_css")
    assert res.code == 0, res.message
    assert not (s.etc / "custom.css").exists()
    assert "locale: de" in s.text()
