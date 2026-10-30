"""``mm3 module check``: catalog, static stage, suppressions, report formats and exit codes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from hatchling.builders.wheel import WheelBuilder
from packaging.specifiers import SpecifierSet

import mm3.core.modcheck.build as build_mod
from mm3.cli.main import main
from mm3.core.modcheck.catalog import CATALOG
from mm3.core.modcheck.project import ToolError, load_project
from mm3.core.modcheck.report import Check, Finding, Report, details, summary, to_junit
from mm3.core.modcheck.runner import run_check
from mm3.core.modcheck.static import bounded_to_one_major, run_static
from module_projects import chromium_available, edit, write_greeting

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ONLY = {"MOD-96", "MOD-97", "MOD-98", "MOD-99"}


def contract_rows() -> dict[str, tuple[str, str]]:
    """Section 11 of the Contract: id to (level, checked by)."""
    text = (ROOT / "docs/spec/contract.md").read_text(encoding="utf-8")
    section = text.split("## 11. Compliance checklist")[1].split("## 12.")[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"MOD-\d+", cells[0]):
            rows[cells[0]] = (cells[2], cells[3])
    return rows


def test_core_281_catalog_matches_section_11_of_the_contract() -> None:
    rows = contract_rows()
    assert set(CATALOG) == set(rows) - CATALOG_ONLY
    for mod_id, row in CATALOG.items():
        level, checked_by = rows[mod_id]
        assert row.level == level.split(" /")[0]
        words = checked_by.lower()
        for category, needle in (
            ("static", "static"),
            ("kit", "test kit"),
            ("harness", "harness"),
            ("review", "review"),
        ):
            assert (category in row.categories) == (needle in words), (mod_id, category)


@pytest.mark.parametrize(
    ("spec", "ok"),
    [
        (">=1.0,<2", True),
        (">=1.2.3,<2.0.0", True),
        ("~=1.0", True),
        ("==1.*", True),
        (">=1.0", False),
        (">=1.0,<3", False),
        ("<2", False),
        (">=1.0,<=1.9", True),
        (">=2.0,<2.5", True),
        (">=1.0,<2.5", False),
    ],
)
def test_mod_03_bounded_to_one_major(spec: str, ok: bool) -> None:
    assert bounded_to_one_major(SpecifierSet(spec)) is ok


# --- static stage on a project built with hatchling -------------------------------------


def build(proj: Path, tmp: Path) -> Path:
    """Build the wheel as ``uv build`` would, with the mm3-i18n hook, no network."""
    [wheel] = WheelBuilder(str(proj)).build(directory=str(tmp / "dist"), versions=["standard"])
    return Path(wheel)


def static(proj: Path, tmp: Path, *, wheel: bool = True) -> dict[str, Check]:
    mod = load_project(proj)
    if wheel:
        extract = tmp / "wheel"
        extract.mkdir(exist_ok=True)
        mod.attach_wheel(build(proj, tmp), extract)
    checks, _ = run_static(mod)
    return {c.id: c for c in checks}


def statuses(checks: dict[str, Check]) -> dict[str, str]:
    return {i: c.status for i, c in checks.items() if c.status != "pass"}


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    return write_greeting(tmp_path)


def pkg(proj: Path) -> Path:
    return proj / "src" / "mm3_greeting"


def test_core_281_reference_module_passes_every_static_check(proj: Path, tmp_path: Path) -> None:
    checks = static(proj, tmp_path)
    assert statuses(checks) == {}
    assert len(checks) == sum("static" in r.categories for r in CATALOG.values())


def test_core_281_static_stage_without_a_wheel_skips_wheel_checks(
    proj: Path, tmp_path: Path
) -> None:
    assert statuses(static(proj, tmp_path, wheel=False)) == {}


STATIC = Path("src/mm3_greeting/static")
JS = STATIC / "greeting.js"
CSS = STATIC / "greeting.css"


def po(lang: str) -> Path:
    return Path(f"src/mm3_greeting/locales/{lang}/LC_MESSAGES/messages.po")


def py_edit(old: str, new: str) -> tuple[Path, str, str]:
    return (Path("src/mm3_greeting/__init__.py"), old, new)


MUTATIONS = {
    "MOD-01": (Path("pyproject.toml"), 'name = "mm3-greeting"', 'name = "greeting"'),
    "MOD-02": (Path("pyproject.toml"), 'version = "1.0.0"', 'version = "1.0"'),
    "MOD-03": (Path("pyproject.toml"), "mm3-core>=1.0,<2", "mm3-core>=1.0"),
    "MOD-04": (Path("pyproject.toml"), "greeting = ", "hello = "),
    "MOD-05": (Path("pyproject.toml"), "[tool.hatch.build.hooks.mm3-i18n]\n", ""),
    "MOD-11": py_edit("async def start", "def start"),
    "MOD-12": py_edit(
        "        await self._publish()",
        "        import asyncio\n        asyncio.create_task(self._publish())",
    ),
    "MOD-17": py_edit(
        "        await self._publish()", "        raise TransientError('NO_SUCH_KEY')"
    ),
    "MOD-23": py_edit("from pydantic", "import httpx\nfrom pydantic"),
    "MOD-28": py_edit(
        "hour = self.ctx.now().hour", "import time\n        hour = int(time.time()) % 24"
    ),
    "MOD-29": py_edit(
        "        await self._publish()", "        await self.ctx.send_notification('GREETING_SENT')"
    ),
    "MOD-40": py_edit(
        "        await self._publish()", "        await self.ctx.send_notification('greeting_sent')"
    ),
    "MOD-45": py_edit("(ModuleConfig):", "(object):"),
    "MOD-46": py_edit('title="CFG_NAME"', 'title="Name"'),
    "MOD-47": py_edit(
        "    name: LocalizedText",
        "    token: SecretStr = SecretStr('')\n    name: LocalizedText",
    ),
    "MOD-52": py_edit("    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)\n", ""),
    "MOD-61": py_edit(
        'self.ctx.set_state({"part": part})', 'self.ctx.set_state({"p": "Not here"})'
    ),
    "MOD-70": py_edit(
        "        await self._publish()", "        await self.ctx.http.get('https://x')"
    ),
    "MOD-80": py_edit("        await self._publish()", "        print('started')"),
    "MOD-86": py_edit(
        "        await self._publish()",
        "        try:\n            await self._publish()\n"
        "        except Exception:\n            pass",
    ),
    "MOD-30": (JS, '"mm3-greeting", G', '"mm3-other", G'),
    "MOD-31": (JS, "    if (!state.part)", "    this.innerHTML = state.x;\n    if (!state.part)"),
    "MOD-32": (JS, "    if (!state.part)", "    fetch('/x');\n    if (!state.part)"),
    "MOD-33": (CSS, ".greeting", "@font-face { font-family: x; }\n.greeting"),
    "MOD-35": (JS, "    if (!state.part)", "    setInterval(f, 1);\n    if (!state.part)"),
    "MOD-37": (JS, '"/core/mm3.js"', '"/core/mm3.js";\nimport "lit"'),
    "MOD-62": (po("de"), 'msgid "HEADER"\nmsgstr "[de] Greeting"\n\n', ""),
    "MOD-60": (po("en"), 'msgid "HEADER"', 'msgid "Header text"'),
    "MOD-63": (po("en"), "Optional name", "{count} names"),
    "MOD-67": (po("en"), 'msgid "HEADER"\nmsgstr "Greeting"\n\n', ""),
}


# Rows that report a warning even though the row is a MUST (Contract 11: "static (warning)",
# "static (heuristic)").
WARNING_ONLY = {"MOD-63", "MOD-86"}
# A defect may legitimately trip a related row, but never an unrelated one.
RELATED = {
    "MOD-01": {"MOD-04"},
    "MOD-04": {"MOD-05"},
    "MOD-05": {"MOD-62"},
    "MOD-29": {"MOD-40"},
    "MOD-40": {"MOD-29"},
    "MOD-45": {"MOD-46"},
    "MOD-60": {"MOD-17", "MOD-46", "MOD-62"},
    "MOD-62": {"MOD-17", "MOD-46"},
    "MOD-63": {"MOD-62"},
}


@pytest.mark.parametrize("mod_id", sorted(MUTATIONS))
def test_core_281_one_defect_fails_its_row(mod_id: str, proj: Path, tmp_path: Path) -> None:
    rel, old, new = MUTATIONS[mod_id]
    edit(proj / rel, old, new)
    if mod_id == "MOD-17":
        edit(pkg(proj) / "__init__.py", "from mm3 import", "from mm3 import TransientError,")
    checks = static(proj, tmp_path)
    if mod_id in WARNING_ONLY or CATALOG[mod_id].level == "SHOULD":
        assert checks[mod_id].status == "warn", checks[mod_id]
    else:
        assert checks[mod_id].status == "fail", checks[mod_id]
    failed = {i for i, s in statuses(checks).items() if s == "fail"} - {mod_id}
    assert failed <= RELATED.get(mod_id, set()), failed


# --- suppressions -----------------------------------------------------------------------


def test_core_281_allow_comment_silences_a_should_finding(proj: Path, tmp_path: Path) -> None:
    init = pkg(proj) / "__init__.py"
    edit(init, "    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)\n", "")
    edit(
        init,
        "class GreetingModule",
        "# mm3: allow MOD-52 sized by its grid cell\nclass GreetingModule",
    )
    check = static(proj, tmp_path)["MOD-52"]
    assert check.status == "suppressed"
    assert check.findings[0].suppressed == "sized by its grid cell"
    assert check.findings[0].as_dict()["suppressed"] == "sized by its grid cell"


def test_core_281_allow_comment_needs_a_reason(proj: Path, tmp_path: Path) -> None:
    init = pkg(proj) / "__init__.py"
    edit(init, "    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)\n", "")
    edit(init, "class GreetingModule", "# mm3: allow MOD-52\nclass GreetingModule")
    check = static(proj, tmp_path)["MOD-52"]
    assert check.status == "warn"
    assert "needs a reason" in check.findings[0].message


def test_core_281_allow_comment_has_no_effect_on_a_must(proj: Path, tmp_path: Path) -> None:
    init = pkg(proj) / "__init__.py"
    edit(
        init,
        "hour = self.ctx.now().hour",
        "import time\n        hour = int(time.time()) % 24  # mm3: allow MOD-28 sure",
    )
    check = static(proj, tmp_path)["MOD-28"]
    assert check.status == "fail"
    assert "no effect on a MUST" in check.findings[0].message


def test_core_281_allow_comment_in_javascript_uses_slashes() -> None:
    from mm3.core.modcheck.report import apply_suppressions

    check = Check("MOD-52", "SHOULD", "static", findings=[Finding("x", "static/a.js", 2)])
    apply_suppressions([check], {"static/a.js": "const a = 1;\nlet b; // mm3: allow MOD-52 fine\n"})
    assert check.status == "suppressed"
    other = Check("MOD-52", "SHOULD", "static", findings=[Finding("x", "static/a.js", 2)])
    apply_suppressions([other], {"static/a.js": "// mm3: allow MOD-44 other id\nlet b;\n"})
    assert other.status == "warn"


# --- report -----------------------------------------------------------------------------


def sample_report() -> Report:
    checks = [
        Check("MOD-01", "MUST", "static"),
        Check("MOD-28", "MUST", "static", findings=[Finding("wall clock", "m.py", 4)]),
        Check("MOD-52", "SHOULD", "static", findings=[Finding("no size", "m.py", 1)]),
        Check("MOD-10", "MUST", "kit"),
        Check("MOD-19", "SHOULD", "review", findings=[Finding("Confirm", severity="warn")]),
    ]
    for c in checks:
        c.settle()
    return Report("0.0.1", "mm3-demo", "1.0.0", "1.0", checks, ["static", "kit"], ["a note"])


def test_core_281_report_json_has_the_documented_shape() -> None:
    data = sample_report().as_dict()
    assert set(data) == {
        "tool_version", "module", "version", "api_version", "result", "stages", "checks", "notes",
    }  # fmt: skip
    assert data["result"] == "non_compliant"
    assert set(data["checks"][1]) == {"id", "level", "category", "status", "findings"}
    assert data["checks"][1]["findings"] == [{"file": "m.py", "line": 4, "message": "wall clock"}]
    assert [c["status"] for c in data["checks"]] == ["pass", "fail", "warn", "pass", "review"]


def test_core_281_result_is_compliant_without_a_must_failure() -> None:
    report = sample_report()
    report.checks = [c for c in report.checks if c.id != "MOD-28"]
    assert report.result == "compliant"


def test_core_281_summary_and_details_text() -> None:
    report = sample_report()
    text = summary(report)
    assert "Static      1 passed, 1 failed, 1 warnings" in text
    assert "Test kit    1 passed, 0 failed" in text
    assert "Harness     not run" in text
    assert "Review      1 items to confirm" in text
    assert text.endswith("Result      NOT COMPLIANT (API 1.0)")
    assert details(report) == (
        "FAIL MOD-28 [static] m.py:4 wall clock\nwarn MOD-52 [static] m.py:1 no size"
    )


def test_core_281_junit_has_one_case_per_check() -> None:
    root = ET.fromstring(to_junit(sample_report()))  # noqa: S314
    suite = root.find("testsuite")
    assert suite is not None
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("5", "1", "1")
    cases = {c.get("name"): c for c in suite.findall("testcase")}
    assert cases["MOD-28"].find("failure") is not None
    assert cases["MOD-19"].find("skipped") is not None
    assert cases["MOD-01"].find("failure") is None


# --- tool errors (exit code 2) ----------------------------------------------------------


def test_core_281_project_without_pyproject_is_a_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match=r"no pyproject\.toml"):
        load_project(tmp_path)


def test_core_281_missing_uv_is_a_tool_error(proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_mod.shutil, "which", lambda _name: None)
    with pytest.raises(ToolError, match="uv is not on PATH"):
        run_check(proj, "static")


def test_core_279_missing_testing_extra_stops_the_check_with_exit_2(
    proj: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mm3.core.modcheck.runner as runner

    monkeypatch.setattr(runner, "missing_testing_modules", lambda: ["playwright"])
    assert main(["module", "check", str(proj), "--only", "kit"]) == 2
    assert "pip install 'mm3-core[testing]'" in capsys.readouterr().err


# --- end to end with uv -----------------------------------------------------------------

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
needs_chromium = pytest.mark.skipif(
    not chromium_available(), reason="Chromium cannot start (set MM3_CHROMIUM)"
)


@pytest.fixture(scope="session")
def find_links(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory with the mm3-build wheel, which is not on an index yet."""
    out = tmp_path_factory.mktemp("links")
    done = subprocess.run(  # noqa: S603 - fixed arguments
        [
            str(shutil.which("uv")),
            "build",
            "--package",
            "mm3-build",
            "--wheel",
            "--out-dir",
            str(out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return out


@pytest.fixture
def uv_env(find_links: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV_FIND_LINKS", str(find_links))
    monkeypatch.delenv("UV_OFFLINE", raising=False)
    assert sys.executable  # the venv is made for this interpreter


@needs_uv
@pytest.mark.usefixtures("uv_env")
@needs_chromium
def test_core_281_compliant_module_passes_all_stages(
    proj: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junit = tmp_path / "junit.xml"
    assert main(["module", "check", str(proj), "--junit", str(junit)]) == 0
    out = capsys.readouterr().out
    assert "Static      36 passed, 0 failed, 0 warnings" in out
    assert "Test kit    17 passed, 0 failed" in out
    assert "Harness     6 passed, 0 failed" in out
    assert "Result      COMPLIANT (API 1.0)" in out
    report = json.loads((proj / ".mm3" / "check-report.json").read_text())
    assert report["result"] == "compliant"
    assert report["stages"] == ["static", "kit", "harness"]
    assert report["module"] == "mm3-greeting"
    assert report["version"] == "1.0.0"
    assert {c["category"] for c in report["checks"]} == {"static", "kit", "harness", "review"}
    assert ET.parse(junit).getroot().find("testsuite") is not None  # noqa: S314


@needs_uv
@pytest.mark.usefixtures("uv_env")
def test_core_281_static_only_prints_json_and_exits_1_on_a_must(
    proj: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edit(
        pkg(proj) / "__init__.py",
        "hour = self.ctx.now().hour",
        "import time\n        hour = int(time.time()) % 24",
    )
    assert main(["module", "check", str(proj), "--only", "static", "--format", "json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["result"] == "non_compliant"
    assert data["stages"] == ["static"]
    [bad] = [c for c in data["checks"] if c["status"] == "fail"]
    assert bad["id"] == "MOD-28"
    assert bad["findings"][0]["file"] == "src/mm3_greeting/__init__.py"


@needs_uv
@needs_chromium  # the default run includes the harness stage, which needs a browser
@pytest.mark.usefixtures("uv_env")
def test_core_281_kit_finds_what_the_static_stage_cannot(
    proj: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init = (
        "    def __init__(self, ctx, config):\n"
        "        super().__init__(ctx, config)\n"
        "        open(__file__).close()\n\n"
        "    async def start(self)"
    )
    edit(pkg(proj) / "__init__.py", "    async def start(self)", init)
    assert main(["module", "check", str(proj)]) == 1
    out = capsys.readouterr().out
    assert "FAIL MOD-10 [kit]" in out
    assert "Static      36 passed, 0 failed" in out


@needs_uv
@pytest.mark.usefixtures("uv_env")
def test_core_281_module_that_cannot_be_loaded_fails_the_kit_rows(
    proj: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edit(
        pkg(proj) / "__init__.py",
        "from __future__ import annotations",
        "from __future__ import annotations\n\nraise RuntimeError('boom')",
    )
    assert main(["module", "check", str(proj)]) == 1
    out = capsys.readouterr().out
    assert "not run: the module cannot be loaded" in out
    assert "boom" in out


@needs_uv
@pytest.mark.usefixtures("uv_env")
def test_core_281_a_failing_build_fails_mod_05_not_the_tool(
    proj: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    po = pkg(proj) / "locales" / "it" / "LC_MESSAGES" / "messages.po"
    po.write_text('msgid "HELLO"\nnot a po line\n', encoding="utf-8")
    assert main(["module", "check", str(proj), "--only", "static"]) == 1
    assert "FAIL MOD-05 [static] the wheel cannot be built" in capsys.readouterr().out


def test_mod_53_64_65_81_82_rows_for_a_person_are_on_the_review_list() -> None:
    from mm3.core.modcheck.report import review_checks

    listed = {c.id for c in review_checks()}
    assert {"MOD-53", "MOD-64", "MOD-65", "MOD-81", "MOD-82"} <= listed


def test_core_281_compiled_catalog_does_not_depend_on_the_clock(tmp_path: Path) -> None:
    """A .po without dates compiles to the same bytes a minute later (flaky MOD-62)."""
    import datetime as dt
    import types
    from unittest import mock

    import babel.messages.catalog as babel_catalog

    from mm3_build.catalogs import compiled

    po = tmp_path / "en" / "LC_MESSAGES" / "messages.po"
    po.parent.mkdir(parents=True)
    po.write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: en\\n"\n\n'
        'msgid "A"\nmsgstr "a"\n',
        encoding="utf-8",
    )
    first = compiled(po)[0]

    class Later(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> Later:  # type: ignore[override]
            return super().now(tz) + dt.timedelta(minutes=1)  # type: ignore[return-value]

    shifted = types.SimpleNamespace(**{**vars(dt), "datetime": Later})
    with mock.patch.object(babel_catalog, "datetime", shifted):
        assert compiled(po)[0] == first
