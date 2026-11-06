"""Home Assistant over MQTT: discovery, the engine's entities and modules' entities.

With ``engine.mqtt.ha_discovery`` on, the bridge publishes one retained
discovery config per entity, all on one device, and keeps their states.
The engine's own entities (reload, page, kiosk volume and mute, and per
instance a visibility and an enabled switch) are defined here; a module adds
its own through ``ctx.mqtt.ha_entity()``. The topics the bridge published are
recorded in ``<state>/mqtt/discovery.json``, so entities that are gone are
cleared from Home Assistant (CORE-343 to CORE-345).

Implements:
    CORE-343: Core spec 0.34, §9.3 MQTT bridge.
    CORE-344: Core spec 0.34, §9.3 MQTT bridge.
    CORE-345: Core spec 0.34, §9.3 MQTT bridge.
    CORE-236: Core spec 0.34, §6.9 Pages and visibility.
    CORE-240: Core spec 0.34, §6.9 Pages and visibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from mm3.__about__ import __version__
from mm3.core.i18n.catalogs import locale_chain
from mm3.core.logging import get_logger
from mm3.core.services.mqtt import HaEntityHandle, HaEntitySpec
from mm3.core.settings import MqttSettings
from mm3.errors import TransientError
from mm3.messages import Notification
from mm3.mqtt import MqttMessage

if TYPE_CHECKING:
    from mm3.core.i18n import I18n
    from mm3.core.services.mqttbridge import MqttBridge
    from mm3.core.supervisor import State, Supervisor

__all__ = ["ENGINE", "HaControls", "HaService", "state_text"]

ENGINE = "_engine"
"""The instance name of the engine's own entities (CORE-343)."""
MANUFACTURER = "MagicMirror 3"
_STATEFUL = ("sensor", "binary_sensor", "switch", "number", "select")
_COMMANDS = ("switch", "number", "select", "button")
_ON = ("ON", "TRUE", "1")
_OFF = ("OFF", "FALSE", "0")

_log = get_logger(subsystem="mqtt")


class PagesControl(Protocol):
    """What the engine's page and visibility entities need from the page service."""

    listeners: list[Callable[[], None]]

    @property
    def page(self) -> str:
        """Current page id."""
        ...

    @property
    def ids(self) -> list[str]:
        """Page ids in order."""
        ...

    def visible(self, instance_id: str) -> bool:
        """Whether no hide reason applies."""
        ...

    def set_page(self, page: str, reason: Any) -> None:
        """Ask for a page change."""
        ...

    def set_manual(self, instance_id: str, hidden: bool) -> None:
        """Set or clear the ``manual`` hide reason."""
        ...


class _AgentStatus(Protocol):
    @property
    def connected(self) -> bool: ...


class KioskAudio(Protocol):
    """What the kiosk entities need from the kiosk agents (CORE-296, CORE-306)."""

    audio_listeners: list[Callable[[], None]]

    @property
    def status(self) -> _AgentStatus:
        """The agent's status; ``connected`` tells whether a kiosk is there."""
        ...

    def audio_block(self) -> dict[str, Any] | None:
        """The audio values the kiosk applies."""
        ...

    async def set_runtime_audio(
        self, *, volume: int | None = None, mute: bool | None = None
    ) -> bool:
        """Set run-time volume or mute; False without an agent."""
        ...


@dataclass
class HaControls:
    """What Home Assistant can steer, as far as this mirror has it.

    Attributes:
        pages: The page service; without it there are no page and visibility entities.
        kiosk: The kiosk agents; without them there are no volume and mute entities.
        reload_shells: Reloads every browser (``POST /api/v1/shell/reload``).
    """

    pages: PagesControl | None = None
    kiosk: KioskAudio | None = None
    reload_shells: Callable[[], None] | None = None


def state_text(value: str | float | bool | None) -> str:
    """Return the payload of an entity state (CORE-344).

    Args:
        value: A bool, a number, a string, or ``None`` for unknown.

    Returns:
        ``ON`` or ``OFF`` for a bool, ``None`` for unknown (Home Assistant shows
        the entity as unknown), the text of a number or string otherwise.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    return str(value)


@dataclass(frozen=True)
class _EngineEntity:
    """One of the engine's entities."""

    object_id: str
    component: str
    key: str
    values: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)
    state: Callable[[], str] | None = None
    command: Callable[[str], Awaitable[None]] | None = None


