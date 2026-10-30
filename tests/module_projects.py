"""Write module projects to disk for the ``mm3 module check`` tests.

``write_greeting`` is the reference module of Module Contract section 13;
tests change one thing in it and expect the matching MOD id to fail.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

LANGS = ("en", "it", "fr", "es", "de")

PYPROJECT = """\
[build-system]
requires = ["hatchling", "mm3-build>=1.0,<2"]
build-backend = "hatchling.build"

[project]
name = "mm3-greeting"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["mm3-core>=1.0,<2"]
readme = "README.md"

[project.entry-points."mm3.modules"]
greeting = "mm3_greeting:GreetingModule"

[tool.hatch.build.targets.wheel]
packages = ["src/mm3_greeting"]

[tool.hatch.build.hooks.mm3-i18n]

[tool.mm3]
category = "clock"
screenshot = "docs/screenshot.png"
"""

INIT = '''\
"""Greeting module: shows a time-of-day greeting."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from mm3 import LocalizedText, MirrorModule, ModuleConfig


class GreetingConfig(ModuleConfig):
    """Configuration for :class:`GreetingModule`."""

    name: LocalizedText | None = Field(None, title="CFG_NAME", description="CFG_NAME_HELP")


class GreetingModule(MirrorModule):
    """Publishes the current part of the day to its front-end."""

    name: ClassVar[str] = "greeting"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = GreetingConfig
    element: ClassVar[str | None] = "mm3-greeting"
    scripts: ClassVar[list[str]] = ["greeting.js"]
    styles: ClassVar[list[str]] = ["greeting.css"]
    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)

    async def start(self) -> None:
        """Publish once, then every minute."""
        await self._publish()
        self.ctx.scheduler.every(60, self._publish)

    async def _publish(self) -> None:
        hour = self.ctx.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        self.ctx.set_state({"part": part})
        self.ctx.log.debug("part_of_day", part=part)

    def snapshot(self) -> dict[str, str]:
        """Latest state for newly connected clients."""
        return dict(self.ctx.state)
'''

JS = """\
import { MirrorElement, html } from "/core/mm3.js";

const KEYS = { morning: "GOOD_MORNING", afternoon: "GOOD_AFTERNOON", evening: "GOOD_EVENING" };

class GreetingElement extends MirrorElement {
  render(state) {
    if (!state.part) return html``;
    const name = this.config.name;
    const key = name ? `${KEYS[state.part]}_NAME` : KEYS[state.part];
    return html`<p class="greeting">${this.t(key, { name })}</p>`;
  }
}
customElements.define("mm3-greeting", GreetingElement);
"""

CSS = ".greeting { font-size: 2em; }\n"

README = """\
# mm3-greeting

Shows a greeting for the time of day.

```yaml
- module: greeting
  id: hello
```

![Screenshot](docs/screenshot.png)

## Notifications

Emits none, consumes none.

## Grants

None.
"""

MESSAGES = {
    "GOOD_MORNING": "Good morning",
    "GOOD_MORNING_NAME": "Good morning, {name}",
    "GOOD_AFTERNOON": "Good afternoon",
    "GOOD_AFTERNOON_NAME": "Good afternoon, {name}",
    "GOOD_EVENING": "Good evening",
    "GOOD_EVENING_NAME": "Good evening, {name}",
    "CFG_NAME": "Name",
    "CFG_NAME_HELP": "Optional name shown after the greeting",
    "HEADER": "Greeting",
}


def po(messages: dict[str, str], lang: str) -> str:
    """Return a ``.po`` file; non-English texts are marked with the language."""
    head = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    body = "".join(
        f'msgid "{k}"\nmsgstr "{v if lang == "en" else f"[{lang}] {v}"}"\n\n'
        for k, v in messages.items()
    )
    return head + body


def write_greeting(root: Path, *, name: str = "greeting") -> Path:
    """Write the reference module's source tree under ``root / mm3-<name>``.

    Args:
        root: Parent directory.
        name: Module name (default ``greeting``).

    Returns:
        The project directory.
    """
    proj = root / f"mm3-{name}"
    pkg = proj / "src" / f"mm3_{name}"
    (pkg / "static").mkdir(parents=True)
    (proj / "docs").mkdir()
    (proj / "docs" / "screenshot.png").write_bytes(b"\x89PNG\r\n")
    swap = {"greeting": name, "Greeting": name.title()}

    def put(path: Path, text: str) -> None:
        for old, new in swap.items():
            text = text.replace(old, new)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    put(proj / "pyproject.toml", PYPROJECT)
    put(proj / "README.md", README)
    put(pkg / "__init__.py", INIT)
    put(pkg / "static" / "greeting.js".replace("greeting", name), JS)
    put(pkg / "static" / "greeting.css".replace("greeting", name), CSS)
    for lang in LANGS:
        put(pkg / "locales" / lang / "LC_MESSAGES" / "messages.po", po(MESSAGES, lang))
    return proj


def edit(path: Path, old: str, new: str) -> None:
    """Replace ``old`` by ``new`` in a file; fail when ``old`` is not there."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def dedent(text: str) -> str:
    """Return ``text`` dedented, for inline snippets in tests."""
    return textwrap.dedent(text)
