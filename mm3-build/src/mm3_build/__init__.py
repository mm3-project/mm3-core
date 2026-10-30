"""mm3-build: compiles MM3 message catalogs into every wheel.

The hatchling build hook ``mm3-i18n`` (:mod:`mm3_build.plugin`) compiles each
``locales/<lang>/LC_MESSAGES/messages.po`` of the package into a ``.mo`` and a
``messages.json`` inside the wheel. :mod:`mm3_build.catalogs` holds the
compiler and the quality gate that ``mm3 i18n compile`` and ``check`` use.

Implements:
    CORE-280: Core spec 0.17, §10.2 Packaging and deployment.
"""

__version__ = "1.0.0"
"""Release version, versioned with the Module API (CORE-280)."""
