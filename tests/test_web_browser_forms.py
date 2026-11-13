"""Form posts from a real browser reach the handlers (CORE-63, CORE-253, CORE-254, CORE-378).

The other web tests send ``Origin`` by hand. A browser sends what its
``Referrer-Policy`` allows: with ``no-referrer`` it sends ``Origin: null`` on
every same-origin form post, so the login, logout and pairing forms were
refused before the token was looked at.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from playwright.async_api import Browser, async_playwright

from mm3.core.settings import AdminSettings, EngineSettings, ServerSettings
from mm3.core.web.gateway import WebGateway
from mm3.core.web.sessions import ADMIN_COOKIE, DISPLAY_COOKIE
from mm3.core.web.tokens import TokenStore
from module_projects import chromium_available

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not chromium_available(), reason="Chromium cannot start (set MM3_CHROMIUM)"),
]


@pytest.fixture
async def gateway(tmp_path: Path) -> AsyncIterator[WebGateway]:
    settings = EngineSettings().model_copy(
        update={
            "server": ServerSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
            "admin": AdminSettings.model_construct(host="127.0.0.1", port=0, allow=[]),
        }
    )
    gw = WebGateway(settings, tmp_path)
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    import os

    async with async_playwright() as pw:
        exe = os.environ.get("MM3_CHROMIUM") or None
        b = await pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        try:
            yield b
        finally:
            await b.close()


async def test_core_253_browser_login_and_logout(
    gateway: WebGateway, browser: Browser, tmp_path: Path
) -> None:
    token = TokenStore(tmp_path).new("browser-test", "admin")
    base = f"http://127.0.0.1:{gateway.admin.port}"
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(f"{base}/admin/login")
    await page.fill("input[name=token]", token)
    await page.click("button[type=submit]")
    await page.wait_for_url(f"{base}/admin")
    cookies = {c["name"] for c in await context.cookies(base)}
    assert ADMIN_COOKIE in cookies
    await page.click("button[type=submit]")
    await page.wait_for_url(f"{base}/admin/login")
    assert ADMIN_COOKIE not in {c["name"] for c in await context.cookies(base)}
    await context.close()


async def test_core_378_browser_login_returns_to_the_page_asked_for(
    gateway: WebGateway, browser: Browser, tmp_path: Path
) -> None:
    token = TokenStore(tmp_path).new("browser-landing", "admin")
    base = f"http://127.0.0.1:{gateway.admin.port}"
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(f"{base}/admin/debug?view=logs")
    await page.wait_for_url(f"{base}/admin/login?next=/admin/debug%3Fview%3Dlogs")
    await page.fill("input[name=token]", token)
    await page.click("button[type=submit]")
    await page.wait_for_url(f"{base}/admin/debug?view=logs")
    await page.goto(f"{base}/admin/login")
    await page.wait_for_url(f"{base}/admin")
    await context.close()


async def test_core_254_browser_pairing(
    gateway: WebGateway, browser: Browser, tmp_path: Path
) -> None:
    token = TokenStore(tmp_path).new("tv", "display")
    base = f"http://127.0.0.1:{gateway.mirror.port}"
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(f"{base}/pair#{token}")
    await page.wait_for_function("() => location.pathname === '/' ")
    assert DISPLAY_COOKIE in {c["name"] for c in await context.cookies(base)}
    await context.close()
