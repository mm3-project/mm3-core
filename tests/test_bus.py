"""Bus routing and inboxes (CORE-20..29) and the envelope."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from conftest import Recorder
from mm3.core.bus import Bus, Inbox
from mm3.core.logging import recent_records
from mm3.messages import ClientMessage, Notification, check_payload


def note(
    name: str, *, target: str | None = None, payload: dict[str, object] | None = None
) -> Notification:
    nid = uuid4()
    return Notification(
        id=nid,
        trace=nid,
        ts=datetime.now(UTC),
        name=name,
        source="core",
        target=target,
        payload=payload or {},
    )


class Now:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def drain(inbox: Inbox) -> list[str]:
    names = []
    while len(inbox):
        item = inbox._items.popleft()
        assert isinstance(item, Notification)
        names.append(item.name)
    return names


@pytest.fixture
def bus() -> tuple[Bus, dict[str, Inbox], Recorder]:
    b = Bus()
    now = Now()
    inboxes = {i: Inbox(i, 1000, now) for i in ("a", "b", "c")}
    b.register(inboxes["a"], None)
    b.register(inboxes["b"], {"WEATHER_*", "CORE_READY"})
    b.register(inboxes["c"], None)
    rec = Recorder()
    b.add_endpoint(rec)
    return b, inboxes, rec


def test_core_20_envelope_fields_and_validation() -> None:
    n = note("CALENDAR_EVENTS", payload={"x": 1})
    assert n.v == 1
    assert n.trace == n.id
    assert Notification.model_validate_json(n.model_dump_json()) == n
    with pytest.raises(ValidationError):
        note("calendarEvents")
    with pytest.raises(ValidationError):
        Notification(id=uuid4(), trace=uuid4(), ts=datetime(2026, 1, 1), name="X", source="core")


def test_core_26_payload_json_and_size() -> None:
    check_payload({"ok": [1, "two", None]})
    with pytest.raises(ValueError, match="JSON-serialisable"):
        check_payload({"bad": object()})
    with pytest.raises(ValueError, match="JSON object"):
        check_payload([1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        check_payload({"big": "x" * (256 * 1024)})
    with pytest.raises(ValidationError):
        ClientMessage(type="t", payload={"n": float("nan")}, client="k", trace=uuid4())


def test_core_21_broadcast_excludes_sender(bus: tuple[Bus, dict[str, Inbox], Recorder]) -> None:
    b, inboxes, rec = bus
    b.publish(note("HELLO"), sender_instance="a")
    assert drain(inboxes["a"]) == []
    assert drain(inboxes["c"]) == ["HELLO"]
    assert rec.notifications[0][1] == "a"  # endpoint told to skip a's own elements


def test_core_25_subscribes_globs(bus: tuple[Bus, dict[str, Inbox], Recorder]) -> None:
    b, inboxes, _ = bus
    for name in ("WEATHER_UPDATED", "CALENDAR_EVENTS", "WEATHERX"):
        b.publish(note(name))
    assert drain(inboxes["b"]) == ["WEATHER_UPDATED"]


def test_core_29_subscribes_applies_to_core_names(
    bus: tuple[Bus, dict[str, Inbox], Recorder],
) -> None:
    b, inboxes, _ = bus
    b.publish(note("CORE_READY"))
    b.publish(note("CORE_SUSPEND"))
    assert drain(inboxes["b"]) == ["CORE_READY"]
    assert drain(inboxes["a"]) == ["CORE_READY", "CORE_SUSPEND"]


def test_core_29_targeted_ignores_subscribes(bus: tuple[Bus, dict[str, Inbox], Recorder]) -> None:
    b, inboxes, rec = bus
    b.publish(note("PRIVATE_THING", target="b"), sender_instance="a")
    assert drain(inboxes["b"]) == ["PRIVATE_THING"]
    assert drain(inboxes["c"]) == []
    assert rec.notifications[-1][0].target == "b"


def test_core_29_unknown_target_dropped_and_logged(
    bus: tuple[Bus, dict[str, Inbox], Recorder],
) -> None:
    b, inboxes, rec = bus
    b.publish(note("LOST", target="zzz"))
    assert all(len(i) == 0 for i in inboxes.values())
    assert rec.notifications == []
    record = recent_records()[-1]
    assert record["event"] == "target_dropped"
    assert record["level"] == "debug"


def test_core_23_order_preserved_per_sender(bus: tuple[Bus, dict[str, Inbox], Recorder]) -> None:
    b, inboxes, _ = bus
    names = [f"N_{i}" for i in range(20)]
    for name in names:
        b.publish(note(name), sender_instance="a")
    assert drain(inboxes["c"]) == names


def test_core_27_full_inbox_drops_oldest_counts_and_warns_once_a_minute() -> None:
    now = Now()
    inbox = Inbox("x", 3, now)
    for i in range(5):
        inbox.put(note(f"N_{i}"))
    assert drain(inbox) == ["N_2", "N_3", "N_4"]
    assert inbox.dropped == 2
    warnings = [r for r in recent_records() if r["event"] == "inbox_full_dropped_oldest"]
    assert len(warnings) == 1
    now.t = 61
    for i in range(4):
        inbox.put(note(f"M_{i}"))
    warnings = [r for r in recent_records() if r["event"] == "inbox_full_dropped_oldest"]
    assert len(warnings) == 2
    assert inbox.dropped == 3


async def test_core_28_inbox_get_waits_in_order() -> None:
    inbox = Inbox("x", 10, Now())
    inbox.put(note("FIRST"))
    msg = ClientMessage(type="click", client="k1", trace=uuid4())
    inbox.put(msg)
    first = await inbox.get()
    assert isinstance(first, Notification)
    assert await inbox.get() == msg


def test_core_28_client_message_only_to_registered(
    bus: tuple[Bus, dict[str, Inbox], Recorder],
) -> None:
    b, _inboxes, rec = bus
    msg = ClientMessage(type="click", client="k1", trace=uuid4())
    assert b.deliver_client_message("a", msg)
    assert not b.deliver_client_message("nope", msg)
    b.unregister("a")
    assert not b.is_registered("a")
    b.remove_endpoint(rec)
    b.publish(note("AFTER"))
    assert rec.notifications == []
