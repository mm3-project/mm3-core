"""A fake ``ffmpeg`` for the media service tests (CORE-397, CORE-400, CORE-401).

:func:`install` writes an executable script that behaves like the two
``ffmpeg`` children of the media service: with ``image2pipe`` it writes JPEG
frames to standard output, with ``-f hls`` it writes a playlist, an init
segment and segments into the output folder. What it does is chosen by the
last path segment of the input URL:

- ``ok``: run until terminated;
- ``fail``: print an error and exit 1 at once;
- ``end``: produce a little, then exit 0 (the stream ended);
- ``hevc``: announce H.265 video (not browser-playable);
- ``pcm``: announce H.264 video with ``pcm_mulaw`` audio;
- ``adts``: without ``-bsf:a aac_adtstoasc``, fail as ffmpeg does on ADTS AAC.

Each run appends one JSON line to ``$FAKE_FFMPEG_LOG``: its arguments, its
nice value and its pid. Its standard error always repeats the input URL, so
the tests can check that credentials are masked in the log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = r"""
import json, os, signal, sys, time
argv = sys.argv[1:]
url = argv[argv.index("-i") + 1]
mode = url.rstrip("/").rsplit("/", 1)[-1]
log = os.environ.get("FAKE_FFMPEG_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "nice": os.nice(0), "pid": os.getpid()}) + "\n")
err = sys.stderr
err.write(f"Input #0, mpegts, from '{url}':\n")
video = "hevc" if mode == "hevc" else "h264"
err.write(f"  Stream #0:0[0x100]: Video: {video} (Main), yuv420p, 640x480, 25 fps\n")
audio = "pcm_mulaw" if mode == "pcm" else "aac"
err.write(f"  Stream #0:1[0x101]: Audio: {audio} (LC), 48000 Hz, stereo\n")
if mode == "fail":
    err.write(f"{url}: Connection refused\n")
    err.flush()
    sys.exit(1)
err.write("Output #0, x:\n")
err.flush()
if mode == "adts" and "-bsf:a" not in argv:
    err.write("[mp4 @ 0x1] Malformed AAC bitstream detected: "
              "use the audio bitstream filter 'aac_adtstoasc' to fix it\n")
    err.flush()
    time.sleep(0.05)
    sys.exit(1)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(255))
frame = 0
if "image2pipe" in argv:
    out = sys.stdout.buffer
    while True:
        frame += 1
        out.write(b"\xff\xd8" + b"frame-%d" % frame + b"\xff\xd9")
        out.flush()
        if mode == "end" and frame >= 3:
            sys.exit(0)
        time.sleep(0.05)
else:
    playlist = argv[-1]
    folder = os.path.dirname(playlist)
    with open(os.path.join(folder, "init.mp4"), "wb") as f:
        f.write(b"init")
    while True:
        with open(os.path.join(folder, f"seg{frame}.m4s"), "wb") as f:
            f.write(b"segment-%d" % frame)
        tmp = playlist + ".tmp"
        with open(tmp, "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:2\n")
            f.write(f'#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2.0,\nseg{frame}.m4s\n')
        os.replace(tmp, playlist)
        frame += 1
        if mode == "end" and frame >= 2:
            sys.exit(0)
        time.sleep(0.05)
"""


def install(folder: Path) -> Path:
    """Write the fake ``ffmpeg`` into ``folder`` and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "ffmpeg"
    path.write_text(f"#!{sys.executable}\n{SCRIPT}")
    path.chmod(0o755)
    return path


def runs(log: Path) -> list[dict[str, object]]:
    """The runs recorded in ``log``, oldest first."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
