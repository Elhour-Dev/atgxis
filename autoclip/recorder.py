"""Rolling-buffer recorder: streamlink -> ffmpeg (stream copy, no re-encode) -> short .ts segments.

Every segment is stamped with the WALL-CLOCK time we received it. Chat is also stamped with wall-clock
time, so the two always line up, even after ads/gaps/reconnects (relative video time would drift).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger("autoclip.rec")


@dataclass(frozen=True)
class Segment:
    path: Path
    t0: float   # wall-clock start
    t1: float   # wall-clock end


def kill(proc: subprocess.Popen | None):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def ffmpeg_exe() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def streamlink_cmd(url: str, quality: str) -> list[str]:
    exe = shutil.which("streamlink") or "streamlink"
    return [exe, "--stdout", "--loglevel", "warning", "--retry-streams", "5", "--retry-max", "3", url, quality]


def segmenter_cmd(cfg: Config) -> list[str]:
    return [ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
            "-segment_time", str(cfg.segment_s), "-segment_format", "mpegts", "-reset_timestamps", "1",
            "-segment_list", "list.csv", "-segment_list_type", "csv", "-segment_list_flags", "+live",
            "seg_%06d.ts"]


class Recorder(threading.Thread):
    def __init__(self, cfg: Config, url: str, stop: threading.Event, label: str,
                 source_cmd: list[str] | None = None):
        super().__init__(daemon=True, name=f"rec-{label}")
        self.cfg, self.url, self.stop_evt, self.source_cmd = cfg, url, stop, source_cmd
        base = Path(cfg.buffer_dir) if cfg.buffer_dir else Path(tempfile.gettempdir())
        self.root = base / f"autoclip_buf_{label}"
        self._segs: list[Segment] = []
        self._lock = threading.Lock()
        self.live = threading.Event()

    # ---- queries -----------------------------------------------------------------------------
    def segments_between(self, a: float, b: float) -> list[Segment]:
        with self._lock:
            return [s for s in self._segs if s.t1 > a and s.t0 < b]

    def last_segment_time(self) -> float:
        with self._lock:
            return self._segs[-1].t1 if self._segs else 0.0

    # ---- thread ------------------------------------------------------------------------------
    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        wait = 5.0
        while not self.stop_evt.is_set():
            try:
                got = self._run_once()
            except Exception as e:
                log.error("recorder error: %s", e)
                got = 0
            self.live.clear()
            if self.stop_evt.is_set():
                break
            if got == 0:
                log.info("stream offline or not reachable; checking again in %.0fs", wait)
                self.stop_evt.wait(wait)
                wait = min(wait * 1.5, 60.0)       # polite back-off while the channel is offline
            else:
                wait = 5.0
                log.info("stream interrupted; reconnecting")
                self.stop_evt.wait(3)
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_once(self) -> int:
        run_dir = self.root / f"run_{int(time.time())}"
        run_dir.mkdir(parents=True, exist_ok=True)
        src = self.source_cmd or streamlink_cmd(self.url, self.cfg.quality)
        with open(run_dir / "source.log", "wb") as slog, open(run_dir / "ffmpeg.log", "wb") as flog:
            sp = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=slog)
            fp = subprocess.Popen(segmenter_cmd(self.cfg), stdin=sp.stdout, stdout=subprocess.DEVNULL, stderr=flog, cwd=run_dir)
            sp.stdout.close()
            seen, produced, last_new = 0, 0, time.time()
            try:
                while not self.stop_evt.is_set() and fp.poll() is None:
                    time.sleep(0.5)
                    rows = self._read_rows(run_dir / "list.csv")
                    if len(rows) > seen:
                        now = time.time()
                        t_end, built = now, []
                        for name, st, en in reversed(rows[seen:]):   # newest first; older ones sit just before it
                            built.append(Segment(run_dir / name, t_end - (en - st), t_end))
                            t_end -= en - st
                        with self._lock:
                            self._segs.extend(reversed(built))
                        seen, produced, last_new = len(rows), len(rows), now
                        self.live.set()
                    elif produced and time.time() - last_new > 45:
                        log.warning("no new video for 45s; restarting capture")
                        break
                    elif not produced and time.time() - last_new > 90:
                        break
                    self._janitor()
            finally:
                kill(sp)
                kill(fp)
        return produced

    @staticmethod
    def _read_rows(csv: Path) -> list[tuple[str, float, float]]:
        try:
            lines = csv.read_text().splitlines()
        except OSError:
            return []
        rows = []
        for ln in lines:
            parts = ln.split(",")
            if len(parts) >= 3:
                try:
                    rows.append((parts[0], float(parts[1]), float(parts[2])))
                except ValueError:
                    pass
        return rows

    def _janitor(self):
        cutoff = time.time() - self.cfg.buffer_s
        with self._lock:
            old = [s for s in self._segs if s.t1 < cutoff]
            self._segs = [s for s in self._segs if s.t1 >= cutoff]
        for s in old:
            try:
                s.path.unlink()
            except OSError:
                pass
