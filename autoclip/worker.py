"""One ChannelWorker = one stream: recorder + chat reader + detector + cutting + metadata."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from .audio import AudioMeter
from .chat import TwitchChat, YouTubeChat
from .config import Config
from .cutter import cut_clip, cut_draft, draft_offset, export_clip, loudness_jump, probe_duration
from .describe import suggest as suggest_description
from .detector import ChatEvent, Spike, SpikeDetector
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


def _dir_gb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9 if p.exists() else 0.0


class ChannelWorker(threading.Thread):
    def __init__(self, cfg: Config, target: str, out_root: Path, stop: threading.Event,
                 source_cmd: list[str] | None = None, chat_factory: Callable[[Callable], threading.Thread] | None = None,
                 pool: ThreadPoolExecutor | None = None):
        platform, label, url = parse_target(target)
        super().__init__(daemon=True, name=f"worker-{label}")
        self.cfg, self.platform, self.label, self.url, self.stop_evt = cfg, platform, label, url, stop
        self.session = datetime.now().strftime("%Y-%m-%d_%H%M")
        self.out_dir = out_root / label / self.session
        self.detector = SpikeDetector(cfg)
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self.recorder = Recorder(cfg, url, stop, label, source_cmd)
        if chat_factory:
            self.chat = chat_factory(self.events.put)
        elif platform == "twitch":
            self.chat = TwitchChat(label, self.events.put, stop)
        else:
            self.chat = YouTubeChat(url, self.events.put, stop)
        self.own_pool = pool is None          # one shared pool across streams keeps total CPU use bounded
        self.pool = pool or ThreadPoolExecutor(max_workers=cfg.cut_workers, thread_name_prefix=f"cut-{label}")
        self.recent: list[tuple[float, float]] = []
        self._warned_layout = False
        self.stats = {"spikes": 0, "near_misses": 0, "clips": 0, "skipped_duplicates": 0, "failed_cuts": 0}
        self.meter = None
        if cfg.audio_assist:
            self.meter = AudioMeter(self.events.put, stop)
            self.recorder.on_segments = self.meter.submit
        self.pending: list[tuple[float, float, float, Spike]] = []   # (cut_at, a, b, spike)
        self.clips_made = 0
        self.root = out_root

    # ---- main loop ---------------------------------------------------------------------------
    def run(self):
        log.info("[%s] watching %s (warming up %ds before clipping)", self.label, self.url, self.cfg.warmup_s)
        self.recorder.start()
        self.chat.start()
        if self.meter:
            self.meter.start()
        grace = self.cfg.chat_grace_s + (4 if self.platform == "youtube" else 0)
        last_status = time.time()
        while not self.stop_evt.wait(1.0):
            now = time.time()
            try:
                while True:
                    self.detector.add(self.events.get_nowait())
            except queue.Empty:
                pass
            for sp in self.detector.advance(now - grace):
                self.stats["spikes"] += 1
                self._schedule(sp)
            for note in self.detector.pop_notes():
                self.stats["near_misses"] += 1
                log.info("[%s] %s", self.label, note)
            for item in [p for p in self.pending if now >= p[0]]:
                self.pending.remove(item)
                self.pool.submit(self._cut, item[1], item[2], item[3])
            if now - last_status > 300:
                last_status = now
                log.info("[%s] chat msgs=%d video=%s clips=%d", self.label, getattr(self.chat, "messages", 0),
                         "live" if self.recorder.live.is_set() else "waiting", self.clips_made)
        if self.pending:
            log.info("[%s] %d spike(s) still waiting for video at shutdown were dropped", self.label, len(self.pending))
        if self.own_pool:
            self.pool.shutdown(wait=True)
        log.info("[%s] session summary: %s", self.label, ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in self.stats.items()))
        self.recorder.join(timeout=20)      # make sure ffmpeg/streamlink are really stopped and the buffer is deleted

    def _schedule(self, sp: Spike):
        c = self.cfg
        a, b = sp.start - c.pre_s, sp.end + c.post_s
        if b - a > c.max_clip_s:
            a = b - c.max_clip_s
        log.info("[%s] spike: %d chatters (normal %.0f, %.1fx)%s, score %.0f -> clip in ~%ds",
                 self.label, sp.peak, sp.baseline, sp.peak / max(sp.baseline, 1),
                 f", audio +{sp.audio_db:.0f} dB" if sp.audio_db >= 3 else "", sp.score, c.post_s + c.segment_s * 2)
        self.pending.append((b + c.segment_s * 1.5 + 2, a, b, sp))

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

    def _cut(self, a: float, b: float, sp: Spike):
        c = self.cfg
        try:
            if _dir_gb(self.root) > c.max_clips_gb:
                log.warning("[%s] clips folder over %.0f GB; not cutting (raise max_clips_gb or purge)", self.label, c.max_clips_gb)
                return
            if self._duplicate(a, b):
                log.info("[%s] skipped a clip that mostly repeats the previous one", self.label)
                self.stats["skipped_duplicates"] += 1
                return
            self.out_dir.mkdir(parents=True, exist_ok=True)
            cid = "clip_" + datetime.fromtimestamp(sp.onset).strftime("%H%M%S")
            out = self.out_dir / f"{cid}.mp4"
            segs = self.recorder.segments_between(a, b)
            sa, sb = suggest_window(sp, c)                      # the 30 s window (wall-clock)
            if c.cut_mode == "encode":
                if not cut_clip(segs, a, b, out, c):
                    return
                d0, sel = max(a, segs[0].t0), None
                sug_a = sa - d0
                moment = sp.onset - d0
            else:
                res = cut_draft(segs, a, b, out)
                if not res:
                    self.stats["failed_cuts"] += 1
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
                    "duration_s": round(dur, 1), "moment_s": round(moment, 1), "clip_len_s": c.clip_len_s,
                    "suggest_start_s": sug[0], "suggest_end_s": sug[1], "exports": [],
                    "spike": {k: v for k, v in asdict(sp).items() if k != "score"}}
            meta["description"] = suggest_description(meta, c)
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(out.with_suffix(".clip.json"))
            self.recent.append((a, b))
            self.clips_made += 1
            self.stats["clips"] += 1
            log.info("[%s] saved %s  draft %.0fs, suggested trim %.0f-%.0fs, score %.0f", self.label, out.name, dur, sug[0], sug[1], score)
            if c.vertical == "auto":                                        # ready-to-post 9:16 of the suggested trim
                layout = LayoutStore(self.root).get(self.label)
                if not layout and not self._warned_layout:
                    self._warned_layout = True
                    log.info("[%s] no webcam/gameplay layout saved yet: vertical clips use a blurred full frame. "
                             "Open the review page and press 'Set layout' once for this channel.", self.label)
                fin = out.with_name(f"{cid}_vertical.mp4")
                if export_clip(out, fin, sug[0], sug[1], c, vertical=True, layout=layout):
                    meta["exports"].append({"file": fin.name, "start": sug[0], "end": sug[1], "vertical": True,
                                            "captions": False, "layout": (layout or {}).get("mode", c.vertical_mode),
                                            "copy": self._copy_final(fin)})
                    tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(out.with_suffix(".clip.json"))
                    log.info("[%s] vertical ready: %s", self.label, fin.name)
        except Exception:
            self.stats["failed_cuts"] += 1
            log.exception("[%s] cut failed", self.label)
