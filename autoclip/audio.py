"""Measures the volume of every recorded video segment. A loud moment is a second 'something happened' signal."""
from __future__ import annotations

import logging
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

from .detector import ChatEvent
from .recorder import Segment, ffmpeg_exe

log = logging.getLogger("autoclip.audio")
_MEAN = re.compile(r"mean_volume:\s*(-?[\d.]+|-inf) dB")


def mean_volume(path: Path) -> float | None:
    """Mean loudness of a media file in dB (about -20 normal speech, -40 quiet, -91 silence); None if unreadable."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    kw = {"creationflags": 0x00004000} if sys.platform == "win32" else {}          # below-normal priority on Windows
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, errors="replace", **kw)
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = _MEAN.search(r.stderr)
    if not m:
        return None
    return -91.0 if m.group(1) == "-inf" else float(m.group(1))


class AudioMeter(threading.Thread):
    """Feed it Segments (any thread); it posts ChatEvent(kind='audio') to `sink` for the detector."""

    def __init__(self, sink, stop: threading.Event):
        super().__init__(daemon=True, name="audio-meter")
        self.q: queue.Queue[Segment] = queue.Queue(maxsize=200)
        self.sink, self.stop_evt = sink, stop

    def submit(self, segs: list[Segment]) -> None:
        for s in segs:
            try:
                self.q.put_nowait(s)
            except queue.Full:                 # falling behind: drop, the detector copes with gaps
                pass

    def run(self):
        while not self.stop_evt.is_set():
            try:
                seg = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                db = mean_volume(seg.path)
                if db is not None:
                    self.sink(ChatEvent(t=(seg.t0 + seg.t1) / 2, kind="audio", db=db))
            except Exception:                      # skip this segment; the thread must live on
                log.exception("audio level of %s could not be measured", seg.path.name)
