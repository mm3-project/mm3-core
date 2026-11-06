"""Power hooks: suspend, resume and the display, for modules with ``system.power``.

Presence and power is a module (mm3-presence); the engine offers only these
generic hooks. Instances ask to suspend or resume the mirror; the engine is
suspended only while at least one instance asked to suspend and none holds
power, so two modules never fight over it (CORE-53). The display is turned on
or off through the kiosk agent (CORE-346).

Implements:
    CORE-52: Core spec 0.34, §9.2 Presence and power: module boundary.
    CORE-53: Core spec 0.34, §9.2 Presence and power: module boundary.
    CORE-346: Core spec 0.34, §9.2 Presence and power: module boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from mm3.core.logging import get_logger
from mm3.errors import TransientError

if TYPE_CHECKING:
    from mm3.core.supervisor import State, Supervisor

__all__ = ["DisplayPower", "PowerService"]

_log = get_logger("mm3.power")

_GONE = frozenset({"STOPPING", "STOPPED", "FAILED", "DISABLED"})
"""Instance states in which a standing power request is dropped (CORE-53)."""

DisplayPower = Callable[[bool], Awaitable[None]]
"""Turns the display on or off; raises ``TransientError`` when it cannot (CORE-346)."""


class PowerService:
    """Reference-counted suspend and resume, and the display switch.

    Each instance has at most one standing request: ``request_suspend`` makes
    it a *suspender*, ``request_resume`` makes it a *holder*. The engine is
    suspended while there is a suspender and no holder (CORE-53). An instance
    that stops drops its request.

    Args:
        supervisor: Suspends and resumes the instances.
        display: Turns the display on or off, or ``None`` without a kiosk agent.
    """

    def __init__(self, supervisor: Supervisor, display: DisplayPower | None = None) -> None:
        self.supervisor = supervisor
        self.display = display
        self._suspenders: set[str] = set()
        self._holders: set[str] = set()
        self._target = supervisor.suspended
        self._lock = asyncio.Lock()
        supervisor.state_listeners.append(self._state_changed)

    def close(self) -> None:
        """Stop listening to the supervisor."""
        if self._state_changed in self.supervisor.state_listeners:
            self.supervisor.state_listeners.remove(self._state_changed)

    @property
    def holders(self) -> frozenset[str]:
        """Instance ids that hold power (CORE-53)."""
        return frozenset(self._holders)

    @property
    def suspenders(self) -> frozenset[str]:
        """Instance ids that asked to suspend (CORE-53)."""
        return frozenset(self._suspenders)

    def request_suspend(self, instance_id: str, reason: str) -> None:
        """Record that an instance wants the mirror suspended (CORE-52, CORE-53).

        Args:
            instance_id: The requesting instance.
            reason: Why; logged.
        """
        self._holders.discard(instance_id)
        self._suspenders.add(instance_id)
        _log.info("power_suspend_requested", instance=instance_id, reason=reason)
        self._apply()

    def request_resume(self, instance_id: str, reason: str) -> None:
        """Record that an instance holds power: the mirror stays resumed (CORE-53).

        Args:
            instance_id: The requesting instance.
            reason: Why; logged.
        """
        self._suspenders.discard(instance_id)
        self._holders.add(instance_id)
        _log.info("power_resume_requested", instance=instance_id, reason=reason)
        self._apply()

    def release(self, instance_id: str) -> None:
        """Drop an instance's request, for instance stop (CORE-53).

        Args:
            instance_id: The instance.
        """
        if instance_id not in self._suspenders and instance_id not in self._holders:
            return
        self._suspenders.discard(instance_id)
        self._holders.discard(instance_id)
        self._apply()

    async def set_display_power(self, on: bool) -> None:
        """Turn the display on or off (CORE-346).

        Args:
            on: True to turn it on.

        Raises:
            TransientError: No kiosk agent is available, or it did not report in time.
        """
        if self.display is None:
            raise TransientError("CORE_DISPLAY_UNAVAILABLE")
        await self.display(on)

    def _wanted(self) -> bool:
        return bool(self._suspenders) and not self._holders

    def _apply(self) -> None:
        """Start the transition when the wanted state differs from the last one asked for.

        Transitions run one after the other, in the order they were asked for, so a
        resume right after a suspend is not lost (CORE-53).
        """
        wanted = self._wanted()
        if wanted == self._target:
            return
        self._target = wanted
        self.supervisor.background(
            self._transition(wanted), "power-suspend" if wanted else "power-resume"
        )

    async def _transition(self, wanted: bool) -> None:
        async with self._lock:
            await self.supervisor.set_suspended(wanted)

    def _state_changed(self, instance_id: str, _old: State, new: State) -> None:
        if str(new) in _GONE:
            self.release(instance_id)
