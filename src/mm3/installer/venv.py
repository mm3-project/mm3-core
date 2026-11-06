"""Staged venvs: freeze, plan, build, make read-only, test (CORE-134, CORE-354).

A new venv is built under ``/opt/mm3/venvs/<timestamp>/`` with
``python3 -m venv`` and ``pip``, never by copying: every package of the current
venv is pinned to its frozen version, the module being changed is replaced by
its verified wheel file (or left out for a removal, together with the packages
only it required), and the module's other dependencies come from PyPI
(CORE-354). Building and testing follow CORE-367:

1. the installer creates the directory owned by ``mm3-build``;
2. ``python3 -m venv`` and ``pip install --only-binary=:all:`` run as
   ``mm3-build`` without capabilities;
3. the installer gives the tree to ``mm3-install`` and removes every write bit;
4. the import test and ``mm3 module check --only static`` run as ``mm3-build``
   against the read-only venv.

A freeze of the new venv is compared with the old one: a package that
something other than the changed module requires and whose version changed
aborts the operation, listing the conflicts.

Implements:
    CORE-134: Core spec 0.33, §10.7 Installer service.
    CORE-354: Core spec 0.33, §10.7 Installer service.
    CORE-367: Core spec 0.35, §10.7 Installer service.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from mm3.installer.files import Owner, make_read_only
from mm3.installer.paths import BUILD_USER, Layout
from mm3.installer.runner import Runner

__all__ = [
    "KEEP_VENVS",
    "BuildError",
    "Dist",
    "Freeze",
    "VenvBuilder",
    "conflicts",
    "only_required_by",
]

KEEP_VENVS = 3
"""Staged venvs kept for rollback and "Restore previous version" (CORE-135)."""
TOOLS = frozenset({"pip", "setuptools", "wheel"})
"""Packages every venv brings itself; never pinned."""

# Run inside a venv's interpreter (as mm3-build): print every distribution with
# its version and requirements as JSON. Markers are evaluated by the installer.
_FREEZE = (
    "import importlib.metadata as m, json, sys\n"
    "out = [{'name': d.metadata['Name'], 'version': d.version,"
    " 'requires': list(d.requires or []),"
    " 'module': any(e.group == 'mm3.modules' for e in d.entry_points)}"
    " for d in m.distributions()]\n"
    "json.dump(out, sys.stdout)\n"
)

_IMPORT_TEST = (
    "import importlib.metadata as m, sys\n"
    "name = sys.argv[1]\n"
    "eps = [e for e in m.distribution(name).entry_points if e.group == 'mm3.modules']\n"
    "if not eps: sys.exit('no mm3.modules entry point in ' + name)\n"
    "for e in eps: e.load()\n"
    "import mm3.core.engine\n"
)


class BuildError(RuntimeError):
    """A build step failed; the message names the step and the reason."""


@dataclass(frozen=True)
class Dist:
    """One installed distribution."""

    name: str
    version: str
    requires: tuple[str, ...] = ()
    module: bool = False

    @property
    def key(self) -> str:
        """The normalized name."""
        return canonicalize_name(self.name)


Freeze = dict[str, Dist]
"""Installed distributions by normalized name."""


def _deps(dist: Dist, env: Mapping[str, str]) -> set[str]:
    out: set[str] = set()
    for line in dist.requires:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        # Extras are not installed by default: requirements behind an
        # ``extra == ...`` marker are left out.
        if req.marker is not None and not req.marker.evaluate({**env, "extra": ""}):
            continue
        out.add(canonicalize_name(req.name))
    return out


def only_required_by(freeze: Freeze, module: str) -> set[str]:
    """The packages ``module`` requires that nothing else installed requires.

    Args:
        freeze: The current venv.
        module: The module's normalized name.

    Returns:
        The normalized names, not including ``module`` itself.
    """
    env = {k: str(v) for k, v in default_environment().items()}
    graph = {k: _deps(d, env) & freeze.keys() for k, d in freeze.items()}
    closure: set[str] = set()
    todo = [module]
    while todo:
        for dep in graph.get(todo.pop(), set()):
            if dep not in closure and dep != module:
                closure.add(dep)
                todo.append(dep)
    removable = set(closure)
    changed = True
    while changed:
        changed = False
        for key, deps in graph.items():
            if key == module or key in removable:
                continue
            needed = deps & removable
            if needed:
                removable -= needed
                changed = True
    # A package needed by a removable one stays removable only if every one of
    # its dependants is removable too, which the loop above ensures.
    return removable - TOOLS


def conflicts(old: Freeze, new: Freeze, changing: Iterable[str]) -> list[str]:
    """Packages whose version changed although only ``changing`` may change.

    Args:
        old: The current venv.
        new: The new venv.
        changing: Normalized names allowed to change: the module and, for a
            removal, the packages only it required.

    Returns:
        One line per conflict, sorted.
    """
    allowed = set(changing) | TOOLS
    out = []
    for key, dist in old.items():
        if key in allowed:
            continue
        now = new.get(key)
        if now is None:
            out.append(f"{dist.name} {dist.version} would be removed")
        elif now.version != dist.version:
            out.append(f"{dist.name} {dist.version} would become {now.version}")
    return sorted(out)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class VenvBuilder:
    """Builds staged venvs through a :class:`~mm3.installer.runner.Runner`.

    Args:
        layout: The installer's paths.
        runner: Starts the children.
        build_owner: Owner of a venv while it is built (``mm3-build``), or None
            to keep ours (tests).
        final_owner: Owner after the build (``mm3-install``), or None.
        python: The system interpreter that creates venvs.
    """

    def __init__(
        self,
        layout: Layout,
        runner: Runner,
        *,
        build_owner: Owner | None = None,
        final_owner: Owner | None = None,
        python: str = "/usr/bin/python3",
    ) -> None:
        self.layout = layout
        self.runner = runner
        self.build_owner = build_owner
        self.final_owner = final_owner
        self.python = python

    # --- venvs ---------------------------------------------------------------------

    def current(self) -> Path | None:
        """The venv the engine runs from (the target of the symlink)."""
        link = self.layout.venv_link
        return link.resolve() if link.is_symlink() else None

    def kept(self) -> list[Path]:
        """The staged venvs, newest first."""
        root = self.layout.venvs
        if not root.is_dir():
            return []
        return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)

    def previous(self) -> Path | None:
        """The newest kept venv that is not the current one."""
        current = self.current()
        return next((p for p in self.kept() if p != current), None)

    def switch(self, target: Path) -> None:
        """Point the symlink at ``target`` atomically (CORE-134)."""
        link = self.layout.venv_link
        tmp = link.with_name(f".{link.name}.new")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, link)

    def prune(self, keep: int = KEEP_VENVS) -> list[Path]:
        """Delete the oldest venvs beyond ``keep``; never the current or previous.

        Returns:
            The deleted venvs.
        """
        protected = {self.current(), self.previous()}
        removed = []
        for path in self.kept()[keep:]:
            if path in protected:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
        return removed

    def discard(self, venv: Path) -> None:
        """Delete a staged venv that was never switched to."""
        if venv != self.current():
            shutil.rmtree(venv, ignore_errors=True)

    # --- children -------------------------------------------------------------------

    def _home(self, name: str) -> Path:
        home = self.layout.tmp / f"home-{name}"
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(mode=0o700, parents=True)
        if self.build_owner is not None:
            os.chown(home, *self.build_owner)
        return home

    def _run(self, step: str, argv: list[str], home: Path) -> str:
        done = self.runner.run(argv, user=BUILD_USER, home=home)
        if done.returncode != 0:
            raise BuildError(f"{step} failed (exit {done.returncode}): {done.output.strip()}")
        return done.output

    def freeze(self, venv: Path) -> Freeze:
        """The distributions installed in ``venv`` (read as ``mm3-build``).

        Raises:
            BuildError: The interpreter cannot list them.
        """
        home = self._home("freeze")
        try:
            out = self._run("freeze", [str(venv / "bin" / "python"), "-I", "-c", _FREEZE], home)
        finally:
            self.runner.reap(BUILD_USER)
            shutil.rmtree(home, ignore_errors=True)
        try:
            rows = json.loads(out.strip().splitlines()[-1])
            dists = [
                Dist(r["name"], r["version"], tuple(r["requires"]), bool(r.get("module")))
                for r in rows
            ]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise BuildError(f"freeze returned no list: {exc}") from exc
        return {d.key: d for d in dists}

    def pins(
        self,
        old: Freeze,
        *,
        drop: Iterable[str],
        local_wheels: Mapping[str, Path],
    ) -> list[str]:
        """The pip arguments that reinstall ``old`` except ``drop``.

        Args:
            old: The current venv.
            drop: Normalized names to leave out.
            local_wheels: Kept wheel files of modules installed from local
                paths, by normalized name; they are installed from the file.

        Returns:
            ``name==version`` pins and wheel paths, sorted.
        """
        skip = set(drop) | TOOLS
        out = []
        for key, dist in sorted(old.items()):
            if key in skip:
                continue
            wheel = local_wheels.get(key)
            out.append(str(wheel) if wheel else f"{dist.name}=={dist.version}")
        return out

    def build(self, requirements: list[str], now: datetime) -> Path:
        """Build a new venv with ``requirements`` and make it read-only.

        Args:
            requirements: Pins and wheel files for ``pip install``.
            now: The build's time, which names the directory.

        Returns:
            The new venv (not switched to).

        Raises:
            BuildError: ``venv`` or ``pip`` failed; the directory is removed.
        """
        root = self.layout.venvs
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
        target = root / _stamp(now)
        n = 1
        while target.exists():
            n += 1
            target = root / f"{_stamp(now)}-{n}"
        target.mkdir(mode=0o755)
        if self.build_owner is not None:
            os.chown(target, *self.build_owner)
        home = self._home("build")
        try:
            self._run("venv", [self.python, "-m", "venv", str(target)], home)
            pip = [
                str(target / "bin" / "python"),
                "-I",
                "-m",
                "pip",
                "install",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--no-input",
                *requirements,
            ]
            self._run("pip install", pip, home)
        except BuildError:
            shutil.rmtree(target, ignore_errors=True)
            raise
        finally:
            self.runner.reap(BUILD_USER)
            shutil.rmtree(home, ignore_errors=True)
        make_read_only(target, self.final_owner)
        return target

    def import_test(self, venv: Path, module: str | None) -> None:
        """Import the module's entry points and the engine, as ``mm3-build``.

        Args:
            venv: The read-only new venv.
            module: The module's distribution name, or None (a removal) to
                import only the engine.

        Raises:
            BuildError: The import failed.
        """
        home = self._home("import")
        python = str(venv / "bin" / "python")
        argv = (
            [python, "-I", "-c", _IMPORT_TEST, module]
            if module
            else [python, "-I", "-c", "import mm3.core.engine"]
        )
        try:
            self._run("import test", argv, home)
        finally:
            self.runner.reap(BUILD_USER)
            shutil.rmtree(home, ignore_errors=True)

    def static_check(self, venv: Path, wheel: Path) -> None:
        """Run ``mm3 module check --only static`` on the wheel, as ``mm3-build``.

        Raises:
            BuildError: A MUST failed (exit 1) or the check could not run.
        """
        home = self._home("check")
        argv = [str(venv / "bin" / "mm3"), "module", "check", "--only", "static", str(wheel)]
        try:
            self._run("mm3 module check", argv, home)
        finally:
            self.runner.reap(BUILD_USER)
            shutil.rmtree(home, ignore_errors=True)
