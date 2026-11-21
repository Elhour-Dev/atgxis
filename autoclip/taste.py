"""Learns which clips YOU keep and which you trash, so good ones can be recognised later.

Every Keep / Trash / Undo in the review page is written to a feedback log together with a snapshot of the clip's measurable features
(how big the spike was, how loud, which streamer, what chat was spamming ...). The log lives in your settings folder, NOT next to the
clip files, so "Delete trashed files" never makes the program forget a decision.

From that log a small model is trained (regularised logistic regression, plain Python, no extra packages). Training is honest about
itself: it is cross-validated, i.e. every clip is scored by a model that never saw that clip, and the resulting accuracy (and the confidence
needed to reach a target precision) is stored next to the model. Autopilot only acts when those out-of-sample numbers are good enough.

Which decisions count:
  * explicit: Keep (1) and Trash (0). Undo withdraws the decision;
  * implicit: you posted the clip yourself (counts as a Keep, but never overrides an explicit decision);
  * NEVER the autopilot's own posts: a model that learns from its own choices would only ever agree with itself.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from collections import Counter
from pathlib import Path

from . import settings
from .describe import CATEGORIES, _TOKEN, classify

VERSION = 1
EARLY_LABELS, EARLY_PER_CLASS = 10, 3         # below this: no guesses at all
MIN_LABELS, MIN_PER_CLASS = 30, 8             # below this: guesses are shown, but autopilot may not post
MIN_AUC = 0.65                                # a model that is barely better than a coin flip must not post anything
MAX_TRAIN = 800                               # newest decisions only: taste drifts, and pure Python must stay fast
RIDGE = 3.0                                   # shrinkage: with few decisions the model stays modest instead of memorising
MIN_SUPPORT = 20                              # a precision figure built on fewer clips than this is not believed (measured: 12/12 streaks fool it)
Z_LOWER = 1.28                                # precision is judged by its lower confidence bound (90% one-sided), not the lucky average
FLOOR = 0.55                                  # never act below this probability, whatever the numbers say
FOLDS = 5
NUM = ("heur", "ratio", "size", "base", "hype", "spam", "len", "rate", "audio", "jump", "jump_known", "res")
CATS = tuple(CATEGORIES)                      # funny / shock / hype; "default" is the reference level
LABELS = {"heur": "overall clip score", "ratio": "chat size compared to normal", "size": "how many chatted at once", "base": "channel size",
          "hype": "hype words in chat", "spam": "same messages repeated", "len": "how long the spike lasted", "rate": "chat speed",
          "audio": "loud stream audio", "jump": "audio got louder", "jump_known": "audio measured", "res": "video sharpness"}
_PUNCT = set("!?.,:;'\"()[]{}<>-_/\\|~`^*+=@#$%&")


# ---------------------------------------------------------------------------------------------------- where things live
_local = threading.local()


def folder() -> Path:
    """Where learning keeps its files. A background training thread keeps working in the folder it was scheduled for."""
    return getattr(_local, "base", None) or settings.config_dir() / "taste"


def feedback_path() -> Path:
    return folder() / "feedback.jsonl"


def model_path() -> Path:
    return folder() / "model.json"


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                                   # Windows: someone is reading it right now
            if i == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


# ---------------------------------------------------------------------------------------------------- features
def _num(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _top(top) -> list[tuple[str, int]]:
    """The spammed messages as clean (text, count) pairs, whatever a damaged clip record holds: this runs inside the Keep / Trash click."""
    out = []
    for item in top if isinstance(top, (list, tuple)) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
            out.append((item[0], int(_num(item[1]))))
    return out


def chat_terms(top) -> list[str]:
    """What chat was spamming, as presence markers: 't:kekw' for words, 'p:no way' for short phrases."""
    out: set[str] = set()
    for item in _top(top):
        text = item[0].lower().strip()
        toks = [t for t in _TOKEN.findall(text) if t not in _PUNCT and not t.isdigit()]
        out.update("t:" + t for t in toks[:6])
        if 2 <= len(toks) <= 3 and len(text) <= 24:
            out.add("p:" + " ".join(toks))
    return sorted(out)


def sample(meta: dict) -> dict:
    """Everything the model may use, measured from the clip's record. Plain JSON: it is stored with every decision."""
    sp = meta.get("spike") or {}
    peak, base = _num(sp.get("peak")), max(_num(sp.get("baseline")), 1.0)
    msgs, users = _num(sp.get("msgs")), max(_num(sp.get("users")), 1.0)
    length = max(_num(sp.get("end")) - _num(sp.get("start")), 1.0)
    jump = meta.get("loudness_jump_db")
    res = meta.get("res") or []
    x = {"heur": _clamp(_num(meta.get("score")) / 100),
         "ratio": _clamp(math.log2(max(peak, 1.0) / base) / 6),
         "size": _clamp(math.log10(peak + 1) / 3),
         "base": _clamp(math.log10(base + 1) / 3),
         "hype": _clamp(_num(sp.get("hype_frac"))),
         "spam": _clamp(msgs / users / 8),
         "len": _clamp(math.log10(length) / 2),
         "rate": _clamp(math.log10(msgs / length + 1) / 2),
         "audio": _clamp(_num(sp.get("audio_db")) / 20),
         "jump": _clamp(_num(jump) / 15, -1.0, 1.0),
         "jump_known": 1.0 if jump is not None else 0.0,
         "res": _clamp(_num(res[1]) / 1080, 0.0, 1.5) if len(res) == 2 and _num(res[1]) else 1.0}
    return {"x": {k: round(v, 4) for k, v in x.items()}, "cat": classify(_top(sp.get("top"))),
            "ch": str(meta.get("channel", "")).lower(), "terms": chat_terms(sp.get("top"))}


