"""``mm3 new-module``: write a module package that passes ``mm3 module check``.

``mm3 new-module <name> --kind full|backend|frontend`` generates a src-layout
package with ``mm3-build`` as build requirement (CORE-280), the module class
with its declarations, all five locale catalogs, a README with the
"Notifications" and "Grants" sections and a sample test. The front-end-only
kind writes the class with no hooks, which Module Contract section 1 calls
the generated stub (MOD-95).

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    CORE-280: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 11, §12.2 mm3.testing kit.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TextIO

__all__ = ["KINDS", "NAME_PATTERN", "add_new_module_parser", "run_new_module", "scaffold"]

KINDS = ("full", "backend", "frontend")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
LANGUAGES = ("en", "it", "fr", "es", "de")
EXIT_FAILED = 1

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4510000000049454e44ae426082"
)  # 1x1 transparent PNG, a placeholder for docs/screenshot.png

TEXTS: dict[str, dict[str, tuple[str, ...]]] = {
    # key: (en, it, fr, es, de)
    "texts": {
        "CFG_LABEL": ("Label", "Etichetta", "Libellé", "Etiqueta", "Beschriftung"),
        "CFG_LABEL_HELP": (
            "Optional text shown by the module",
            "Testo facoltativo mostrato dal modulo",
            "Texte facultatif affiché par le module",
            "Texto opcional que muestra el módulo",
            "Optionaler Text, den das Modul anzeigt",
        ),
        "EMPTY": (
            "Waiting for data",
            "In attesa di dati",
            "En attente de données",
            "Esperando datos",
            "Warte auf Daten",
        ),
        "UPDATED": ("Updated", "Aggiornato", "Mis à jour", "Actualizado", "Aktualisiert"),
    }
}

PYPROJECT = """\
[build-system]
requires = ["hatchling", "mm3-build>=1.0,<2"]
build-backend = "hatchling.build"

[project]
name = "@DIST@"
version = "0.1.0"
description = "@TITLE@ module for MM3"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["mm3-core>=1.0,<2"]

[project.entry-points."mm3.modules"]
@NAME@ = "@PKG@:@CLASS@"

[dependency-groups]
dev = ["mm3-core[testing]>=1.0,<2"]

[tool.hatch.build.targets.wheel]
packages = ["src/@PKG@"]

[tool.hatch.build.hooks.mm3-i18n]

[tool.mm3]
category = "other"
screenshot = "docs/screenshot.png"

[tool.pytest.ini_options]
asyncio_mode = "auto"
"""

README = """\
# @DIST@

