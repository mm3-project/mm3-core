"""Structured logging (CORE-110, CORE-111) and state directory (CORE-198)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import uuid4

from mm3.core.logging import RING_SIZE, configure_logging, get_logger, recent_records
from mm3.core.services.storage import resolve_state_dir
from mm3.core.trace import current_trace, trace_scope


def test_core_110_json_lines_with_required_fields() -> None:
    out = io.StringIO()
    configure_logging("info", stream=out, json=True)
    log = get_logger("weather", "weather_home")
    log.debug("hidden")
    trace = uuid4()
    with trace_scope(trace):
        log.info("forecast_fetched", items=12)
    try:
        raise ValueError("bad")
    except ValueError:
        log.exception("fetch_failed")
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["event"] for r in lines] == ["forecast_fetched", "fetch_failed"]
    first = lines[0]
    assert {"ts", "level", "module", "instance", "event", "trace_id"} <= first.keys()
    assert first["trace_id"] == str(trace)
    assert first["module"] == "weather"
    assert first["instance"] == "weather_home"
    assert first["items"] == 12
    assert "ValueError: bad" in lines[1]["exception"]
    assert lines[1]["level"] == "error"
    assert current_trace() is None


def test_core_110_console_format_and_ring_buffer() -> None:
    out = io.StringIO()
    configure_logging("debug", stream=out, json=False)
    log = get_logger(subsystem="bus")
    for i in range(RING_SIZE + 10):
        log.debug("tick", i=i)
    log.warning("careful")
    log.error("oops")
    records = recent_records()
    assert len(records) == RING_SIZE
    assert records[-1]["event"] == "oops"
    assert records[-2]["level"] == "warning"
    assert "careful" in out.getvalue()


def test_core_198_state_directory_resolution(tmp_path: Path) -> None:
    assert resolve_state_dir({"STATE_DIRECTORY": "/var/lib/mm3:/other"}) == Path("/var/lib/mm3")
    assert resolve_state_dir({"XDG_STATE_HOME": str(tmp_path)}) == tmp_path / "mm3"
    assert resolve_state_dir({}) == Path.home() / ".local" / "state" / "mm3"


async def test_core_183_system_clock() -> None:
    import asyncio

    from mm3.core.clock import SystemClock

    clock = SystemClock()
    before = clock.boottime()
    await clock.sleep(0.01)
    assert clock.boottime() > before
    assert clock.wall().tzinfo is not None
    event = asyncio.Event()
    assert await clock.wait(event, 0.01) is False
    event.set()
    assert await clock.wait(event, 1) is True


def test_core_189_engine_timezone_validated() -> None:
    import pytest
    from pydantic import ValidationError

    from mm3.core.settings import EngineSettings

    assert EngineSettings(timezone="Europe/Rome").timezone == "Europe/Rome"
    with pytest.raises(ValidationError):
        EngineSettings(timezone="Mars/Olympus")
