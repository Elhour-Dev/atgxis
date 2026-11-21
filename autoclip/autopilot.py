"""Autopilot: posts the clips the taste model is confident you would keep, without you pressing anything.

Posting to public accounts cannot be undone, so every rule below is enforced here in code (not only in the page):

  * OFF until you switch it on. "Suggest" only marks picks in the review page; "Post" is the only mode that publishes.
  * It acts only when the model has PROVEN itself on clips it never saw while learning (taste.readiness), and only above that confidence.
  * Only channels you ticked (publishing someone else's stream needs their permission), only platforms you ticked, only through a
    connected account, only a 9:16 export of the suggested trim. Exception: streamers of a LIVE paid campaign you entered yourself
    (campaigns.py) count as ticked for posting (never for auto-trash), only on that campaign's platforms you ticked, only with an
    export made for the campaign, and only while the campaign's pre-post check passes.
  * A veto window: a pick waits `delay_min` minutes, marked in the review page, before it goes out. Trash it, or press "Don't post", to stop it.
  * A cap per 24 hours and a minimum gap between posts. Clips older than `max_age_h` are never posted.
  * Never twice: a platform that already has this clip is skipped, and a post that was started but never reported back (crash, power
    cut) is NOT retried by itself, because it may have gone out.
  * Circuit breaker: if you trash too many of its recent posts it switches itself to "Suggest" and says why.
  * Score rules: it only ever posts clips scoring at least `min_score` (70). Clips scoring under `trash_below` (40) from the streamers you
    ticked are moved to Trash (not deleted: Undo brings them back, "Delete trashed files" frees the space). Clips in between are left
    for you. These trashes are the Autopilot's, never counted as your taste or style.
  * Everything it does is written to autopilot_log.jsonl next to your settings.
  * Your style (style.py, switch: use_style): trim, captions, title and description the way you do them, only your usual platforms
    (always among the ticked ones), and clips your pick rules say you would not post are skipped. Style can only shape a post or skip
    it: none of the limits above are read from it.

It runs inside `python -m autoclip run` (a thread), or on its own with `python -m autoclip autopilot run`.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import campaigns, settings, share, style, taste

log = logging.getLogger("autoclip.autopilot")

MODES = ("off", "suggest", "post")
DEFAULTS = {"mode": "off", "platforms": [], "provider": "auto", "channels": [], "max_per_day": 3, "gap_min": 60, "delay_min": 10,
            "min_precision": 0.85, "min_confidence": 0.0, "max_age_h": 24, "captions": False, "paused_reason": "",
            "use_style": True, "min_score": 70, "trash_below": 40}
RANGES = {"min_score": (0, 100), "trash_below": (0, 100), "max_per_day": (1, 20), "gap_min": (0, 1440), "delay_min": (0, 1440), "max_age_h": (1, 168),
          "min_precision": (0.6, 0.95), "min_confidence": (0.0, 0.99)}
PROVIDERS = ("auto", "direct")              # only providers that post by themselves: the Share assistant needs you
NAMES = {"min_score": "lowest score it may post", "trash_below": "score under which it trashes a clip", "max_per_day": "posts per 24 hours", "gap_min": "minutes between posts", "delay_min": "minutes before posting",
         "max_age_h": "hours before a clip is too old", "min_precision": "required accuracy", "min_confidence": "required confidence"}
RETRY_AFTER_S = 900
MAX_ATTEMPTS = 2
EXPORT_WAIT_S = 900                         # the recorder may still be rendering this clip's 9:16 version
BREAKER_WINDOW, BREAKER_MIN, BREAKER_RATE = 10, 5, 0.3
SKIP = {"posted", "posting", "held", "failed", "expired", "trashed", "restored"}      # states autopilot never touches again ("skipped" is re-checked: rules change)
MATCH_S = 0.05                              # an existing 9:16 export is reused only if its trim is this close to the planned one


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
                if isinstance(v, bool):
                    raise ValueError(f"{NAMES[k]}: use a number")
                try:
                    v = float(v) if isinstance(DEFAULTS[k], float) else int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{NAMES[k]}: use a number") from None
                if not lo <= v <= hi:
                    span = f"{lo:.0%} and {hi:.0%}" if isinstance(DEFAULTS[k], float) else f"{lo} and {hi}"
                    raise ValueError(f"{NAMES[k]} must be between {span}")
            elif k in ("captions", "use_style"):
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true or false")
            elif k == "paused_reason":
                v = str(v)[:300]
        except (ValueError, TypeError) as e:
            bad(str(e) if isinstance(e, ValueError) else f"bad value for {k}")
            continue                                              # a damaged file falls back to the safe default for that setting only
        out[k] = v
    if not strict and out["trash_below"] > out["min_score"]:      # a hand-edited file: never trash clips it would otherwise post
        out["trash_below"] = out["min_score"]
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
    if new["trash_below"] > new["min_score"]:
        raise ValueError("the trash score must not be higher than the lowest score it may post")
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


def _latest_vertical(path_: Path, m: dict, want: tuple[float, float, bool] | None = None, camp: dict | None = None) -> dict | None:
    """The newest 9:16 export on disk; with `want` = (start, end, captions) only one that matches that plan; with `camp` only one
    made for that paid campaign (disclosure burned in, must_show kept visible)."""
    for e in reversed(m.get("exports") or []):
        if e.get("vertical") and (path_.parent / e["file"]).is_file() and (camp is None or campaigns.export_ready(e, camp)):
            if want is None:
                return e
            try:
                if abs(float(e["start"]) - want[0]) <= MATCH_S and abs(float(e["end"]) - want[1]) <= MATCH_S and (
                        want[2] is None or bool(e.get("captions")) == want[2]):
                    return e
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _touched(m: dict) -> bool:
    """You already did something with this clip (exported it, posted it, edited its text): never auto-trash it."""
    d = m.get("description") or {}
    return bool(m.get("shares")) or any(style.export_by(e, m) == "you" for e in m.get("exports") or []) \
        or style.title_source(d) != "auto" or style.body_edited(d)


def clip_score(m: dict) -> float | None:
    """The clip's score as a number, or None when it is missing or broken (such a clip is never trashed or posted by score)."""
    v = m.get("score")
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if x != x or abs(x) == float("inf"):
        return None
    return float(math.floor(x + 0.5))                           # the score the card shows: 39.6 is a 40, as on the page


