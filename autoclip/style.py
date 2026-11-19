"""Your clip style: how YOU title, describe, trim, caption and post clips, learned continuously, so the Autopilot does it your way.

Three files in <settings folder>/style/:
  * examples.jsonl - one line per decision or edit (Keep, Trash, Done, Undo, title/description edit, export, post, "mark as posted").
    Each line is a full snapshot taken at that moment, so "Delete trashed files" never loses anything.
  * learned.json   - simple rules counted from your last 50 examples (per streamer, plus "default" for all of them). Rewritten in the
    background after every new line.
  * taught.json    - smarter rules written by the autoclip-teacher skill (title templates, hook lines, skip rules). Only TAUGHT_FIELDS.

load(channel) merges DEFAULTS <- learned <- taught. A broken file or field is ignored, never fatal.

Which examples count (same as taste.py):
  * good = Keep, Done or posted by you; bad = Trash of a clip you never posted; Undo withdraws the decision;
  * an Autopilot post you trash later = bad + rule_fail;
  * NEVER the Autopilot's own untouched output: its titles, trims and captions are logged as by "autopilot" and not learned from.

Rules can only shape what Autopilot does (texts, trim, captions) or make it skip a clip / a platform. They never loosen a limit, add a
platform or streamer you did not tick, or lower a threshold: those are enforced in autopilot.py and are not read from here.
"""
from __future__ import annotations

import copy
import json
import os
import re
import statistics
import threading
import time
import unicodedata
import zlib
from collections import Counter
from pathlib import Path

from . import settings

LAST_N = 50                 # examples per channel the learner looks at: style drifts
MIN_EXAMPLES = 5            # a field changes only with at least this many examples ...
AGREE = 0.7                 # ... and at least this share of them agreeing
TRIM_TOL_S = 1.5            # two trim shifts "agree" when they are this close to the median
LEN_TOL = 0.25              # two lengths "agree" when they are within 25% of the median
MIN_CLIP_S = 3.0            # a style trim never makes a clip shorter than this
FAMILIES = ("taught", "speech", "chat", "category", "loud", "big", "hook")
PLACEHOLDERS = ("streamer", "phrase", "quote", "count")
CASES = ("upper", "lower", "title", "sentence")
CAPTION_MODES = ("speech", "always", "never")

DEFAULTS = {
    "title": {"source_order": list(FAMILIES), "templates": [], "case": None, "max_words": 0, "emoji_end": None},
    "body": {"hooks": [], "credit_line": None, "hashtags_fixed": [], "hashtags_max": 9, "channel_tag": True},
    "captions": {"mode": None, "min_words": 3},
    "trim": {"start_shift_s": 0.0, "end_shift_s": 0.0, "max_len_s": 0.0},
    "platforms": [],
    "pick": {"skip_terms": [], "need_speech": False, "min_len_s": 0.0, "max_len_s": 0.0},
}
TAUGHT_FIELDS = ("title.templates", "title.source_order", "body.hooks", "pick.skip_terms", "pick.need_speech")
LEARNED_FIELDS = ("title.case", "title.max_words", "title.emoji_end", "body.hashtags_fixed", "body.hashtags_max", "body.credit_line",
                  "captions.mode", "trim.start_shift_s", "trim.end_shift_s", "trim.max_len_s", "platforms", "pick.min_len_s",
                  "pick.max_len_s")
_TAG = re.compile(r"#(\w+)", re.UNICODE)
_local = threading.local()


# ---------------------------------------------------------------------------------------------------- where things live
def folder() -> Path:
    return getattr(_local, "base", None) or settings.config_dir() / "style"


def examples_path() -> Path:
    return folder() / "examples.jsonl"


def learned_path() -> Path:
    return folder() / "learned.json"


def taught_path() -> Path:
    return folder() / "taught.json"


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                                   # Windows: someone is reading it right now
            if i == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------- the snapshot
def _num(v) -> float | None:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def export_by(e: dict, m: dict | None = None) -> str:
    """Who made an export: "you", "autopilot" or "recorder" (the automatic 9:16 render; older records have no "by", then the file
    name and the clip's posts tell)."""
    by = e.get("by")
    if by in ("you", "autopilot", "recorder"):
        return by
    if m and any(isinstance(sh, dict) and sh.get("actor") == "autopilot" and sh.get("file") == e.get("file") for sh in (m.get("shares") or {}).values()):
        return "autopilot"
    return "recorder" if re.search(r"_vertical(_\d+)?\.mp4$", str(e.get("file", ""))) else "you"


