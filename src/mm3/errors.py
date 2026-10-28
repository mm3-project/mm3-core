"""Exceptions shared by the engine and modules.

Modules signal expected failures with :class:`TransientError` and
:class:`ConfigurationError`; every other exception is treated as a bug
(Module Contract section 3.4). The context raises the remaining types
(section 4.1).

Implements:
    -: Module Contract 1.0 rev 6, §3.4 Error semantics.
    -: Module Contract 1.0 rev 6, §4.1 Exceptions raised by the context.
    CORE-220, CORE-221: Core spec 0.7, §5.4 Request and reply.
    CORE-192: Core spec 0.7, §7 Lifecycle and error handling.
"""

from __future__ import annotations

__all__ = [
    "ConfigurationError",
    "ContextClosedError",
    "MM3Error",
    "RemoteError",
    "RequestTimeoutError",
    "ReservedNameError",
    "TransientError",
]


class MM3Error(Exception):
    """Base class of all MM3 exceptions."""


class TransientError(MM3Error):
    """A temporary problem, for example the network is down.

    The engine logs a warning and the instance stays RUNNING; the module
    retries by itself. A task that raises it ends.

    Args:
        message_key: Catalog key describing the problem (MOD-17).

    Implements:
        CORE-192: Core spec 0.7, §7 Lifecycle and error handling.
    """

    def __init__(self, message_key: str) -> None:
        super().__init__(message_key)
        self.message_key = message_key


class ConfigurationError(MM3Error):
    """The configuration is valid in form but rejected in use.

    The engine moves the instance to DISABLED until its configuration changes.

    Args:
        field: Name of the configuration field at fault.
        message_key: Catalog key describing the problem (MOD-17).

    Implements:
        CORE-192, CORE-195: Core spec 0.7, §7 Lifecycle and error handling.
    """

    def __init__(self, field: str, message_key: str) -> None:
        super().__init__(f"{field}: {message_key}")
        self.field = field
        self.message_key = message_key


class ReservedNameError(MM3Error, ValueError):
    """A module tried to send a notification whose name starts with ``CORE_``.

    Implements:
        CORE-22: Core spec 0.9, §5.3 Routing rules.
    """


class ContextClosedError(MM3Error, RuntimeError):
    """A context member was used after the instance's ``stop()`` returned.

    Implements:
        CORE-191: Core spec 0.7, §7 Lifecycle and error handling.
    """


class RequestTimeoutError(MM3Error):
    """No reply to ``ctx.request`` arrived within its timeout.

    Callers MUST handle it and keep working without the reply (MOD-26).

    Args:
        name: Name of the request.
        timeout: The timeout that expired, in seconds.

    Implements:
        CORE-220: Core spec 0.7, §5.4 Request and reply.
    """

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(f"no reply to {name} within {timeout} s")
        self.name = name
        self.timeout = timeout


class RemoteError(MM3Error):
    """The responder's ``on_request`` raised; carries its error reply.

    Args:
        code: ``transient``, ``configuration`` or ``internal``.
        message_key: Catalog key describing the problem (``REQUEST_FAILED``
            for ``internal``).
        source: Who answered, ``"<module>/<instance>"``.

    Implements:
        CORE-221: Core spec 0.7, §5.4 Request and reply.
    """

    def __init__(self, code: str, message_key: str, source: str) -> None:
        super().__init__(f"{source}: {code}: {message_key}")
        self.code = code
        self.message_key = message_key
        self.source = source
