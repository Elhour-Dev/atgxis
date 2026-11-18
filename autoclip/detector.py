"""Chat-spike detector: pure logic, no I/O, driven by timestamps so it is fully testable.

What counts as a spike: the number of DIFFERENT chatters in the last `window_s` seconds jumps well
above the channel's own normal level. Counting unique chatters (not messages) makes it hard for one
person, a bot, or `!command` spam to fake a spike, and a raid is explicitly muted.
"""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from .config import Config


@dataclass
class ChatEvent:
    t: float            # unix time the message was seen
    user: str = ""
    text: str = ""
    kind: str = "msg"   # "msg" | "raid"


@dataclass
class Spike:
    start: float
    end: float
    onset: float        # best estimate of the second chat really took off (tighter than `start`)
    peak_t: float
    peak: int           # most unique chatters seen in one window
    baseline: float
    msgs: int
    users: int
    hype_frac: float
    top: list[tuple[str, int]] = field(default_factory=list)
    score: float = 0.0


class _Bucket:
    __slots__ = ("users", "n", "hype", "texts")

    def __init__(self):
        self.users: set[str] = set()
        self.n = 0
        self.hype = 0
        self.texts: Counter[str] = Counter()


class _Acc:
    def __init__(self, start: float, baseline: float):
        self.start = start
        self.onset = start
        self.end = start
        self.baseline = baseline
        self.peak = 0
        self.peak_t = start
        self.users: set[str] = set()
        self.n = 0
        self.hype = 0
        self.texts: Counter[str] = Counter()

    def add(self, b: _Bucket | None):
        if b:
            self.users |= b.users
            self.n += b.n
            self.hype += b.hype
            self.texts += b.texts


_TOKEN = re.compile(r"[^\W_]+|[^\w\s]", re.UNICODE)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_spike(peak: int, baseline: float, hype_frac: float) -> float:
    """0-100 heuristic: how far above normal, how many people, how 'hype' the words are."""
    ratio = math.log2(max(peak, 1) / max(baseline, 1.0))
    return round(100 * (0.4 * _clamp(ratio / 4) + 0.3 * _clamp(math.log10(peak + 1) / 2)
                        + 0.3 * _clamp(hype_frac / 0.5)), 1)


