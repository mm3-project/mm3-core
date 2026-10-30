"""Browser harness of the test kit (Module Contract 12.2), driven by Playwright.

``BrowserHarness`` loads a module's element into a stand-alone shell page
that hosts it the way the real shell does (same ``mm3.js``, theme, CSP and
cell structure), feeds it states, resizes the cell, toggles preview and
locale, and records render times, CSP violations, attempted network calls
and overflow. Nothing is served over a socket: Playwright answers the page's
requests from the engine's and the module's files.

``run_browser_checks`` runs the harness rows of Contract section 11
(MOD-32, 34, 36, 51, 54, 84).

Implements:
    MOD-32, MOD-34, MOD-36: Module Contract 1.0 rev 11, §5.3 Rules.
    MOD-51, MOD-54: Module Contract 1.0 rev 11, §6.3 Layout and preview.
    MOD-84: Module Contract 1.0 rev 11, §9 Logging, health and errors.
    -: Module Contract 1.0 rev 11, §12.2 mm3.testing kit.
    CORE-63: Core spec 0.18, §10.1 Security.
    CORE-279: Core spec 0.18, §10.2 Packaging and deployment.
    CORE-288: Core spec 0.19, §6.2 Rules.
"""

from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from mm3.core.extras import MissingExtraError, missing_testing_modules
from mm3.testing.contract import CheckResult

_missing = missing_testing_modules()
if _missing:
    raise MissingExtraError(_missing)

from playwright.async_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from mm3.core.i18n.compiler import compiled  # noqa: E402
from mm3.core.web.security import CSP  # noqa: E402
from mm3.core.web.shell import STATIC_DIR, VENDOR_DIR, inside  # noqa: E402
from mm3.module import MirrorModule  # noqa: E402

__all__ = [
    "BROWSER_CHECKS",
    "BrowserHarness",
    "BrowserUnavailableError",
    "Render",
    "run_browser_checks",
]

BROWSER_CHECKS = ("MOD-32", "MOD-34", "MOD-36", "MOD-51", "MOD-54", "MOD-84")
"""The section 11 rows the browser harness checks."""

ORIGIN = "http://mm3.test"
VIEWPORT = (1080, 1920)
SIZES = ((400, 300), VIEWPORT)
DEFAULT_MIN_SIZE = (200, 60)
RENDER_BUDGET_MS = 16.0
PAGE_JS = Path(__file__).with_name("browser_page.js")
PAGE_HTML = (
    '<!doctype html>\n<html lang="en" data-mode="preview">\n<head>\n<meta charset="utf-8">\n'
    '<link rel="icon" href="data:,">\n<link rel="stylesheet" href="/core/theme.css">\n'
    '<link rel="stylesheet" href="/core/shell.css">\n'
    '<script type="module" src="/harness/page.js"></script>\n'
    '</head>\n<body>\n<main id="mm3-grid"></main>\n</body>\n</html>\n'
)
MODULE_PREFIX = "/modules/mm3test/v/"


class BrowserUnavailableError(RuntimeError):
    """Chromium cannot be started (``playwright install chromium``, or ``MM3_CHROMIUM``)."""


@dataclass(frozen=True)
class Render:
    """What one state rendered.

    Attributes:
        ms: Best render time in milliseconds.
        failed: The element reported a failure.
        error: Exception text, when rendering raised.
        html: The shadow root's markup without lit markers.
    """

    ms: float | None
    failed: bool
    error: str | None
    html: str


def _package_dir(module: type[MirrorModule]) -> Path:
    top = module.__module__.split(".")[0]
    spec = importlib.util.find_spec(top)
    if spec is None or not spec.submodule_search_locations:
        raise ValueError(f"cannot find the package of {module.__module__}")
    return Path(next(iter(spec.submodule_search_locations)))


