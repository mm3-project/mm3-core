"""Test kit for module developers (Module Contract section 12.2).

The virtual clock is available now; ``ModuleHarness``, the MQTT and HTTP
fakes and the browser harness used by ``mm3 module check`` come with the
test kit milestone.

Implements:
    -: Module Contract 1.0 rev 4, §12.2 mm3.testing kit.
"""

from mm3.testing.clock import VirtualClock

__all__ = ["VirtualClock"]
