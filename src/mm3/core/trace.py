"""Trace ids carried through handlers, job runs and the notifications they send.

A notification sent while a handler, a job run or a client message is being
handled inherits that trace (through contextvars); any other starts a new
trace equal to its own id.

Implements:
    CORE-111: Core spec 0.4, §10.5 Diagnostics and debugging.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

__all__ = ["current_trace", "trace_scope"]

_TRACE: ContextVar[UUID | None] = ContextVar("mm3_trace", default=None)


def current_trace() -> UUID | None:
    """Return the trace id of the work in progress, if any.

    Returns:
        The trace id, or ``None`` outside handlers and job runs.
    """
    return _TRACE.get()


@contextmanager
def trace_scope(trace: UUID) -> Iterator[None]:
    """Bind a trace id for the duration of a block.

    Args:
        trace: The trace id to bind.

    Yields:
        Nothing; the trace is reset on exit.
    """
    token = _TRACE.set(trace)
    try:
        yield
    finally:
        _TRACE.reset(token)