# ---------------------------------------------------------------------------------------------------- the feedback log
_log_lock = threading.Lock()


def record(meta: dict, label: int | None, src: str, when: float | None = None) -> None:
    """Append one decision. label 1 = good clip, 0 = bad clip, None = decision withdrawn (Undo). Never raises for a full disk etc.:
    pressing Keep must work even if the log cannot be written."""
    cid = meta.get("id")
    if not cid:
        return
    entry = {"v": VERSION, "t": round(when or time.time(), 1), "clip": cid, "label": label, "src": src}
    if label is not None:
        entry["s"] = sample(meta)
    try:
        folder().mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _log_lock, open(feedback_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"could not save your decision for learning: {e}")


_cache: dict = {}


def _read_log() -> list[dict]:
    p = feedback_path()
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache.get("key") == key:
        return _cache["rows"]
    rows = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                                   # a half-written last line must not break learning
                if isinstance(r, dict) and r.get("clip"):
                    rows.append(r)
    except OSError:
        return []
    _cache.update(key=key, rows=rows)
    return rows


LABEL_RULES = 2                        # bump when labels() reads the same log differently (2: a Trash after your own post is a Keep)
IMPLICIT = {"posted"}                  # you posted it yourself: a Keep, unless you decided explicitly


def labels() -> list[dict]:
    """The decisions in force, oldest first: [{clip, label, src, t, s}]. The latest explicit decision per clip wins; an Undo removes it;
    an implicit 'posted' only counts for a clip with no explicit decision. A Trash AFTER you posted the clip yourself was only to free
    disk space (before the "Done" button existed), so it counts as a Keep."""
    explicit: dict[str, dict] = {}
    implicit: dict[str, dict] = {}
    posted: set[str] = set()
    for r in _read_log():
        cid = r["clip"]
        if r.get("src") in IMPLICIT:
            if r.get("label") == 1 and r.get("s"):
                implicit[cid] = r
                posted.add(cid)
        elif r.get("label") in (0, 1) and r.get("s"):
            if r["label"] == 0 and cid in posted:
                r = {**r, "label": 1}
            explicit[cid] = r
        else:
            explicit.pop(cid, None)                           # Undo
    out = {**implicit, **explicit}
    return sorted(out.values(), key=lambda r: r.get("t", 0))


def counts() -> dict:
    ls = labels()
    keeps = sum(1 for r in ls if r["label"] == 1)
    return {"total": len(ls), "keeps": keeps, "trashes": len(ls) - keeps}


def posted_by_you(m: dict) -> bool:
    """You posted this clip yourself (not the Autopilot): shared by you, or marked as posted by hand."""
    return any(sh.get("actor", "you") == "you" and sh.get("status") in ("done", "draft") for sh in (m.get("shares") or {}).values())


