"""MM3: a modular smart-mirror platform in Python.

The names exported here, together with ``mm3.config``, ``mm3.errors``,
``mm3.messages``, ``mm3.notifications``, ``mm3.scheduler`` and
``mm3.context``, are the module API
defined in the MM3 Module Contract. Nothing under ``mm3.core`` is part of it.

Implements:
    MOD-20: Module Contract 1.0 rev 6, §4.2 Guarantees.
    MOD-94: Module Contract 1.0 rev 6, §10 API versioning and deprecation.
"""

from mm3.__about__ import MODULE_API_VERSION, __version__
from mm3.config import CronExpression, ModuleConfig
from mm3.context import ModuleContext
from mm3.errors import ConfigurationError, RemoteError, RequestTimeoutError, TransientError
from mm3.messages import ClientMessage, Notification
from mm3.module import MirrorModule

__all__ = [
    "MODULE_API_VERSION",
    "ClientMessage",
    "ConfigurationError",
    "CronExpression",
    "MirrorModule",
    "ModuleConfig",
    "ModuleContext",
    "Notification",
    "RemoteError",
    "RequestTimeoutError",
    "TransientError",
    "__version__",
]