def _can_caption() -> bool:
    """Is speech-to-text installed? Without it a captions export can only fail: skip that wasted encode."""
    import importlib.util
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def _words(p: Path, m: dict) -> list:
    try:
        from .captions import cached_words
        return cached_words(p.parent / m["file"]) if m.get("file") else []
    except Exception:
        return []


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

    def _auto_trash(self, p: Path, score: float, limit: int, now: float) -> bool:
        """Move a low-scoring clip to Trash (recoverable). Written as the Autopilot's, so it never teaches your taste."""
        from .review import _write_json
        msg = f"score {score:.0f} is under {limit}"
        with self.store.lock:
            try:
                before = p.stat().st_mtime_ns
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            if m.get("status") != "new" or _touched(m):
                return False
            try:
                if p.stat().st_mtime_ns != before:                # the review page (maybe another process) just wrote it: leave it
                    return False
            except OSError:
                return False
            m["status"] = "trash"
            m["autopilot"] = {**(m.get("autopilot") or {}), "state": "trashed", "at": round(now, 1), "message": msg}
            _write_json(p, m)
        log_event(t=now, clip=m["id"], action="trashed", score=round(score), message=msg)
        return True

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
        """(trashed, decided) among the autopilot's most recent posts that you have judged since. Only Keep and Trash are judgements:
        "done" (posted, files deleted for space) says nothing about quality, so it is neither regret nor approval."""
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
        camps = campaigns.load()                                   # streamers of a LIVE paid campaign count as permitted
        if not ready["ok"] or not s["platforms"] or not (s["channels"] or camps):
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
            if state in SKIP:
                continue
            ticked = m.get("channel", "").lower() in s["channels"]
            camp = campaigns.for_channel(m.get("channel"), now, camps) if camps else None
            if not ticked and not camp:                            # e.g. its campaign ended, hit its cap or ran out of budget
                if state in ("queued", "retry") and camps and campaigns.for_channel(m.get("channel"), now, camps, live=False):
                    self._set(p, "cancelled", message="this streamer is no longer ticked or in a live campaign")
                continue
            score = clip_score(m)
            age = now - _created(m)
            if score is None:                                      # no usable score: never trashed, never posted, left for you
                continue
            if ticked and s["trash_below"] and score < s["trash_below"] and not _touched(m):     # only streamers you ticked are auto-trashed
                # like a post, a trash waits out your veto time, and old backlog is left alone
                if s["delay_min"] * 60 <= age <= s["max_age_h"] * 3600 and self._auto_trash(p, score, s["trash_below"], now):
                    acts.append({"action": "trashed", "clip": m["id"], "score": round(score)})
                elif state in ("queued", "retry"):
                    self._set(p, "cancelled", message=f"score {score:.0f} is under {s['trash_below']}")
                continue
            if score < s["min_score"]:                             # left for you to check: never posted
                if state in ("queued", "retry"):
                    self._set(p, "cancelled", message=f"score {score:.0f} is under {s['min_score']}")
                continue
            todo = [pl for pl in usable if pl not in _handled(m)]
            if camp:                                               # only platforms the campaign pays for (and you ticked)
                todo = [pl for pl in todo if pl in camp["platforms"]]
                if not todo and state in ("queued", "retry"):
                    self._set(p, "cancelled", message="none of the campaign's platforms is ticked or connected")
            if not todo:
                continue
            pred = taste.predict(m)
            fresh = now - _created(m) <= s["max_age_h"] * 3600
            if not pred or pred["p"] < ready["threshold"] or not fresh:
                if state in ("queued", "retry"):
                    self._set(p, "expired" if not fresh else "cancelled", message="too old" if not fresh else "no longer looks like one you would keep")
                continue
            if s["use_style"]:                                     # your style may only narrow what happens, never widen it
                rules = style.load(m.get("channel"))
                todo = style.platforms_for(rules, todo)
                if not todo:                                       # not a judgement of the clip: tick that platform and it comes back
                    if state in ("queued", "retry"):
                        self._set(p, "cancelled", message="none of your usual platforms is ticked")
                    continue
                why = style.skip_reason(m, rules, _words(p, m)) if state not in ("queued", "retry") else None
                if why:
                    if state not in ("queued", "retry", "skipped"):
                        self._set(p, "skipped", message=why, rules=rules["meta"]["version"])
                        log_event(t=now, clip=m["id"], action="skipped", message=why, rules=rules["meta"]["version"])
                        acts.append({"action": "skipped", "clip": m["id"], "message": why})
                    continue
            why = self._campaign_skip(p, m, camp) if camp else None
            if why:
                if state in ("queued", "retry"):
                    self._set(p, "cancelled", message=why)
                elif state != "skipped":
                    self._set(p, "skipped", message=why)
                    log_event(t=now, clip=m["id"], action="skipped", message=why, campaign=camp["id"])
                    acts.append({"action": "skipped", "clip": m["id"], "message": why})
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

    @staticmethod
    def _campaign_skip(p: Path, m: dict, camp: dict) -> str | None:
        """Why a campaign clip cannot pass the campaign's rules whatever the trim (length, speech), or None."""
        try:
            a, b, dur = float(m["suggest_start_s"]), float(m["suggest_end_s"]), float(m.get("duration_s") or m["suggest_end_s"])
        except (KeyError, TypeError, ValueError):
            return "the clip has no usable trim"
        fit = campaigns.fit_trim(a, b, dur, camp)
        if not fit:
            return f"the draft is shorter than the {camp['rules']['min_len_s']:g} s that '{camp['name']}' asks for"
        if camp["rules"]["need_speech"]:
            words = _words(p, m)
            if words:                                              # unknown (no speech-to-text): left to the card's warning
                from .captions import words_in_window
                if not words_in_window(words, *fit):
                    return f"'{camp['name']}' needs speech, and nobody talks in this clip"
        return None

    def _post(self, p: Path, m: dict, prob: float, todo: list[str], s: dict, now: float) -> dict | None:
        cid = m["id"]
        ap = m.get("autopilot") or {}
        attempts = int(ap.get("attempts", 0)) + 1
        rules = style.load(m.get("channel")) if s["use_style"] else None
        ver = rules["meta"]["version"] if rules else None
        try:
            words = _words(p, m) if rules else []
            if rules:
                start, end = style.trim_for(m, rules)
                from .captions import words_in_window
                speech = (len(words_in_window(words, start, end)) >= rules["captions"]["min_words"]) if words else None
                caps = style.captions_for(rules, speech, bool(s["captions"])) and _can_caption()
                want = (start, end, caps if rules["captions"]["mode"] else None)        # no captions rule: any existing 9:16 is fine
            else:
                start, end, caps, want = float(m["suggest_start_s"]), float(m["suggest_end_s"]), bool(s["captions"]), None
            camp = campaigns.for_channel(m.get("channel"), now)
            if camp:                                               # the campaign's length rule, and only an export made for it
                fit = campaigns.fit_trim(start, end, float(m.get("duration_s") or end), camp)
                if not fit:
                    raise RuntimeError("the draft is shorter than the campaign's minimum length")
                start, end = fit
                want = (start, end, want[2] if want else None)
            yours = [e for e in m.get("exports") or [] if e.get("vertical") and style.export_by(e, m) == "you" and (p.parent / str(e.get("file"))).is_file()]
            if camp:
                yours = [e for e in yours if campaigns.export_ready(e, camp) and campaigns.length_ok(e, camp)]
                ex = yours[-1] if yours else _latest_vertical(p, m, want, camp)
            else:
                ex = yours[-1] if rules and yours else _latest_vertical(p, m, want)        # your own 9:16 export beats any rule
            if not ex:
                if not _latest_vertical(p, m) and getattr(self.store.cfg, "vertical", "auto") == "auto" and now - _created(m) < EXPORT_WAIT_S:
                    return None                                    # the recorder is probably still rendering the vertical version
                try:
                    self.store.export(cid, start, end, True, caps, by="autopilot", hook="auto")
                except Exception as e:                             # no speech after all / no speech-to-text: post it without captions
                    if not caps or not (isinstance(e, ValueError) or type(e).__name__ == "CaptionsUnavailable"):
                        raise
                    caps = False
                    self.store.export(cid, start, end, True, False, by="autopilot", hook="auto")
                p2, m2 = self.store.find(cid)
                ex = _latest_vertical(p2, m2, (start, end, caps if want[2] is not None else None) if want else None, camp) if p2 else None
                m = m2 or m
            if not ex:
                raise RuntimeError("no 9:16 export could be made")
            d = m.get("description") or self.store._fresh_description(p, m)
            title, body = d.get("title", ""), d.get("body", "")
            if rules and (style.title_source(d) == "auto" or not style.body_edited(d)):
                from .describe import suggest
                mine = suggest(m, self.store.cfg, words or None, (start, end), rules=rules)
                title = title if style.title_source(d) != "auto" else mine["title"]       # never replace text you wrote
                body = body if style.body_edited(d) else mine["body"]
            ig = share.instagram_problem(ex) if "instagram" in todo else None
            if ig:                                                 # Instagram only gets original clips: post to the others
                todo = [pl for pl in todo if pl != "instagram"]
                log_event(t=now, clip=cid, action="skip_platform", platforms=["instagram"], message="Instagram Reels " + ig)
                if not todo:
                    return self._failed(p, cid, MAX_ATTEMPTS, "Instagram Reels " + ig, now)
            texts = {pl: {"title": title, "body": body} for pl in todo}
            self._set(p, "posting", attempts=attempts, platforms=todo, **({"rules": ver} if ver else {}))
            log_event(t=now, clip=cid, action="attempt", platforms=todo, p=prob, title=title[:200], **({"rules": ver} if ver else {}))      # written BEFORE anything is sent
            res = share.run(self.store, share.ShareRequest(cid, ex["file"], todo, texts, s["provider"], False, actor="autopilot"))
        except Exception as e:                                     # ShareError (validation), export failure, ...
            return self._failed(p, cid, attempts, str(e), now)
        done = {pl: r for pl, r in res.items() if r["status"] in ("done", "draft")}
        if not done:
            return self._failed(p, cid, attempts, "; ".join(f"{pl}: {r['message']}" for pl, r in res.items()), now)
        msg = "; ".join(f"{pl}: {r['status']}" + (f" ({r['message']})" if r["status"] == "failed" else "") for pl, r in res.items())
        self._set(p, "posted", p=prob, platforms=list(done), message=msg, **({"rules": ver} if ver else {}))
        log_event(t=now, clip=cid, action="posted", platforms=list(done), p=prob, message=msg, **({"rules": ver} if ver else {}))
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


def _style_status() -> dict | None:
    try:
        return style.status()
    except Exception:
        return None


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
            "queue": queue, "recent": recent, "style": _style_status(),
            "channels": sorted({str(c.get("channel", "")).lower() for _, c in metas if c.get("channel")}),
            "providers": share.providers_info(), "platforms": share.PLATFORMS}