def title_source(d: dict) -> str:
    """picked / typed / auto. Older records have no marker: a title that is not one of the suggestions is yours."""
    if "title_src" in d:
        return d["title_src"] if d["title_src"] in ("picked", "typed") else "auto"
    t = d.get("title")
    return "auto" if not t or t in (d.get("alts") or []) + (d.get("seen") or []) else "typed"


def body_edited(d: dict) -> bool:
    """Older records have no marker: a body that differs from the automatic one (or has no automatic one) is yours."""
    if "body_edited" in d:
        return bool(d["body_edited"])
    return bool(d.get("body")) and d.get("body") != d.get("auto_body")


def _seen_edit(d: dict) -> bool:
    """For LEARNING: the body counts as yours only with proof (the marker, or a recorded automatic text it differs from).
    body_edited() is the cautious version used to PROTECT your text, where "unknown" must mean "yours"."""
    if "body_edited" in d:
        return bool(d["body_edited"])
    return bool(d.get("auto_body")) and d.get("body") != d.get("auto_body")


def _had_speech(m: dict, base_dir: Path | None) -> bool | None:
    if not base_dir or not m.get("file"):
        return None
    try:
        from .captions import cached_words, words_in_window
        words = cached_words(Path(base_dir) / m["file"])
    except Exception:
        return None
    if not words:
        return None                                           # never transcribed: we do not know
    lo, hi = _num(m.get("suggest_start_s")) or 0.0, _num(m.get("suggest_end_s")) or _num(m.get("duration_s")) or 0.0
    return len(words_in_window(words, lo, hi)) >= DEFAULTS["captions"]["min_words"]


def snapshot(m: dict, base_dir: Path | None = None) -> dict:
    """Everything the learner (and the teacher skill) needs about a clip, taken now. Fields that are not known are None."""
    from .describe import chat_phrase, classify
    sp = m.get("spike") or {}
    top = [[str(t), int(c)] for t, c in (sp.get("top") or [])[:3] if isinstance(c, (int, float))] if isinstance(sp.get("top"), list) else []
    d = m.get("description") or {}
    body = str(d.get("body", ""))
    mine = [e for e in (m.get("exports") or []) if isinstance(e, dict) and export_by(e, m) == "you"]
    ex = mine[-1] if mine else None
    trim, len_s, caps = None, None, None
    if ex and _num(ex.get("start")) is not None and _num(ex.get("end")) is not None:
        s, e = float(ex["start"]), float(ex["end"])
        trim = [round(s - (_num(m.get("suggest_start_s")) or 0.0), 2), round(e - (_num(m.get("suggest_end_s")) or e), 2)]
        len_s, caps = round(e - s, 2), bool(ex.get("captions"))
    sug_len = None
    if _num(m.get("suggest_end_s")) is not None:
        sug_len = round(float(m["suggest_end_s"]) - (_num(m.get("suggest_start_s")) or 0.0), 2)
        len_s = len_s if len_s is not None else sug_len
    shares = m.get("shares") or {}
    plats = sorted(pl for pl, sh in shares.items() if isinstance(sh, dict) and sh.get("actor", "you") == "you"
                   and sh.get("status") in ("done", "draft"))
    try:
        phrase = chat_phrase(top)[0]
        cat = classify(top)
    except Exception:
        phrase, cat = None, "default"
    return {"id": m.get("id"), "channel": str(m.get("channel", "")).lower(), "cat": cat, "chat_phrase": phrase, "top3": top,
            "title": str(d.get("title", "")), "title_src": title_source(d), "body": body, "body_edited": _seen_edit(d),
            "hashtags": list(dict.fromkeys(_TAG.findall(body))), "captions": caps, "had_speech": _had_speech(m, base_dir),
            "trim_shift": trim, "len_s": len_s, "sug_len_s": sug_len, "platforms": plats}


# ---------------------------------------------------------------------------------------------------- the example log
_log_lock = threading.Lock()