@TITLE@ module for [MM3](https://github.com/mm3-project/mm3-core). @PURPOSE@

![Screenshot](docs/screenshot.png)

## Configuration

```yaml
- module: @NAME@
  id: @NAME@
@CONFIG@
```

## Notifications

@NOTIFICATIONS@

## Grants

None: the module needs no capabilities.

## Development

```console
uv run pytest
uv run mm3 module check .
```
"""

BACKEND_CLASS = '''\
"""@TITLE@: @PURPOSE@"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from mm3 import LocalizedText, MirrorModule, ModuleConfig


class @CLASSBASE@Config(ModuleConfig):
    """Configuration of the @NAME@ module."""

    label: LocalizedText | None = Field(None, title="CFG_LABEL", description="CFG_LABEL_HELP")


class @CLASSBASE@Updated(BaseModel):
    """Payload of ``@EVENT@``."""

    updated_at: str


class @CLASS@(MirrorModule):
    """@PURPOSE@"""

    name: ClassVar[str] = "@NAME@"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = @CLASSBASE@Config
@ELEMENT@    emits: ClassVar[Any] = {"@EVENT@": @CLASSBASE@Updated}

    async def start(self) -> None:
        """Publish once, then every minute."""
        await self._publish()
        self.ctx.scheduler.every(60, self._publish)

    async def _publish(self) -> None:
        stamp = self.ctx.now().astimezone(UTC).isoformat()
        self.ctx.set_state({"updated_at": stamp})
        await self.ctx.send_notification("@EVENT@", {"updated_at": stamp})
        self.ctx.log.debug("refreshed", updated_at=stamp)

    def snapshot(self) -> dict[str, Any]:
        """Latest state for newly connected clients."""
        return dict(self.ctx.state)
'''

FRONTEND_CLASS = '''\
"""@TITLE@: @PURPOSE@"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from mm3 import LocalizedText, MirrorModule, ModuleConfig


class @CLASSBASE@Config(ModuleConfig):
    """Configuration of the @NAME@ module."""

    label: LocalizedText | None = Field(None, title="CFG_LABEL", description="CFG_LABEL_HELP")


class @CLASS@(MirrorModule):
    """@PURPOSE@

    A front-end-only module: no backend hooks, the front-end draws from its
    configuration.
    """

    name: ClassVar[str] = "@NAME@"
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = @CLASSBASE@Config
@ELEMENT@'''

ELEMENT_LINES = """\
    element: ClassVar[str | None] = "mm3-@NAME@"
    scripts: ClassVar[list[str]] = ["@NAME@.js"]
    styles: ClassVar[list[str]] = ["@NAME@.css"]
    min_size_px: ClassVar[tuple[int, int] | None] = (200, 60)
"""

FULL_JS = """\
import { MirrorElement, html } from "/core/mm3.js";

class @ELEMENTCLASS@ extends MirrorElement {
  /** Draw the last update time; the empty state shows a waiting text (MOD-36). */
  render(state) {
    if (!state.updated_at) {
      return html`<p class="empty">${this.t("EMPTY")}</p>`;
    }
    const label = this.config.label || this.t("HEADER");
    return html`<p class="label">${label}</p>
      <p class="time">${this.t("UPDATED")} ${this.fmtDate(state.updated_at, "time")}</p>`;
  }
}
customElements.define("mm3-@NAME@", @ELEMENTCLASS@);
"""

FRONTEND_JS = """\
import { MirrorElement, html } from "/core/mm3.js";

class @ELEMENTCLASS@ extends MirrorElement {
  /** Draw the configured label, or the module's header text. */
  render() {
    return html`<p class="label">${this.config.label || this.t("HEADER")}</p>`;
  }
}
customElements.define("mm3-@NAME@", @ELEMENTCLASS@);
"""

CSS = """\
p { margin: 0; overflow-wrap: anywhere; }
.label { font-size: 1.25em; }
.time, .empty { color: var(--mm3-color-dimmed); }
"""

TEST = '''\
"""Contract tests of @DIST@ (Module Contract 12.2)."""

from mm3.testing import ModuleHarness

from @PKG@ import @CLASS@


async def test_runs_and_meets_the_contract() -> None:
    async with ModuleHarness(@CLASS@, config={}) as h:
        await h.advance(minutes=1)
        await h.check_contract()
'''


def _po(texts: dict[str, str]) -> str:
    head = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    return head + "".join(f'msgid "{k}"\nmsgstr "{v}"\n\n' for k, v in texts.items())


def scaffold(name: str, kind: str, parent: Path) -> Path:
    """Write a module package.

    Args:
        name: Module name, ``[a-z][a-z0-9_]*``.
        kind: ``full``, ``backend`` or ``frontend``.
        parent: Directory to create ``mm3-<name>`` in.

    Returns:
        The project directory.

    Raises:
        ValueError: Bad name or kind.
        FileExistsError: The project directory exists.
    """
    if not NAME_PATTERN.match(name):
        raise ValueError(f"module name {name!r} must match [a-z][a-z0-9_]*")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    dist = "mm3-" + name.replace("_", "-")
    pkg = "mm3_" + name
    base = "".join(part.title() for part in name.split("_"))
    title = " ".join(part.title() for part in name.split("_"))
    purposes = {
        "full": "Shows when it last refreshed.",
        "backend": "Publishes a notification every minute.",
        "frontend": "Shows its configured label.",
    }
    event = f"{name.upper()}_UPDATED"
    root = parent / dist
    if root.exists():
        raise FileExistsError(f"{root} already exists")
    swap = {
        "@DIST@": dist,
        "@PKG@": pkg,
        "@NAME@": name,
        "@CLASS@": f"{base}Module",
        "@CLASSBASE@": base,
        "@ELEMENTCLASS@": f"{base}Element",
        "@TITLE@": title,
        "@PURPOSE@": purposes[kind],
        "@EVENT@": event,
        "@ELEMENT@": ELEMENT_LINES if kind != "backend" else "",
        "@CONFIG@": "  label: Hello",
        "@NOTIFICATIONS@": (
            f"Emits `{event}`, consumes none."
            if kind != "frontend"
            else "Emits none, consumes none."
        ),
    }

    def fill(text: str) -> str:
        for _ in range(2):  # @ELEMENT@ brings @NAME@ with it
            for key, value in swap.items():
                text = text.replace(key, value)
        return text

    files: dict[str, str] = {
        "pyproject.toml": PYPROJECT,
        "README.md": README,
        ".gitignore": ".mm3/\n__pycache__/\ndist/\n.venv/\n",
        f"src/{pkg}/__init__.py": FRONTEND_CLASS if kind == "frontend" else BACKEND_CLASS,
        f"tests/test_{name}.py": TEST,
    }
    if kind != "backend":
        files[f"src/{pkg}/static/{name}.js"] = FRONTEND_JS if kind == "frontend" else FULL_JS
        files[f"src/{pkg}/static/{name}.css"] = CSS
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = fill(text)
        if path.endswith("__init__.py") and kind != "frontend":
            content = content.replace(
                "from typing import Any, ClassVar",
                "from datetime import UTC\nfrom typing import Any, ClassVar",
            )
        target.write_text(content, encoding="utf-8")
    messages = TEXTS["texts"]
    for i, lang in enumerate(LANGUAGES):
        catalog = {"HEADER": title, **{k: v[i] for k, v in messages.items()}}
        po = root / "src" / pkg / "locales" / lang / "LC_MESSAGES" / "messages.po"
        po.parent.mkdir(parents=True, exist_ok=True)
        po.write_text(_po(catalog), encoding="utf-8")
    shot = root / "docs" / "screenshot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(_PNG)
    return root


def add_new_module_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add ``mm3 new-module`` to the top-level parser.

    Args:
        sub: The top-level subparsers.
    """
    p = sub.add_parser("new-module", help="write a module that passes mm3 module check")
    p.add_argument("name", help="module name: lower case letters, digits and underscores")
    p.add_argument("--kind", choices=KINDS, default="full", help="default: full")
    p.add_argument("--path", type=Path, default=Path(), help="directory to create it in")


def run_new_module(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """Run ``mm3 new-module``.

    Args:
        args: Parsed arguments.
        out: Output stream; stdout by default.

    Returns:
        0 on success, 1 when the name is bad or the directory exists.
    """
    stream = out or sys.stdout
    try:
        root = scaffold(args.name, args.kind, args.path)
    except (ValueError, FileExistsError) as exc:
        print(f"mm3 new-module: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(
        f"Created {root} ({args.kind} module). Next: cd {root} && mm3 module check .", file=stream
    )
    return 0
