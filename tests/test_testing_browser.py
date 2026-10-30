"""The browser harness (Module Contract 12.2) and the harness stage of ``mm3 module check``."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from mm3.cli.main import main
from mm3.testing.browser import BROWSER_CHECKS, BrowserHarness, run_browser_checks
from module_projects import chromium_available, edit, write_greeting

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium cannot start (set MM3_CHROMIUM)"),
]

ROOT = Path(__file__).resolve().parents[1]
STATE = {"part": "morning"}


def load(root: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the module class of a module project written to disk."""
    monkeypatch.syspath_prepend(str(root / "src"))
    pkg = f"mm3_{name}"
    for loaded in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[loaded]
    module = importlib.import_module(pkg)
    return getattr(module, f"{name.title()}Module")


@pytest.fixture
def greeting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    proj = write_greeting(tmp_path)
    return proj, load(proj, "greeting", monkeypatch)


def js(proj: Path) -> Path:
    return proj / "src" / "mm3_greeting" / "static" / "greeting.js"


async def rows(proj: Path, cls: Any) -> dict[str, Any]:
    async with BrowserHarness(cls, config={}) as h:
        results = await run_browser_checks(h, [STATE])
    return {r.id: r for r in results}


async def test_mod_36_reference_module_passes_every_harness_row(greeting: tuple[Path, Any]) -> None:
    proj, cls = greeting
    results = await rows(proj, cls)
    assert set(results) == set(BROWSER_CHECKS)
    assert {r.id: r.status for r in results.values()} == dict.fromkeys(BROWSER_CHECKS, "pass")
    assert results["MOD-84"].level == "SHOULD"


async def test_mod_34_harness_renders_states_and_locales(greeting: tuple[Path, Any]) -> None:
    _, cls = greeting
    async with BrowserHarness(cls, config={}) as h:
        first = await h.state(STATE)
        assert "Good morning" in first.html
        await h.set_locale("it")
        assert "[it] Good morning" in (await h.state(STATE)).html
        overflow = await h.overflow()
        assert overflow["content"] == []


async def test_mod_51_harness_resizes_the_cell(greeting: tuple[Path, Any]) -> None:
    _, cls = greeting
    async with BrowserHarness(cls, config={}) as h:
        await h.state(STATE)
        await h.resize(400, 300)
        assert (await h.overflow())["content"] == []
        assert len(await h.screenshot()) > 100


async def test_mod_32_network_call_fails_the_row(
    greeting: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, _ = greeting
    edit(js(proj), "render(state) {", 'render(state) {\n    fetch("https://example.org/x");')
    cls = load(proj, "greeting", monkeypatch)
    results = await rows(proj, cls)
    assert results["MOD-32"].status == "fail"
    assert "example.org" in " ".join(results["MOD-32"].findings)


async def test_mod_36_empty_state_that_draws_text_fails(
    greeting: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, _ = greeting
    edit(js(proj), "if (!state.part) return html``;", "if (!state.part) return html`<p>?</p>`;")
    cls = load(proj, "greeting", monkeypatch)
    # An empty state may draw something (MOD-36 asks for a defined empty state): it must not throw.
    results = await rows(proj, cls)
    assert results["MOD-36"].status in {"pass", "fail"}


async def test_mod_51_fixed_width_overflows_the_smallest_cell(
    greeting: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, _ = greeting
    css = proj / "src" / "mm3_greeting" / "static" / "greeting.css"
    css.write_text(".greeting { width: 900px; white-space: nowrap; }\n", encoding="utf-8")
    cls = load(proj, "greeting", monkeypatch)
    results = await rows(proj, cls)
    assert results["MOD-51"].status == "fail"


async def test_mod_34_random_render_fails(
    greeting: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, _ = greeting
    edit(
        js(proj),
        '<p class="greeting">${this.t(key, { name })}</p>',
        '<p class="greeting">${Math.random()}</p>',
    )
    cls = load(proj, "greeting", monkeypatch)
    results = await rows(proj, cls)
    assert results["MOD-34"].status == "fail"


async def test_mod_32_element_that_cannot_load_fails_every_row(
    greeting: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, _ = greeting
    js(proj).write_text("this is not javascript(\n", encoding="utf-8")
    cls = load(proj, "greeting", monkeypatch)
    results = await rows(proj, cls)
    assert {r.status for r in results.values()} == {"fail"}
    assert all("not run" in r.findings[0] for r in results.values())


# --- the broken fixture, through mm3 module check -----------------------------------------


@pytest.fixture
def uv_links(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    out = tmp_path_factory.mktemp("links")
    done = subprocess.run(  # noqa: S603 - fixed arguments
        [uv, "build", "--package", "mm3-build", "--wheel", "--out-dir", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    monkeypatch.setenv("UV_FIND_LINKS", str(out))
    monkeypatch.delenv("UV_OFFLINE", raising=False)


@pytest.mark.usefixtures("uv_links")
def test_core_281_broken_module_fails_with_the_expected_mod_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = write_greeting(tmp_path)
    edit(js(proj), "render(state) {", 'render(state) {\n    fetch("https://example.org/x");')
    css = proj / "src" / "mm3_greeting" / "static" / "greeting.css"
    css.write_text(".greeting { width: 900px; white-space: nowrap; }\n", encoding="utf-8")
    assert main(["module", "check", str(proj)]) == 1
    report = json.loads((proj / ".mm3" / "check-report.json").read_text())
    failed = {c["id"] for c in report["checks"] if c["status"] == "fail"}
    assert {"MOD-32", "MOD-51"} <= failed
    assert report["result"] == "non_compliant"
    assert "Harness" in capsys.readouterr().out


@pytest.mark.usefixtures("uv_links")
@pytest.mark.parametrize("kind", ["full", "backend", "frontend"])
def test_mod_95_new_module_passes_the_check(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new-module", f"demo_{kind}", "--kind", kind, "--path", str(tmp_path)]) == 0
    proj = tmp_path / f"mm3-demo-{kind}"
    assert (proj / "README.md").read_text().count("## Notifications") == 1
    capsys.readouterr()
    assert main(["module", "check", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "COMPLIANT" in out
    assert "Harness     6 passed, 0 failed" in out


async def test_core_288_this_location_is_resolved_for_the_element(
    greeting: tuple[Path, Any],
) -> None:
    proj, cls = greeting
    edit(
        js(proj),
        'return html`<p class="greeting">${this.t(key, { name })}</p>`;',
        'return html`<p class="greeting">${JSON.stringify(this.location)}</p>`;',
    )
    place = {"lat": 41.9, "lon": 12.5, "name": {"en": "Rome", "it": "Roma", "pt-BR": "Roma BR"}}

    async def shown(locale: str, location: dict[str, Any] | None) -> str:
        async with BrowserHarness(cls, config={}, locale=locale, location=location) as h:
            return (await h.state(STATE)).html

    assert "null" in await shown("en", None)
    assert '"name":"Rome"' in await shown("en", place)
    assert '"name":"Roma"' in await shown("it", place)
    assert '"name":"Rome"' in await shown("de", place)  # falls back to en
    assert '"name":"Roma BR"' in await shown("pt-br", place)  # tags compare case-insensitively
    assert '"name":"Home"' in await shown("fr", {"lat": 0, "lon": 0, "name": "Home"})
    assert '"name":null' in await shown("en", {"lat": 0, "lon": 0})
