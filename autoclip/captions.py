"""Viral-style burned-in captions: big bold uppercase, 1-3 words at a time, current word highlighted.

Speech-to-text (faster-whisper) is optional and only runs when you tick "Captions" at export.
Building the subtitle file and burning it in needs only ffmpeg.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .config import Config
from .recorder import ffmpeg_exe

log = logging.getLogger("autoclip.captions")
Word = tuple[str, float, float]          # (text, start_s, end_s) relative to the file it was transcribed from

_MODEL = {}


class CaptionsUnavailable(RuntimeError):
    pass


def decode_audio(path: Path):
    """16 kHz mono float32 samples, decoded by ffmpeg. Handing Whisper a ready array means it never calls PyAV
    (`av`), whose version mismatches with faster-whisper cause errors like "unexpected keyword argument 'metadata_errors'"."""
    import numpy as np
    r = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                       capture_output=True, timeout=300)
    if r.returncode != 0 or not r.stdout:
        raise CaptionsUnavailable("this clip has no audio track to caption")
    return np.frombuffer(r.stdout, dtype=np.float32)


SR = 16000
_STRICT: dict = {}                                    # faster-whisper defaults + the voice-activity filter
_LENIENT: dict = {"no_speech_threshold": None, "log_prob_threshold": None}   # never skip a block as "no speech"


MIN_LANG_CONFIDENCE = 0.5       # below this, a "detected" language is a guess made on noise, not on a voice


def _invented(seg) -> bool:
    """A segment from a relaxed pass that looks made up: Whisper itself says 'probably no speech' AND the text is a poor fit for the
    audio. This is what music / game sound / silence turns into when the no-speech checks are switched off (often in a random language)."""
    return (getattr(seg, "no_speech_prob", None) or 0.0) > 0.6 and (getattr(seg, "avg_logprob", None) or 0.0) < -1.2


def _run_model(model, audio, vad: bool, lenient: bool = False, language: str | None = None, heard: dict | None = None) -> list[Word]:
    kw = dict(word_timestamps=True, vad_filter=vad, condition_on_previous_text=False, beam_size=5,
              language=language or None, **(_LENIENT if lenient else _STRICT))
    if vad and lenient:
        kw["vad_parameters"] = {"threshold": 0.25, "min_silence_duration_ms": 700, "speech_pad_ms": 400}
    try:
        segs, info = model.transcribe(audio, **kw)
    except TypeError:                                 # an older faster-whisper without one of the optional arguments
        kw.pop("vad_parameters", None)
        segs, info = model.transcribe(audio, **kw)
    if heard is not None:                             # the language Whisper detected (known before the first segment is produced)
        heard["language"] = getattr(info, "language", None)
        heard["prob"] = getattr(info, "language_probability", None)
    return [(w.word.strip(), float(w.start), float(w.end)) for s in segs if not (lenient and _invented(s))
            for w in (s.words or []) if w.word.strip()]


def loud_seconds(audio, floor_db: float = -48.0) -> list[bool]:
    """Per second of audio: is there anything audible (above `floor_db`)? Used to tell real silence from missed speech."""
    import numpy as np
    n = len(audio) // SR
    if n <= 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * SR].reshape(n, SR) ** 2, axis=1) + 1e-12)
    return [bool(20 * np.log10(r) > floor_db) for r in rms]


def find_gaps(words: list[Word], length_s: float, loud: list[bool], min_gap: float = 6.0) -> list[tuple[float, float]]:
    """Stretches of >= min_gap seconds with no recognised word although the audio is audible: Whisper probably skipped them."""
    marks = [0.0] + sorted(x for w in words for x in (w[1], w[2])) + [length_s]
    gaps = []
    for lo, hi in zip(marks, marks[1:]):
        if hi - lo >= min_gap:
            seconds = [loud[i] for i in range(int(lo), min(int(hi) + 1, len(loud)))]
            if seconds and sum(seconds) >= 0.5 * len(seconds):
                gaps.append((lo, hi))
    return gaps


def _merge(words: list[Word], extra: list[Word]) -> list[Word]:
    out = sorted(words + extra, key=lambda w: (w[1], w[2]))
    clean: list[Word] = []
    for w in out:                                     # drop duplicates produced by overlapping retries
        if clean and w[0].lower() == clean[-1][0].lower() and abs(w[1] - clean[-1][1]) < 0.4:
            continue
        clean.append(w)
    return clean


def recognise(model, audio, language: str | None = None) -> list[Word]:
    """Best-effort transcript of one audio array: normal pass, then fill every audible-but-empty stretch with relaxed settings."""
    heard: dict = {}
    words = _run_model(model, audio, vad=True, language=language, heard=heard)
    length = len(audio) / SR
    loud = loud_seconds(audio)
    # One language for the whole clip. Left to detect it again on every retry and gap, Whisper "hears" random languages in music,
    # game sound and silence. Pin what the first pass detected (or what the user configured).
    if not language and heard.get("language") and (words or (heard.get("prob") or 1.0) >= MIN_LANG_CONFIDENCE):
        language = heard["language"]
    if not words:
        if not language and heard.get("language"):
            log.info("captions: no speech found and no confident language (%s, %.2f): treating as silence", heard["language"], heard.get("prob") or 0)
            return []                                 # a low-confidence guess on noise: do not coax text out of it
        log.info("captions: no speech with the strict pass, retrying relaxed")
        words = _run_model(model, audio, vad=True, lenient=True, language=language) or \
            _run_model(model, audio, vad=False, lenient=True, language=language)
    for rnd in range(2):                                # two rounds are plenty: relaxed, then no voice filter at all
        gaps = find_gaps(words, length, loud)
        if not gaps:
            break
        extra: list[Word] = []
        for lo, hi in gaps:
            a, b = max(lo - 0.5, 0.0), min(hi + 0.5, length)
            chunk = audio[int(a * SR): int(b * SR)]
            log.info("captions: filling %.1f-%.1fs, audible but no words yet", lo, hi)
            got = _run_model(model, chunk, vad=(rnd == 0), lenient=True, language=language)
            extra += [(t, x + a, y + a) for t, x, y in got if lo - 0.2 <= x + a <= hi + 0.2]
        if not extra:
            continue
        words = _merge(words, extra)
    return words


def transcribe(path: Path, cfg: Config, start: float | None = None, end: float | None = None) -> list[Word]:
    """Word-level transcript with timestamps relative to the START OF THE FILE.
    With start/end only that part (plus 1.5 s of margin) is transcribed: faster, and Whisper cannot skip it because of
    some other minute. Results are cached next to the clip as <clip>.words.json (a list of windows)."""
    cache = path.with_suffix(".words.json")
    lo_req = 0.0 if start is None else max(start - 1.5, 0.0)
    hi_req = 1e9 if end is None else end + 1.5
    windows = _read_cache(cache)
    for w in windows:
        if w["lo"] <= lo_req + 0.05 and w["hi"] >= min(hi_req, w.get("len", hi_req)) - 0.05 and w["words"]:
            return [tuple(x) for x in w["words"]]
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise CaptionsUnavailable("Captions need:  pip install faster-whisper") from e
    audio = decode_audio(path)                         # fail fast (no audio track) before loading a large model
    total = len(audio) / SR
    lo, hi = lo_req, min(hi_req, total)
    key = (cfg.whisper_model, cfg.whisper_device)
    if key not in _MODEL:
        try:
            _MODEL[key] = WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type="auto")
        except Exception:
            _MODEL[key] = WhisperModel(cfg.whisper_model, device="cpu", compute_type="int8")
    part = audio[int(lo * SR): int(hi * SR)]
    words = [(t, a + lo, b + lo) for t, a, b in recognise(_MODEL[key], part, getattr(cfg, "whisper_language", "") or None)]
    if not words:
        raise CaptionsUnavailable("no speech could be detected in this part of the clip (music, game sound or silence only)")
    windows = [w for w in windows if not (lo <= w["lo"] and w["hi"] <= hi)] + [{"lo": lo, "hi": hi, "len": total, "words": words}]
    cache.write_text(json.dumps(windows, ensure_ascii=False), encoding="utf-8")
    return words


def cached_words(path: Path) -> list[Word]:
    """Every word already transcribed for this clip (from earlier caption exports). Free: no model run."""
    seen: dict[tuple, Word] = {}
    for w in _read_cache(path.with_suffix(".words.json")):
        for t, a, b in w["words"]:
            seen[(round(a, 2), t)] = (t, a, b)
    return sorted(seen.values(), key=lambda w: w[1])


def _read_cache(cache: Path) -> list[dict]:
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    # the old format was a plain list of words; it came from the flawed whole-file pass, so it is discarded
    return [w for w in data if isinstance(w, dict) and {"lo", "hi", "words"} <= set(w)] if isinstance(data, list) else []


def words_in_window(words: list[Word], start: float, end: float) -> list[Word]:
    return [w for w in words if w[2] > start and w[1] < end]


def _clean(t: str) -> str:
    return t.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ").upper()


def _ts(t: float) -> str:
    t = max(t, 0.0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def caption_geometry(vertical: bool, seam: bool, box: dict | None) -> dict:
    """Where captions go, in ASS pixel units. One source of truth for the subtitle file AND the editor's sample.
    box = {x,y,w,h} as fractions of the 1080x1920 output (only used for vertical exports)."""
    w, h = (1080, 1920) if vertical else (1920, 1080)
    if vertical and box:
        bx, by, bw, bh = box["x"] * w, box["y"] * h, box["w"] * w, box["h"] * h
        return {"w": w, "h": h, "size": int(max(24, min(bh * 0.6, 150))), "cx": bx + bw / 2, "cy": by + bh / 2,
                "ml": int(bx), "mr": int(w - bx - bw), "an": 5, "wrap": 0}
    size = int(h * 0.046) if vertical else int(h * 0.062)
    if vertical and seam:                                     # split layout: on the line between webcam and gameplay
        return {"w": w, "h": h, "size": size, "cx": w / 2, "cy": h / 2, "ml": int(w * 0.06), "mr": int(w * 0.06), "an": 5, "wrap": 2}
    margin_v = int(h * 0.30) if vertical else int(h * 0.09)
    return {"w": w, "h": h, "size": size, "cx": w / 2, "cy": h - margin_v, "ml": int(w * 0.06), "mr": int(w * 0.06), "an": 2, "wrap": 2}


def build_ass(words: list[Word], start: float, end: float, vertical: bool, font: str = "Arial", seam: bool = False,
              box: dict | None = None) -> str:
    """ASS subtitle text for the window [start, end] of the transcribed file (times shifted to start at 0).

    Two rules keep captions rock-steady (fixes "appears low, then jumps up"):
      1. no two captions ever overlap in time (Whisper's word times often do; the renderer then STACKS the overlapping
         lines and pushes one up), and
      2. every caption carries an explicit \\pos, which switches the renderer's collision-avoidance off.
    """
    g = caption_geometry(vertical, seam, box)
    size = g["size"]
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {g['w']}\nPlayResY: {g['h']}\nWrapStyle: {g['wrap']}\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
            "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Cap,{font},{size},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(4, size // 14)},2,"
            f"{g['an']},{g['ml']},{g['mr']},0,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    ws = [(t, a - start, b - start) for t, a, b in words if b > start and a < end]
    ws = [(t, max(a, 0.0), min(b, end - start)) for t, a, b in ws]
    chunks, cur = [], []
    for item in ws:                                    # group: <=3 words, <=1.6s, break on pauses
        if cur and (len(cur) >= 3 or item[2] - cur[0][1] > 1.6 or item[1] - cur[-1][2] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(item)
    if cur:
        chunks.append(cur)
    events: list[list] = []                            # [start, end, text]
    for ch in chunks:
        for i, (_, a, b) in enumerate(ch):
            nxt = ch[i + 1][1] if i + 1 < len(ch) else b + 0.08
            text = " ".join(("{\\c&H00FFFF&}" + _clean(t) + "{\\c&HFFFFFF&}") if j == i else _clean(t) for j, (t, _, _) in enumerate(ch))
            events.append([a, max(nxt, a + 0.05), text])
    events.sort(key=lambda e: e[0])
    for i in range(len(events) - 1):                   # rule 1: never overlap the next caption
        events[i][1] = min(events[i][1], events[i + 1][0])
    pos = f"{{\\an{g['an']}\\pos({g['cx']:.0f},{g['cy']:.0f})}}"       # rule 2
    lines = [f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{pos}{text}\n" for a, b, text in events if b - a >= 0.03]
    return head + "".join(lines)
