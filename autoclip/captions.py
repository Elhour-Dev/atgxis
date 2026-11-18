"""Viral-style burned-in captions: big bold uppercase, 1-3 words at a time, current word highlighted.

Speech-to-text (faster-whisper) is optional and only runs when you tick "Captions" at export.
Building the subtitle file and burning it in needs only ffmpeg.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("autoclip.captions")
Word = tuple[str, float, float]          # (text, start_s, end_s) relative to the file it was transcribed from

_MODEL = {}


class CaptionsUnavailable(RuntimeError):
    pass


def transcribe(path: Path, cfg: Config) -> list[Word]:
    """Word-level transcript with timestamps. Cached next to the clip as <clip>.words.json."""
    cache = path.with_suffix(".words.json")
    if cache.exists():
        return [tuple(w) for w in json.loads(cache.read_text(encoding="utf-8"))]
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise CaptionsUnavailable("Captions need:  pip install faster-whisper") from e
    key = (cfg.whisper_model, cfg.whisper_device)
    if key not in _MODEL:
        try:
            _MODEL[key] = WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type="auto")
        except Exception:
            _MODEL[key] = WhisperModel(cfg.whisper_model, device="cpu", compute_type="int8")
    segs, _ = _MODEL[key].transcribe(str(path), word_timestamps=True, vad_filter=True)
    words = [(w.word.strip(), float(w.start), float(w.end)) for s in segs for w in (s.words or []) if w.word.strip()]
    cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


def _clean(t: str) -> str:
    return t.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ").upper()


def _ts(t: float) -> str:
    t = max(t, 0.0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words: list[Word], start: float, end: float, vertical: bool, font: str = "Arial") -> str:
    """ASS subtitle text for the window [start, end] of the transcribed file (times shifted to start at 0)."""
    w, h = (1080, 1920) if vertical else (1920, 1080)
    size, margin_v = (int(h * 0.046), int(h * 0.30)) if vertical else (int(h * 0.062), int(h * 0.09))
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
            "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Cap,{font},{size},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(4, size // 14)},2,2,"
            f"{int(w * 0.06)},{int(w * 0.06)},{margin_v},1\n\n"
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
    lines = []
    for ch in chunks:
        for i, (_, a, b) in enumerate(ch):
            nxt = ch[i + 1][1] if i + 1 < len(ch) else b + 0.08
            text = " ".join(("{\\c&H00FFFF&}" + _clean(t) + "{\\c&HFFFFFF&}") if j == i else _clean(t) for j, (t, _, _) in enumerate(ch))
            lines.append(f"Dialogue: 0,{_ts(a)},{_ts(max(nxt, a + 0.05))},Cap,,0,0,0,,{text}\n")
    return head + "".join(lines)
