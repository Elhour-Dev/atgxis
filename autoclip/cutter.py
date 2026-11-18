"""Cut clips out of the rolling buffer with ffmpeg."""
from __future__ import annotations

import logging
import os
import time
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config
from . import layout as lay
from .recorder import Segment, ffmpeg_exe, ffprobe_exe

log = logging.getLogger("autoclip.cut")


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")


_ENC_CACHE: dict[str, str] = {}


def pick_encoder(cfg: Config) -> str:
    """'auto' -> first hardware H.264 encoder that REALLY works here (a 1-frame test encode), else libx264.
    Many PCs list nvenc in ffmpeg without having the GPU for it, so listing is not enough."""
    if cfg.encoder != "auto":
        return cfg.encoder
    if "auto" in _ENC_CACHE:
        return _ENC_CACHE["auto"]
    chosen = "libx264"
    listing = _run([ffmpeg_exe(), "-hide_banner", "-encoders"], 30).stdout
    for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if enc not in listing:
            continue
        probe = _run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                      "-frames:v", "1", "-c:v", enc, "-f", "null", "-"], 30)
        if probe.returncode == 0:
            chosen = enc
            break
    log.info("video encoder: %s", chosen)
    _ENC_CACHE["auto"] = chosen
    return chosen


def _vcodec(cfg: Config) -> list[str]:
    enc = pick_encoder(cfg)
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p"]
    q = str(cfg.crf + 1)
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p6", "-rc", "vbr", "-cq", q, "-b:v", "0", "-pix_fmt", "yuv420p"]
    if enc == "h264_qsv":
        return ["-c:v", enc, "-global_quality", q, "-pix_fmt", "nv12"]
    if enc == "h264_amf":
        return ["-c:v", enc, "-quality", "quality", "-rc", "cqp", "-qp_i", q, "-qp_p", q, "-pix_fmt", "yuv420p"]
    return ["-c:v", enc, "-pix_fmt", "yuv420p", "-b:v", "8M"]


def _acodec(cfg: Config) -> list[str]:
    af = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"] if cfg.normalize_audio else []
    return [*af, "-c:a", "aac", "-b:a", "256k", "-ar", "48000"]


def _low_priority(cmd: list[str], cfg: Config) -> tuple[list[str], dict]:
    """Run ffmpeg at below-normal priority so the game / the stream you are watching never competes with it."""
    if not cfg.low_priority:
        return cmd, {}
    if sys.platform == "win32":
        return cmd, {"creationflags": 0x00004000}          # BELOW_NORMAL_PRIORITY_CLASS
    nice = shutil.which("nice")
    return ([nice, "-n", "10", *cmd] if nice else cmd), {}


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


def _filters(cfg: Config, vertical: bool, ass: str | None, layout: dict | None = None) -> list[str]:
    """ffmpeg filter arguments: optional 9:16 layout (split / blur / crop), fps cap, optional burned-in captions.
    `ass` is a bare file name (ffmpeg runs in that folder) so Windows drive letters never break the filter."""
    post = ([f"fps={cfg.out_fps}"] if cfg.out_fps else []) + ([f"ass={ass}"] if ass else [])
    if not vertical:
        return ["-vf", ",".join(post)] if post else []
    base = layout if layout else {"mode": cfg.vertical_mode}
    graph = lay.chain(base, getattr(cfg, "sharpen", 0.0))
    if post:
        graph += f";[v]{','.join(post)}[vo]"
        return ["-filter_complex", graph, "-map", "[vo]", "-map", "0:a?"]
    return ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?"]


def cut_draft(segs: list[Segment], a: float, b: float, out: Path) -> tuple[list[Segment], float] | None:
    """FAST draft: join the buffered segments covering [a,b] with stream copy (no re-encode, ~instant, lossless).
    The draft starts/ends on segment boundaries, so it is a few seconds wider than asked; trim it in the review page.
    Returns (segments used, wall-clock start of the draft) or None if the buffer does not cover the window."""
    sel = sorted((s for s in segs if s.path.exists() and s.t1 > a and s.t0 < b), key=lambda s: s.t0)
    if not sel:
        log.warning("nothing in buffer for that window")
        return None
    covered = sum(min(s.t1, b) - max(s.t0, a) for s in sel)
    if covered < 0.5 * (b - a):
        log.warning("buffer covers only %.0fs of %.0fs wanted; skipping", covered, b - a)
        return None
    listing = out.with_suffix(".txt")
    listing.write_text("".join("file '{}'\n".format(s.path.resolve().as_posix().replace("'", "'\\''")) for s in sel))
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
           "-c", "copy", "-movflags", "+faststart", str(out)]
    try:
        r = _run(cmd)
    finally:
        listing.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        log.error("draft cut failed: %s", r.stderr.strip()[-400:])
        out.unlink(missing_ok=True)
        return None
    return sel, sel[0].t0