class _ModuleEntity:
    """A module's entity as the service tracks it."""

    def __init__(
        self,
        service: HaService,
        instance: str,
        spec: HaEntitySpec,
        deliver: Callable[[MqttMessage], None],
    ) -> None:
        self._service = service
        self.instance = instance
        self.spec = spec
        self.deliver = deliver
        self.object_id = spec.object_id
        self.live = True
        self.state: str | None = None

    async def set_state(self, value: str | float | bool | None) -> None:
        """Publish the state; kept and sent again after every connect (CORE-344)."""
        self.state = state_text(value)
        if self.live:
            await self._service.publish_module_state(self)

    async def remove(self) -> None:
        """Clear the entity in Home Assistant now; idempotent (CORE-345)."""
        if not self.live:
            return
        await self._service.remove_module_entity(self)

    def detach(self) -> None:
        """Forget the live entity; Home Assistant keeps it (the instance stopped)."""
        self._service.detach_module_entity(self)


class HaService:
    """Discovery and entities of the MQTT bridge.

    Args:
        bridge: The bridge, for the connection and the settings.
        supervisor: Instances and the engine locale.
        i18n: Catalogs that name the entities.
        state_dir: Engine state directory.

    Implements:
        CORE-343: Core spec 0.34, §9.3 MQTT bridge.
        CORE-344: Core spec 0.34, §9.3 MQTT bridge.
        CORE-345: Core spec 0.34, §9.3 MQTT bridge.
    """

    def __init__(
        self, bridge: MqttBridge, supervisor: Supervisor, i18n: I18n, state_dir: Path
    ) -> None:
        self._bridge = bridge
        self._sup = supervisor
        self._i18n = i18n
        self._file = state_dir / "mqtt" / "discovery.json"
        self._settings: MqttSettings | None = None
        self._controls = HaControls()
        self._modules: dict[tuple[str, str], _ModuleEntity] = {}
        self._seen: dict[str, set[str]] = {}
        self._recorded: dict[str, dict[str, str]] | None = None
        self._configs: dict[str, str] = {}
        self._states: dict[str, str] = {}
        self._cancels: dict[str, Callable[[], None]] = {}
        self._lock = asyncio.Lock()
        self._jobs: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._pending: set[str] = set()
        self._worker: asyncio.Task[None] | None = None
        supervisor.state_listeners.append(self._instance_state)

    # --- wiring ---------------------------------------------------------------------

    def attach_controls(self, controls: HaControls) -> None:
        """Say what the engine entities can steer; call before the engine starts.

        Args:
            controls: Page service, kiosk agents and shell reload.
        """
        self._controls = controls
        if controls.pages is not None:
            controls.pages.listeners.append(self._changed)
        if controls.kiosk is not None:
            controls.kiosk.audio_listeners.append(self._changed)

    def _changed(self) -> None:
        self._request("states")

    def _instance_state(self, instance_id: str, old: State, new: State) -> None:
        if new.value == "STARTING":
            self._seen[instance_id] = set()
        self._request("states")

    @property
    def discovery(self) -> bool:
        """Whether discovery is on (``ha_discovery`` of a configured bridge)."""
        return self._settings is not None and self._settings.ha_discovery

    def on_notification(self, n: Notification) -> None:
        """Follow the engine events that change an entity (page, config, locale)."""
        if self._settings is None or not self._settings.ha_discovery:
            return
        if n.name == "CORE_LOCALE_CHANGED" and n.target is None:
            self._request("resend")
        elif n.name in ("CORE_PAGE_CHANGED", "CORE_CONFIG_CHANGED"):
            self._request("sync")

    # --- life cycle -----------------------------------------------------------------

    async def apply(self, settings: MqttSettings | None) -> None:
        """Use new bridge settings: move the command subscriptions (CORE-343).

        With ``None`` (the bridge is switched off) the entities already published
        are cleared while the broker is still connected.

        Args:
            settings: ``engine.mqtt``, ``None`` for off.
        """
        if settings is None and self._settings is not None and self._bridge.connected:
            async with self._lock:
                self._settings = None
                with contextlib.suppress(TransientError):
                    await self._sweep_locked()
        self._settings = settings
        self._configs.clear()
        self._states.clear()
        self._resubscribe()
        if settings is not None and self._worker is None:
            self._worker = asyncio.create_task(self._work(), name="mm3-mqtt-ha")

    async def close(self) -> None:
        """Stop the worker and drop the subscriptions (engine stop)."""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        for cancel in self._cancels.values():
            cancel()
        self._cancels.clear()
        self._settings = None

    async def connected(self) -> None:
        """After every connect: publish everything again (CORE-343)."""
        self._configs.clear()
        self._states.clear()
        await self.sync()

    async def sync(self) -> None:
        """Publish configs and states that are missing, and clear stale entities.

        Called after the instances of a start or of a hot reload have started
        (CORE-345) and after every connect. It does nothing while the broker is
        away; the next connect does it all.
        """
        async with self._lock:
            await self._sync_locked()

    # --- jobs -----------------------------------------------------------------------

    def _request(self, kind: str) -> None:
        if self._worker is None or kind in self._pending:
            return
        self._pending.add(kind)
        self._jobs.put_nowait((kind, None))

    async def _work(self) -> None:
        while True:
            kind, arg = await self._jobs.get()
            self._pending.discard(kind)
            try:
                if kind == "cmd":
                    await self._run_command(*arg)
                    self._request("states")
                elif kind == "resend":
                    self._configs.clear()
                    await self.sync()
                elif kind == "sync":
                    await self.sync()
                elif kind == "states":
                    async with self._lock:
                        await self._states_locked()
            except TransientError:
                _log.debug("mqtt_ha_job_deferred", job=kind)
            except Exception as exc:  # one bad job never stops Home Assistant
                _log.warning("mqtt_ha_job_failed", job=kind, error=type(exc).__name__)

    # --- subscriptions --------------------------------------------------------------

    def _topic(self, instance: str, object_id: str, leaf: str) -> str:
        assert self._settings is not None  # noqa: S101 - callers check
        return f"{self._settings.base_topic}/ha/{instance}/{object_id}/{leaf}"

    def _resubscribe(self) -> None:
        """Subscribe to the command topics Home Assistant needs now (CORE-343, CORE-344)."""
        wanted: dict[str, Callable[[MqttMessage], None]] = {}
        s = self._settings
        if s is not None and s.ha_discovery:
            wanted[f"{s.base_topic}/ha/{ENGINE}/+/set"] = self._on_engine_command
            wanted[f"{s.discovery_prefix}/status"] = self._on_ha_status
            for ent in self._modules.values():
                if ent.spec.commands:
                    wanted[self._topic(ent.instance, ent.object_id, "set")] = ent.deliver
        for topic in [t for t in self._cancels if t not in wanted]:
            self._cancels.pop(topic)()
        for topic, deliver in wanted.items():
            if topic not in self._cancels:
                self._subscribe_later(topic, deliver)

    def _subscribe_later(self, topic: str, deliver: Callable[[MqttMessage], None]) -> None:
        self._cancels[topic] = self._bridge.connection.add_subscription(topic, 1, deliver)

    def _on_engine_command(self, message: MqttMessage) -> None:
        parts = message.topic.split("/")
        if len(parts) >= 2 and self._worker is not None:
            self._jobs.put_nowait(("cmd", (parts[-2], message.text())))

    def _on_ha_status(self, message: MqttMessage) -> None:
        if message.text().strip().lower() == "online":
            self._request("resend")

    # --- entities of modules --------------------------------------------------------

    async def register(
        self, instance_id: str, spec: HaEntitySpec, deliver: Callable[[MqttMessage], None]
    ) -> HaEntityHandle:
        """Register a module's entity (CORE-344).

        With discovery off the entity is kept but publishes nothing; it appears
        when discovery is switched on.

        Args:
            instance_id: Owner instance.
            spec: What the module asked for.
            deliver: Puts a command into the instance inbox.

        Returns:
            The entity.
        """
        key = (instance_id, spec.object_id)
        old = self._modules.get(key)
        if old is not None:
            old.live = False
        entity = _ModuleEntity(self, instance_id, spec, deliver)
        self._modules[key] = entity
        self._seen.setdefault(instance_id, set()).add(spec.object_id)
        if self.discovery:
            self._resubscribe()
            if self._bridge.connected:
                async with self._lock:
                    with contextlib.suppress(TransientError):
                        await self._publish_module_config(entity)
        return entity

    async def publish_module_state(self, entity: _ModuleEntity) -> None:
        """Publish a module entity's state when discovery is on and the broker is connected."""
        if not self.discovery or not self._bridge.connected or entity.state is None:
            return
        topic = self._topic(entity.instance, entity.object_id, "state")
        if entity.spec.component == "button":
            return
        await self._bridge.connection.publish(topic, entity.state.encode(), 1, True)
        self._states[topic] = entity.state

    def detach_module_entity(self, entity: _ModuleEntity) -> None:
        """Forget a live entity without clearing it (its instance stopped)."""
        entity.live = False
        key = (entity.instance, entity.object_id)
        if self._modules.get(key) is entity:
            del self._modules[key]
        if self._settings is not None:
            self._resubscribe()

    async def remove_module_entity(self, entity: _ModuleEntity) -> None:
        """Clear a module's entity in Home Assistant at once (CORE-345)."""
        self.detach_module_entity(entity)
        if self._recorded is None:
            self._load()
        topic = self._config_topic(entity.instance, entity.object_id, entity.spec.component)
        self._configs.pop(topic, None)
        records = self._records()
        if topic in records and self._bridge.connected:
            async with self._lock:
                with contextlib.suppress(TransientError):
                    await self._bridge.connection.publish(topic, b"", 1, True)
                    records.pop(topic, None)
                    await asyncio.to_thread(self._save)

    # --- the engine's entities ------------------------------------------------------

    def _engine_entities(self) -> list[_EngineEntity]:
        c = self._controls
        out: list[_EngineEntity] = []
        if c.reload_shells is not None:
            reload = c.reload_shells

            async def do_reload(_: str) -> None:
                reload()

            out.append(_EngineEntity("reload", "button", "CORE_HA_RELOAD", command=do_reload))
        if c.pages is not None:
            pages = c.pages

            async def set_page(text: str) -> None:
                if text in pages.ids:
                    pages.set_page(text, "ha")

            out.append(
                _EngineEntity(
                    "page",
                    "select",
                    "CORE_HA_PAGE",
                    extra={"options": list(pages.ids)},
                    state=lambda: pages.page,
                    command=set_page,
                )
            )
        if c.kiosk is not None:
            out.extend(self._kiosk_entities(c.kiosk))
        for inst in self._sup.instances():
            out.extend(self._instance_entities(inst.instance_id))
        return out

    def _kiosk_entities(self, kiosk: KioskAudio) -> list[_EngineEntity]:
        def value(field_: str) -> Any:
            if not kiosk.status.connected:
                return None
            block = kiosk.audio_block()
            return None if block is None else block.get(field_)

        def volume_state() -> str:
            v = value("volume")
            return state_text(None if v is None else int(v))

        def mute_state() -> str:
            v = value("mute")
            return state_text(None if v is None else bool(v))

        async def set_volume(text: str) -> None:
            try:
                volume = round(float(text))
            except ValueError:
                return
            await kiosk.set_runtime_audio(volume=max(0, min(100, volume)))

        async def set_mute(text: str) -> None:
            flag = _flag(text)
            if flag is not None:
                await kiosk.set_runtime_audio(mute=flag)

        return [
            _EngineEntity(
                "kiosk_volume",
                "number",
                "CORE_HA_KIOSK_VOLUME",
                extra={
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "unit_of_measurement": "%",
                    "mode": "slider",
                },
                state=volume_state,
                command=set_volume,
            ),
            _EngineEntity(
                "kiosk_mute", "switch", "CORE_HA_KIOSK_MUTE", state=mute_state, command=set_mute
            ),
        ]

    def _instance_entities(self, iid: str) -> list[_EngineEntity]:
        out: list[_EngineEntity] = []
        values = {"instance": iid}
        pages = self._controls.pages
        if pages is not None:

            async def set_visible(text: str) -> None:
                flag = _flag(text)
                if flag is not None and self._has_element(iid):
                    pages.set_manual(iid, not flag)

            out.append(
                _EngineEntity(
                    f"{iid}_visible",
                    "switch",
                    "CORE_HA_INSTANCE_VISIBLE",
                    values,
                    state=lambda: state_text(pages.visible(iid)),
                    command=set_visible,
                )
            )

        def enabled() -> str:
            try:
                return state_text(not self._sup.get(iid).runtime_stopped)
            except KeyError:
                return state_text(None)

        async def set_enabled(text: str) -> None:
            flag = _flag(text)
            if flag is None:
                return
            inst = self._sup.get(iid)
            await (inst.runtime_start() if flag else inst.runtime_stop())

        out.append(
            _EngineEntity(
                f"{iid}_enabled",
                "switch",
                "CORE_HA_INSTANCE_ENABLED",
                values,
                state=enabled,
                command=set_enabled,
            )
        )
        return out

    def _has_element(self, iid: str) -> bool:
        try:
            module = self._sup.get(iid).spec.module
        except KeyError:
            return False
        entry = self._sup.registry.get(module)
        return entry is not None and entry.cls is not None and entry.cls.element is not None

    async def _run_command(self, object_id: str, text: str) -> None:
        for ent in self._engine_entities():
            if ent.object_id == object_id and ent.command is not None:
                await ent.command(text)
                return
        _log.debug("mqtt_ha_command_unknown", object=object_id)

    # --- discovery configs ----------------------------------------------------------

    def _chain(self) -> list[str]:
        return locale_chain(None, self._sup.settings.locale)

    def _device(self) -> dict[str, Any]:
        s = self._settings
        assert s is not None  # noqa: S101 - callers check
        location = self._sup.settings.location
        name = (
            location.name.resolve(self._chain())
            if location is not None and location.name is not None
            else s.node_id
        )
        return {
            "identifiers": [s.node_id],
            "name": name,
            "manufacturer": MANUFACTURER,
            "sw_version": __version__,
        }

    def _config_topic(self, instance: str, object_id: str, component: str) -> str:
        s = self._settings
        assert s is not None  # noqa: S101 - callers check
        return f"{s.discovery_prefix}/{component}/{s.node_id}/{instance}-{object_id}/config"

    def _config(
        self,
        instance: str,
        object_id: str,
        component: str,
        name: str,
        extra: Mapping[str, Any],
        *,
        commands: bool,
    ) -> dict[str, Any]:
        s = self._settings
        assert s is not None  # noqa: S101 - callers check
        cfg: dict[str, Any] = {
            "name": name,
            "unique_id": f"{s.node_id}-{instance}-{object_id}",
            "device": self._device(),
            "availability_topic": f"{s.base_topic}/state",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        if component in _STATEFUL:
            cfg["state_topic"] = self._topic(instance, object_id, "state")
        if commands and component in _COMMANDS:
            cfg["command_topic"] = self._topic(instance, object_id, "set")
        if component == "switch":
            cfg.update(payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF")
        elif component == "binary_sensor":
            cfg.update(payload_on="ON", payload_off="OFF")
        elif component == "button":
            cfg["payload_press"] = "PRESS"
        cfg.update(extra)
        return cfg

    def _module_config(self, ent: _ModuleEntity) -> dict[str, Any]:
        spec = ent.spec
        extra: dict[str, Any] = {}
        if spec.device_class:
            extra["device_class"] = spec.device_class
        if spec.unit:
            extra["unit_of_measurement"] = spec.unit
        if spec.icon:
            extra["icon"] = spec.icon
        if spec.options:
            extra["options"] = list(spec.options)
        for key in ("min", "max", "step"):
            if (v := getattr(spec, key)) is not None:
                extra[key] = v
        name = self._i18n.translate(spec.module or None, self._chain(), spec.name)
        return self._config(
            ent.instance, spec.object_id, spec.component, name, extra, commands=spec.commands
        )

    async def _publish_config(self, topic: str, cfg: Mapping[str, Any]) -> None:
        text = json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)
        if self._configs.get(topic) == text:
            return
        await self._bridge.connection.publish(topic, text.encode(), 1, True)
        self._configs[topic] = text

    async def _publish_module_config(self, ent: _ModuleEntity) -> None:
        topic = self._config_topic(ent.instance, ent.object_id, ent.spec.component)
        await self._publish_config(topic, self._module_config(ent))
        self._records()[topic] = {"instance": ent.instance, "object": ent.object_id}
        await asyncio.to_thread(self._save)
        await self.publish_module_state(ent)

    # --- records (CORE-345) ---------------------------------------------------------

    def _load(self) -> None:
        self._recorded = {}
        try:
            data = json.loads(self._file.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            _log.warning("mqtt_discovery_record_unreadable", file=str(self._file))
            return
        entities = data.get("entities") if isinstance(data, dict) else None
        if isinstance(entities, dict):
            for topic, rec in entities.items():
                if isinstance(topic, str) and isinstance(rec, dict):
                    inst, obj = rec.get("instance"), rec.get("object")
                    if isinstance(inst, str) and isinstance(obj, str):
                        self._recorded[topic] = {"instance": inst, "object": obj}

    def _records(self) -> dict[str, dict[str, str]]:
        if self._recorded is None:
            self._load()
        assert self._recorded is not None  # noqa: S101 - set by _load
        return self._recorded

    def _save(self) -> None:
        records = self._records()
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"entities": records}, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self._file)

    def _is_stale(self, topic: str, rec: Mapping[str, str], live: set[str]) -> bool:
        """Decide whether a recorded entity is gone (CORE-345).

        An entity is gone when discovery is off, when it is an engine entity that
        no longer exists, when its instance is removed or disabled, or when its
        instance is RUNNING and did not register it again. The entities of
        suspended, runtime-stopped and FAILED instances stay.
        """
        if topic in live:
            return False
        if not self.discovery or rec["instance"] == ENGINE:
            return True
        try:
            inst = self._sup.get(rec["instance"])
        except KeyError:
            return True
        seen = self._seen.get(rec["instance"], set())
        return inst.state.value == "RUNNING" and rec["object"] not in seen

    async def _sweep_locked(self, live: set[str] | None = None) -> None:
        records = self._records()
        keep = live if live is not None else set()
        stale = [t for t, r in records.items() if self._is_stale(t, r, keep)]
        for topic in stale:
            await self._bridge.connection.publish(topic, b"", 1, True)
            records.pop(topic, None)
            self._configs.pop(topic, None)
        if stale:
            await asyncio.to_thread(self._save)

    # --- publishing -----------------------------------------------------------------

    async def _sync_locked(self) -> None:
        if self._settings is None or not self._bridge.connected:
            return
        try:
            live: set[str] = set()
            if self.discovery:
                self._resubscribe()
                records = self._records()
                changed = False
                for eng in self._engine_entities():
                    topic = self._config_topic(ENGINE, eng.object_id, eng.component)
                    name = self._i18n.translate(None, self._chain(), eng.key, values=eng.values)
                    cfg = self._config(
                        ENGINE,
                        eng.object_id,
                        eng.component,
                        name,
                        eng.extra,
                        commands=eng.command is not None,
                    )
                    if eng.state is None:
                        cfg.pop("state_topic", None)
                    await self._publish_config(topic, cfg)
                    live.add(topic)
                    if records.get(topic) != {"instance": ENGINE, "object": eng.object_id}:
                        records[topic] = {"instance": ENGINE, "object": eng.object_id}
                        changed = True
                for ent in list(self._modules.values()):
                    topic = self._config_topic(ent.instance, ent.object_id, ent.spec.component)
                    await self._publish_config(topic, self._module_config(ent))
                    live.add(topic)
                    if records.get(topic) != {"instance": ent.instance, "object": ent.object_id}:
                        records[topic] = {"instance": ent.instance, "object": ent.object_id}
                        changed = True
                if changed:
                    await asyncio.to_thread(self._save)
            await self._sweep_locked(live)
            await self._states_locked()
        except TransientError:
            _log.debug("mqtt_ha_sync_deferred")

    async def _states_locked(self) -> None:
        if not self.discovery or not self._bridge.connected:
            return
        for eng in self._engine_entities():
            if eng.state is None:
                continue
            topic = self._topic(ENGINE, eng.object_id, "state")
            text = eng.state()
            if self._states.get(topic) != text:
                await self._bridge.connection.publish(topic, text.encode(), 1, True)
                self._states[topic] = text
        for ent in list(self._modules.values()):
            if ent.state is not None and ent.spec.component != "button":
                topic = self._topic(ent.instance, ent.object_id, "state")
                if self._states.get(topic) != ent.state:
                    await self._bridge.connection.publish(topic, ent.state.encode(), 1, True)
                    self._states[topic] = ent.state


def _flag(text: str) -> bool | None:
    """Read a switch command: ``ON``/``OFF`` (also ``true``/``false`` and ``1``/``0``)."""
    word = text.strip().upper()
    if word in _ON:
        return True
    if word in _OFF:
        return False
    return None