def import_existing(root: Path) -> int:
    """Decisions you made before learning existed are still in the clip files (Keep / Trash). Take them over once."""
    known = {r["clip"] for r in _read_log()}
    n = 0
    for p in Path(root).rglob("*.clip.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (m.get("autopilot") or {}).get("state") == "trashed" and m.get("status") == "trash":
            continue                                           # moved to Trash by the Autopilot's score rule, not by you
        if m.get("id") and m["id"] not in known and m.get("status") in ("keep", "trash", "done"):
            # a trashed clip you had posted yourself was trashed to free space, not because it was bad
            mine = posted_by_you(m)
            if m["status"] == "done" and not mine:
                continue                                       # Done on an Autopilot-only post: no judgement of yours
            good = m["status"] == "keep" or mine
            record(m, 1 if good else 0, "import", when=p.stat().st_mtime)
            known.add(m["id"])
            n += 1
    return n


# ---------------------------------------------------------------------------------------------------- the model
def _spec(rows: list[dict]) -> dict:
    """Which columns the model has, decided from the training data only (so cross-validation cannot peek at the held-out clips)."""
    xs = [r["s"]["x"] for r in rows]
    mean, std = {}, {}
    for k in NUM:
        vals = [x.get(k, 0.0) for x in xs]
        m = sum(vals) / len(vals)
        mean[k] = m
        std[k] = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1.0
    ch = Counter(r["s"].get("ch", "") for r in rows)
    terms = Counter(t for r in rows for t in set(r["s"].get("terms", [])))
    return {"mean": mean, "std": std,
            "channels": [c for c, n in ch.most_common(12) if n >= 3 and c],
            "terms": [t for t, n in terms.most_common(30) if n >= 3 and n < 0.97 * len(rows)]}


def _vector(s: dict, spec: dict) -> list[tuple[int, float]]:
    """Sparse row [(column, value)]. Column 0 is the bias."""
    row = [(0, 1.0)]
    x = s.get("x", {})
    for i, k in enumerate(NUM, 1):
        z = (x.get(k, 0.0) - spec["mean"][k]) / spec["std"][k]
        if z:
            row.append((i, z))
    j = 1 + len(NUM)
    if s.get("cat") in CATS:
        row.append((j + CATS.index(s["cat"]), 1.0))
    j += len(CATS)
    ch = s.get("ch", "")
    if ch in spec["channels"]:
        row.append((j + spec["channels"].index(ch), 1.0))
    j += len(spec["channels"])
    have = set(s.get("terms", []))
    for i, t in enumerate(spec["terms"]):
        if t in have:
            row.append((j + i, 1.0))
    return row


def _width(spec: dict) -> int:
    return 1 + len(NUM) + len(CATS) + len(spec["channels"]) + len(spec["terms"])


def _names(spec: dict) -> list[str]:
    return ["bias", *NUM, *("cat:" + c for c in CATS), *("ch:" + c for c in spec["channels"]), *spec["terms"]]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60)))
    e = math.exp(max(z, -60))
    return e / (1.0 + e)


