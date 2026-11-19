"""Autopilot: posts the clips the taste model is confident you would keep, without you pressing anything.

Posting to public accounts cannot be undone, so every rule below is enforced here in code (not only in the page):

  * OFF until you switch it on. "Suggest" only marks picks in the review page; "Post" is the only mode that publishes.
  * It acts only when the model has PROVEN itself on clips it never saw while learning (taste.readiness), and only above that confidence.
  * Only channels you ticked (publishing someone else's stream needs their permission), only platforms you ticked, only through a
    connected account, only a 9:16 export of the suggested trim.
  * A veto window: a pick waits `delay_min` minutes, marked in the review page, before it goes out. Trash it, or press "Don't post", to stop it.
  * A cap per 24 hours and a minimum gap between posts. Clips older than `max_age_h` are never posted.
  * Never twice: a platform that already has this clip is skipped, and a post that was started but never reported back (crash, power
    cut) is NOT retried by itself, because it may have gone out.
  * Circuit breaker: if you trash too many of its recent posts it switches itself to "Suggest" and says why.
  * Everything it does is written to autopilot_log.jsonl next to your settings.

It runs inside `python -m autoclip run` (a thread), or on its own with `python -m autoclip autopilot run`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import settings, share, taste

log = logging.getLogger("autoclip.autopilot")

MODES = ("off", "suggest", "post")
DEFAULTS = {"mode": "off", "platforms": [], "provider": "auto", "channels": [], "max_per_day": 3, "gap_min": 60, "delay_min": 10,
            "min_precision": 0.85, "min_confidence": 0.0, "max_age_h": 24, "captions": False, "paused_reason": ""}
RANGES = {"max_per_day": (1, 20), "gap_min": (0, 1440), "delay_min": (0, 1440), "max_age_h": (1, 168),
          "min_precision": (0.6, 0.95), "min_confidence": (0.0, 0.99)}
PROVIDERS = ("auto", "direct")              # only providers that post by themselves: the Share assistant needs you
NAMES = {"max_per_day": "posts per 24 hours", "gap_min": "minutes between posts", "delay_min": "minutes before posting",
         "max_age_h": "hours before a clip is too old", "min_precision": "required accuracy", "min_confidence": "required confidence"}
RETRY_AFTER_S = 900
MAX_ATTEMPTS = 2
EXPORT_WAIT_S = 900                         # the recorder may still be rendering this clip's 9:16 version
BREAKER_WINDOW, BREAKER_MIN, BREAKER_RATE = 10, 5, 0.3
SKIP = {"posted", "posting", "held", "failed", "expired"}      # states autopilot never touches again


# ---------------------------------------------------------------------------------------------------- settings + log
def path() -> Path:
    return settings.config_dir() / "autopilot.json"


def log_path() -> Path:
    return settings.config_dir() / "autopilot_log.jsonl"


def _clean(s: dict, strict: bool) -> dict:
    out = dict(DEFAULTS)

    def bad(msg):
        if strict:
            raise ValueError(msg)

    for k in DEFAULTS:
        if k not in s:
            continue
        v = s[k]
        try:
            if k == "mode":
                if v not in MODES:
                    raise ValueError("mode must be off, suggest or post")
            elif k == "provider":
                if v not in PROVIDERS:
                    raise ValueError("autopilot can only use a method that posts by itself (Quick connect / my connected accounts)")
            elif k == "platforms":
                if not isinstance(v, list) or any(p not in share.PLATFORMS for p in v):
                    raise ValueError("unknown platform")
                v = [p for p in dict.fromkeys(v)]
            elif k == "channels":
                if not isinstance(v, list) or any(not isinstance(c, str) or not re.fullmatch(r"[\w.-]{1,60}", c) for c in v):
                    raise ValueError("bad channel name")
                v = list(dict.fromkeys(c.lower() for c in v))
            elif k in RANGES:
                lo, hi = RANGES[k]
                try:
                    v = float(v) if isinstance(DEFAULTS[k], float) else int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{NAMES[k]}: use a number") from None
                if not lo <= v <= hi:
                    span = f"{lo:.0%} and {hi:.0%}" if isinstance(DEFAULTS[k], float) else f"{lo} and {hi}"
                    raise ValueError(f"{NAMES[k]} must be between {span}")
            elif k == "captions":
                v = bool(v)
            elif k == "paused_reason":
                v = str(v)[:300]
        except (ValueError, TypeError) as e:
            bad(str(e) if isinstance(e, ValueError) else f"bad value for {k}")
            continue                                              # a damaged file falls back to the safe default for that setting only
        out[k] = v
    return out


def load() -> dict:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return _clean(raw if isinstance(raw, dict) else {}, strict=False)


def save(patch: dict) -> dict:
    """Validate and store changes (ValueError says what is wrong). Choosing a mode yourself clears an automatic pause."""
    new = _clean({**load(), **{k: v for k, v in patch.items() if k in DEFAULTS}}, strict=True)
    if "mode" in patch and "paused_reason" not in patch:
        new["paused_reason"] = ""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(new, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return new


_log_lock = threading.Lock()


def log_event(**kw) -> None:
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        with _log_lock, open(log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(kw.pop("t", None) or time.time(), 1), **kw}, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("could not write the autopilot log: %s", e)


def read_log(limit: int | None = None) -> list[dict]:
    rows = []
    try:
        with open(log_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:] if limit else rows


# ---------------------------------------------------------------------------------------------------- helpers
def _created(m: dict) -> float:
    try:
        return datetime.fromisoformat(str(m.get("created"))).timestamp()
    except ValueError:
        return 0.0


def _handled(m: dict) -> set[str]:
    """Platforms this clip is already on (or waiting in): posted by you, by an earlier autopilot run, or opened in the Share assistant."""
    return {pl for pl, sh in (m.get("shares") or {}).items() if sh.get("status") in ("done", "draft", "opened")}


def _latest_vertical(path_: Path, m: dict) -> dict | None:
    for e in reversed(m.get("exports") or []):
        if e.get("vertical") and (path_.parent / e["file"]).is_file():
            return e
    return None


class Autopilot:
    def __init__(self, store, stop: threading.Event | None = None, clock=time.time):
        self.store, self.stop, self.clock = store, stop or threading.Event(), clock
        self._last_train = 0.0

    # ---- meta bookkeeping -----------------------------------------------------------------------------
    def _set(self, p: Path, state: str, /, **kw) -> dict:
        """Record what autopilot is doing with a clip, inside the clip's own record (the review page paints it)."""
        from .review import _write_json
        with self.store.lock:
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            m["autopilot"] = {**(m.get("autopilot") or {}), "state": state, "at": round(self.clock(), 1), **kw}
            _write_json(p, m)
            return m

    def hold(self, cid: str) -> bool:
        """'Don't post this one': never touched again by autopilot (it does not count as a Keep or Trash)."""
        p, m = self.store.find(cid)
        if not p:
            return False
        self._set(p, "held", message="stopped by you")
        log_event(clip=cid, action="held", by="you")
        return True

    def _keep_model_fresh(self, now: float) -> None:
        if taste.is_stale() and now - self._last_train > 60:
            self._last_train = now
            try:
                taste.train()
            except Exception:
                log.exception("autopilot: training failed")

    # ---- the safety net -------------------------------------------------------------------------------
    def regret(self, metas: list) -> tuple[int, int]:
        """(trashed, decided) among the autopilot's most recent posts that you have judged since."""
        mine = [(m["autopilot"].get("at", 0), m["status"]) for _, m in metas
                if (m.get("autopilot") or {}).get("state") == "posted" and m.get("status") in ("keep", "trash")]
        mine.sort(reverse=True)
        recent = mine[:BREAKER_WINDOW]
        return sum(1 for _, st in recent if st == "trash"), len(recent)

    # ---- one pass ---------------------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> list[dict]:
        """Look at every clip once; queue qualifying ones, post at most one that is due. Returns what it did (for the log and tests)."""
        now = self.clock() if now is None else now
        s = load()
        self._keep_model_fresh(now)
        if s["mode"] != "post":
            return []                                              # "suggest" needs no action: the review page marks the picks
        ready = taste.readiness(taste.model(), s["min_precision"], s["min_confidence"])
        if not ready["ok"] or not s["platforms"] or not s["channels"]:
            return []
        metas = list(self.store._metas())
        bad, decided = self.regret(metas)
        if decided >= BREAKER_MIN and bad / decided >= BREAKER_RATE:
            why = f"You trashed {bad} of its last {decided} posts that you checked, so it stopped posting. Review them, then switch it back on."
            save({"mode": "suggest", "paused_reason": why})
            log_event(action="paused", message=why)
            log.warning("autopilot paused: %s", why)
            return [{"action": "paused", "message": why}]
        prov = share.PROVIDERS.get(s["provider"])
        usable = [pl for pl in s["platforms"] if prov and prov.configured() and prov.problems(pl) is None]
        acts: list[dict] = []
        due = []
        for p, m in metas:
            ap = m.get("autopilot") or {}
            state = ap.get("state")
            if m.get("status") != "new":
                if state in ("queued", "retry"):
                    self._set(p, "cancelled", message="you decided on this clip")
                continue
            if state in SKIP or m.get("channel", "").lower() not in s["channels"]:
                continue
            todo = [pl for pl in usable if pl not in _handled(m)]
            if not todo:
                continue
            pred = taste.predict(m)
            fresh = now - _created(m) <= s["max_age_h"] * 3600
            if not pred or pred["p"] < ready["threshold"] or not fresh:
                if state in ("queued", "retry"):
                    self._set(p, "expired" if not fresh else "cancelled", message="too old" if not fresh else "no longer looks like one you would keep")
                continue
            if state not in ("queued", "retry"):
                self._set(p, "queued", p=pred["p"], post_at=round(now + s["delay_min"] * 60, 1), message="")
                log_event(t=now, clip=m["id"], action="queued", p=pred["p"], post_at=round(now + s["delay_min"] * 60, 1))
                acts.append({"action": "queued", "clip": m["id"], "p": pred["p"]})
                continue
            when = ap.get("retry_at") if state == "retry" else ap.get("post_at", 0)
            if now >= (when or 0):
                due.append((pred["p"], m.get("score", 0), p, m, todo))
        if not due:
            return acts
        posted = [e for e in read_log() if e.get("action") == "posted"]
        if sum(1 for e in posted if now - e["t"] < 86400) >= s["max_per_day"]:
            return acts
        if posted and now - max(e["t"] for e in posted) < s["gap_min"] * 60:
            return acts
        due.sort(key=lambda t: (-t[0], -t[1]))
        pr, _, p, m, todo = due[0]
        res = self._post(p, m, pr, todo, s, now)
        if res:
            acts.append(res)
        return acts

    def _post(self, p: Path, m: dict, prob: float, todo: list[str], s: dict, now: float) -> dict | None:
        cid = m["id"]
        ap = m.get("autopilot") or {}
        attempts = int(ap.get("attempts", 0)) + 1
        ex = _latest_vertical(p, m)
        try:
            if not ex:
                if getattr(self.store.cfg, "vertical", "auto") == "auto" and now - _created(m) < EXPORT_WAIT_S:
                    return None                                    # the recorder is probably still rendering the vertical version
                self.store.export(cid, float(m["suggest_start_s"]), float(m["suggest_end_s"]), True, bool(s["captions"]))
                p2, m2 = self.store.find(cid)
                ex = _latest_vertical(p2, m2) if p2 else None
                m = m2 or m
            if not ex:
                raise RuntimeError("no 9:16 export could be made")
            d = m.get("description") or self.store._fresh_description(p, m)
            texts = {pl: {"title": d.get("title", ""), "body": d.get("body", "")} for pl in todo}
            self._set(p, "posting", attempts=attempts, platforms=todo)
            log_event(t=now, clip=cid, action="attempt", platforms=todo, p=prob)      # written BEFORE anything is sent
            res = share.run(self.store, share.ShareRequest(cid, ex["file"], todo, texts, s["provider"], False, actor="autopilot"))
        except Exception as e:                                     # ShareError (validation), export failure, ...
            return self._failed(p, cid, attempts, str(e), now)
        done = {pl: r for pl, r in res.items() if r["status"] in ("done", "draft")}
        if not done:
            return self._failed(p, cid, attempts, "; ".join(f"{pl}: {r['message']}" for pl, r in res.items()), now)
        msg = "; ".join(f"{pl}: {r['status']}" + (f" ({r['message']})" if r["status"] == "failed" else "") for pl, r in res.items())
        self._set(p, "posted", p=prob, platforms=list(done), message=msg)
        log_event(t=now, clip=cid, action="posted", platforms=list(done), p=prob, message=msg)
        log.info("autopilot posted %s to %s (%.0f%% sure)", cid, ", ".join(done), prob * 100)
        return {"action": "posted", "clip": cid, "platforms": list(done), "p": prob}

    def _failed(self, p: Path, cid: str, attempts: int, msg: str, now: float) -> dict:
        final = attempts >= MAX_ATTEMPTS
        self._set(p, "failed" if final else "retry", attempts=attempts, message=msg[:500],
                  **({} if final else {"retry_at": round(now + RETRY_AFTER_S, 1)}))
        log_event(t=now, clip=cid, action="failed", attempts=attempts, message=msg[:500])
        log.warning("autopilot could not post %s (%s)%s", cid, msg, "" if final else ": will try once more later")
        return {"action": "failed", "clip": cid, "message": msg}

    # ---- the loop + what the page shows ---------------------------------------------------------------------
    def loop(self, every: float = 30.0) -> None:
        try:
            taste.import_existing(self.store.root)
        except Exception:
            log.exception("autopilot: could not read earlier decisions")
        while not self.stop.wait(every):
            try:
                self.tick()
            except Exception:
                log.exception("autopilot: error (carrying on)")

    def start(self, every: float = 30.0) -> threading.Thread:
        t = threading.Thread(target=self.loop, args=(every,), daemon=True, name="autopilot")
        t.start()
        return t


