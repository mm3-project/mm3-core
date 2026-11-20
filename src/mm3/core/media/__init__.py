"""The media service (core spec 9.7): the only code of the engine that starts ``ffmpeg``.

- :mod:`mm3.core.media.command`: deliveries, ``ffmpeg`` command lines, codecs, masking.
- :mod:`mm3.core.media.folder`: the media folder, read without following links.
- :mod:`mm3.core.media.instance`: ``ctx.media`` of one instance (Contract 4.5).
- :mod:`mm3.core.media.service`: the children, their viewers, limits and state.

The names below load :mod:`mm3.core.media.service` when first used, so that the
small modules the context needs (``instance``, ``command``, ``folder``) import without
the configuration package, which imports the supervisor.

Implements:
    CORE-397: Core spec 0.51, §9.7 Media service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mm3.core.media.service import MediaRefused, MediaService, MjpegViewer

__all__ = ["MediaRefused", "MediaService", "MjpegViewer"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from mm3.core.media import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
