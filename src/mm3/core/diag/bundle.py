"""The diagnostic bundle: one zip with everything needed to diagnose a mirror.

The zip holds ``manifest.json`` ``{created_at, partial, missing, versions,
python, os, redacted}``, ``packages.txt``, ``config/`` (the redacted config
set), ``logs.jsonl``, ``health.json``, ``browser-errors.jsonl`` and
``kiosk.json``. A partial bundle, built by the CLI when the engine does not
answer, has ``journal.txt`` instead of ``logs.jsonl`` and names the parts it
lacks in ``missing`` (CORE-318, CORE-311).

Redaction is mandatory: every secret value is replaced by ``!masked`` in the
config files, logs and the other parts, and ``redacted`` lists each replaced
field as ``{file, path}`` (CORE-116).

Implements:
    CORE-116, CORE-311: Core spec 0.22, §10.5 Diagnostics and debugging.
    CORE-318: Core spec 0.24, §10.5 Diagnostics and debugging.
"""

from __future__ import annotations

import importlib.metadata
import io
import json
import platform
import socket
import sys
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mm3.__about__ import __version__
from mm3.core.config.loader import rt_mask_strings
from mm3.core.diag.secrets import MASK, Masker

__all__ = [
    "PARTS",
    "Bundle",
    "bundle_name",
    "package_list",
    "redact_yaml",
    "system_info",
]

PARTS = (
    "packages.txt",
    "config/",
    "logs.jsonl",
    "health.json",
    "browser-errors.jsonl",
    "kiosk.json",
)
"""The parts of a full bundle besides ``manifest.json`` (CORE-318)."""


def bundle_name(now: datetime | None = None, host: str | None = None) -> str:
    """Return ``mm3-diag-<host>-<UTC time>.zip`` (CORE-318).

    Args:
        now: Creation time; now by default.
        host: Host name; this machine's by default.

    Returns:
        The file name.
    """
    when = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in (host or socket.gethostname()))
    return f"mm3-diag-{name or 'host'}-{when}.zip"


def package_list() -> str:
    """Return the venv package list as ``name==version`` lines, sorted."""
    names = {
        f"{d.metadata['Name']}=={d.version}"
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    }
    return "".join(f"{n}\n" for n in sorted(names, key=str.lower))


def system_info() -> tuple[dict[str, str], dict[str, str]]:
    """Return the ``python`` and ``os`` objects of the manifest."""
    python = {"version": platform.python_version(), "implementation": sys.implementation.name}
    os_info = {"system": platform.system(), "release": platform.release()}
    try:
        os_release = platform.freedesktop_os_release()
    except OSError:
        os_release = {}
    if os_release.get("PRETTY_NAME"):
        os_info["distribution"] = os_release["PRETTY_NAME"]
    return python, os_info


def redact_yaml(text: str, masker: Masker) -> tuple[str, list[str]]:
    """Replace every secret scalar of a YAML text by the tag ``!masked`` (CORE-116).

    Args:
        text: The file's text.
        masker: The secret values.

    Returns:
        The redacted text and the JSON pointers of the replaced values. A file
        that does not parse is masked as text, with pointer ``""`` when
        anything was replaced.
    """
    try:
        return rt_mask_strings(text, lambda v: masker.mask(v) == MASK, MASK)
    except Exception:
        masked = masker.mask_text(text)
        return masked, ([""] if masked != text else [])


@dataclass
class Bundle:
    """A diagnostic bundle being assembled (CORE-318).

    Args:
        masker: The secret values to redact.
        partial: Built without the engine (``mm3 diag bundle`` fallback).
        now: Creation time.

    Implements:
        CORE-318: Core spec 0.24, §10.5 Diagnostics and debugging.
    """

    masker: Masker = field(default_factory=Masker)
    partial: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    modules: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    redacted: list[dict[str, str]] = field(default_factory=list)
    _files: dict[str, bytes] = field(default_factory=dict)

    def add_text(self, name: str, text: str) -> None:
        """Add a text part, with every secret replaced (CORE-116)."""
        masked = self.masker.mask_text(text)
        if masked != text:
            self.redacted.append({"file": name, "path": ""})
        self._files[name] = masked.encode("utf-8")

    def add_json(self, name: str, value: Any) -> None:
        """Add a JSON part, secrets masked by value (CORE-319)."""
        masked = self.masker.mask(value)
        if masked != value:
            self.redacted.append({"file": name, "path": ""})
        self._files[name] = json.dumps(masked, indent=2, default=str).encode("utf-8")

    def add_jsonl(self, name: str, records: Iterable[Mapping[str, Any]]) -> None:
        """Add a JSON lines part (logs), secrets masked by value."""
        lines = []
        hit = False
        for record in records:
            masked = self.masker.mask(dict(record))
            hit = hit or masked != dict(record)
            lines.append(json.dumps(masked, default=str))
        if hit:
            self.redacted.append({"file": name, "path": ""})
        self._files[name] = "".join(f"{line}\n" for line in lines).encode("utf-8")

    def add_config(self, files: Sequence[Path], config_dir: Path) -> None:
        """Add the config set under ``config/``, redacted (CORE-116).

        Args:
            files: The files of the set (main file, includes, templates, custom.css).
            config_dir: The configuration directory; names are relative to it.
        """
        for path in files:
            try:
                rel = path.relative_to(config_dir).as_posix()
            except ValueError:
                rel = path.name
            name = f"config/{rel}"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                self.missing.append(name)
                continue
            if path.suffix in (".yaml", ".yml"):
                text, paths = redact_yaml(text, self.masker)
                self.redacted.extend({"file": name, "path": p} for p in paths)
                self._files[name] = text.encode("utf-8")
            else:
                self.add_text(name, text)

    def add_bytes(self, name: str, data: bytes) -> None:
        """Add a part that needs no redaction (the package list)."""
        self._files[name] = data

    def names(self) -> list[str]:
        """Return the parts added so far."""
        return sorted(self._files)

    def build(self) -> bytes:
        """Write the zip with its manifest.

        Returns:
            The zip file's bytes.
        """
        python, os_info = system_info()
        manifest = {
            "created_at": self.now.isoformat(timespec="seconds"),
            "partial": self.partial,
            "missing": sorted(set(self.missing)),
            "versions": {"mm3_core": __version__, "modules": dict(sorted(self.modules.items()))},
            "python": python,
            "os": os_info,
            "redacted": self.redacted,
        }
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name in sorted(self._files):
                zf.writestr(name, self._files[name])
        return out.getvalue()
