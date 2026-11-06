"""Fakes and builders shared by the installer tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name, parse_wheel_filename

from mm3.core.catalog import BUNDLE_URL, INDEX_URL, Publisher
from mm3.installer.fetch import FetchError
from mm3.installer.paths import Layout
from mm3.installer.runner import Completed
from mm3.installer.venv import Dist, Freeze, VenvBuilder
from mm3.installer.verify import VerifyError

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_wheel(name: str, version: str, *, requires: Iterable[str] = ()) -> bytes:
    """A minimal wheel of a module (one mm3.modules entry point)."""
    pkg = name.replace("-", "_")
    info = f"{pkg}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {r}\n" for r in requires
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{pkg}/__init__.py", "class Module:\n    pass\n")
        z.writestr(f"{info}/METADATA", meta)
        z.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        z.writestr(f"{info}/entry_points.txt", f"[mm3.modules]\n{pkg} = {pkg}:Module\n")
        z.writestr(f"{info}/RECORD", "")
    return buf.getvalue()


def wheel_name(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


def release(name: str, version: str, data: bytes, api: str = ">=1.0,<2") -> dict[str, Any]:
    return {
        "version": version,
        "api": api,
        "capabilities": ["http.outbound"],
        "grants": {"http": ["https://api.example.com/"]},
        "check": {"result": "pass", "report_url": "https://example.com/report"},
        "files": [
            {"filename": wheel_name(name, version), "sha256": hashlib.sha256(data).hexdigest()}
        ],
    }


def entry(name: str, releases: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "pypi": name,
        "repository": f"https://github.com/example/{name}",
        "badge": "community",
        "publisher": {"repository": f"example/{name}", "workflow": "release.yml"},
        "category": "info",
        "license": "MIT",
        "description": {"en": f"The {name} module", "it": f"Il modulo {name}"},
        "releases": releases,
        **extra,
    }


def index_bytes(
    modules: list[dict[str, Any]],
    *,
    serial: int = 10,
    generated: datetime = NOW - timedelta(hours=1),
    lifetime: timedelta = timedelta(days=7),
) -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "serial": serial,
            "generated_at": generated.isoformat(),
            "expires": (generated + lifetime).isoformat(),
            "modules": modules,
        }
    ).encode()


class FakeFetcher:
    """Serves bytes by URL; ``download`` writes them to a file."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def serve_index(self, raw: bytes, bundle: bytes = b'{"bundle": true}') -> None:
        self.files[INDEX_URL] = raw
        self.files[BUNDLE_URL] = bundle

    def serve_release(
        self, name: str, version: str, data: bytes, provenance: bytes = b"{}"
    ) -> None:
        filename = wheel_name(name, version)
        file_url = f"https://files.pythonhosted.org/packages/xx/{filename}"
        self.files[f"https://pypi.org/pypi/{name}/{version}/json"] = json.dumps(
            {
                "urls": [
                    {
                        "filename": filename,
                        "url": file_url,
                        "packagetype": "bdist_wheel",
                        "digests": {"sha256": hashlib.sha256(data).hexdigest()},
                    }
                ]
            }
        ).encode()
        self.files[file_url] = data
        self.files[f"https://pypi.org/integrity/{name}/{version}/{filename}/provenance"] = (
            provenance
        )

    def get(self, url: str, limit: int, *, accept: str | None = None) -> bytes:
        self.calls.append(url)
        if url not in self.files:
            raise FetchError(f"{url} answered HTTP 404")
        data = self.files[url]
        if len(data) > limit:
            raise FetchError(f"{url} is larger than {limit} bytes")
        return data

    def download(self, url: str, dest: Path, limit: int) -> None:
        dest.write_bytes(self.get(url, limit))


class FakeVerification:
    def __init__(self) -> None:
        self.index_ok = True
        self.wheel_ok = True
        self.wheels: list[tuple[str, Publisher]] = []

    def verify_index(self, index: bytes, bundle: bytes) -> None:
        if not self.index_ok:
            raise VerifyError("index signature does not verify: bad")

    def verify_wheel(self, wheel: Path, provenance: bytes, publisher: Publisher) -> None:
        self.wheels.append((wheel.name, publisher))
        if not self.wheel_ok:
            raise VerifyError(f"no attestation for {wheel.name} verifies")


class FakeEngine:
    def __init__(self) -> None:
        self.ready: list[bool] = []
        self.diag: list[int] = []
        self.restarts = 0
        self.checked: list[str] = []

    def restart(self) -> bool:
        self.restarts += 1
        return self.ready.pop(0) if self.ready else True

    def diag_check(self, venv: Path) -> Completed:
        self.checked.append(venv.name)
        return Completed(self.diag.pop(0) if self.diag else 0, "checks")


BASE: Freeze = {
    "mm3-core": Dist("mm3-core", "0.9.0", ("pydantic>=2",)),
    "pydantic": Dist("pydantic", "2.9.0"),
    "pip": Dist("pip", "24.0"),
}


class FakeBuilder(VenvBuilder):
    """A builder whose venvs are directories with a recorded freeze."""

    def __init__(self, layout: Layout) -> None:
        super().__init__(layout, runner=None)  # type: ignore[arg-type]
        self.freezes: dict[str, Freeze] = {}
        self.extra_deps: dict[str, list[Dist]] = {}
        self.override: dict[str, Dist] = {}
        self.fail_build: str | None = None
        self.fail_import = False
        self.requirements: list[list[str]] = []
        self.counter = 0

    def seed(self, name: str, freeze: Freeze) -> Path:
        path = self.layout.venvs / name
        path.mkdir(parents=True)
        self.freezes[name] = dict(freeze)
        self.switch(path)
        return path

    def freeze(self, venv: Path) -> Freeze:
        return dict(self.freezes[venv.name])

    def build(self, requirements: list[str], now: datetime) -> Path:
        self.requirements.append(list(requirements))
        if self.fail_build:
            from mm3.installer.venv import BuildError

            raise BuildError(self.fail_build)
        self.counter += 1
        path = self.layout.venvs / f"20261106T{self.counter:06d}Z"
        path.mkdir(parents=True)
        freeze: Freeze = {}
        for req in requirements:
            if req.endswith(".whl"):
                dist, version, _, _ = parse_wheel_filename(Path(req).name)
                d = Dist(str(dist), str(version), (), True)
                freeze[d.key] = d
                for dep in self.extra_deps.get(d.key, []):
                    freeze[dep.key] = dep
            else:
                n, _, v = req.partition("==")
                freeze[canonicalize_name(n)] = Dist(n, v)
        freeze.update(self.override)
        freeze["pip"] = Dist("pip", "24.0")
        self.freezes[path.name] = freeze
        return path

    def import_test(self, venv: Path, module: str | None) -> None:
        if self.fail_import:
            from mm3.installer.venv import BuildError

            raise BuildError("import test failed (exit 1): ImportError")