def draft_offset(sel: list[Segment], wall: float) -> float:
    """Wall-clock time -> seconds into the draft file (correct even if ad breaks left gaps between segments)."""
    acc = 0.0
    for s in sel:
        if wall < s.t0:
            return acc
        if wall <= s.t1:
            return acc + (wall - s.t0)
        acc += s.t1 - s.t0
    return acc


def probe_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of the first video stream."""
    r = _run([ffprobe_exe(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)], 30)
    try:
        w, h = r.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except ValueError:
        return None


def probe_duration(path: Path) -> float | None:
    r = _run([ffprobe_exe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 30)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def _forget(p: Path) -> None:
    """Delete a leftover file; never raise (on Windows a file that is open elsewhere cannot be deleted)."""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _place(tmp: Path, final: Path) -> Path:
    """Move a finished temp file onto its final name and return where it really ended up.
    Windows cannot replace a file that is open in another program (the browser playing the previous export, an antivirus scan,
    Explorer's preview: WinError 32). Retry briefly, then fall back to a numbered name, so an export is never lost."""
    for _ in range(6):
        try:
            os.replace(tmp, final)
            return final
        except PermissionError:
            time.sleep(0.5)
    for n in range(2, 60):
        alt = final.with_name(f"{final.stem}_{n}{final.suffix}")
        try:
            os.replace(tmp, alt)
            log.info("%s is in use by another program; saved as %s instead", final.name, alt.name)
            return alt
        except PermissionError:
            continue
    raise PermissionError(f"could not save {final.name}: it and every alternative name are in use by other programs")


def export_clip(src: Path, out: Path, start: float, end: float, cfg: Config, vertical: bool = False,
                ass_text: str | None = None, layout: dict | None = None, errors: list | None = None) -> bool:
    """FINAL clip: ONE precise encode that does trim + optional 9:16 layout + optional burned captions together.
    ffmpeg runs inside the output folder with bare file names so Windows drive letters ('C:') never break the ass filter."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")           # encode here, then swap in: a half-written file never replaces a good one
    _forget(tmp)
    ass_name = None
    if ass_text:
        ass_name = out.with_suffix(".ass").name
        (out.parent / ass_name).write_text(ass_text, encoding="utf-8")
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(start, 0):.3f}", "-i", str(src.resolve()),
           "-t", f"{max(end - start, 0.5):.3f}", *_filters(cfg, vertical, ass_name, layout), *_vcodec(cfg),
           *_acodec(cfg), "-movflags", "+faststart", tmp.name]
    cmd, kw = _low_priority(cmd, cfg)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, errors="replace", cwd=out.parent, **kw)
    finally:
        if ass_name:
            _forget(out.parent / ass_name)
    if r.returncode != 0 or not tmp.exists():
        log.error("export failed: %s", r.stderr.strip()[-500:])
        if errors is not None:                       # lets the review page show WHY, e.g. "No such filter: 'ass'"
            errors.append(r.stderr.strip().splitlines()[-1][:300] if r.stderr.strip() else f"ffmpeg exit code {r.returncode}")
        _forget(tmp)
        return False
    try:
        return _place(tmp, out)                      # the Path it was saved as (truthy); may carry a _2 suffix if `out` was locked
    except PermissionError as e:
        _forget(tmp)
        if errors is not None:
            errors.append(str(e))
        return False


def grab_frame(src: Path, t: float, layout_graph: str | None = None, width: int = 1280) -> bytes | None:
    """One JPEG frame at time t (for the layout editor). With layout_graph: the finished 9:16 composition of that frame."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(src), "-frames:v", "1"]
    cmd += ["-filter_complex", layout_graph, "-map", "[v]"] if layout_graph else ["-vf", f"scale={width}:-2"]
    cmd += ["-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None


def make_vertical(src: Path, dst: Path, cfg: Config, layout: dict | None = None) -> bool:
    """Whole-file 9:16 conversion (kept for compatibility); export_clip(vertical=True) is the main path."""
    d = src.parent
    return export_clip(src, dst, 0.0, probe_duration(src) or 9999, cfg, vertical=True, layout=layout) if d else False


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