def catalog_for(module: type[MirrorModule], locale: str) -> dict[str, Any]:
    """Return the merged catalog the element gets for a locale (locale, language, ``en``).

    Args:
        module: The module class.
        locale: Locale, e.g. ``it`` or ``pt-BR``.

    Returns:
        Message id to text or plural forms; empty without catalogs.
    """
    if module.locales_dir is None:
        return {}
    root = _package_dir(module) / module.locales_dir
    chain = [c for c in dict.fromkeys([locale, locale.split("-")[0], "en"])]
    merged: dict[str, Any] = {}
    for lang in reversed(chain):
        data = root / lang / "messages.json"
        po = root / lang / "LC_MESSAGES" / "messages.po"
        if data.is_file():
            merged.update(json.loads(data.read_text(encoding="utf-8")))
        elif po.is_file():
            merged.update(compiled(po)[2])
    return merged


class BrowserHarness:
    """Host one module element in a stand-alone shell (Contract 12.2).

    ``async with BrowserHarness(Weather, config={...}) as h:`` starts
    Chromium, mounts the element and waits for it. ``await h.state({...})``
    renders a state.

    Args:
        module: The module class; it needs an ``element``.
        config: The instance configuration the element receives as
            ``this.config`` (public form: no secrets).
        locale: Initial locale.
        timezone: IANA time zone the element formats dates in.
        location: ``engine.location`` as ``{"lat": .., "lon": .., "name": ..}``
            (``name`` text or per-language mapping); the element reads it as
            ``this.location``. ``None`` when unset.
        preview: Mount as in the admin live preview (``this.preview``).
        chromium: Path of a Chromium executable; default ``MM3_CHROMIUM`` or
            Playwright's own.

    Attributes:
        stray_requests: Paths the page asked for that no file answers.
    """

    def __init__(
        self,
        module: type[MirrorModule],
        *,
        config: Mapping[str, Any] | None = None,
        locale: str = "en",
        timezone: str = "UTC",
        location: Mapping[str, Any] | None = None,
        preview: bool = False,
        chromium: str | None = None,
    ) -> None:
        if module.element is None:
            raise ValueError(f"{module.name} has no front-end (element is None)")
        self.module = module
        self.config = dict(config or {})
        self.locale = locale
        self.timezone = timezone
        self.location = dict(location) if location is not None else None
        self.preview = preview
        self.chromium = chromium or os.environ.get("MM3_CHROMIUM") or None
        self.stray_requests: list[str] = []
        self.mount_failures: list[str] = []
        self._static = _package_dir(module) / "static"
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # --- life cycle ---------------------------------------------------------------

    async def __aenter__(self) -> BrowserHarness:
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(executable_path=self.chromium)
        except Exception as exc:
            await self._pw.stop()
            raise BrowserUnavailableError(
                f"Chromium cannot be started ({str(exc).splitlines()[0]}); "
                "run 'playwright install chromium' or set MM3_CHROMIUM"
            ) from exc
        self._context = await self._browser.new_context(
            viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]}
        )
        await self._context.route(f"{ORIGIN}/**", self._serve)
        self._page = await self._context.new_page()
        await self._page.goto(f"{ORIGIN}/")
        await self._page.wait_for_function("() => window.mm3test !== undefined")
        await self.mount()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    @property
    def page(self) -> Page:
        """The Playwright page."""
        if self._page is None:
            raise RuntimeError("the harness is not open")
        return self._page

    async def _serve(self, route: Route) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        body: bytes | None = None
        if path == "/":
            body, kind = PAGE_HTML.encode(), "text/html"
        elif path == "/harness/page.js":
            body, kind = PAGE_JS.read_bytes(), "text/javascript"
        else:
            file: Path | None = None
            if path.startswith("/core/vendor/"):
                file = inside(VENDOR_DIR, path.removeprefix("/core/vendor/"))
            elif path.startswith("/core/"):
                file = inside(STATIC_DIR, path.removeprefix("/core/"))
            elif path.startswith(MODULE_PREFIX):
                file = inside(self._static, path.removeprefix(MODULE_PREFIX))
            if file is not None:
                body = file.read_bytes()
            kind = {".js": "text/javascript", ".mjs": "text/javascript"}.get(
                file.suffix if file else "",
                mimetypes.guess_type(path)[0] or "application/octet-stream",
            )
        if body is None:
            self.stray_requests.append(path)
            await route.fulfill(status=404, body=b"", headers={"content-security-policy": CSP})
            return
        await route.fulfill(
            status=200, body=body, headers={"content-type": kind, "content-security-policy": CSP}
        )

    # --- driving the element ---------------------------------------------------------

    async def mount(self, *, preview: bool | None = None, locale: str | None = None) -> bool:
        """(Re)create the element; return whether its scripts loaded and defined it."""
        if preview is not None:
            self.preview = preview
        if locale is not None:
            self.locale = locale
        m = self.module
        info = {
            "id": f"{m.name}_browser",
            "element": m.element,
            "scripts": [f"{MODULE_PREFIX}{s}" for s in m.scripts],
            "styles": [f"{MODULE_PREFIX}{s}" for s in m.styles],
            "config": self.config,
            "locale": self.locale,
            "timezone": self.timezone,
            "location": self.location,
            "preview": self.preview,
            "catalog": catalog_for(m, self.locale),
        }
        result = await self.page.evaluate("(info) => window.mm3test.mount(info)", info)
        self.mount_failures = list(result["failures"])
        return bool(result["ok"])

    async def state(self, state: Mapping[str, Any], *, repeat: int = 3) -> Render:
        """Render a state (the element's ``set_state``) and return what happened."""
        r = await self.page.evaluate(
            "([s, n]) => window.mm3test.setState(s, n)", [dict(state), repeat]
        )
        return Render(r["ms"], bool(r["failed"]), r["error"], str(r["html"]))

    async def resize(self, width: int, height: int) -> None:
        """Give the cell a size, as a grid area would (``onResize`` follows)."""
        await self.page.evaluate("([w, h]) => window.mm3test.resize(w, h)", [width, height])

    async def overflow(self) -> dict[str, Any]:
        """Return ``{content, page, cell}``: elements outside the cell, page scroll, cell size."""
        return dict(await self.page.evaluate("() => window.mm3test.overflow()"))

    async def set_locale(self, locale: str) -> None:
        """Switch the element's locale as ``CORE_LOCALE_CHANGED`` does."""
        self.locale = locale
        await self.page.evaluate(
            "([l, c]) => window.mm3test.setLocale(l, c)", [locale, catalog_for(self.module, locale)]
        )

    async def report(self) -> dict[str, Any]:
        """Return everything the page recorded (network attempts, CSP violations, logs)."""
        return dict(await self.page.evaluate("() => window.mm3test.report"))

    async def screenshot(self) -> bytes:
        """Return a PNG of the cell."""
        node = self.page.locator(".mm3-cell")
        return await node.screenshot()

    async def compare_screenshot(self, path: Path, *, update: bool = False) -> None:
        """Compare the cell with a stored PNG; write it when missing or ``update``.

        Args:
            path: The baseline file (one per locale, by convention).
            update: Rewrite the baseline.

        Raises:
            AssertionError: The picture differs; the new one is saved next to
                the baseline as ``<name>.actual.png``.
        """
        _compare(path, await self.screenshot(), update)