def record(m: dict, event: str, verdict: str = "none", by: str = "you", base_dir: Path | None = None, rule_fail: bool = False,
           when: float | None = None, learn: bool = True) -> None:
    """Append one line. Never raises: pressing Keep must work even if the log cannot be written."""
    try:
        if not m or not m.get("id"):
            return
        ap = m.get("autopilot") or {}
        ver = ap.get("rules") or load(m.get("channel"))["meta"]["version"]          # the rules in force when you decided
        line = {"t": round(when or time.time(), 1), "ev": event, **snapshot(m, base_dir), "verdict": verdict if verdict in ("good", "bad") else "none",
                "by": by if by in ("you", "autopilot") else "you", "rules_ver": ver}
        if rule_fail:
            line["rule_fail"] = True
        folder().mkdir(parents=True, exist_ok=True)
        with _log_lock, open(examples_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"could not save this example of your style: {e}")
        return
    if learn:
        retrain_soon()


def verdict_for(status: str, m: dict) -> tuple[str, bool]:
    """(verdict, rule_fail) for a Keep / Trash / Done / Undo of this clip, following the rules in the module docstring."""
    from .taste import posted_by_you
    mine = posted_by_you(m)
    by_ap = (m.get("autopilot") or {}).get("state") == "posted"
    if status == "keep":
        return "good", False
    if status == "done":
        return ("good" if mine else "none"), False
    if status == "trash":
        if mine:
            return "none", False                                  # trashed to free space after you posted it: not a bad clip
        return "bad", by_ap
    return "none", False                                          # "new" = Undo


def read_examples() -> list[dict]:
    rows = []
    try:
        with open(examples_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("id"):
                    rows.append(r)
    except OSError:
        return []
    return rows


def examples(rows: list[dict] | None = None) -> list[dict]:
    """One example per clip that YOU judged (good or bad), newest last. Later lines win, Undo withdraws, a None field never wipes a known one."""
    by_id: dict[str, dict] = {}
    for r in rows if rows is not None else read_examples():
        cur = by_id.setdefault(r["id"], {"verdict": "none"})
        for k, v in r.items():
            if k == "verdict":
                continue
            if v is not None or k not in cur:
                cur[k] = v
        if r.get("by", "you") != "you":
            continue                                               # the autopilot's own lines never set a verdict
        only_marks = cur["verdict"] == "good" and set(cur.get("vevs") or []) == {"mark"} and not r.get("platforms")
        if r.get("ev") == "undo" or (r.get("ev") == "mark_reset" and only_marks):
            cur["verdict"] = "none"                                # Undo, or the only "mark as posted" taken back
            cur.pop("rule_fail", None)
            cur["vevs"] = []
        elif r.get("verdict") in ("good", "bad"):
            if r["verdict"] != cur["verdict"]:
                cur["vevs"] = []
            cur["verdict"] = r["verdict"]
            cur["vt"] = r.get("t", 0)
            cur["vevs"] = [*(cur.get("vevs") or []), r.get("ev")]
    out = [r for r in by_id.values() if r["verdict"] in ("good", "bad")]
    return sorted(out, key=lambda r: r.get("vt", 0))


# ---------------------------------------------------------------------------------------------------- learning
def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) == "So" or ch in "️‍"


def ends_with_emoji(t: str) -> bool:
    t = t.rstrip()
    return bool(t) and _is_emoji(t[-1])


def case_of(t: str) -> str | None:
    words = [w for w in re.findall(r"[^\W\d_]+", t) if w]
    if not words:
        return None
    if all(w.isupper() for w in words):
        return "upper"
    if all(w.islower() for w in words):
        return "lower"
    if all(w[0].isupper() for w in words):
        return "title"
    if words[0][0].isupper() and all(w[0].islower() or w.isupper() for w in words[1:]):
        return "sentence"
    return None


def _vote(values: list):
    """(value, ok): the most common value and whether enough examples agree on it."""
    vals = [v for v in values if v is not None]
    if len(vals) < MIN_EXAMPLES:
        return None, False
    v, n = Counter(json.dumps(x, sort_keys=True) for x in vals).most_common(1)[0]
    return json.loads(v), n / len(vals) >= AGREE


def _agree_num(values: list[float], tol_abs: float = 0.0, tol_rel: float = 0.0) -> float | None:
    """The median, if at least AGREE of the values are close to it (|x - median| <= tol_abs + tol_rel * |median|)."""
    vals = [float(v) for v in values if _num(v) is not None]
    if len(vals) < MIN_EXAMPLES:
        return None
    med = statistics.median(vals)
    close = sum(1 for v in vals if abs(v - med) <= tol_abs + tol_rel * abs(med))
    return med if close / len(vals) >= AGREE else None


