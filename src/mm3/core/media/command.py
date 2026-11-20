"""The ``ffmpeg`` command lines of the media service, and what is read back from them.

Pure functions only: which delivery a source uses (CORE-400), the arguments
of each child (CORE-397, CORE-401, CORE-403), the input codecs read from
``ffmpeg``'s standard error and whether they play in a browser (CORE-401),
and the masking of URL credentials in log lines (CORE-397, CORE-403).
Nothing here starts a process.

Implements:
    CORE-397: Core spec 0.51, §9.7 Media service.
    CORE-400: Core spec 0.51, §9.7 Media service.
    CORE-401: Core spec 0.48, §9.7 Media service.
    CORE-402: Core spec 0.52, §9.7 Media service.
    CORE-403: Core spec 0.48, §9.7 Media service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from mm3.core.config.media import MediaSource
from mm3.core.diag.secrets import MASK

__all__ = [
    "ADTS_HINT",
    "CA_FILE",
    "HLS_LIST_SIZE",
    "HLS_PLAYLIST",
    "HLS_SEGMENT_S",
    "PLAYABLE_AUDIO",
    "PLAYABLE_VIDEO",
    "Codecs",
    "Delivery",
    "Encoder",
    "delivery",
    "ffmpeg_command",
    "mask_url_credentials",
    "parse_codecs",
    "unplayable",
]

Delivery = Literal["mjpeg", "hls", "file"]
"""How a source reaches the browser (CORE-400)."""

Encoder = Literal["vaapi", "x264"]
"""The H.264 encoder of a transcoding child (CORE-401)."""

CA_FILE = "/etc/ssl/certs/ca-certificates.crt"
"""Debian's system CA store, against which ``https`` and ``rtsps`` are verified (CORE-403)."""

HLS_SEGMENT_S = 2
HLS_LIST_SIZE = 6
HLS_PLAYLIST = "index.m3u8"
HLS_INIT = "init.mp4"
HLS_SEGMENT_PATTERN = "seg%d.m4s"

PLAYABLE_VIDEO = frozenset({"h264"})
PLAYABLE_AUDIO = frozenset({"aac", "mp3", "opus"})
"""Browser-playable codecs (CORE-401)."""

ADTS_HINT = "aac_adtstoasc"
"""``ffmpeg`` names this filter when copied AAC needs it for fragmented MP4."""

RW_TIMEOUT_US = 10_000_000
"""A network read that stalls for 10 s ends the child with an error, so it is restarted."""

# Implements CORE-403 (Core spec 0.48, §9.7 Media service): the protocols each
# scheme may use; a redirect to another scheme is refused by ffmpeg itself.
_WHITELIST: dict[str, str] = {
    "https": "https,tls,tcp,crypto",
    "http": "http,tcp,crypto",
    "rtsps": "rtsps,rtsp,rtp,srtp,udp,tcp,tls",
    "rtsp": "rtsp,rtp,udp,tcp",
}

_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<info>[^\s/@]+)@")
_STREAM = re.compile(r"^\s*Stream #0:\d+\S*: (?P<type>Video|Audio): (?P<codec>[A-Za-z0-9_]+)")


def delivery(source: MediaSource) -> Delivery:
    """Return the delivery of a source (CORE-400).

    ``auto`` is MJPEG for a ``camera`` source and for an ``rtsp`` source with
    ``audio: false``, HLS for the other ``rtsp`` and ``stream`` sources, and
    the file itself for ``files``. ``hls`` on a ``camera`` source uses MJPEG
    (the warning ``CORE_CFG_MEDIA_DELIVERY`` of CORE-399).

    Args:
        source: The source.

    Returns:
        ``mjpeg``, ``hls`` or ``file``.

    Implements:
        CORE-400: Core spec 0.51, §9.7 Media service.
    """
    if source.kind == "files":
        return "file"
    if source.kind == "camera":
        return "mjpeg"
    if source.delivery != "auto":
        return source.delivery
    if source.kind == "rtsp" and not source.has_audio:
        return "mjpeg"
    return "hls"


def _input_args(url: str) -> list[str]:
    """Input options: TLS verification, the protocol whitelist and timeouts (CORE-403)."""
    scheme = urlsplit(url).scheme.lower()
    args = ["-protocol_whitelist", _WHITELIST[scheme], "-rw_timeout", str(RW_TIMEOUT_US)]
    if scheme in ("https", "rtsps"):
        args += ["-tls_verify", "1", "-ca_file", CA_FILE]
    if scheme in ("rtsp", "rtsps"):
        args += ["-rtsp_transport", "tcp"]
    return [*args, "-i", url]


def _scale(width: int | None) -> list[str]:
    return [f"scale={width}:-2"] if width is not None else []