def _compare(path: Path, image: bytes, update: bool) -> None:
    if update or not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        return
    if path.read_bytes() != image:
        actual = path.with_suffix(".actual.png")
        actual.write_bytes(image)
        raise AssertionError(f"{path.name} differs from the baseline; see {actual.name}")


# --- the harness rows --------------------------------------------------------------------


def _min_size(module: type[MirrorModule]) -> tuple[int, int]:
    size = module.min_size_px
    return (int(size[0]), int(size[1])) if size else DEFAULT_MIN_SIZE


async def run_browser_checks(
    h: BrowserHarness, states: Sequence[Mapping[str, Any]]
) -> list[CheckResult]:
    """Run the harness rows of Contract section 11 on a mounted element.

    Args:
        h: An open harness.
        states: States to feed, as the backend sends them (the empty state
            ``{}`` is always tried first).

    Returns:
        One result per row of :data:`BROWSER_CHECKS`, category ``harness``.
    """
    results = {mid: CheckResult(mid, category="harness") for mid in BROWSER_CHECKS}
    results["MOD-84"].level = "SHOULD"
    fed = [dict(s) for s in states][:6]
    if h.mount_failures:
        for r in results.values():
            r.fail(f"not run: the element cannot be loaded ({'; '.join(h.mount_failures)})")
        return list(results.values())
    await _empty_state(h, results["MOD-36"])
    await _pure_and_fast(h, fed, results["MOD-34"])
    await _logging(h, fed, results["MOD-84"])
    await _sizes(h, fed, results["MOD-51"])
    await _preview(h, fed, results["MOD-54"])
    await _network(h, results["MOD-32"])
    return list(results.values())


