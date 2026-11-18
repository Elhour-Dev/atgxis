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


def transcribe(path: Path, cfg: Config) -> list[Word]:
    """Word-level transcript with timestamps. Cached next to the clip as <clip>.words.json."""
    cache = path.with_suffix(".words.json")
    if cache.exists():
        return [tuple(w) for w in json.loads(cache.read_text(encoding="utf-8"))]
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise CaptionsUnavailable("Captions need:  pip install faster-whisper") from e
    audio = decode_audio(path)                     # fail fast (no audio track) before loading a large model
    key = (cfg.whisper_model, cfg.whisper_device)
    if key not in _MODEL:
        try:
            _MODEL[key] = WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type="auto")
        except Exception:
            _MODEL[key] = WhisperModel(cfg.whisper_model, device="cpu", compute_type="int8")
    segs, _ = _MODEL[key].transcribe(audio, word_timestamps=True, vad_filter=True)
    words = [(w.word.strip(), float(w.start), float(w.end)) for s in segs for w in (s.words or []) if w.word.strip()]
    cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


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
