"""Optional parts of ``mm3-core`` and the check that they are installed.

The test kit and ``mm3 module check`` need the extra ``mm3-core[testing]``
(pytest, pytest-asyncio, Playwright); mirrors install ``mm3-core`` without
it. Without the extra, ``import mm3.testing`` stops with a message naming
the install command, and ``mm3 module check`` exits with code 2.

Implements:
    CORE-279: Core spec 0.17, §10.2 Packaging and deployment.
"""

from __future__ import annotations

import importlib.util

__all__ = ["TESTING_EXTRA", "TESTING_INSTALL", "MissingExtraError", "missing_testing_modules"]

TESTING_EXTRA: tuple[tuple[str, str], ...] = (
    ("pytest", "pytest"),
    ("pytest_asyncio", "pytest-asyncio"),
    ("playwright", "playwright"),
)
"""Import name and distribution of each package the testing extra adds."""

TESTING_INSTALL = "pip install 'mm3-core[testing]'"
"""Command that installs the testing extra."""


class MissingExtraError(ImportError):
    """The testing extra is not installed.

    Args:
        missing: Distributions that are missing.
    """

    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            f"the MM3 test kit needs the extra mm3-core[testing] ({', '.join(missing)} "
            f"missing); install it with: {TESTING_INSTALL}"
        )
        self.missing = missing


def missing_testing_modules() -> list[str]:
    """Return the distributions of the testing extra that are not installed.

    Returns:
        Distribution names, empty when the extra is complete.

    Implements:
        CORE-279: Core spec 0.17, §10.2 Packaging and deployment.
    """
    return [dist for module, dist in TESTING_EXTRA if importlib.util.find_spec(module) is None]