class SpikeDetector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bots = {b.lower() for b in cfg.bots}
        self.hype = {w.lower() for w in cfg.hype_terms}
        self.buckets: dict[int, _Bucket] = {}
        self.samples: deque[tuple[int, int]] = deque()
        self.state = "idle"             # idle | active | cooling
        self.cur: _Acc | None = None
        self.low_run = 0
        self.cool_start = 0
        self.cooldown_until = 0
        self.raid_until = 0.0
        self.first_sec: int | None = None
        self.last_sec: int | None = None
        self.late = 0                   # messages that arrived after their second was processed

    # ---- input -------------------------------------------------------------------------------
    def add(self, ev: ChatEvent) -> None:
        if ev.kind == "raid":
            self.raid_until = max(self.raid_until, ev.t + self.cfg.raid_suppress_s)
            return
        text = ev.text.strip()
        if not text or text.startswith("!") or ev.user.lower() in self.bots:
            return
        sec = int(ev.t)
        if self.last_sec is not None and sec <= self.last_sec:
            self.late += 1
            return
        b = self.buckets.setdefault(sec, _Bucket())
        b.users.add(ev.user.lower())
        b.n += 1
        low = text.lower()
        b.texts[low[:60]] += 1
        if any(tok in self.hype for tok in _TOKEN.findall(low)):
            b.hype += 1

    # ---- clock ---------------------------------------------------------------------------------
    def advance(self, t: float) -> list[Spike]:
        """Promise: no more events older than t will arrive. Returns finished spikes."""
        out: list[Spike] = []
        upto = int(t) - 1
        if self.first_sec is None:
            self.first_sec = upto
            self.last_sec = upto - 1
        for s in range(self.last_sec + 1, upto + 1):
            sp = self._tick(s)
            if sp:
                out.append(sp)
            self.last_sec = s
        return out

    def flush(self) -> Spike | None:
        """Finish any open spike (used on shutdown)."""
        if self.cur and self.state in ("active", "cooling"):
            sp = self._finish(self.cur.end if self.state == "cooling" else (self.last_sec or self.cur.start))
            self.state = "idle"
            return sp
        return None

    # ---- internals -----------------------------------------------------------------------------
    def _window_users(self, s: int) -> int:
        users: set[str] = set()
        for i in range(s - self.cfg.window_s + 1, s + 1):
            b = self.buckets.get(i)
            if b:
                users |= b.users
        return len(users)

    def _baseline(self) -> float:
        vals = sorted(v for _, v in self.samples)
        if not vals:
            return 1.0
        return max(float(vals[len(vals) // 2]), 1.0)

    def _threshold(self, baseline: float) -> float:
        c = self.cfg
        return max(c.min_users, c.mult * baseline, baseline + c.z * math.sqrt(baseline))

    def _tick(self, s: int) -> Spike | None:
        c = self.cfg
        value = self._window_users(s)
        result: Spike | None = None

        if self.state == "idle":
            base = self._baseline()
            warm = (s - self.first_sec) >= c.warmup_s
            if warm and s >= self.cooldown_until and s >= self.raid_until and value >= self._threshold(base):
                self.cur = _Acc(start=s - c.window_s + 1, baseline=base)
                for i in range(s - c.window_s + 1, s + 1):
                    self.cur.add(self.buckets.get(i))
                self.cur.peak, self.cur.peak_t, self.cur.end = value, s, s
                self.cur.onset = self._onset(s)
                self.state, self.low_run = "active", 0
            else:
                # only learn "normal" from quiet times, never from raids or spikes
                if s >= self.raid_until:
                    self.samples.append((s, value))
                while self.samples and self.samples[0][0] < s - c.baseline_s:
                    self.samples.popleft()

        elif self.state == "active":
            cur = self.cur
            cur.add(self.buckets.get(s))
            if value > cur.peak:
                cur.peak, cur.peak_t = value, s
            if value >= cur.baseline * c.exit_ratio + 1:
                cur.end, self.low_run = s, 0
            else:
                self.low_run += 1
            if s - cur.start >= c.max_active_s:                    # very long spike: split
                result = self._finish(s)
                self.state, self.cooldown_until = "idle", s + c.cooldown_s
            elif self.low_run >= c.hold_s:
                self.state, self.cool_start = "cooling", s

        elif self.state == "cooling":
            cur = self.cur
            if value >= self._threshold(cur.baseline):             # second wave: same clip
                cur.add(self.buckets.get(s))
                cur.end, self.low_run, self.state = s, 0, "active"
                if value > cur.peak:
                    cur.peak, cur.peak_t = value, s
            elif s - self.cool_start >= c.merge_gap_s:
                result = self._finish(cur.end)
                self.state = "idle"

        for k in [k for k in self.buckets if k < s - c.window_s - 1]:
            del self.buckets[k]
        return result

    def _onset(self, s: int) -> int:
        """First second of the trigger window where chat really took off (>=25% of the window's busiest second)."""
        counts = [(i, len(b.users) if (b := self.buckets.get(i)) else 0) for i in range(s - self.cfg.window_s + 1, s + 1)]
        top = max(c for _, c in counts)
        return next((i for i, c in counts if c >= max(2, 0.25 * top)), s - self.cfg.window_s + 1)

    def _finish(self, end: float) -> Spike:
        cur = self.cur
        hype_frac = cur.hype / cur.n if cur.n else 0.0
        sp = Spike(start=cur.start, end=max(end, cur.start + 1), onset=cur.onset, peak_t=cur.peak_t, peak=cur.peak,
                   baseline=cur.baseline, msgs=cur.n, users=len(cur.users), hype_frac=round(hype_frac, 3),
                   top=cur.texts.most_common(8))
        sp.score = score_spike(sp.peak, sp.baseline, sp.hype_frac)
        self.cur = None
        return sp
