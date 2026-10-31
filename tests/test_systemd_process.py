"""``mm3 run`` under a fake systemd: READY=1 after the listeners, watchdog pings, STOPPING=1."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_core_290_mm3_run_notifies_systemd_and_pings_the_watchdog(tmp_path: Path) -> None:
    sock_path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(sock_path))
    server.settimeout(0.5)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"engine:\n  server: {{host: 127.0.0.1, port: {free_port()}}}\n"
        f"  admin: {{host: 127.0.0.1, port: {free_port()}}}\n"
    )
    env_file = tmp_path / "mm3.env"
    env_file.write_text("")
    env_file.chmod(0o600)
    env = {
        **os.environ,
        "NOTIFY_SOCKET": str(sock_path),
        "WATCHDOG_USEC": "1000000",
        "STATE_DIRECTORY": str(tmp_path / "state"),
        "RUNTIME_DIRECTORY": str(tmp_path / "run"),
    }
    env.pop("WATCHDOG_PID", None)
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and arguments
        [
            sys.executable,
            "-m",
            "mm3.cli.main",
            "run",
            "--config",
            str(config),
            "--env-file",
            str(env_file),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    received: list[list[str]] = []

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                received.append(server.recv(4096).decode().split("\n"))
            except TimeoutError:
                continue

    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any("READY=1" in m for m in received):
            pump(0.5)
            if proc.poll() is not None:
                break
        assert any("READY=1" in m for m in received), (
            proc.stderr.read().decode() if proc.stderr else ""
        )
        ready = next(m for m in received if "READY=1" in m)
        assert any(f.startswith("STATUS=") for f in ready)
        pump(2.0)  # WatchdogSec/2 = 0.5 s: several pings
        assert sum(m == ["WATCHDOG=1"] for m in received) >= 2
        proc.send_signal(signal.SIGTERM)
        pump(3.0)
        assert ["STOPPING=1"] in received
        assert proc.wait(timeout=20) == 0
        assert received.index(["STOPPING=1"]) > received.index(ready)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        server.close()


@pytest.mark.skipif(sys.platform != "linux", reason="systemd notification is Linux only")
def test_core_290_a_start_that_aborts_never_sends_ready(tmp_path: Path) -> None:
    sock_path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(sock_path))
    server.settimeout(0.5)
    config = tmp_path / "config.yaml"
    config.write_text("modules:\n  - module: nope-not-installed\n")
    env_file = tmp_path / "mm3.env"
    env_file.write_text("")
    env_file.chmod(0o600)
    env = {**os.environ, "NOTIFY_SOCKET": str(sock_path)}
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [
            sys.executable,
            "-m",
            "mm3.cli.main",
            "run",
            "--config",
            str(config),
            "--env-file",
            str(env_file),
        ],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2  # CORE-30
    with pytest.raises(TimeoutError):
        server.recv(4096)
    server.close()