def _chol_solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = A[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 1e-12:
                    raise ValueError("matrix is not positive definite")
                L[i][i] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def _fit_weights(rows: list[list[tuple[int, float]]], y: list[int], d: int, lam: float = RIDGE) -> list[float]:
    """Ridge-regularised logistic regression by Newton's method (a handful of iterations; the problem is convex)."""
    n = len(y)
    pen = [1e-6] + [lam] * (d - 1)                                 # the bias is not shrunk
    w = [0.0] * d
    w[0] = math.log((sum(y) + 0.5) / (n - sum(y) + 0.5))

    def loss(w_):
        tot = 0.0
        for r, yi in zip(rows, y):
            z = sum(w_[j] * v for j, v in r)
            tot += math.log1p(math.exp(-abs(z))) + max(z, 0.0) - yi * z
        return tot + 0.5 * sum(p * a * a for p, a in zip(pen, w_))

    cur = loss(w)
    for _ in range(40):
        g = [pen[j] * w[j] for j in range(d)]
        H = [[0.0] * d for _ in range(d)]
        for r, yi in zip(rows, y):
            p = _sigmoid(sum(w[j] * v for j, v in r))
            res, s = p - yi, max(p * (1 - p), 1e-9)
            for a, (ja, va) in enumerate(r):             # rows are sorted by column, so (ja, jb) with a <= b is the upper triangle
                g[ja] += res * va
                sv = s * va
                Ha = H[ja]
                for jb, vb in r[a:]:
                    Ha[jb] += sv * vb
        for j in range(d):
            H[j][j] += pen[j]
            for k in range(j):
                H[j][k] = H[k][j]
        try:
            step = _chol_solve(H, g)
        except ValueError:
            break
        t = 1.0
        for _ in range(10):                                         # step halving: never let a step make the fit worse
            cand = [a - t * b for a, b in zip(w, step)]
            new = loss(cand)
            if new <= cur + 1e-9:
                break
            t /= 2
        else:
            break
        done = max(abs(t * b) for b in step) < 1e-5
        w, cur = cand, new
        if done:
            break
    return w


def _train(rows: list[dict]) -> dict:
    spec = _spec(rows)
    X = [_vector(r["s"], spec) for r in rows]
    w = _fit_weights(X, [int(r["label"]) for r in rows], _width(spec))
    return {**spec, "names": _names(spec), "w": [round(a, 5) for a in w]}


def _score(core: dict, s: dict) -> float:
    return _sigmoid(sum(core["w"][j] * v for j, v in _vector(s, core)))


def auc(pairs: list[tuple[float, int]]) -> float | None:
    """Chance that a random kept clip is scored above a random trashed one (0.5 = no better than guessing, 1 = perfect)."""
    pos = [p for p, y in pairs if y == 1]
    neg = [p for p, y in pairs if y == 0]
    if not pos or not neg:
        return None
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
    ranks, i = [0.0] * len(pairs), 0
    while i < len(order):                                           # average ranks for ties
        j = i
        while j + 1 < len(order) and pairs[order[j + 1]][0] == pairs[order[i]][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    rp = sum(ranks[k] for k in range(len(pairs)) if pairs[k][1] == 1)
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def cross_validate(rows: list[dict], folds: int = FOLDS, seed: int = 7) -> list[tuple[float, int]]:
    """Out-of-sample probability for every decision: each clip is scored by a model trained WITHOUT it."""
    idx = list(range(len(rows)))
    rnd = random.Random(seed)
    pos = [i for i in idx if rows[i]["label"] == 1]
    neg = [i for i in idx if rows[i]["label"] == 0]
    rnd.shuffle(pos)
    rnd.shuffle(neg)
    k = max(2, min(folds, len(pos), len(neg)))
    fold = {i: n % k for n, i in enumerate(pos)}
    fold.update({i: n % k for n, i in enumerate(neg)})
    out: dict[int, float] = {}
    for f in range(k):
        train = [rows[i] for i in idx if fold[i] != f]
        core = _train(train)
        for i in idx:
            if fold[i] == f:
                out[i] = _score(core, rows[i]["s"])
    return [(out[i], int(rows[i]["label"])) for i in idx]


def wilson_lower(hits: int, n: int, z: float = Z_LOWER) -> float:
    if n <= 0:
        return 0.0
    p = hits / n
    den = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den


def choose_threshold(oof: list, target: float) -> dict | None:
    """The lowest confidence at which the out-of-sample clips it would have posted were good often enough. Two independent checks must
    both agree, because scanning many cut-points finds lucky streaks (measured: a user whose real ceiling was 70% got a 94% streak):
      1. the LOWER confidence bound of the observed precision reaches the target (a lucky 5-out-of-5 is not proof);
      2. the model's own predicted chance, averaged over those clips, reaches the target too (its probabilities are calibrated).
    None if no confidence level passes."""
    pairs = sorted(((float(p), int(y)) for p, y in oof), reverse=True)
    best, hits, psum = None, 0, 0.0
    for i, (p, y) in enumerate(pairs, 1):
        hits += y
        psum += p
        nxt = pairs[i][0] if i < len(pairs) else -1.0
        if nxt == p:
            continue                                                # a threshold sits between distinct scores
        if i >= MIN_SUPPORT and p >= FLOOR and wilson_lower(hits, i) >= target and psum / i >= target:
            best = {"threshold": round(p, 4), "precision": round(hits / i, 3), "lower": round(wilson_lower(hits, i), 3), "support": i, "hits": hits,
                    "expected": round(psum / i, 3)}
    return best


def curve(oof: list, levels=(0.5, 0.6, 0.7, 0.8, 0.9)) -> list[dict]:
    """What each confidence level WOULD have done on the clips you reviewed (out-of-sample): how many it picks and how often you agreed."""
    out = []
    for t in levels:
        sel = [int(y) for p, y in oof if p >= t]
        out.append({"at": t, "picked": len(sel), "agreed": sum(sel), "precision": round(sum(sel) / len(sel), 3) if sel else None,
                    "share": round(len(sel) / len(oof), 3) if oof else 0.0})
    return out


def train(rows: list[dict] | None = None) -> dict:
    """Train on the newest decisions, cross-validate, store the result. Returns the model record (also saved to disk)."""
    lines = len(_read_log())
    rows = rows if rows is not None else labels()
    rows = rows[-MAX_TRAIN:]
    keeps = sum(1 for r in rows if r["label"] == 1)
    m: dict = {"v": VERSION, "trained_at": round(time.time(), 1), "n": len(rows), "keeps": keeps, "trashes": len(rows) - keeps,
               "log_lines": lines, "label_rules": LABEL_RULES}
    if len(rows) < EARLY_LABELS or min(keeps, len(rows) - keeps) < EARLY_PER_CLASS:
        m.update(state="none", reason=f"Keep or Trash at least {EARLY_LABELS} clips ({EARLY_PER_CLASS}+ of each) and it starts guessing.")
        _write_atomic(model_path(), m)
        return m
    oof = cross_validate(rows)
    core = _train(rows)
    a = auc(oof)
    m.update(core=core, oof=[[round(p, 3), y] for p, y in oof], auc=None if a is None else round(a, 3),
             brier=round(sum((p - y) ** 2 for p, y in oof) / len(oof), 4), base_rate=round(keeps / len(rows), 3),
             state="early" if len(rows) < MIN_LABELS or min(keeps, len(rows) - keeps) < MIN_PER_CLASS else "ready")
    _write_atomic(model_path(), m)
    return m


_model_cache: dict = {}


def model() -> dict | None:
    p = model_path()
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns)
    if _model_cache.get("key") != key:
        try:
            _model_cache.update(key=key, m=json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
    return _model_cache["m"]


def is_stale() -> bool:
    """Has the decision log changed since the model was trained? (Exact: counts every line, so a flipped Keep -> Trash is noticed too.)"""
    lines = len(_read_log())
    m = model() or {}
    return lines > 0 and (m.get("log_lines") != lines or m.get("label_rules", 1) != LABEL_RULES)


RETRAIN_DELAY_S = 1.0
_sched = threading.Lock()
_dirty: set[Path] = set()                 # folders with decisions nobody has learned from yet
_running: set[Path] = set()               # folders that have a training thread


def retrain_soon() -> None:
    """Learn from the latest decision in the background. Several quick clicks share one training run, and a click that arrives while a run
    is finishing is never lost (the check-and-stop and the scheduling happen under the same lock)."""
    base = folder()
    with _sched:
        _dirty.add(base)
        if base in _running:
            return
        _running.add(base)
    threading.Thread(target=_retrain_worker, args=(base,), daemon=True, name="taste-train").start()


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
            train()
        except Exception as e:                                      # learning must never take the review page down
            print(f"learning failed: {e}")


def readiness(m: dict | None, min_precision: float = 0.85, min_confidence: float = 0.0) -> dict:
    """Is the model good enough for autopilot to ACT, and from which confidence? {ok, reason, threshold, precision, lower, support}."""
    if not m or m.get("state") == "none":
        return {"ok": False, "threshold": None, "reason": (m or {}).get("reason") or f"Keep or Trash at least {EARLY_LABELS} clips and it starts learning."}
    if m["state"] != "ready":
        need = max(MIN_LABELS - m["n"], 0)
        why = f"{need} more decisions needed" if need else f"at least {MIN_PER_CLASS} kept and {MIN_PER_CLASS} trashed clips are needed ({m['keeps']} / {m['trashes']} so far)"
        return {"ok": False, "threshold": None, "reason": f"Still learning: {why} before it may post anything."}
    if (m.get("auc") or 0) < MIN_AUC:
        return {"ok": False, "threshold": None,
                "reason": f"It cannot see a pattern in your choices yet (accuracy {m.get('auc')}). Keep reviewing; this changes as you add clips."}
    pick = choose_threshold(m["oof"], min_precision)
    if not pick:
        return {"ok": False, "threshold": None,
                "reason": f"It cannot yet pick clips you would keep {min_precision:.0%} of the time with confidence. More decisions help; so does a lower precision target."}
    pick["threshold"] = max(pick["threshold"], float(min_confidence))
    return {"ok": True, "reason": "", **pick}


def predict(meta: dict) -> dict | None:
    """{'p': chance you would keep it, 'state': 'early'|'ready', 'why': [{text, w}]} or None while there is nothing to guess from."""
    m = model()
    if not m or not m.get("core"):
        return None
    core, s = m["core"], sample(meta)
    contrib = [(core["names"][j], core["w"][j] * v) for j, v in _vector(s, core) if j > 0]
    p = _sigmoid(core["w"][0] + sum(c for _, c in contrib))
    contrib.sort(key=lambda t: -abs(t[1]))
    why = [{"text": _describe(n), "w": round(c, 2)} for n, c in contrib[:3] if abs(c) >= 0.15 and n != "jump_known"]
    return {"p": round(p, 3), "state": m["state"], "why": why}


def _describe(name: str) -> str:
    if name.startswith("ch:"):
        return "streamer " + name[3:]
    if name.startswith("cat:"):
        return "a " + name[4:] + " reaction"
    if name.startswith("t:"):
        return 'chat says "' + name[2:] + '"'
    if name.startswith("p:"):
        return 'chat says "' + name[2:] + '"'
    return LABELS.get(name, name)
