"""Rolling-buffer recorder: streamlink -> ffmpeg (stream copy, no re-encode) -> short .ts segments.

Every segment is stamped with the WALL-CLOCK time we received it. Chat is also stamped with wall-clock
time, so the two always line up, even after ads/gaps/reconnects (relative video time would drift).
"""
from __future__ import annotations

import logging
import re
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


def ffprobe_exe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def streamlink_cmd(url: str, quality: str) -> list[str]:
    exe = shutil.which("streamlink") or "streamlink"
    return [exe, "--stdout", "--loglevel", "info", "--retry-streams", "5", "--retry-max", "3", url, quality]


def segmenter_cmd(cfg: Config) -> list[str]:
    return [ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
            "-segment_time", str(cfg.segment_s), "-segment_format", "mpegts", "-reset_timestamps", "1",
            "-segment_list", "list.csv", "-segment_list_type", "csv", "-segment_list_flags", "+live",
            "seg_%06d.ts"]


# What streamlink says while Twitch shows ads. Real example (streamlink 8.6.3): "[plugins.twitch][info] Will skip ad segments".
AD_WORDS = re.compile(r"advert|pre-?roll|mid-?roll|commercial|\bads?\b", re.I)
START_WAIT_S = 90            # a capture that has produced nothing at all this long is abandoned (unless an ad break is announced)


def streamlink_tail(log_path: Path, nbytes: int = 4000) -> str:
    """The end of streamlink's own log (what it was saying when video stopped)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def ad_break_announced(log_path: Path) -> bool:
    """Streamlink pauses its output while a Twitch ad break runs (and says so in its log). Silence then is not a dead stream."""
    return bool(AD_WORDS.search(streamlink_tail(log_path)))


def last_words(log_path: Path, limit: int = 220) -> str:
    lines = [ln.strip() for ln in streamlink_tail(log_path).splitlines() if ln.strip()]
    return lines[-1][:limit] if lines else ""


def should_restart(idle: float, produced: bool, ad_break: bool, cfg: Config) -> bool:
    """Give up on this capture and start a new one? A stall normally means a dead connection (restart after cfg.stall_s), but during
    an ad break streamlink is paused on purpose: restarting would only start a new session, with a new pre-roll ad, over and over.
    So with an ad break announced we wait up to cfg.ad_stall_s."""
    limit = cfg.stall_s if produced else START_WAIT_S
    if idle <= limit:
        return False
    return not (ad_break and idle <= cfg.ad_stall_s)


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
        self.on_segments = None          # optional callback(list[Segment]) for every batch of new segments
        self.paused_for_ad = False       # streamlink announced an ad break and we are waiting it out
        self.defer_cleanup = False       # True: the owner (ChannelWorker) deletes the buffer via cleanup() once its last cut is done

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
        if not self.defer_cleanup:
            self.cleanup()

    def cleanup(self) -> None:
        """Delete the whole rolling buffer. With defer_cleanup the worker calls this AFTER it has cut the clips that were
        still waiting when Ctrl+C was pressed (they need the video; deleting it at stop time lost them)."""
        with self._lock:
            self._segs.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_once(self) -> int:
        run_dir = self.root / f"run_{int(time.time())}"
        run_dir.mkdir(parents=True, exist_ok=True)
        src = self.source_cmd or streamlink_cmd(self.url, self.cfg.quality)
        with open(run_dir / "source.log", "wb") as slog, open(run_dir / "ffmpeg.log", "wb") as flog:
            sp = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=slog)
            fp = subprocess.Popen(segmenter_cmd(self.cfg), stdin=sp.stdout, stdout=subprocess.DEVNULL, stderr=flog, cwd=run_dir)
            sp.stdout.close()
            offset, produced, last_new, last_clean, ad_noted = 0, 0, time.time(), 0.0, False
            slog_path = run_dir / "source.log"
            try:
                while not self.stop_evt.is_set() and fp.poll() is None:
                    time.sleep(0.5)
                    rows, offset = self._read_new(run_dir / "list.csv", offset)    # only the new lines: the file grows all day
                    if rows:
                        now = time.time()
                        t_end, built = now, []
                        for name, st, en in reversed(rows):          # newest first; older ones sit just before it
                            built.append(Segment(run_dir / name, t_end - (en - st), t_end))
                            t_end -= en - st
                        with self._lock:
                            self._segs.extend(reversed(built))
                        if self.on_segments:
                            try:
                                self.on_segments(list(reversed(built)))
                            except Exception:
                                log.exception("segment callback failed")
                        produced += len(rows)
                        last_new = now
                        self.live.set()
                        self.paused_for_ad, ad_noted = False, False
                    else:
                        idle = time.time() - last_new
                        if idle > 15:
                            self.live.clear()                      # the status must not say "live" while no video arrives
                        if idle > (self.cfg.stall_s if produced else START_WAIT_S):
                            ad = ad_break_announced(slog_path)
                            self.paused_for_ad = ad
                            if should_restart(idle, bool(produced), ad, self.cfg):
                                if produced:
                                    say = last_words(slog_path)
                                    log.warning("no new video for %ds; restarting capture%s", idle, f" (streamlink said: {say})" if say else "")
                                break
                            if not ad_noted:
                                ad_noted = True
                                log.info("streamlink is paused for a Twitch ad break: waiting for it to end (up to %ds) instead of restarting",
                                         self.cfg.ad_stall_s)
                    if time.time() - last_clean >= 2.0:                  # pruning twice a second was wasted work
                        last_clean = time.time()
                        self._janitor()
            finally:
                kill(sp)
                kill(fp)
        if not produced:                              # offline check that recorded nothing: no folder + logs left behind per check
            say = last_words(slog_path)                # but keep the reason: "video: waiting" is useless without it
            log.info("capture ended without any video%s", f" (streamlink said: {say})" if say else " (streamlink said nothing)")
            shutil.rmtree(run_dir, ignore_errors=True)
        return produced

    @staticmethod
    def _read_new(csv: Path, offset: int) -> tuple[list[tuple[str, float, float]], int]:
        """Rows added to the segment list since byte `offset` -> (rows, new offset). Re-reading the whole file twice a second
        costs more every hour (it only ever grows); a half-written last line is left for the next call."""
        try:
            with open(csv, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return [], offset
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        rows = []
        for ln in data[:end].decode("utf-8", "replace").splitlines():
            parts = ln.split(",")
            if len(parts) >= 3:
                try:
                    rows.append((parts[0], float(parts[1]), float(parts[2])))
                except ValueError:
                    pass
        return rows, offset + end + 1

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