async def _empty_state(h: BrowserHarness, r: CheckResult) -> None:
    out = await h.state({}, repeat=1)
    if out.error or out.failed:
        r.fail(f"render({{}}) failed: {out.error or 'the element reported a failure'}")


async def _pure_and_fast(h: BrowserHarness, states: list[dict[str, Any]], r: CheckResult) -> None:
    for i, state in enumerate(states):
        first = await h.state(state)
        second = await h.state(state)
        for out in (first, second):
            if out.error or out.failed:
                r.fail(f"state {i}: render failed: {out.error or 'the element reported a failure'}")
                break
        else:
            if first.html != second.html:
                r.fail(
                    f"state {i}: the same state rendered two different outputs (render is not pure)"
                )
            slowest = max(x for x in (first.ms, second.ms) if x is not None)
            if slowest > RENDER_BUDGET_MS and r.status != "fail":
                r.status = "warn"
                r.findings.append(
                    f"state {i}: render took {slowest:.1f} ms (should be under 16 ms)"
                )


async def _logging(h: BrowserHarness, states: list[dict[str, Any]], r: CheckResult) -> None:
    await h.mount()
    for state in states:
        await h.state(state, repeat=1)
    infos: dict[int, int] = {}
    for rec in (await h.report())["logs"]:
        if rec.get("level") == "info":
            infos[int(rec["seq"])] = infos.get(int(rec["seq"]), 0) + 1
    for count in infos.values():
        if count > 1:
            r.status = "warn"
            r.findings.append(
                f"{count} info log records for one refresh; repetition belongs at debug"
            )
            break


async def _sizes(h: BrowserHarness, states: list[dict[str, Any]], r: CheckResult) -> None:
    await h.mount()
    sizes = list(dict.fromkeys([_min_size(h.module), *SIZES]))
    for width, height in sizes:
        await h.resize(width, height)
        for i, state in enumerate(states or [{}]):
            out = await h.state(state, repeat=1)
            if out.error:
                continue  # MOD-34 reports it
            await h.resize(width, height)
            found = await h.overflow()
            if found["content"]:
                r.fail(
                    f"{width}x{height}, state {i}: content outside the cell: "
                    + ", ".join(found["content"])
                )
            if found["page"]:
                r.fail(f"{width}x{height}, state {i}: the page scrolls (a fixed page-level size)")


async def _preview(h: BrowserHarness, states: list[dict[str, Any]], r: CheckResult) -> None:
    await h.mount(preview=True)
    for i, state in enumerate([{}, *states]):
        out = await h.state(state, repeat=1)
        if out.error or out.failed:
            r.fail(f"preview, state {i}: render failed: {out.error or 'failure reported'}")
    shown = await h.page.evaluate("() => window.mm3test.mediaInPreview()")
    media = (await h.report())["media"]
    if shown or media:
        r.fail("the preview shows or requests camera, microphone or video; render a placeholder")
    await h.mount(preview=False)


async def _network(h: BrowserHarness, r: CheckResult) -> None:
    report = await h.report()
    for call in report["net"]:
        r.fail(f"{call['api']}({call['url']!r}): a front-end must not call any URL")
    for violation in report["csp"]:
        r.fail(f"CSP violation: {violation['directive']} blocked {violation['blocked']!r}")
    for path in sorted(set(h.stray_requests)):
        r.fail(f"requested {path}, which is neither the engine's nor the module's file")
    for err in report["errors"][:3]:
        r.fail(f"uncaught error: {err}")
