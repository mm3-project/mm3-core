"""The media service (core spec 9.7): the only code of the engine that starts ``ffmpeg``.

- :mod:`mm3.core.media.command`: deliveries, ``ffmpeg`` command lines, codecs, masking.
- :mod:`mm3.core.media.folder`: the media folder, read without following links.
- :mod:`mm3.core.media.service`: the children, their viewers, limits and state.

Implements:
    CORE-397: Core spec 0.51, §9.7 Media service.
"""

from __future__ import annotations

from mm3.core.media.service import MediaRefused, MediaService, MjpegViewer

__all__ = ["MediaRefused", "MediaService", "MjpegViewer"]
