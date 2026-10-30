"""``MirrorModule``: base class of every module backend.

Every hook has a default, so a backend-only module overrides what it
needs and a front-end-only module ships the class with no hooks at all.

Implements:
    -: Core spec 0.7, §4.2 Backend interface (Python).
    CORE-10: Core spec 0.7, §4.4 Contract rules.
    CORE-221: Core spec 0.7, §5.4 Request and reply.
    MOD-10, MOD-11: Module Contract 1.0 rev 6, §3.3 Rules.
    -: Module Contract 1.0 rev 8, §3.1 Class attributes.
    -: Module Contract 1.0 rev 6, §3.2 Hooks.
    MOD-29: Module Contract 1.0 rev 8, §6.1 Messaging.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel

from mm3.config import ModuleConfig
from mm3.context import ModuleContext
from mm3.messages import ClientMessage, Notification

__all__ = ["MirrorModule"]


class MirrorModule(abc.ABC):
    """Base class for every MM3 module backend.

    Subclasses set the class attributes and override the hooks they need.
    ``__init__`` only stores ``ctx`` and ``config`` (MOD-10).

    ``emits``, ``consumes`` and ``answers`` declare every notification the
    module sends (backend and front-end), handles and answers, each with its
    Pydantic payload models (MOD-29). The engine validates only registered
    names (CORE-225); the test kit validates every payload against these
    models, and ``subscribes`` stays the delivery filter.

    Args:
        ctx: The instance's context.
        config: The validated instance configuration.

    Implements:
        -: Module Contract 1.0 rev 6, §3.2 Hooks.
        CORE-10: Core spec 0.7, §4.4 Contract rules.
        MOD-29: Module Contract 1.0 rev 8, §6.1 Messaging.
    """

    name: ClassVar[str]
    api_version: ClassVar[str] = "1.0"
    config_model: ClassVar[type[ModuleConfig]] = ModuleConfig
    element: ClassVar[str | None] = None
    scripts: ClassVar[list[str]] = []
    styles: ClassVar[list[str]] = []
    locales_dir: ClassVar[str | None] = "locales"
    subscribes: ClassVar[set[str] | None] = None
    emits: ClassVar[Mapping[str, type[BaseModel]]] = MappingProxyType({})
    consumes: ClassVar[Mapping[str, type[BaseModel]]] = MappingProxyType({})
    answers: ClassVar[Mapping[str, tuple[type[BaseModel], type[BaseModel]]]] = MappingProxyType({})
    capabilities: ClassVar[set[str]] = set()
    min_size_px: ClassVar[tuple[int, int] | None] = None
    admin_element: ClassVar[str | None] = None
    admin_scripts: ClassVar[list[str]] = []

    def __init__(self, ctx: ModuleContext, config: ModuleConfig) -> None:
        self.ctx = ctx
        self.config = config

    async def start(self) -> None:  # noqa: B027
        """Acquire resources and schedule jobs."""

    async def stop(self) -> None:  # noqa: B027
        """Release resources; MUST be idempotent (MOD-13)."""

    async def suspend(self) -> None:  # noqa: B027
        """The instance is hidden or the screen is off."""

    async def resume(self) -> None:  # noqa: B027
        """The instance is visible again."""

    async def reconfigure(self, new: ModuleConfig) -> bool:
        """Apply a changed configuration.

        Args:
            new: The new validated configuration.

        Returns:
            True if applied live; False asks the engine to restart the instance.
        """
        return False

    async def on_notification(self, n: Notification) -> None:  # noqa: B027
        """Handle a bus notification that matches ``subscribes``."""

    async def on_client_message(self, msg: ClientMessage) -> None:  # noqa: B027
        """Handle a message from this module's own front-end."""

    async def on_request(self, req: Notification) -> dict[str, Any] | None:
        """Answer a request delivered to this instance (core spec 5.4).

        Args:
            req: The request (``expects_reply`` is true).

        Returns:
            The reply payload, or ``None`` when the request is not for this
            module (MOD-24); ``None`` sends nothing.
        """
        return None

    def snapshot(self) -> dict[str, Any]:
        """Return the last renderable state, sent to every browser that connects."""
        return {}
