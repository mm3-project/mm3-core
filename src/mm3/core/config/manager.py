"""Hot reload: triggers, validation, and applying the new configuration.

A reload starts 500 ms after the last change to a watched file (main file,
includes, widget templates, custom.css, env file), on SIGHUP,
or after a history operation. Reloads run one at a time, and changes that
arrive during a reload cause exactly one more. An invalid configuration
never replaces the running one; a valid one is recorded as a version and
then applied in the order of CORE-210.

Implements:
    CORE-02: Core spec 0.9, §3 Engine components.
    CORE-208: Core spec 0.11, §6.3 Hot reload.
    CORE-209, CORE-210, CORE-245: Core spec 0.9, §6.3 Hot reload.
    CORE-211: Core spec 0.19, §6.3 Hot reload.
    MOD-50: Module Contract 1.0 rev 6, §6.2 Configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mm3.core.config.assets import CUSTOM_CSS, TEMPLATE_DIR
from mm3.core.config.diff import ReloadPlan, plan_reload
from mm3.core.config.validate import ConfigResult, load_config
from mm3.core.config.watch import ConfigWatcher
from mm3.core.logging import get_logger, set_log_level
from mm3.core.registry import Registry
from mm3.core.supervisor import State, has_backend

if TYPE_CHECKING:
    from mm3.core.engine import Engine

__all__ = [
    "DEBOUNCE_S",
    "ERRORS_IN_EVENT",
    "ConfigManager",
    "MemoryVersions",
    "Origin",
    "VersionRecorder",
]

DEBOUNCE_S = 0.5
ERRORS_IN_EVENT = 20
Origin = Literal["initial", "file", "sighup", "history"]

_log = get_logger(subsystem="config")


class VersionRecorder(Protocol):
    """Records valid configurations as numbered versions (core spec 6.8)."""

    async def record(
        self, result: ConfigResult, sections: list[str], origin: Origin, changed: bool = True
    ) -> int:
        """Record a configuration and return its version number.

        ``changed`` is False when applying it changes nothing (formatting
        only); a recorder may still make a version (CORE-216).
        """
        ...


class MemoryVersions:
    """Version numbers kept in memory until the configuration history exists.

    The running configuration is version 1; each applied change adds one.
    """

    def __init__(self) -> None:
        self.version = 0

    async def record(
        self, result: ConfigResult, sections: list[str], origin: Origin, changed: bool = True
    ) -> int:
        """Return the next version number, or the same one when nothing changed.

        Args:
            result: The configuration (unused).
            sections: Its changed sections (unused).
            origin: What caused it (unused).
            changed: Whether applying it changes anything.

        Returns:
            The version number.
        """
        if changed or self.version == 0:
            self.version += 1
        return self.version


LayoutListener = Callable[[ConfigResult, ReloadPlan], Awaitable[None]]


class ConfigManager:
    """Keeps the running configuration and applies changes to the engine.

    Args:
        engine: The running engine.
        current: The configuration the engine was started with (valid).
        registry: Installed modules, used to validate.
        env_file: The env file (``--env-file``).
        recorder: Records versions; in memory by default.
        debounce: Delay after the last change event (CORE-208).
        environ: Process environment for ``!env``; ``os.environ`` by default.

    Implements:
        CORE-208: Core spec 0.11, §6.3 Hot reload.
        CORE-209, CORE-210: Core spec 0.9, §6.3 Hot reload.
    """

    def __init__(
        self,
        engine: Engine,
        current: ConfigResult,
        *,
        registry: Registry,
        env_file: Path,
        recorder: VersionRecorder | None = None,
        debounce: float = DEBOUNCE_S,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not current.ok or current.config is None:
            raise ValueError("the starting configuration must be valid")
        self.engine = engine
        self.current = current
        self.registry = registry
        self.env_file = env_file
        self.recorder: VersionRecorder = recorder or MemoryVersions()
        self.debounce = debounce
        self.environ = environ
        self.version = 0
        self.disk_valid = True
        self.last_records: list[Any] = []
        self.layout_listeners: list[LayoutListener] = []
        self.applied: tuple[int, int] | None = None
        self._attempted_files: set[Path] = set(current.loaded.files)
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: Origin | None = None
        self._watcher: ConfigWatcher | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    # --- state ---------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return the ``config`` part of ``GET /api/v1/status`` (CORE-209).

        Returns:
            ``{version, disk_valid}``.
        """
        return {"version": self.version, "disk_valid": self.disk_valid}

    def watched_files(self) -> set[Path]:
        """Return the files whose changes start a reload (CORE-208).

        Returns:
            The main file, every included file seen, custom.css (whether it
            exists or not) and the env file.

        Implements:
            CORE-208: Core spec 0.11, §6.3 Hot reload.
        """
        css = self.current.path.parent / CUSTOM_CSS
        return {*self.current.loaded.files, *self._attempted_files, css, self.env_file}

    def watched_directories(self) -> set[Path]:
        """Return the directories watched as a whole: the widget template directory.

        Returns:
            ``<config dir>/widget-templates``.
        """
        return {self.current.path.parent / TEMPLATE_DIR}

    async def start(self, *, watch: bool = True) -> None:
        """Record the running configuration as the first version and start watching.

        Args:
            watch: Start the file watcher.
        """
        self.version = await self.recorder.record(self.current, [], "initial")
        if watch:
            self._watcher = ConfigWatcher(
                self.watched_files, self.watched_directories, self.notify_change
            )
            self._watcher.start()

    async def close(self) -> None:
        """Stop watching and wait for a running reload to finish."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._watcher is not None:
            await self._watcher.close()
            self._watcher = None
        self._pending = None
        await self.idle()

    async def idle(self) -> None:
        """Wait until no reload is running or queued."""
        await self._idle.wait()

    # --- triggers ------------------------------------------------------------------

    def notify_change(self) -> None:
        """A watched file changed: reload 500 ms after the last such event (CORE-208)."""
        if self._timer is not None:
            self._timer.cancel()
        loop = asyncio.get_running_loop()
        self._idle.clear()
        self._timer = loop.call_later(self.debounce, self._fire)

    def _fire(self) -> None:
        self._timer = None
        self.request_reload("file")

    def request_reload(self, origin: Origin = "sighup") -> None:
        """Start a reload now, or exactly one more after the running one (CORE-208).

        Args:
            origin: What asked for it.
        """
        self._idle.clear()
        if self._task is not None and not self._task.done():
            self._pending = origin
            return
        self._task = asyncio.create_task(self._loop(origin), name="mm3-config-reload")

    async def _loop(self, origin: Origin) -> None:
        try:
            next_origin: Origin | None = origin
            while next_origin is not None:
                self._pending = None
                try:
                    await self.reload(next_origin)
                except Exception as exc:  # a failed reload keeps the engine running
                    _log.exception("config_reload_failed", error=f"{type(exc).__name__}: {exc}")
                next_origin = self._pending
        finally:
            if self._timer is None:
                self._idle.set()

    # --- reload --------------------------------------------------------------------

    async def reload(self, origin: Origin = "file") -> bool:
        """Load, validate and, when valid, apply the configuration on disk.

        Args:
            origin: What caused the reload.

        Returns:
            True when a new configuration was applied.

        Implements:
            CORE-209, CORE-210: Core spec 0.9, §6.3 Hot reload.
        """
        new = await asyncio.to_thread(
            load_config,
            self.current.path,
            registry=self.registry,
            env_file=self.env_file,
            environ=self.environ,
            applied=self.applied,
        )
        self._attempted_files = set(new.loaded.files)
        self.last_records = list(new.records)
        if self._watcher is not None:
            self._watcher.refresh()
        if not new.ok or new.config is None:
            self._reject(new)
            return False
        self.disk_valid = True
        for rec in new.warnings:
            _log.warning("config_warning", **rec.as_dict())
        plan = plan_reload(self.current, new)
        if plan.empty:
            # Formatting only: no change to apply, but the history may record
            # a version with no changed sections (CORE-216).
            self.current = new
            version = await self.recorder.record(new, [], origin, changed=False)
            if version != self.version:
                self.version = version
                self.engine.supervisor.emit(
                    "CORE_CONFIG_CHANGED", {"version": version, "sections": []}
                )
            return False
        self.version = await self.recorder.record(new, plan.sections, origin)
        old = self.current
        self.current = new
        await self._apply(old, new, plan)
        self.engine.supervisor.emit(
            "CORE_CONFIG_CHANGED", {"version": self.version, "sections": plan.sections}
        )
        _log.info("config_applied", version=self.version, origin=origin, sections=plan.sections)
        return True

    async def set_applied(self, mode: tuple[int, int] | None) -> bool:
        """Use the mode the kiosk applied for the layout (CORE-145, CORE-146, CORE-215).

        The running configuration is validated again with that mode. When it
        stays valid it replaces the running one (no new version: no file
        changed), the shells resync and ``CORE_DISPLAY_CHANGED`` is emitted
        if the viewport changed. When it does not, the running configuration
        stays and the errors are reported as for a reload (CORE-209).

        Args:
            mode: ``(width, height)`` as applied, in the monitor's orientation.

        Returns:
            True when the viewport changed.

        Implements:
            CORE-146: Core spec 0.16, §6.7 Display output.
        """
        if mode == self.applied:
            return False
        self.applied = mode
        new = await asyncio.to_thread(
            load_config,
            self.current.path,
            registry=self.registry,
            env_file=self.env_file,
            environ=self.environ,
            applied=mode,
        )
        if not new.ok or new.config is None or new.layout is None:
            self._reject(new)
            return False
        old = self.current.layout
        self.current = new
        view = new.layout.viewport
        if old is not None and (old.viewport.width, old.viewport.height) == (
            view.width,
            view.height,
        ):
            return False
        for listener in self.layout_listeners:
            await listener(new, ReloadPlan(display=True))
        display = new.config.display
        self.engine.supervisor.emit(
            "CORE_DISPLAY_CHANGED",
            {
                "viewport": [view.width, view.height],
                "scale": display.scale,
                "rotation": display.rotation,
            },
        )
        _log.info("display_applied", width=view.width, height=view.height)
        return True

    def _reject(self, new: ConfigResult) -> None:
        """Keep the running configuration and report the errors (CORE-209)."""
        self.disk_valid = False
        for rec in new.records:
            (_log.error if rec.severity == "error" else _log.warning)(
                "config_invalid", **rec.as_dict()
            )
        errors = new.errors
        self.engine.supervisor.emit(
            "CORE_CONFIG_ERROR",
            {
                "count": len(errors),
                "errors": [
                    {"file": r.file, "line": r.line, "path": r.path, "code": r.code}
                    for r in errors[:ERRORS_IN_EVENT]
                ],
            },
        )

    async def _apply(self, old: ConfigResult, new: ConfigResult, plan: ReloadPlan) -> None:
        """Apply a plan in the order of CORE-210.

        A changed module ``config`` goes to ``reconfigure()``; the instance
        keeps running when it returns True and is restarted otherwise (MOD-50).

        Implements:
            CORE-210, CORE-245: Core spec 0.9, §6.3 Hot reload.
            CORE-211: Core spec 0.19, §6.3 Hot reload.
            MOD-50: Module Contract 1.0 rev 6, §6.2 Configuration.
        """
        assert new.config is not None  # noqa: S101 - checked by reload()
        sup = self.engine.supervisor
        settings = new.config.engine
        if plan.engine_restart:
            _log.warning("config_needs_restart", keys=sorted(plan.engine_restart))
        sup.apply_settings(settings)
        if "log_level" in plan.engine_live:
            set_log_level(settings.log_level)

        specs = {s.instance_id: s for s in new.instance_specs()}
        touched: set[str] = set()
        # 1. Stop removed and replaced instances.
        for iid in [*plan.stop, *plan.replace]:
            with contextlib.suppress(KeyError):
                await sup.remove(iid)
            touched.add(iid)
        # 2. Reconfigure changed instances; restart those whose grants changed.
        for iid, cfg in plan.reconfigure.items():
            inst = sup.get(iid)
            inst.spec = replace(inst.spec, locale=specs[iid].locale)
            await inst.reconfigure(cfg)
        for iid in plan.restart:
            inst = sup.get(iid)
            inst.spec = specs[iid]
            await inst.restart()
            touched.add(iid)
        # 3. Start added instances.
        for iid in [*plan.replace, *plan.start]:
            sup.add(specs[iid])
            touched.add(iid)
        await sup.start_all()
        # Live engine values that need more than the new settings.
        if plan.engine_live & {"units", "location"}:
            for inst in sup.instances():
                entry = self.registry.get(inst.spec.module)
                cls = entry.cls if entry is not None else None
                running = inst.state in (State.RUNNING, State.SUSPENDED)
                if inst.instance_id not in touched and running and cls and has_backend(cls):
                    await inst.restart()
        for iid, locale in plan.locale.items():
            if iid in touched:
                continue
            sup.get(iid).set_locale(locale)
            sup.emit("CORE_LOCALE_CHANGED", {"locale": locale or settings.locale}, target=iid)
        if "locale" in plan.engine_live:
            sup.emit("CORE_LOCALE_CHANGED", {"locale": settings.locale})
        # 4. Tell the browsers; each listener decides what the plan means for it.
        for listener in self.layout_listeners:
            await listener(new, plan)