def status(store) -> dict:
    """Everything the Autopilot window shows."""
    s = load()
    m = taste.model()
    ready = taste.readiness(m, s["min_precision"], s["min_confidence"])
    now = time.time()
    metas = list(store._metas())
    queue = []
    for _, c in metas:
        ap = c.get("autopilot") or {}
        if ap.get("state") in ("queued", "retry") and c.get("status") == "new":
            queue.append({"id": c["id"], "channel": c.get("channel"), "p": ap.get("p"), "at": ap.get("post_at") or ap.get("retry_at"),
                          "state": ap["state"], "title": (c.get("description") or {}).get("title", "")})
    queue.sort(key=lambda q: q["at"] or 0)
    posted = [e for e in read_log() if e.get("action") == "posted"]
    bad, decided = Autopilot(store).regret(metas)
    titles = {c["id"]: (c.get("description") or {}).get("title", "") for _, c in metas}
    recent = [{**e, "title": titles.get(e.get("clip"), "")} for e in read_log(12)][::-1]
    return {"settings": s, "counts": taste.counts(), "need": {"early": taste.EARLY_LABELS, "ready": taste.MIN_LABELS, "per_class": taste.MIN_PER_CLASS},
            "model": None if not m else {k: m.get(k) for k in ("state", "n", "keeps", "trashes", "auc", "base_rate", "trained_at", "reason")},
            "curve": taste.curve(m["oof"]) if m and m.get("oof") else [], "ready": ready,
            "posted_24h": sum(1 for e in posted if now - e["t"] < 86400), "regret": {"trashed": bad, "decided": decided},
            "queue": queue, "recent": recent,
            "channels": sorted({str(c.get("channel", "")).lower() for _, c in metas if c.get("channel")}),
            "providers": share.providers_info(), "platforms": share.PLATFORMS}
