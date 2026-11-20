"""Reframe: makes a 9:16 export move like a hand-edited one, from the stream's own sound and picture.

  analyze()  ffmpeg -> loudness every 0.25 s + tiny grey frames (4 a second) -> webcam movement and where the gameplay moves.
             Standard library only (no OpenCV needed). Cached per draft in <draft>.signals.json.
  plan()     pure logic, like detector.py: turns those signals into
               * punch-in zooms (eased) on loud + webcam-reaction peaks, slow push-ins on build-ups,
               * layout steps for a split layout: webcam only (hook) -> split (action) -> webcam only (payoff),
               * a gameplay crop that follows where the action is,
               * an optional freeze-frame on the peak (off by default; it keeps the clip's length and sound in sync).
             Rules: at most 2 zooms in any 30 s; a zoom never enlarges the stream more than 1.5x (quality.py), never touches
             a panel that must stay whole (campaign must_show), never mirrors anything. Captions, hook and ad disclosure are
             drawn AFTER, on the finished frame, so no zoom can crop them. Random choices are seeded by the clip, so the
             before / after still, the export and a re-export agree.
  graph()    the ffmpeg filter graph: per panel a fixed crop, a `perspective` that moves the zoom / pan window every frame
             (crop sizes cannot change mid-stream), then ONE lanczos scale. One encode, from the original recording.
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import subprocess
from array import array
from pathlib import Path

from . import layout as lay
from . import quality

log = logging.getLogger("autoclip.reframe")

STEP = 0.25                  # seconds per signal sample
VW, VH = 64, 36              # tiny grey frames used for movement
MAX_ZOOMS = 2                # ... in any 30 s
WINDOW_S = 30.0
MIN_ZOOM = 1.06              # less than this is not worth a zoom
DEFAULTS = {"on": True, "zoom": True, "layouts": True, "track": True, "freeze": False}
FACE_BOX = (0.23, 0.66)      # webcam-only phases: the webcam sits between these heights of the frame (hook above, captions below)
CAPTION_Y = 0.72             # where captions move to during webcam-only phases


def options(saved: dict | None) -> dict:
    s = saved if isinstance(saved, dict) else {}
    out = {k: bool(s.get(k, v)) for k, v in DEFAULTS.items()}
    out["seed"] = str(s.get("seed") or "")[:40]
    return out


# ---------------------------------------------------------------------------------------------------- signals (I/O)
def analyze(src: Path, ffmpeg: str = "ffmpeg") -> dict:
    """Loudness (dB) and grey 64x36 frames' movement for the whole draft, one value per 0.25 s."""
    rate = 8000
    a = subprocess.run([ffmpeg, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                       capture_output=True, timeout=300)
    pcm = array("h")
    pcm.frombytes(a.stdout[: len(a.stdout) // 2 * 2])
    n = int(rate * STEP)
    loud = []
    for i in range(0, len(pcm) - n + 1, n):
        chunk = pcm[i:i + n]
        rms = math.sqrt(sum(x * x for x in chunk) / n) if n else 0
        loud.append(round(20 * math.log10(max(rms, 1.0) / 32768), 2))
    v = subprocess.run([ffmpeg, "-v", "error", "-i", str(src), "-an", "-vf", f"fps={1 / STEP:g},scale={VW}:{VH},format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, timeout=300)
    size = VW * VH
    frames = [v.stdout[i:i + size] for i in range(0, len(v.stdout) - size + 1, size)]
    diffs = [b""] + [bytes(abs(x - y) for x, y in zip(f2, f1)) for f1, f2 in zip(frames, frames[1:])]
    return {"step": STEP, "w": VW, "h": VH, "loud": loud, "motion": [d.hex() for d in diffs]}


def cached_signals(src: Path, ffmpeg: str = "ffmpeg") -> dict:
    cache = src.with_suffix(".signals.json")
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d.get("size") == src.stat().st_size:
            return d
    except (OSError, ValueError):
        pass
    d = analyze(src, ffmpeg)
    d["size"] = src.stat().st_size
    if not (d["loud"] or any(d["motion"])):
        return d                                                  # ffmpeg could not read it: do not remember "nothing happens"
    try:
        cache.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass
    return d


def _rect_px(r: dict | None, w: int, h: int) -> tuple[int, int, int, int]:
    if not r:
        return 0, 0, w, h
    x0, y0 = int(r["x"] * w), int(r["y"] * h)
    return x0, y0, max(x0 + 1, int(round((r["x"] + r["w"]) * w))), max(y0 + 1, int(round((r["y"] + r["h"]) * h)))


def window(sig: dict, start: float, end: float, face: dict | None, region: dict | None) -> dict:
    """The signals inside the trim: loud[], face[] (webcam movement), act[] (gameplay movement) and actx[] (where it is,
    0-1 across `region`), one per 0.25 s from the trim start."""
    st = sig.get("step", STEP)
    i0, i1 = int(start / st), int(math.ceil(end / st))
    w, h = sig.get("w", VW), sig.get("h", VH)
    fx0, fy0, fx1, fy1 = _rect_px(face, w, h) if face else (0, 0, 0, 0)
    rx0, ry0, rx1, ry1 = _rect_px(region, w, h)
    out = {"loud": list(sig.get("loud", [])[i0:i1]), "face": [], "act": [], "actx": []}
    for hx in sig.get("motion", [])[i0:i1]:
        d = bytes.fromhex(hx) if hx else b""
        if not d:
            out["face"].append(0.0)
            out["act"].append(0.0)
            out["actx"].append(None)
            continue
        fsum = sum(d[y * w + x] for y in range(fy0, fy1) for x in range(fx0, fx1)) if face else 0
        out["face"].append(fsum / max(1, (fx1 - fx0) * (fy1 - fy0)) if face else 0.0)
        tot = cx = 0
        for y in range(ry0, ry1):
            row = y * w
            for x in range(rx0, rx1):
                if face and fx0 <= x < fx1 and fy0 <= y < fy1:
                    continue
                m = d[row + x]
                if m > 12:                                   # ignore compression noise
                    tot += m
                    cx += m * (x - rx0 + 0.5)
        out["act"].append(tot / max(1, (rx1 - rx0) * (ry1 - ry0)))
        out["actx"].append(cx / tot / (rx1 - rx0) if tot else None)
    return out


# ---------------------------------------------------------------------------------------------------- the plan (pure)
def _z(xs: list[float], floor: float) -> list[float]:
    if not xs:
        return []
    s = sorted(xs)
    med = s[len(s) // 2]
    mad = sorted(abs(x - med) for x in xs)[len(xs) // 2] * 1.4826
    return [(x - med) / max(mad, floor) for x in xs]


def _smooth(xs: list[float]) -> list[float]:
    return [sum(xs[max(0, i - 1):i + 2]) / len(xs[max(0, i - 1):i + 2]) for i in range(len(xs))]


def allowed(times: list[float], t: float) -> bool:
    """Max 2 zooms in ANY 30 s: a new zoom may start only if fewer than 2 others start within 30 s of it."""
    return sum(1 for x in times if abs(x - t) < WINDOW_S) < MAX_ZOOMS


def peaks(w: dict, dur: float) -> list[tuple[float, float, float, float]]:
    """(time, score, loud_z, face_z) of local peaks of loudness + webcam movement, strongest first."""
    lz, fz = _z(_smooth(w["loud"]), 3.0), _z(_smooth(w["face"]), 2.0) if any(w["face"]) else [0.0] * len(w["loud"])
    n = min(len(lz), len(fz)) if fz else len(lz)
    score = [lz[i] + 0.7 * fz[i] for i in range(n)]
    out = []
    for i in range(1, n - 1):
        t = i * STEP
        if score[i] >= 1.5 and score[i] >= score[i - 1] and score[i] > score[i + 1] and 1.0 <= t <= dur - 1.0:
            out.append((round(t, 2), score[i], lz[i], fz[i]))
    return sorted(out, key=lambda p: -p[1])


def _buildup(loud: list[float], t: float) -> bool:
    i = int(t / STEP)
    a, b = loud[max(0, i - 16):max(0, i - 8)], loud[max(0, i - 8):i]
    return len(a) >= 4 and len(b) >= 4 and sum(b) / len(b) - sum(a) / len(a) >= 3.0


def plan(w: dict, dur: float, layout: dict | None, res, opts: dict, seed: str, must_show: str | None = None,
         hook_s: float = 2.5) -> dict:
    """Everything the export does to this clip's picture. Times are seconds from the trim start."""
    rng = random.Random(seed)
    mode = (layout or {}).get("mode", "blur")
    out = {"seed": seed, "mode": mode, "zooms": [], "phases": [], "track": None, "freeze": None, "notes": []}
    if not opts.get("on") or not res or not res[0]:
        return out
    room = {k: quality.max_zoom(v) for k, v in quality.panels(layout, (int(res[0]), int(res[1]))).items()}
    whole = must_show in ("overlay", "handle")                    # the full frame must stay visible: no crop moves at all
    if must_show == "facecam" and mode != "split":
        whole = True                                              # the webcam is somewhere in the full frame: keep all of it
    if must_show == "facecam":
        room["face"] = 1.0                                        # the webcam must stay whole
    if mode == "split" and layout.get("game_fit") == "blur":
        room["game"] = 1.0                                        # the whole stream frame is shown there: nothing to zoom
    if mode == "blur" and room.get("main", 1.0) > 1.0:
        room["main"] = 1.0                                        # where the webcam is is unknown: a zoom could cut it off
        out["notes"].append("no zoom on the full frame: save a webcam layout (Set layout) so zooms know where the face is")
    if whole:
        room = {k: 1.0 for k in room}
    pk = peaks(w, dur)
    main = pk[0][0] if pk else None

    # layout steps (split layouts with a webcam only): webcam only for the hook, split, webcam only for the payoff (after the zooms)
    steps = bool(opts.get("layouts") and mode == "split" and not whole and dur >= 8)
    if steps and rng.random() < 0.85:
        out["phases"].append([0.0, hook_s])                       # the whole hook: the screen under it never changes

    def in_phase(a: float, b: float) -> bool:
        return any(a < q1 and b > q0 for q0, q1 in out["phases"])

    # zooms
    if opts.get("zoom"):
        starts: list[float] = []
        for t, s, lzv, fzv in pk:
            if mode == "split":
                panel = "face" if fzv >= lzv and room.get("face", 1) >= MIN_ZOOM else "game"
            else:
                panel = "main"
            top = room.get(panel, 1.0)
            if top < MIN_ZOOM:
                continue
            push = _buildup(w["loud"], t) and rng.random() < 0.5
            if push:
                t0, t1 = max(0.2, t - rng.uniform(3.0, 4.5)), t
                t2 = t + rng.uniform(0.6, 1.2)
                t3 = t2 + 0.5
                amp = min(top, rng.uniform(1.06, 1.11))
                push = not in_phase(t0, t3)                       # the build-up starts under the webcam-only intro: punch instead
            if not push:
                t0 = max(0.0, t - 0.15)
                t1 = t0 + 0.18
                t2 = t1 + rng.uniform(0.8, 1.6)
                t3 = t2 + 0.35
                amp = min(top, rng.uniform(1.15, 1.28))
            t3 = min(t3, dur)
            if amp < MIN_ZOOM or t3 - t0 < 0.6 or in_phase(t0, t3) or not allowed(starts, t0):
                continue
            if any(t0 < z["t3"] + 1.0 and t3 > z["t0"] - 1.0 for z in out["zooms"]):
                continue
            starts.append(t0)
            out["zooms"].append({"panel": panel, "kind": "push" if push else "punch", "t0": round(t0, 2), "t1": round(t1, 2),
                                 "t2": round(t2, 2), "t3": round(t3, 2), "amp": round(amp, 3),
                                 "fx": 0.5, "fy": 0.42 if panel == "face" else 0.5})
        out["zooms"].sort(key=lambda z: z["t0"])
        if not out["zooms"] and pk and max(room.values() or [1.0]) < MIN_ZOOM:
            out["notes"].append("no zoom: the layout already enlarges every panel 1.5x or more (or the campaign needs it whole)")

    if steps and main is not None:                                # the payoff: webcam only once the moment (and its zoom) is over
        a = main + rng.uniform(0.6, 1.4)
        a = max([a, dur - rng.uniform(3.0, 4.5)] + [z["t3"] + 0.2 for z in out["zooms"] if z["t0"] <= main + 1.4])
        if dur - a >= 1.5 and a > (out["phases"][0][1] + 2.0 if out["phases"] else 2.0) and not any(z["t3"] > a for z in out["zooms"]):
            out["phases"].append([round(a, 2), round(dur, 2)])

    # gameplay crop that follows the action (split 'cover' gameplay panel, or the plain crop layout)
    tp = "game" if mode == "split" and layout.get("game_fit") != "blur" else "main" if mode == "crop" else None
    if opts.get("track") and tp and not whole:
        free = 1.0 - track_window(layout, res)
        if free > 0.04:
            keys, px = [], 0.5
            for k in range(int(dur) + 1):
                xs = [x for x in w["actx"][int(k / STEP):int((k + 1) / STEP)] if x is not None]
                acts = w["act"][int(k / STEP):int((k + 1) / STEP)]
                if xs and acts and max(acts) > 1.0:
                    want = min(1.0, max(0.0, (sum(xs) / len(xs) - (1 - free) / 2) / free))
                    step = max(-0.10 / free, min(0.10 / free, (want - px) * 0.5))
                    px = min(1.0, max(0.0, px + step))
                keys.append([float(k), round(px, 3)])
            if keys and max(abs(p - 0.5) for _, p in keys) > 0.05:
                out["track"] = {"panel": tp, "keys": keys}

    if opts.get("freeze") and main is not None and not in_phase(main, main + 0.6) and main + 0.6 < dur:
        out["freeze"] = [round(main + 0.05, 2), round(main + 0.55, 2)]
    return out


def applied(p: dict | None) -> bool:
    return bool(p and (p.get("zooms") or p.get("phases") or p.get("track")))


def track_window(layout: dict | None, res) -> float:
    """Width of the moving crop window as a fraction of the area it pans across (1.0 = no room to pan)."""
    mode = (layout or {}).get("mode", "blur")
    W, H = int(res[0]), int(res[1])
    if mode == "crop":
        return min(1.0, (H * lay.OUT_W / lay.OUT_H) / W)
    if mode != "split":
        return 1.0
    _, h2 = lay.heights(layout["split"])
    g = _region(layout)
    return min(1.0, (g["h"] * H * lay.OUT_W / h2) / (g["w"] * W))


def _region(layout: dict) -> dict:
    return layout.get("game") or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


# ---------------------------------------------------------------------------------------------------- filter graph
def _ease(p: str) -> str:
    return f"(1-cos(PI*{p}))/2"


def zoom_expr(zooms: list[dict], T: str) -> str:
    terms = []
    for z in zooms:
        rise = f"clip(({T}-{z['t0']})/{max(z['t1'] - z['t0'], 0.05):.3f},0,1)"
        rise = rise if z["kind"] == "push" else _ease(rise)
        fall = _ease(f"clip(({T}-{z['t2']})/{max(z['t3'] - z['t2'], 0.05):.3f},0,1)")
        terms.append(f"{z['amp'] - 1:.4f}*{rise}*(1-{fall})")
    return "(1" + "".join("+" + t for t in terms) + ")"


def pan_expr(keys: list, T: str) -> str:
    if not keys:
        return "0.5"
    e = f"{keys[0][1]}"
    for (ta, a), (tb, b) in zip(keys, keys[1:]):
        if b != a:
            e += f"+({b - a:.3f})*clip(({T}-{ta})/{tb - ta:.3f},0,1)"
    return f"({e})"


def _moving(region: dict | None, aspect: float, z: str, px: str, fx: float, fy: float, out_w: int, out_h: int,
            sharpen_when: str | None) -> str:
    """Fixed crop of `region`, then a perspective window (panel aspect, panned by px, zoomed by z around fx/fy), then one scale."""
    crop = lay._crop(region) + "," if region else ""
    cw = f"min(W,H*{aspect:.5f})"
    ch = f"({cw}/{aspect:.5f})"
    ww, wh = f"({cw}/{z})", f"({ch}/{z})"
    X = f"((W-{cw})*{px}+({cw}-{ww})*{fx})"
    Y = f"((H-{ch})*0.5+({ch}-{wh})*{fy})"
    x1, y1 = f"({X}+{ww})", f"({Y}+{wh})"
    persp = (f"perspective=x0='{X}':y0='{Y}':x1='{x1}':y1='{Y}':x2='{X}':y2='{y1}':x3='{x1}':y3='{y1}'"
             ":interpolation=cubic:sense=source:eval=frame")
    sharp = (f",unsharp=5:5:{quality.ZOOM_SHARPEN}:5:5:0.0:enable='{sharpen_when}'" if sharpen_when else "")
    return f"{crop}{persp},scale={out_w}:{out_h}:{lay.SCALE}{sharp}"


def graph(layout: dict | None, p: dict, res, fps: float, offset: float = 0.0, sharpen: float = 0.0) -> str | None:
    """filter_complex text from [0:v] to [v] (1080x1920), or None when the plan changes nothing (use layout.chain)."""
    if not applied(p) and not (p or {}).get("freeze"):
        return None
    mode = (layout or {}).get("mode", "blur")
    W, H = int(res[0]), int(res[1])
    off = f"+{offset:.3f}" if offset else ""
    TF = f"(in/{fps:g}{off})"                 # perspective knows frame numbers, not time
    TT = f"(t{off})"
    zs = {k: [z for z in p["zooms"] if z["panel"] == k] for k in ("face", "game", "main")}
    tr = p.get("track") or {}

    def panel(name: str, region: dict | None, out_w: int, out_h: int) -> str | None:
        zz, keys = zs[name], (tr.get("keys") if tr.get("panel") == name else None)
        if not zz and not keys:
            return None
        z, zt = zoom_expr(zz, TF), zoom_expr(zz, TT)
        return _moving(region, out_w / out_h, z, pan_expr(keys or [], TF), zz[0]["fx"] if zz else 0.5,
                       zz[0]["fy"] if zz else 0.5, out_w, out_h, f"gt({zt},1.01)" if zz else None)

    parts, n = [], 0

    def src() -> str:
        nonlocal n
        n += 1
        return f"[s{n - 1}]"

    if mode == "split":
        lay_ = lay.validate(layout)
        h1, h2 = lay.heights(lay_["split"])
        top = panel("face", lay_["facecam"], lay.OUT_W, h1) or lay._cover(lay_["facecam"], lay.OUT_W, h1, sharpen)
        parts.append(f"{src()}{top}[top]")
        if lay_["game_fit"] == "blur":
            g = lay_["game"]
            fg = f"{lay._crop(g)}," if g else ""
            parts.append(f"{src()}split[b1][b2];[b1]{lay._blur_bg(lay.OUT_W, h2)}[gbg];[b2]{fg}scale={lay.OUT_W}:-2:{lay.SCALE}[gfg];"
                         "[gbg][gfg]overlay=(W-w)/2:(H-h)/2[bot]")
        else:
            reg = _region(lay_) if tr.get("panel") == "game" else (lay_["game"] or lay.default_game_rect(lay_["split"], W / H))
            bot = panel("game", reg, lay.OUT_W, h2) or lay._cover(reg, lay.OUT_W, h2)
            parts.append(f"{src()}{bot}[bot]")
        parts.append("[top][bot]vstack=inputs=2[sv]")
        last = "[sv]"
        if p["phases"]:
            by, bmax = int(lay.OUT_H * FACE_BOX[0]), int(lay.OUT_H * (FACE_BOX[1] - FACE_BOX[0]))
            parts.append(f"{src()}{lay._crop(lay_['facecam'])},scale={lay.OUT_W}:{bmax}:force_original_aspect_ratio=decrease:{lay.SCALE}[ff]")
            parts.append(f"{src()}{lay._blur_bg(lay.OUT_W, lay.OUT_H)}[fbg]")
            parts.append(f"[fbg][ff]overlay=(W-w)/2:{by}[fv]")
            on = "+".join(f"between({TT},{a},{b})" for a, b in p["phases"])
            parts.append(f"{last}[fv]overlay=0:0:enable='{on}'[ph]")
            last = "[ph]"
    elif mode == "crop":
        body = panel("main", None, lay.OUT_W, lay.OUT_H)
        parts.append(f"{src()}{body}[mv]" if body else f"{src()}scale=-2:{lay.OUT_H}:{lay.SCALE},crop={lay.OUT_W}:{lay.OUT_H}[mv]")
        last = "[mv]"
    else:
        fh = lay._even(lay.OUT_W * H / W)
        body = panel("main", None, lay.OUT_W, fh)
        parts.append(f"{src()}{lay._blur_bg(lay.OUT_W, lay.OUT_H)}[bg]")
        parts.append(f"{src()}{body or f'scale={lay.OUT_W}:{fh}:{lay.SCALE}'}[fg]")
        parts.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[mv]")
        last = "[mv]"
    if p.get("freeze"):
        a, b = (int(round((x - offset) * fps)) for x in p["freeze"])      # frame numbers of this render
        if b > 0:
            a = max(a, 0)
            parts.append(f"{last}split[fz1][fz2];[fz1][fz2]freezeframes=first={a}:last={b}:replace={a}[fz]")
            last = "[fz]"
    head = f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n)) + ";" if n > 1 else "[0:v]null[s0];"
    return head + ";".join(parts) + f";{last}null[v]"


# ---------------------------------------------------------------------------------------------------- around the graph
def hook_screen(p: dict | None, captions: bool) -> list[tuple[float, float]] | None:
    """What is on screen under the hook when the clip opens on the webcam alone: the webcam box and the captions (moved below
    it). None when the clip opens on the normal layout (then the layout's own bands apply)."""
    if not (p and p.get("phases") and p["phases"][0][0] == 0.0):
        return None
    size = int(lay.OUT_H * 0.046)
    cap = [(lay.OUT_H * CAPTION_Y - size * 2.6, lay.OUT_H * CAPTION_Y + size * 1.4)] if captions else []
    return [(lay.OUT_H * FACE_BOX[0], lay.OUT_H * FACE_BOX[1])] + cap


def hook_bands(p: dict | None) -> list[tuple[float, float]]:
    """Rows the hook must avoid because of the plan: the big webcam during the intro phase."""
    if p and p.get("phases") and p["phases"][0][0] == 0.0:
        return [(lay.OUT_H * FACE_BOX[0], lay.OUT_H * FACE_BOX[1])]
    return []


_DIAL = re.compile(r"^(Dialogue: \d+,)(\d+):(\d\d):(\d\d\.\d\d),(\d+):(\d\d):(\d\d\.\d\d),(Cap,.*?)\\pos\((\d+),(\d+)\)", re.M)


def move_captions(ass: str | None, p: dict | None) -> str | None:
    """Captions shown while the webcam is big move below it, so they never cover the face."""
    if not ass or not p or not p.get("phases"):
        return ass
    y = int(lay.OUT_H * CAPTION_Y)

    def sub(m: re.Match) -> str:
        a = int(m[2]) * 3600 + int(m[3]) * 60 + float(m[4])
        b = int(m[5]) * 3600 + int(m[6]) * 60 + float(m[7])
        inside = sum(max(0.0, min(b, q1) - max(a, q0)) for q0, q1 in p["phases"])
        if inside * 2 < max(b - a, 0.01):
            return m[0]
        return m[0][: m.start(9) - m.start(0)] + f"{m[9]},{y})"
    return _DIAL.sub(sub, ass)


def make(src: Path, start: float, end: float, layout: dict | None, meta: dict, must_show: str | None, saved: dict | None,
         fps: float, ffmpeg: str = "ffmpeg", offset: float = 0.0, sharpen: float = 0.0) -> dict | None:
    """{'plan', 'graph'} for an export of [start, end] of this draft, or None when reframe is off or cannot run here."""
    opts = options(saved)
    res = meta.get("res")
    if not opts["on"] or not res or len(res) < 2:
        return None
    try:
        sig = cached_signals(src, ffmpeg)
        mode = (layout or {}).get("mode", "blur")
        face = layout.get("facecam") if mode == "split" else None
        region = _region(layout) if mode == "split" else None
        w = window(sig, start, end, face, region)
        p = plan(w, end - start, layout, res, opts, opts["seed"] or str(meta.get("id", "")), must_show)
        return {"plan": p, "graph": graph(layout, p, res, fps, offset, sharpen)}
    except (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError) as e:
        log.warning("reframe skipped for %s: %s", meta.get("id"), e)
        return None


def summary(p: dict | None) -> dict:
    p = p or {}
    return {"applied": applied(p), "zooms": len(p.get("zooms") or []), "layouts": bool(p.get("phases")),
            "track": bool(p.get("track")), "freeze": bool(p.get("freeze")), "seed": p.get("seed", "")}