def _limit(values: list[float], upper: bool) -> float | None:
    """A length limit: only when the values agree (within LEN_TOL of the median); then the largest (or smallest) of the agreeing ones."""
    vals = [float(v) for v in values if _num(v) is not None and float(v) > 0]
    med = _agree_num(vals, tol_rel=LEN_TOL)
    if med is None:
        return None
    close = [v for v in vals if abs(v - med) <= LEN_TOL * med]
    return max(close) if upper else min(close)


def learn_fields(rows: list[dict]) -> dict:
    """{dotted field: value} for every field the examples settle. Fields they do not settle are left out (the caller keeps the old value)."""
    good = [r for r in rows if r["verdict"] == "good"]
    out: dict = {}
    titles = [r["title"] for r in good if r.get("title_src") in ("picked", "typed") and r.get("title")]
    if len(titles) >= MIN_EXAMPLES:
        v, ok = _vote([case_of(t) or "mixed" for t in titles])
        if ok and v in CASES:
            out["title.case"] = v
        mw = _limit([len(t.split()) for t in titles], upper=True)
        if mw:
            out["title.max_words"] = int(mw)
        v, ok = _vote([ends_with_emoji(t) for t in titles])
        if ok:
            out["title.emoji_end"] = v
    bodies = [r for r in good if r.get("body_edited")]
    if len(bodies) >= MIN_EXAMPLES:
        tags = Counter(t.lower() for r in bodies for t in dict.fromkeys(x.lower() for x in r.get("hashtags") or []))
        chans = {r.get("channel") for r in bodies}
        fixed = sorted(t for t, n in tags.items() if n / len(bodies) >= AGREE and t not in chans)
        if fixed:
            out["body.hashtags_fixed"] = fixed
        hm = _limit([len(r.get("hashtags") or []) for r in bodies], upper=True)
        if hm:
            out["body.hashtags_max"] = int(hm)
        v, ok = _vote(["http" in str(r.get("body", "")) for r in bodies])
        if ok:
            out["body.credit_line"] = v
    exp = [r for r in good if r.get("captions") is not None]
    if len(exp) >= MIN_EXAMPLES:
        n = len(exp)
        score = {"always": sum(1 for r in exp if r["captions"]) / n, "never": sum(1 for r in exp if not r["captions"]) / n}
        known = [r for r in exp if r.get("had_speech") is not None]
        if len(known) >= MIN_EXAMPLES:
            score["speech"] = sum(1 for r in known if bool(r["captions"]) == bool(r["had_speech"])) / len(known)
        best = max(score.items(), key=lambda kv: (kv[1], kv[0] == "speech"))
        if best[1] >= AGREE:
            out["captions.mode"] = best[0]
    shifts = [r["trim_shift"] for r in good if isinstance(r.get("trim_shift"), list) and len(r["trim_shift"]) == 2]
    for i, key in enumerate(("trim.start_shift_s", "trim.end_shift_s")):
        v = _agree_num([s[i] for s in shifts], tol_abs=TRIM_TOL_S)
        if v is not None:
            out[key] = round(v, 2)
    trimmed = [r["len_s"] for r in good if isinstance(r.get("trim_shift"), list)]
    ml = _limit(trimmed, upper=True)
    if ml:
        out["trim.max_len_s"] = round(ml, 2)
    posted = [r for r in good if r.get("platforms")]
    if len(posted) >= MIN_EXAMPLES:
        cnt = Counter(pl for r in posted for pl in r["platforms"])
        pls = sorted(pl for pl, n in cnt.items() if n / len(posted) >= AGREE)
        if pls:
            out["platforms"] = pls
    lens = [r.get("sug_len_s") for r in good]                    # compared with the suggestion, so learned from the suggestion
    lo, hi = _limit(lens, upper=False), _limit(lens, upper=True)
    if lo and hi:
        out["pick.min_len_s"], out["pick.max_len_s"] = round(lo * 0.5, 2), round(hi * 1.5, 2)    # wide slack: only clearly odd lengths are skipped
    return out


def _get(d: dict, dotted: str):
    a, _, b = dotted.partition(".")
    return d.get(a) if not b else (d.get(a) or {}).get(b)


