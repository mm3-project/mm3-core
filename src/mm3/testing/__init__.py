"""Test kit for module developers (Module Contract section 12.2).

Ships with ``mm3-core`` behind the extra ``mm3-core[testing]``; importing
it without the extra raises ``MissingExtraError`` naming the install
command (core CORE-279).

``ModuleHarness`` runs one module instance on a real engine with fakes for
the network (``h.http``, ``h.mqtt``), recorded browsers and virtual time;
``await h.check_contract()`` runs every test-kit row of Contract section 11.
The browser harness follows in the next part of milestone 4.

Implements:
    -: Module Contract 1.0 rev 9, §12.2 mm3.testing kit.
    CORE-279: Core spec 0.17, §10.2 Packaging and deployment.
"""

from mm3.core.extras import MissingExtraError, missing_testing_modules

_missing = missing_testing_modules()
if _missing:
    raise MissingExtraError(_missing)

from mm3.testing.clock import VirtualClock  # noqa: E402
from mm3.testing.contract import (  # noqa: E402
    KIT_CHECKS,
    CheckResult,
    ContractError,
    run_kit_checks,
    sample_payload,
)
from mm3.testing.fakes import FakeHttp, FakeMqtt, HttpCall, Published, fake_mqtt  # noqa: E402
from mm3.testing.harness import ModuleHarness  # noqa: E402
from mm3.testing.loop import DriveError, LoopClock  # noqa: E402
from mm3.testing.recorders import ClientRecord, LogRecords, NotificationLog  # noqa: E402

__all__ = [
    "KIT_CHECKS",
    "CheckResult",
    "ClientRecord",
    "ContractError",
    "DriveError",
    "FakeHttp",
    "FakeMqtt",
    "HttpCall",
    "LogRecords",
    "LoopClock",
    "ModuleHarness",
    "NotificationLog",
    "Published",
    "VirtualClock",
    "fake_mqtt",
    "run_kit_checks",
    "sample_payload",
]
