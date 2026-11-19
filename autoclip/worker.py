"""One ChannelWorker = one stream: recorder + chat reader + detector + cutting + metadata."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from concurrent.futures import Future, wait as wait_futures
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from .audio import AudioMeter
from .chat import TwitchChat, YouTubeChat
from .config import Config
from .cutter import cut_clip, cut_draft, draft_offset, export_clip, loudness_jump, probe_duration, probe_size
from .describe import suggest as suggest_description
from .disk import clips_gb, free_gb
from .detector import ChatEvent, Spike, SpikeDetector
from .lanes import Lanes
from .layout import LayoutStore
from .recorder import Recorder

log = logging.getLogger("autoclip.worker")


def parse_target(target: str) -> tuple[str, str, str]:
    """-> (platform, label, url). Accepts 'name', twitch.tv/name, or any youtube.com / youtu.be link."""
    t = target.strip()
    if re.search(r"(youtube\.com|youtu\.be)", t):
        m = re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{6,})|(@[\w.-]+)", t)
        label = "yt_" + (m.group(1) or m.group(2) if m else "stream").lstrip("@")
        return "youtube", re.sub(r"[^\w.-]", "_", label), (t if t.startswith("http") else "https://" + t)
    name = re.sub(r"^(https?://)?(www\.)?twitch\.tv/", "", t).strip("/").split("/")[0].lower()
    if not re.fullmatch(r"\w{2,25}", name):
        raise ValueError(f"cannot understand target {target!r}")
    return "twitch", name, f"https://www.twitch.tv/{name}"


def suggest_window(sp: Spike, cfg: Config) -> tuple[float, float]:
    """Wall-clock window of exactly cfg.clip_len_s: a few seconds of lead-in before the moment, and always
    containing the chat peak (the reaction). Long spikes slide the window to the peak instead of getting longer."""
    n = cfg.clip_len_s
    start = sp.onset - cfg.reaction_lag_s - cfg.lead_in_s
    if sp.peak_t + 5 > start + n:
        start = sp.peak_t + 5 - n
    return start, start + n


def fit_window(start: float, dur: float, length: float) -> tuple[float, float]:
    """Clamp a [start, start+length] window inside a file of `dur` seconds, keeping the length when possible."""
    length = min(length, dur)
    start = min(max(start, 0.0), dur - length)
    return round(start, 1), round(start + length, 1)


class ChannelWorker(threading.Thread):
    def __init__(self, cfg: Config, target: str, out_root: Path, stop: threading.Event,
                 source_cmd: list[str] | None = None, chat_factory: Callable[[Callable], threading.Thread] | None = None,
                 lanes: Lanes | None = None):
        platform, label, url = parse_target(target)
        super().__init__(daemon=True, name=f"worker-{label}")
        self.cfg, self.platform, self.label, self.url, self.stop_evt = cfg, platform, label, url, stop
        self.session = datetime.now().strftime("%Y-%m-%d_%H%M")
        self.out_dir = out_root / label / self.session
        self.detector = SpikeDetector(cfg)
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self.recorder = Recorder(cfg, url, stop, label, source_cmd)
        self.recorder.defer_cleanup = True        # the buffer is deleted by _shutdown, after the last waiting clip was cut
        if chat_factory:
            self.chat = chat_factory(self.events.put)
        elif platform == "twitch":
            self.chat = TwitchChat(label, self.events.put, stop)
        else:
            self.chat = YouTubeChat(url, self.events.put, stop)
        # With several streams ONE Lanes object is shared, so total CPU stays bounded whatever the number of streams.
        self.own_lanes = lanes is None
        self.lanes = lanes or Lanes(cfg)
        self.recent: list[tuple[float, float]] = []
        self._lock = threading.Lock()             # recent + stats: drafts of one stream can now be cut at the same time
        self._warned_layout = False
        self.stats = {"spikes": 0, "near_misses": 0, "clips": 0, "skipped_duplicates": 0, "failed_cuts": 0,
                      "verticals": 0, "left_as_drafts": 0, "skipped_disk": 0, "no_video": 0}
        self.meter = None
        if cfg.audio_assist:
            self.meter = AudioMeter(self.events.put, stop)
            self.recorder.on_segments = self.meter.submit
        self.pending: list[tuple[float, float, float, Spike]] = []   # (cut_at, a, b, spike)
        self._jobs: list[Future] = []
        self.clips_made = 0
        self.last_clip_at = 0.0
        self.root = out_root
        self.snapshot: dict = {"label": self.label, "video": "waiting", "msgs_per_min": 0, "normal": 0.0, "warmup_left": cfg.warmup_s,
                               "spikes": 0, "clips": 0, "last_clip": ""}            # refreshed by this thread, read by the status overview
        self._snap_at, self._snap_msgs = time.time(), 0

    # ---- main loop ---------------------------------------------------------------------------
    def run(self):
        log.info("[%s] watching %s (warming up %ds before clipping)", self.label, self.url, self.cfg.warmup_s)
        self.recorder.start()
        self.chat.start()
        if self.meter:
            self.meter.start()
        self._grace = self.cfg.chat_grace_s + (4 if self.platform == "youtube" else 0)
        try:
            while not self.stop_evt.wait(1.0):
                try:
                    self._tick()
                except Exception:              # one bad second must not silently end the watching of this stream for good
                    log.exception("[%s] watcher error (carrying on)", self.label)
        finally:
            self._shutdown()

    def _tick(self):
        now = time.time()
        try:
            while True:
                self.detector.add(self.events.get_nowait())
        except queue.Empty:
            pass
        for sp in self.detector.advance(now - self._grace):
            self.stats["spikes"] += 1
            self._schedule(sp)
        for note in self.detector.pop_notes():
            self.stats["near_misses"] += 1
            log.info("[%s] %s", self.label, note)
        for item in [p for p in self.pending if now >= p[0]]:
            self.pending.remove(item)
            self._submit_cut(item[1], item[2], item[3])
        self._jobs = [f for f in self._jobs if not f.done()]
        if now - self._snap_at >= 5:
            self._refresh_snapshot(now)

    def _refresh_snapshot(self, now: float):
        msgs = getattr(self.chat, "messages", 0)
        rate = (msgs - self._snap_msgs) * 60.0 / max(now - self._snap_at, 1.0)
        self._snap_at, self._snap_msgs = now, msgs
        self.snapshot = {"label": self.label, "video": "live" if self.recorder.live.is_set() else "waiting",
                         "msgs_per_min": round(rate), "normal": self.detector.normal_level(),
                         "warmup_left": self.detector.warmup_left(), "spikes": self.stats["spikes"], "clips": self.clips_made,
                         "last_clip": time.strftime("%H:%M", time.localtime(self.last_clip_at)) if self.last_clip_at else ""}

    def _shutdown(self):
        """Ctrl+C: a spike that was found but still waiting for its post-roll is cut NOW from what the buffer holds (it used
        to be dropped), and only then is the buffer deleted."""
        try:
            sp = self.detector.flush()
            if sp:
                self.stats["spikes"] += 1
                self._schedule(sp, immediate=True)
            for item in self.pending:
                self._submit_cut(item[1], item[2], item[3])
            if self.pending:
                log.info("[%s] stopping: cutting %d clip(s) that were still waiting for video", self.label, len(self.pending))
            self.pending.clear()
            wait_futures(self._jobs, timeout=120)
        except Exception:
            log.exception("[%s] could not finish waiting clips", self.label)
        if self.own_lanes:
            self.lanes.shutdown()
        log.info("[%s] session summary: %s", self.label, ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in self.stats.items()))
        self.recorder.join(timeout=20)      # make sure ffmpeg/streamlink are really stopped...
        self.recorder.cleanup()             # ...then the buffer is deleted

    def _schedule(self, sp: Spike, immediate: bool = False):
        c = self.cfg
        a, b = sp.start - c.pre_s, sp.end + c.post_s
        if b - a > c.max_clip_s:
            a = b - c.max_clip_s
        log.info("[%s] spike: %d chatters (normal %.0f, %.1fx)%s, score %.0f%s",
                 self.label, sp.peak, sp.baseline, sp.peak / max(sp.baseline, 1),
                 f", audio +{sp.audio_db:.0f} dB" if sp.audio_db >= 3 else "", sp.score,
                 "" if immediate else f" -> clip in ~{c.post_s + c.segment_s * 2}s")
        self.pending.append((0.0 if immediate else b + c.segment_s * 1.5 + 2, a, b, sp))

    def _submit_cut(self, a: float, b: float, sp: Spike):
        """Draft lane: its own threads, so a draft never waits behind someone's long 9:16 encode."""
        try:
            self._jobs.append(self.lanes.drafts.submit(self._cut, a, b, sp))
        except RuntimeError:                   # lanes already shut down
            log.warning("[%s] a clip could not be cut because the program is closing", self.label)

    # ---- status (read by the overview printed every status_every_s) ---------------------------
    def status_line(self) -> str:
        s = self.snapshot
        if s["video"] != "live":
            body = "video: waiting (stream offline or connecting)"
        elif s["warmup_left"] > 0:
            body = f"learning normal chat level ({s['warmup_left']}s left)"
        else:
            body = f"chat {s['msgs_per_min']} msg/min (normal {s['normal']:.0f} chatters)"
        return f"{self.label:<18s} {body}  | spikes {s['spikes']}, clips {s['clips']}" + (f" (last {s['last_clip']})" if s["last_clip"] else "")

    # ---- cutting -----------------------------------------------------------------------------
    def _copy_final(self, f: Path) -> str | None:
        """Also place the finished clip in the folder chosen for final clips."""
        if not self.cfg.export_dir:
            return None
        try:
            import shutil
            dest = Path(self.cfg.export_dir) / self.label
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest / f.name)
            return str(dest / f.name)
        except OSError as e:
            log.warning("[%s] could not copy to the final clips folder %s: %s", self.label, self.cfg.export_dir, e)
            return None

    def _duplicate(self, a: float, b: float) -> bool:
        for a2, b2 in self.recent:
            if (min(b, b2) - max(a, a2)) / max(min(b - a, b2 - a2), 1) >= self.cfg.dup_overlap:
                return True
        return False

    def _claim(self, a: float, b: float) -> bool:
        """Check-and-reserve in one step: two spikes of one stream are cut by different threads now, so without this both could
        pass the duplicate check before either one was recorded."""
        with self._lock:
            if self._duplicate(a, b):
                return False
            self.recent = self.recent[-199:] + [(a, b)]          # only recent clips can overlap a new one: keep the list short all day
            return True

    def _unclaim(self, a: float, b: float):
        with self._lock:
            if (a, b) in self.recent:
                self.recent.remove((a, b))

    def _count(self, key: str):
        with self._lock:
            self.stats[key] += 1

    def _cut(self, a: float, b: float, sp: Spike):
        c = self.cfg
        claimed = False
        try:
            if clips_gb(self.root) > c.max_clips_gb:
                log.warning("[%s] clips folder over %.0f GB; not cutting (raise max_clips_gb or purge)", self.label, c.max_clips_gb)
                self._count("skipped_disk")
                return
            free = free_gb(self.root)
            if free is not None and free < c.min_free_gb:
                log.warning("[%s] only %.1f GB free on the clips drive (min_free_gb = %g); not cutting. Free up space.", self.label, free, c.min_free_gb)
                self._count("skipped_disk")
                return
            if not self._claim(a, b):
                log.info("[%s] skipped a clip that mostly repeats the previous one", self.label)
                self._count("skipped_duplicates")
                return
            claimed = True
            self.out_dir.mkdir(parents=True, exist_ok=True)
            cid = "clip_" + datetime.fromtimestamp(sp.onset).strftime("%H%M%S")
            out = self.out_dir / f"{cid}.mp4"
            segs = self.recorder.segments_between(a, b)
            if not segs:                                        # chat went wild but there is no video: stream offline / reconnecting
                log.info("[%s] chat spiked but there is no video in the buffer for that moment (stream offline or reconnecting): skipped", self.label)
                self._unclaim(a, b)
                self._count("no_video")
                return
            sa, sb = suggest_window(sp, c)                      # the 30 s window (wall-clock)
            if c.cut_mode == "encode":
                if not cut_clip(segs, a, b, out, c):
                    self._unclaim(a, b)
                    return
                d0, sel = max(a, segs[0].t0), None
                sug_a = sa - d0
                moment = sp.onset - d0
            else:
                res = cut_draft(segs, a, b, out)
                if not res:
                    self._count("failed_cuts")
                    self._unclaim(a, b)
                    return
                sel, d0 = res
                sug_a = draft_offset(sel, sa)
                moment = draft_offset(sel, sp.onset)
            dur = probe_duration(out) or (b - a)
            sug = fit_window(sug_a, dur, c.clip_len_s)
            score, jump = sp.score, loudness_jump(out, max(moment - c.reaction_lag_s, 1.0))
            if jump is not None and jump > 3:
                score = min(100.0, round(score + min(jump, 10), 1))          # louder after the moment = real reaction
            meta = {"id": f"{self.label}/{self.session}/{cid}", "file": out.name, "channel": self.label,
                    "platform": self.platform, "source_url": self.url, "status": "new", "score": score,
                    "chat_score": sp.score, "loudness_jump_db": jump,
                    "created": datetime.fromtimestamp(d0).isoformat(timespec="seconds"),
                    "res": list(probe_size(out) or []) or None,
                    "duration_s": round(dur, 1), "moment_s": round(moment, 1), "clip_len_s": c.clip_len_s,
                    "suggest_start_s": sug[0], "suggest_end_s": sug[1], "exports": [],
                    "spike": {k: v for k, v in asdict(sp).items() if k != "score"}}
            meta["description"] = suggest_description(meta, c)
            _write_meta(out.with_suffix(".clip.json"), meta)
            with self._lock:
                self.clips_made += 1
                self.stats["clips"] += 1
                self.last_clip_at = time.time()
            log.info("[%s] saved %s  draft %.0fs, suggested trim %.0f-%.0fs, score %.0f", self.label, out.name, dur, sug[0], sug[1], score)
            if c.vertical == "auto":                                        # ready-to-post 9:16 of the suggested trim
                self._queue_vertical(out, cid, sug, score)
        except Exception:
            if claimed:
                self._unclaim(a, b)
            self._count("failed_cuts")
            log.exception("[%s] cut failed", self.label)

    # ---- 9:16 render (encode lane) -------------------------------------------------------------
    def _queue_vertical(self, out: Path, cid: str, sug: tuple[float, float], score: float):
        enc = self.lanes.encodes
        if enc.waiting >= self.cfg.max_encode_backlog:
            self._count("left_as_drafts")
            log.warning("[%s] %s: %d 9:16 renders are already waiting, so this clip stays a draft "
                        "(press Export in the review page for it)", self.label, cid, enc.waiting)
            return
        if not enc.submit(score, self._encode_vertical, out, cid, sug):         # best moments are rendered first
            self._count("left_as_drafts")

    def _encode_vertical(self, out: Path, cid: str, sug: tuple[float, float]):
        c, meta_path = self.cfg, out.with_suffix(".clip.json")
        try:
            meta = _read_meta(meta_path)
            if not out.exists() or not meta or meta.get("status") == "trash":
                return                                                           # purged / trashed while it waited in line
            layout = LayoutStore(self.root).get(self.label)                      # the layout as it is NOW, not when it was queued
            if not layout and not self._warned_layout:
                self._warned_layout = True
                log.info("[%s] no webcam/gameplay layout saved yet: vertical clips use a blurred full frame. "
                         "Open the review page and press 'Set layout' once for this channel.", self.label)
            fin = out.with_name(f"{cid}_vertical.mp4")
            placed = export_clip(out, fin, sug[0], sug[1], c, vertical=True, layout=layout)
            if not placed:
                self._count("failed_cuts")
                return
            fin = placed if isinstance(placed, Path) else fin                    # may carry a _2 suffix if the name was locked
            rec = {"file": fin.name, "start": sug[0], "end": sug[1], "vertical": True, "captions": False,
                   "layout": (layout or {}).get("mode", c.vertical_mode), "copy": self._copy_final(fin)}
            meta = _read_meta(meta_path) or meta                                 # re-read: you may have pressed Keep/Trash meanwhile
            meta.setdefault("exports", []).append(rec)
            _write_meta(meta_path, meta)
            self._count("verticals")
            log.info("[%s] vertical ready: %s", self.label, fin.name)
        except Exception:
            self._count("failed_cuts")
            log.exception("[%s] vertical render failed", self.label)


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta(path: Path, meta: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