def _put(d: dict, dotted: str, v) -> None:
    a, _, b = dotted.partition(".")
    if b:
        d.setdefault(a, {})[b] = v
    else:
        d[a] = v


def learn(rows: list[dict] | None = None) -> dict:
    """Rewrite learned.json from the examples. Per channel (+ "default" for all): a field changes only if the examples settle it."""
    ex = examples(rows)
    old = _read_json(learned_path())
    old = old if isinstance(old, dict) else {}
    groups = {"default": ex[-LAST_N:]}
    for r in ex:
        if r.get("channel"):
            groups.setdefault(r["channel"], [])
    for ch in list(groups):
        if ch != "default":
            groups[ch] = [r for r in ex if r.get("channel") == ch][-LAST_N:]
    new: dict = {}
    for ch, rows_ in groups.items():
        prev = old.get(ch) if isinstance(old.get(ch), dict) else {}
        sec: dict = {}
        found = learn_fields(rows_)
        for f in LEARNED_FIELDS:
            v = found.get(f, _get(prev, f))
            if v is not None:
                _put(sec, f, v)
        sec["examples"] = len(rows_)
        new[ch] = sec
    old_meta = old.get("meta") if isinstance(old.get("meta"), dict) else {}

    def rules_only(d):
        return {k: {f: v for f, v in s.items() if f != "examples"} for k, s in d.items() if k != "meta" and isinstance(s, dict)}

    changed = rules_only(new) != rules_only(old)
    try:
        ver = int(old_meta.get("version") or 0) + (1 if changed else 0)
    except (TypeError, ValueError):
        ver = 1
    now = round(time.time(), 1)
    new["meta"] = {"version": ver, "examples": len(ex), "updated": now if changed or not old_meta.get("updated") else old_meta["updated"]}
    _write_atomic(learned_path(), new)
    return new


RETRAIN_DELAY_S = 1.0
_sched = threading.Lock()
_dirty: set[Path] = set()
_running: set[Path] = set()


def retrain_soon() -> None:
    """Relearn in the background. Quick clicks share one run, and a click during a run is never lost (same pattern as taste.retrain_soon)."""
    base = folder()
    with _sched:
        _dirty.add(base)
        if base in _running:
            return
        _running.add(base)
    threading.Thread(target=_retrain_worker, args=(base,), daemon=True, name="style-learn").start()


def _retrain_worker(base: Path) -> None:
    _local.base = base
    while True:
        time.sleep(RETRAIN_DELAY_S)
        with _sched:
            if base not in _dirty:
                _running.discard(base)
                return
            _dirty.discard(base)
        try:
            learn()
        except Exception as e:                                      # learning must never take the review page down
            print(f"style learning failed: {e}")


# ---------------------------------------------------------------------------------------------------- validation + merge
def _clean_str_list(v, max_items: int = 50, max_len: int = 200) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("must be a list of texts")
    return [x.strip()[:max_len] for x in v if x.strip()][:max_items]


def _template_ok(t: str) -> bool:
    names = re.findall(r"\{(\w*)\}", t)
    return all(n in PLACEHOLDERS for n in names) and "{" not in re.sub(r"\{(\w*)\}", "", t) and "}" not in re.sub(r"\{(\w*)\}", "", t)


