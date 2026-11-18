"""Cut clips out of the rolling buffer with ffmpeg."""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from .config import Config
from .recorder import Segment, ffmpeg_exe

log = logging.getLogger("autoclip.cut")


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")


def _vcodec(cfg: Config) -> list[str]:
    if cfg.encoder == "libx264":
        return ["-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p"]
    return ["-c:v", cfg.encoder, "-pix_fmt", "yuv420p", "-b:v", "6M"]   # hardware encoders: simple bitrate mode


def cut_clip(segs: list[Segment], a: float, b: float, out: Path, cfg: Config) -> bool:
    """Write wall-clock window [a, b] to `out`. Returns False if the buffer does not cover enough of it."""
    segs = sorted(segs, key=lambda s: s.t0)
    segs = [s for s in segs if s.path.exists()]
    if not segs:
        log.warning("nothing in buffer for that window")
        return False
    start, end = max(a, segs[0].t0), min(b, segs[-1].t1)
    covered = sum(min(s.t1, end) - max(s.t0, start) for s in segs if s.t1 > start and s.t0 < end)
    if end - start < 3 or covered < 0.5 * (b - a):
        log.warning("buffer covers only %.0fs of %.0fs wanted; skipping", covered, b - a)
        return False
    listing = out.with_suffix(".txt")
    # concat-demuxer syntax: forward slashes work on Windows too; a quote inside a path must be escaped
    listing.write_text("".join("file '{}'\n".format(s.path.resolve().as_posix().replace("'", "'\\''")) for s in segs))
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
           "-ss", f"{start - segs[0].t0:.3f}", "-t", f"{end - start:.3f}", *_vcodec(cfg),
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    try:
        r = _run(cmd)
    finally:
        listing.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        log.error("ffmpeg cut failed: %s", r.stderr.strip()[-400:])
        out.unlink(missing_ok=True)
        return False
    return True


def make_vertical(src: Path, dst: Path, cfg: Config) -> bool:
    """1080x1920 version. 'blur': whole frame centered over a blurred copy. 'crop': center crop."""
    if cfg.vertical_mode == "crop":
        vf = "scale=-2:1920,crop=1080:1920"
    else:
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
              "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-filter_complex" if "[" in vf else "-vf", vf,
           *_vcodec(cfg), "-c:a", "copy", "-movflags", "+faststart", str(dst)]
    r = _run(cmd, timeout=600)
    if r.returncode != 0:
        log.error("vertical export failed: %s", r.stderr.strip()[-400:])
        dst.unlink(missing_ok=True)
        return False
    return True


_MEAN = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")


def _mean_volume(path: Path, start: float, dur: float | None) -> float | None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-ss", f"{start:.2f}"] + (["-t", f"{dur:.2f}"] if dur else []) + \
          ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    m = _MEAN.search(_run(cmd, 120).stderr)
    return float(m.group(1)) if m else None


def loudness_jump(path: Path, split_s: float) -> float | None:
    """How many dB louder the clip is after `split_s` (the moment) than before it. Streamer shouting/crowd = real moment."""
    quiet, loud = _mean_volume(path, 0, split_s), _mean_volume(path, split_s, None)
    return round(loud - quiet, 1) if quiet is not None and loud is not None else None
