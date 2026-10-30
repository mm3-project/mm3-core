"""The ``mm3-i18n`` hatchling build hook.

Enabled in a package's ``pyproject.toml``::

    [build-system]
    requires = ["hatchling", "mm3-build>=1.0,<2"]
    build-backend = "hatchling.build"

    [tool.hatch.build.hooks.mm3-i18n]

For a wheel it compiles every ``messages.po`` under a ``locales/`` directory
of the files the wheel contains and puts ``messages.mo`` next to it and
``<lang>/messages.json`` beside the locale's ``LC_MESSAGES``; compiled files
already in the source tree are replaced by freshly compiled ones. A catalog
that cannot be read or compiled fails the build. Source distributions are
left alone: the hook runs again when the wheel is built from them.

Implements:
    CORE-263: Core spec 0.15, §8.5 Catalogs at run time.
    CORE-280: Core spec 0.17, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

from mm3_build.catalogs import CatalogError, compiled

__all__ = ["I18nBuildHook", "catalog_dist_paths"]


def catalog_dist_paths(dist_path: str) -> tuple[str, str] | None:
    """Return where a ``.po`` file's compiled files go inside the wheel.

    Args:
        dist_path: The ``.po`` file's path inside the wheel.

    Returns:
        The ``.mo`` and ``messages.json`` paths, or ``None`` when the file is
        not a catalog (``…/locales/<lang>/LC_MESSAGES/messages.po``).
    """
    parts = PurePosixPath(dist_path.replace("\\", "/")).parts
    if parts[-2:] != ("LC_MESSAGES", "messages.po") or "locales" not in parts[:-3]:
        return None
    lang_dir = PurePosixPath(*parts[:-2])
    return (
        str(lang_dir / "LC_MESSAGES" / "messages.mo"),
        str(lang_dir / "messages.json"),
    )


class I18nBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    """Compile the package's catalogs into the wheel.

    Implements:
        CORE-280: Core spec 0.17, §10.2 Packaging and deployment.
    """

    PLUGIN_NAME = "mm3-i18n"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Compile each catalog and force-include the results.

        Args:
            version: Build version (``standard`` or ``editable``).
            build_data: Hatchling build data; ``force_include`` is extended.

        Raises:
            CatalogError: A catalog cannot be read or compiled.
        """
        if self.target_name != "wheel" or version == "editable":
            return
        self._out = Path(tempfile.mkdtemp(prefix="mm3-i18n-"))
        force: dict[str, str] = build_data.setdefault("force_include", {})
        count = 0
        for included in self.build_config.builder.recurse_selected_project_files():
            targets = catalog_dist_paths(included.distribution_path)
            if targets is None:
                continue
            try:
                mo_bytes, json_bytes, _ = compiled(Path(included.path))
            except CatalogError as exc:
                self.app.abort(f"mm3-i18n: {exc}")
                raise  # pragma: no cover - abort() exits
            for dist, data in zip(targets, (mo_bytes, json_bytes), strict=True):
                file = self._out / dist
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
                force[str(file)] = dist
            count += 1
        self.app.display_info(f"mm3-i18n: compiled {count} catalog(s)")

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        """Remove the temporary compiled files.

        Args:
            version: Build version.
            build_data: Hatchling build data.
            artifact_path: The built wheel.
        """
        out = getattr(self, "_out", None)
        if out is not None:
            shutil.rmtree(out, ignore_errors=True)