def validate(field: str, v):
    """The cleaned value, or ValueError with a plain reason."""
    if field == "title.source_order":
        v = _clean_str_list(v)
        if any(x not in FAMILIES for x in v):
            raise ValueError("unknown title source (use: " + ", ".join(FAMILIES) + ")")
        return list(dict.fromkeys(v))
    if field == "title.templates":
        v = _clean_str_list(v, 40, 120)
        if any(not _template_ok(t) for t in v):
            raise ValueError("a template uses an unknown {placeholder} (use: " + ", ".join("{" + p + "}" for p in PLACEHOLDERS) + ")")
        return v
    if field in ("body.hooks", "pick.skip_terms"):
        return _clean_str_list(v, 40, 150)
    if field == "body.hashtags_fixed":
        return [t.lstrip("#") for t in _clean_str_list(v, 20, 40) if re.fullmatch(r"#?\w+", t)]
    if field == "title.case":
        if v is not None and v not in CASES:
            raise ValueError("case must be one of " + ", ".join(CASES))
        return v
    if field == "captions.mode":
        if v is not None and v not in CAPTION_MODES:
            raise ValueError("captions mode must be one of " + ", ".join(CAPTION_MODES))
        return v
    if field in ("title.emoji_end", "body.credit_line"):
        if v is not None and not isinstance(v, bool):
            raise ValueError("must be true, false or null")
        return v
    if field in ("body.channel_tag", "pick.need_speech"):
        if not isinstance(v, bool):
            raise ValueError("must be true or false")
        return v
    if field == "platforms":
        from .share import PLATFORMS
        v = _clean_str_list(v, 10, 40)
        if any(p not in PLATFORMS for p in v):
            raise ValueError("unknown platform")
        return v
    if field in ("title.max_words", "body.hashtags_max", "captions.min_words"):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise ValueError("must be a whole number from 0 to 100")
        return v
    if field in ("trim.start_shift_s", "trim.end_shift_s"):
        x = _num(v) if not isinstance(v, bool) else None
        if x is None or abs(x) > 60:
            raise ValueError("must be a number of seconds between -60 and 60")
        return x
    if field in ("trim.max_len_s", "pick.min_len_s", "pick.max_len_s"):
        x = _num(v) if not isinstance(v, bool) else None
        if x is None or not 0 <= x <= 600:
            raise ValueError("must be a number of seconds between 0 and 600")
        return x
    raise ValueError("unknown field")


ALL_FIELDS = tuple(f"{a}.{b}" if isinstance(sec, dict) else a for a, sec in DEFAULTS.items() for b in (sec if isinstance(sec, dict) else [None]))


def _layer(merged: dict, src: dict, layer: dict, fields, name: str, problems: list | None, where: str) -> None:
    if not isinstance(layer, dict):
        return
    for f in fields:
        a, _, b = f.partition(".")
        if a not in layer or (b and (not isinstance(layer[a], dict) or b not in layer[a])):
            continue
        try:
            _put(merged, f, validate(f, _get(layer, f)))
            src[f] = name
        except Exception as e:                                    # any bad value: ignore this field only
            if problems is not None:
                problems.append(f"{where}: {f}: {e if isinstance(e, ValueError) else 'bad value'}")


def _version(rules: dict) -> str:
    return format(zlib.crc32(json.dumps({k: v for k, v in rules.items() if k != "meta"}, sort_keys=True).encode()), "08x")


def load(channel: str | None = None) -> dict:
    """DEFAULTS <- learned.json <- taught.json, for this streamer. Never raises: a broken file or field is ignored."""
    try:
        return _load(channel)
    except Exception:
        out = copy.deepcopy(DEFAULTS)
        out["meta"] = {"version": _version(out), "examples": 0, "updated": None, "sources": {}}
        return out


def _load(channel: str | None, problems: list | None = None) -> dict:
    out = copy.deepcopy(DEFAULTS)
    src: dict = {}
    ch = str(channel or "").lower()
    learned = _read_json(learned_path())
    taught = _read_json(taught_path())
    for name, data, fields in (("learned", learned, LEARNED_FIELDS), ("taught", taught, TAUGHT_FIELDS)):
        if not isinstance(data, dict):
            continue
        _layer(out, src, data.get("default"), fields, name, problems, f"{name}.json default")
        if ch and ch != "default":
            _layer(out, src, data.get(ch), fields, name, problems, f"{name}.json {ch}")
    lm = learned.get("meta") if isinstance(learned, dict) and isinstance(learned.get("meta"), dict) else {}
    sec = (learned.get(ch) if isinstance(learned, dict) and isinstance(learned.get(ch), dict) else None) if ch else None
    n = sec.get("examples") if sec else (learned.get("default") or {}).get("examples") if isinstance(learned, dict) and isinstance(learned.get("default"), dict) else 0
    out["meta"] = {"version": _version(out), "examples": n if isinstance(n, int) else 0, "updated": lm.get("updated"), "sources": src}
    return out