def ffmpeg_command(
    ffmpeg: str,
    source: MediaSource,
    how: Delivery,
    out_dir: Path | None = None,
    *,
    encoder: Encoder = "x264",
    render_node: Path | None = None,
    adts: bool = False,
    device_path: str | None = None,
) -> list[str]:
    """Build the argument list of one child.

    MJPEG children write JPEG frames to standard output (``image2pipe``),
    which the service hands to every viewer, newest frame first. HLS children
    write 2 s fragmented-MP4 segments and a list of 6 to ``out_dir``. Codecs
    are copied unless ``transcode`` is true: then the video is H.264 from
    VA-API (``h264_vaapi`` on ``render_node``) or ``libx264`` with the preset
    ``veryfast``, and the audio is AAC.

    Args:
        ffmpeg: Path of the ``ffmpeg`` executable.
        source: An ``rtsp`` or ``stream`` source.
        how: ``mjpeg`` or ``hls``.
        out_dir: The source's folder under ``/run/mm3/media/`` (HLS only).
        encoder: The H.264 encoder when transcoding.
        render_node: The VA-API render node, for ``encoder="vaapi"``.
        adts: The copied audio is AAC in ADTS framing (MPEG-TS, RTSP), which
            fragmented MP4 takes only through the ``aac_adtstoasc`` filter; the
            service learns it from a first run (:data:`ADTS_HINT`).
        device_path: The V4L2 device of a ``camera`` source that Media owns
            (CORE-402); its child always delivers MJPEG.

    Returns:
        The argument list, ``ffmpeg`` first.

    Raises:
        ValueError: The source has no URL (or a camera no device), or HLS
            without ``out_dir``.

    Implements:
        CORE-397: Core spec 0.51, §9.7 Media service.
        CORE-400: Core spec 0.51, §9.7 Media service.
        CORE-401: Core spec 0.48, §9.7 Media service.
        CORE-402: Core spec 0.52, §9.7 Media service.
        CORE-403: Core spec 0.48, §9.7 Media service.
    """
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info"]
    if source.kind == "camera":
        if device_path is None:
            raise ValueError("a camera child needs its device")
        args += ["-f", "v4l2", "-i", device_path]
        how = "mjpeg"  # CORE-399: a camera is always MJPEG
    elif source.url is None:
        raise ValueError("only rtsp, stream and camera sources run ffmpeg here")
    else:
        if how == "hls" and encoder == "vaapi" and source.transcode and render_node is not None:
            args += ["-vaapi_device", str(render_node)]
        args += _input_args(source.url)
    if how == "mjpeg":
        filters = [f"fps={source.fps}", *_scale(source.width)]
        return [
            *args,
            "-map", "0:v:0", "-an",
            "-vf", ",".join(filters),
            "-c:v", "mjpeg", "-q:v", "5",
            "-f", "image2pipe", "pipe:1",
        ]  # fmt: skip
    if how != "hls" or out_dir is None:
        raise ValueError("HLS needs an output folder")
    args += ["-map", "0:v:0?"]
    if source.has_audio:
        args += ["-map", "0:a:0?"]
    else:
        args += ["-an"]
    if source.transcode:
        if encoder == "vaapi" and render_node is not None:
            filters = ["format=nv12", "hwupload"]
            if source.width is not None:
                filters.append(f"scale_vaapi=w={source.width}:h=-2")
            args += ["-vf", ",".join(filters), "-c:v", "h264_vaapi"]
        else:
            scale = _scale(source.width)
            args += [
                *(["-vf", scale[0]] if scale else []),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
            ]
        args += ["-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_S})"]
        if source.has_audio:
            args += ["-c:a", "aac"]
    else:
        args += ["-c", "copy"]
        if adts and source.has_audio:
            args += ["-bsf:a", "aac_adtstoasc"]
    return [
        *args,
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_S),
        "-hls_list_size", str(HLS_LIST_SIZE),
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", HLS_INIT,
        "-hls_segment_filename", str(out_dir / HLS_SEGMENT_PATTERN),
        "-hls_flags", "delete_segments+independent_segments",
        str(out_dir / HLS_PLAYLIST),
    ]  # fmt: skip


@dataclass(frozen=True)
class Codecs:
    """The first video and audio codec of the input, as ``ffmpeg`` names them.

    Attributes:
        video: Video codec, or ``None`` without video.
        audio: Audio codec, or ``None`` without audio.
        complete: True once the output section began, so the input list is whole.
    """

    video: str | None = None
    audio: str | None = None
    complete: bool = False


def parse_codecs(lines: list[str]) -> Codecs:
    """Read the input codecs from the start of ``ffmpeg``'s standard error.

    Only the ``Input #0`` section counts; the ``Output #0`` line ends it.

    Args:
        lines: The lines read so far.

    Returns:
        What was found.

    Implements:
        CORE-401: Core spec 0.48, §9.7 Media service.
    """
    video: str | None = None
    audio: str | None = None
    seen_input = False
    for line in lines:
        if line.startswith("Input #0"):
            seen_input = True
            continue
        if line.startswith("Output #0"):
            return Codecs(video, audio, complete=seen_input)
        if not seen_input:
            continue
        m = _STREAM.match(line)
        if m is None:
            continue
        if m["type"] == "Video" and video is None:
            video = m["codec"]
        elif m["type"] == "Audio" and audio is None:
            audio = m["codec"]
    return Codecs(video, audio, complete=False)


def unplayable(codecs: Codecs, *, audio: bool) -> bool:
    """True when a copied source would not play in a browser (CORE-401).

    Args:
        codecs: The input codecs.
        audio: Whether the child keeps the sound.

    Returns:
        True when the video is not H.264, or the kept audio is not AAC, MP3 or Opus.

    Implements:
        CORE-401: Core spec 0.48, §9.7 Media service.
    """
    if codecs.video is not None and codecs.video not in PLAYABLE_VIDEO:
        return True
    return audio and codecs.audio is not None and codecs.audio not in PLAYABLE_AUDIO


def mask_url_credentials(text: str) -> str:
    """Replace the user information of every URL in a text with ``!masked`` (CORE-303).

    Args:
        text: A log line.

    Returns:
        The line, with ``scheme://user:pass@host`` written ``scheme://!masked@host``.

    Implements:
        CORE-397: Core spec 0.51, §9.7 Media service.
        CORE-403: Core spec 0.48, §9.7 Media service.
    """
    return _USERINFO.sub(lambda m: f"{m['scheme']}{MASK}@", text)
