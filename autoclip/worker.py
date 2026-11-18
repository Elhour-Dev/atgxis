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

from .chat import TwitchChat, YouTubeChat
from .config import Config
from .cutter import cut_clip, loudness_jump, make_vertical
from .detector import ChatEvent, Spike, SpikeDetector
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


def _dir_gb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9 if p.exists() else 0.0


class ChannelWorker(threading.Thread):
    def __init__(self, cfg: Config, target: str, out_root: Path, stop: threading.Event,
                 source_cmd: list[str] | None = None, chat_factory: Callable[[Callable], threading.Thread] | None = None):
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
        self.pool = ThreadPoolExecutor(max_workers=cfg.cut_workers, thread_name_prefix=f"cut-{label}")
        self.pending: list[tuple[float, float, float, Spike]] = []   # (cut_at, a, b, spike)
        self.clips_made = 0
        self.root = out_root

    # ---- main loop ---------------------------------------------------------------------------
    def run(self):
        log.info("[%s] watching %s (warming up %ds before clipping)", self.label, self.url, self.cfg.warmup_s)
        self.recorder.start()
        self.chat.start()
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
                self._schedule(sp)
            for item in [p for p in self.pending if now >= p[0]]:
                self.pending.remove(item)
                self.pool.submit(self._cut, item[1], item[2], item[3])
            if now - last_status > 300:
                last_status = now
                log.info("[%s] chat msgs=%d video=%s clips=%d", self.label, getattr(self.chat, "messages", 0),
                         "live" if self.recorder.live.is_set() else "waiting", self.clips_made)
        if self.pending:
            log.info("[%s] %d spike(s) still waiting for video at shutdown were dropped", self.label, len(self.pending))
        self.pool.shutdown(wait=True)
        self.recorder.join(timeout=20)      # make sure ffmpeg/streamlink are really stopped and the buffer is deleted

    def _schedule(self, sp: Spike):
        c = self.cfg
        a, b = sp.start - c.pre_s, sp.end + c.post_s
        if b - a > c.max_clip_s:
            a = b - c.max_clip_s
        log.info("[%s] spike: %d chatters (normal %.0f), score %.0f -> clip in ~%ds",
                 self.label, sp.peak, sp.baseline, sp.score, c.post_s + c.segment_s * 2)
        self.pending.append((b + c.segment_s * 1.5 + 2, a, b, sp))

    # ---- cutting -----------------------------------------------------------------------------
    def _cut(self, a: float, b: float, sp: Spike):
        try:
            if _dir_gb(self.root) > self.cfg.max_clips_gb:
                log.warning("[%s] clips folder over %.0f GB; not cutting (raise max_clips_gb or purge)", self.label, self.cfg.max_clips_gb)
                return
            self.out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(sp.start).strftime("%H%M%S")
            cid = f"clip_{stamp}"
            out = self.out_dir / f"{cid}.mp4"
            if not cut_clip(self.recorder.segments_between(a, b), a, b, out, self.cfg):
                return
            score, jump = sp.score, loudness_jump(out, self.cfg.pre_s * 0.8)
            if jump is not None and jump > 3:
                score = min(100.0, round(score + min(jump, 10), 1))          # louder after the moment = real reaction
            meta = {"id": f"{self.label}/{self.session}/{cid}", "file": out.name, "channel": self.label,
                    "platform": self.platform, "source_url": self.url, "status": "new", "score": score,
                    "chat_score": sp.score, "loudness_jump_db": jump,
                    "created": datetime.fromtimestamp(a).isoformat(timespec="seconds"),
                    "duration_s": round(b - a, 1), "spike": {k: v for k, v in asdict(sp).items() if k != "score"},
                    "vertical": None}
            if self.cfg.vertical == "auto":
                v = out.with_name(cid + "_vertical.mp4")
                if make_vertical(out, v, self.cfg):
                    meta["vertical"] = v.name
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(out.with_suffix(".clip.json"))
            self.clips_made += 1
            log.info("[%s] saved %s (score %.0f)", self.label, out, score)
        except Exception:
            log.exception("[%s] cut failed", self.label)
