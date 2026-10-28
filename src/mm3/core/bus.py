"""In-process event bus: routing, subscriptions and bounded inboxes.

Every receiving instance has its own inbox; the bus only enqueues, so a
send never waits for a receiver. Browsers and external bridges (web
gateway, MQTT) attach as endpoints. Requests are routed like other
notifications; replies bypass inboxes and resolve the waiting request.

Implements:
    CORE-05: Core spec 0.7, §3 Engine components.
    CORE-21..29: Core spec 0.9, §5.3 Routing rules.
    CORE-220, CORE-222: Core spec 0.7, §5.4 Request and reply.
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from mm3.core.logging import get_logger
from mm3.messages import ClientMessage, Notification

__all__ = ["Bus", "BusEndpoint", "ClientChannel", "Inbox", "InboxItem", "NullClientChannel"]

InboxItem = Notification | ClientMessage

_log = get_logger(subsystem="bus")


class Inbox:
    """Bounded queue of one instance, consumed one item at a time.

    When full, the oldest entry is dropped; a warning is logged at most once
    a minute and every drop is counted.

    Args:
        instance_id: Owner instance.
        size: Capacity (``engine.bus.queue_size``).
        now: Monotonic time source in seconds, for rate-limiting the warning.

    Implements:
        CORE-27, CORE-28: Core spec 0.7, §5.3 Routing rules.
    """

    WARN_EVERY = 60.0

    def __init__(self, instance_id: str, size: int, now: Callable[[], float]) -> None:
        self.instance_id = instance_id
        self._items: deque[InboxItem] = deque()
        self._size = size
        self._now = now
        self._ready = asyncio.Event()
        self._last_warning: float | None = None
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def put(self, item: InboxItem) -> None:
        """Append an item, dropping the oldest when full.

        Args:
            item: Notification or client message.
        """
        if len(self._items) >= self._size:
            self._items.popleft()
            self.dropped += 1
            now = self._now()
            if self._last_warning is None or now - self._last_warning >= self.WARN_EVERY:
                self._last_warning = now
                _log.warning(
                    "inbox_full_dropped_oldest",
                    instance=self.instance_id,
                    dropped_total=self.dropped,
                    queue_size=self._size,
                )
        self._items.append(item)
        self._ready.set()

    def clear(self) -> None:
        """Discard queued items (a new incarnation starts empty)."""
        self._items.clear()

    async def get(self) -> InboxItem:
        """Wait for and remove the oldest item.

        Returns:
            The item, in arrival order.
        """
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()


class BusEndpoint(Protocol):
    """A non-instance receiver: the web gateway, the MQTT bridge, a recorder."""

    def deliver(self, n: Notification, exclude_instance: str | None) -> None:
        """Receive a notification.

        Args:
            n: The notification. For a targeted one, ``n.target`` names the
                instance whose elements should receive it.
            exclude_instance: Instance whose own elements must not receive a
                broadcast (the sender), if any.
        """
        ...


class ClientChannel(Protocol):
    """Path from a backend to its own front-end elements (web gateway)."""

    def send_client(
        self, instance_id: str, type: str, payload: dict[str, Any], client: str | None
    ) -> None:
        """Deliver a ``client`` frame."""
        ...

    def send_state(self, instance_id: str, state: dict[str, Any], delta: bool) -> None:
        """Deliver a ``state`` frame (full snapshot or shallow delta)."""
        ...


class NullClientChannel:
    """Client channel used until the web gateway exists; drops everything."""

    def send_client(
        self, instance_id: str, type: str, payload: dict[str, Any], client: str | None
    ) -> None:
        """Drop a ``client`` frame."""

    def send_state(self, instance_id: str, state: dict[str, Any], delta: bool) -> None:
        """Drop a ``state`` frame."""


class _Receiver:
    def __init__(self, inbox: Inbox, subscribes: set[str] | None) -> None:
        self.inbox = inbox
        self.patterns = None if subscribes is None else tuple(subscribes)

    def wants(self, name: str) -> bool:
        if self.patterns is None:
            return True
        return any(fnmatch.fnmatchcase(name, p) for p in self.patterns)


class Bus:
    """Routes notifications to instance inboxes and endpoints.

    Implements:
        CORE-21..29: Core spec 0.9, §5.3 Routing rules.
        CORE-220, CORE-222: Core spec 0.7, §5.4 Request and reply.
    """

    def __init__(self) -> None:
        self._receivers: dict[str, _Receiver] = {}
        self._endpoints: list[BusEndpoint] = []
        self._pending: dict[UUID, asyncio.Future[Notification]] = {}

    def open_request(self, request_id: UUID) -> asyncio.Future[Notification]:
        """Start waiting for the first reply to a request.

        Call before publishing the request, and :meth:`close_request` when
        done waiting.

        Args:
            request_id: Id of the request notification.

        Returns:
            A future resolved with the first reply.
        """
        future: asyncio.Future[Notification] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    def close_request(self, request_id: UUID) -> None:
        """Stop waiting for replies to a request; later replies are dropped.

        Args:
            request_id: Id of the request notification.
        """
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.cancel()

    def _resolve(self, reply: Notification) -> None:
        # Implements CORE-222 (Core spec 0.7, §5.4 Request and reply): replies
        # bypass inboxes and subscribes; the first wins, later ones are dropped.
        assert reply.reply_to is not None  # noqa: S101 - caller checked
        future = self._pending.get(reply.reply_to)
        if future is None or future.done():
            _log.debug(
                "reply_dropped", name=reply.name, reply_to=str(reply.reply_to), source=reply.source
            )
            return
        future.set_result(reply)

    def register(self, inbox: Inbox, subscribes: set[str] | None) -> None:
        """Start delivering to a running instance.

        Args:
            inbox: The instance's inbox.
            subscribes: Name globs to deliver; ``None`` delivers all.
        """
        self._receivers[inbox.instance_id] = _Receiver(inbox, subscribes)

    def unregister(self, instance_id: str) -> None:
        """Stop delivering to an instance.

        Args:
            instance_id: The instance.
        """
        self._receivers.pop(instance_id, None)

    def is_registered(self, instance_id: str) -> bool:
        """Tell whether an instance currently receives notifications.

        Args:
            instance_id: The instance.

        Returns:
            True while the instance is registered.
        """
        return instance_id in self._receivers

    def add_endpoint(self, endpoint: BusEndpoint) -> None:
        """Attach a non-instance receiver.

        Args:
            endpoint: The endpoint.
        """
        self._endpoints.append(endpoint)

    def remove_endpoint(self, endpoint: BusEndpoint) -> None:
        """Detach a non-instance receiver.

        Args:
            endpoint: The endpoint.
        """
        self._endpoints.remove(endpoint)

    def publish(self, n: Notification, sender_instance: str | None = None) -> None:
        """Route one notification.

        A broadcast reaches every backend and endpoint except both halves of
        the sender instance, filtered by ``subscribes``. A targeted one goes
        to the target's backend and elements, ignoring ``subscribes``; an
        unknown or stopped target is dropped and logged at debug level.
        Enqueueing is synchronous, so order per sender is preserved. A reply
        goes to the waiting request, never to an inbox; endpoints see it
        like a targeted notification.

        Args:
            n: The notification.
            sender_instance: Instance id of the sender, if it is an instance.
        """
        if n.is_reply:
            self._resolve(n)
            for endpoint in self._endpoints:
                endpoint.deliver(n, None)
            return
        if n.target is not None:
            receiver = self._receivers.get(n.target)
            if receiver is None:
                _log.debug("target_dropped", target=n.target, name=n.name, id=str(n.id))
                return
            receiver.inbox.put(n)
            for endpoint in self._endpoints:
                endpoint.deliver(n, None)
            return
        for instance_id, receiver in self._receivers.items():
            if instance_id != sender_instance and receiver.wants(n.name):
                receiver.inbox.put(n)
        for endpoint in self._endpoints:
            endpoint.deliver(n, sender_instance)

    def deliver_client_message(self, instance_id: str, msg: ClientMessage) -> bool:
        """Queue a message from an instance's own front-end.

        Args:
            instance_id: The receiving instance.
            msg: The message.

        Returns:
            True if queued; False when the instance is not running.
        """
        receiver = self._receivers.get(instance_id)
        if receiver is None:
            return False
        receiver.inbox.put(msg)
        return True
