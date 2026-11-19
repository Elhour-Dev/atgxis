"""Disk bookkeeping for long unattended runs: how big the clips folder is, how much room is left, and clean-up of buffers that an
unclean exit (closing the console window, a crash, a power cut) left behind."""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

BUFFER_PREFIX = "autoclip_buf_"
_CACHE: dict[str, tuple[float, float]] = {}
_LOCK = threading.Lock()


def dir_gb(p: Path) -> float:
    total = 0
    if p.exists():
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:                      # a file vanished mid-scan (another stream's temp file)
                pass
    return total / 1e9


def clips_gb(p: Path, max_age: float = 30.0) -> float:
    """Size of the clips folder in GB, re-measured at most every `max_age` s. Every stream checks it before every cut, and
    walking a folder with hundreds of clips for each of them is real disk work; the limit is soft, so 30 s old is fine."""
    key, now = str(p), time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < max_age:
            return hit[1]
    gb = dir_gb(p)
    with _LOCK:
        _CACHE[key] = (time.time(), gb)
    return gb


def free_gb(p: Path) -> float | None:
    """Free space (GB) on the drive that holds `p` (which need not exist yet). None if it cannot be read."""
    p = Path(p).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return None


def _newest_mtime(d: Path) -> float:
    newest = d.stat().st_mtime
    for f in d.rglob("*"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    return newest


def sweep_stale_buffers(base: Path, max_age_s: float = 900.0) -> int:
    """Delete rolling-buffer folders (autoclip_buf_*) nothing has written to for `max_age_s`. A live buffer gets new video every
    few seconds (and new folders while a stream is offline), so only leftovers of a dead run match. Returns how many were removed."""
    removed = 0
    if not base.is_dir():
        return 0
    for d in base.glob(BUFFER_PREFIX + "*"):
        try:
            if d.is_dir() and time.time() - _newest_mtime(d) > max_age_s:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