def check() -> list[str]:
    """Every problem in learned.json and taught.json, in plain words. Empty = all fine."""
    probs: list[str] = []
    for name, p, fields in (("learned.json", learned_path(), LEARNED_FIELDS), ("taught.json", taught_path(), TAUGHT_FIELDS)):
        if not p.exists():
            continue
        data = _read_json(p)
        if not isinstance(data, dict):
            probs.append(f"{name}: not valid JSON (an object is expected)")
            continue
        for ch, sec in data.items():
            if ch == "meta":
                continue
            if not isinstance(sec, dict):
                probs.append(f"{name} {ch}: must be an object")
                continue
            for a, v in sec.items():
                if a in ("examples", "note", "notes"):
                    continue
                keys = [f"{a}.{b}" for b in v] if isinstance(v, dict) and isinstance(DEFAULTS.get(a), dict) else [a]
                for k in keys:
                    if k not in fields:
                        probs.append(f"{name} {ch}: {k} is not allowed here" + (" (the teacher may only write: " + ", ".join(TAUGHT_FIELDS) + ")" if name == "taught.json" else ""))
            _layer({}, {}, sec, fields, name, probs, f"{name} {ch}")
    return probs


# ---------------------------------------------------------------------------------------------------- using the rules
def skip_reason(m: dict, rules: dict, words: list | None = None) -> str | None:
    """Why the pick rules say Autopilot must NOT post this clip, or None. Pick rules only ever skip."""
    pk = rules.get("pick") or {}
    lo, hi = _num(m.get("suggest_start_s")) or 0.0, _num(m.get("suggest_end_s")) or _num(m.get("duration_s")) or 0.0
    ln = hi - lo
    if pk.get("min_len_s") and ln < pk["min_len_s"]:
        return f"shorter than you usually post ({ln:.0f}s < {pk['min_len_s']:.0f}s)"
    if pk.get("max_len_s") and ln > pk["max_len_s"]:
        return f"longer than you usually post ({ln:.0f}s > {pk['max_len_s']:.0f}s)"
    terms = [t.lower() for t in pk.get("skip_terms") or []]
    if terms:
        said = " ".join(w[0] for w in (words or []) if lo - 1 <= w[1] <= hi + 1)
        hay = " ".join(str(t) for t, _ in ((m.get("spike") or {}).get("top") or [])) + " " + said
        hay = hay.lower()
        for t in terms:
            if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", hay):
                return f'skip rule: "{t}"'
    if pk.get("need_speech") and words:                         # never transcribed = unknown: not a reason to skip
        from .captions import words_in_window
        if len(words_in_window(words, lo, hi)) < (rules.get("captions") or {}).get("min_words", 3):
            return "skip rule: no speech in this clip"
    return None


def trim_for(m: dict, rules: dict) -> tuple[float, float]:
    """The suggested trim moved by your usual shifts, clamped to the draft. Falls back to the plain suggestion if the result is not usable."""
    s0, e0 = float(m["suggest_start_s"]), float(m["suggest_end_s"])
    dur = _num(m.get("duration_s")) or e0
    t = rules.get("trim") or {}
    s = max(0.0, s0 + float(t.get("start_shift_s") or 0))
    e = min(dur, e0 + float(t.get("end_shift_s") or 0))
    if t.get("max_len_s") and e - s > t["max_len_s"]:
        e = s + float(t["max_len_s"])
    if e - s < MIN_CLIP_S or e - s > 600:
        return s0, e0
    return round(s, 2), round(e, 2)


def captions_for(rules: dict, speech: bool | None, fallback: bool) -> bool:
    """Burn captions? By your rule; never when the clip has no speech. With no rule: the Autopilot setting (and still not without speech)."""
    mode = (rules.get("captions") or {}).get("mode")
    if speech is False:
        return False
    if mode == "never":
        return False
    if mode == "always":
        return True
    if mode == "speech":
        return speech is not False                                # unknown: try; the export drops captions if nothing is said
    return bool(fallback)


def platforms_for(rules: dict, ticked: list[str]) -> list[str]:
    """Your usual platforms, but ONLY among the ones you ticked. No rule = all ticked; never adds one."""
    want = rules.get("platforms") or []
    return [p for p in ticked if p in want] if want else list(ticked)


# ---------------------------------------------------------------------------------------------------- what the Autopilot window shows
def status() -> dict:
    r = load(None)
    lm = _read_json(learned_path())
    lm = lm.get("meta") if isinstance(lm, dict) and isinstance(lm.get("meta"), dict) else {}
    fields = [{"field": f, "value": _get(r, f), "source": r["meta"]["sources"].get(f, "default")} for f in ALL_FIELDS]
    return {"examples": len(examples()), "updated": lm.get("updated"), "version": r["meta"]["version"], "fields": fields, "problems": check()}
