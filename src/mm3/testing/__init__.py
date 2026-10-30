"""Test kit for module developers (Module Contract section 12.2).

Ships with ``mm3-core`` behind the extra ``mm3-core[testing]``; importing
it without the extra raises ``MissingExtraError`` naming the install
command (core CORE-279). The virtual clock is available now;
``ModuleHarness``, the MQTT and HTTP fakes and the browser harness used by
``mm3 module check`` follow in the next parts of milestone 4.

Implements:
    -: Module Contract 1.0 rev 8, §12.2 mm3.testing kit.
    CORE-279: Core spec 0.17, §10.2 Packaging and deployment.
"""

from mm3.core.extras import MissingExtraError, missing_testing_modules

_missing = missing_testing_modules()
if _missing:
    raise MissingExtraError(_missing)

from mm3.testing.clock import VirtualClock  # noqa: E402

__all__ = ["VirtualClock"]
