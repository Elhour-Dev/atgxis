"""Whisper silently skips 30 s blocks it believes contain no speech. These tests use a fake model that does exactly that."""
import json
import types

import numpy as np
import pytest

from autoclip import captions as cap
from autoclip.config import Config

SR = cap.SR


class FakeModel:
    """Speaks 'hello' every 2 s. The strict pass (default thresholds) wrongly skips seconds 20-50; lenient settings find them."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        n = len(audio) / SR
        strict = kw.get("no_speech_threshold", 0.6) is not None
        self.calls.append((n, strict, kw.get("vad_filter")))
        words = []
        for t in np.arange(0.5, n - 0.5, 2.0):
            global_t = t
            if strict and n > 40 and 20 <= global_t < 50:
                continue                                   # the bug being simulated
            words.append(types.SimpleNamespace(word=" hello", start=float(t), end=float(t) + 0.4))
        return iter([types.SimpleNamespace(words=words)]), None


def tone(seconds, amp=0.1):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_gaps_in_audible_audio_are_filled():
    audio = tone(80)
    words = cap.recognise(FakeModel(), audio)
    covered = [w for w in words if 20 <= w[1] < 50]
    assert len(covered) >= 12, "the skipped middle must be recovered"
    assert not cap.find_gaps(words, 80, cap.loud_seconds(audio))


def test_real_silence_is_not_chased():
    audio = np.zeros(60 * SR, dtype=np.float32)
    assert cap.loud_seconds(audio) == [False] * 60
    assert cap.find_gaps([], 60, cap.loud_seconds(audio)) == []


def test_window_only_transcription_and_cache(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    audio = tone(90)
    model = FakeModel()
    monkeypatch.setattr(cap, "decode_audio", lambda p: audio)
    monkeypatch.setitem(cap._MODEL, ("small", "auto"), model)
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", types.SimpleNamespace(WhisperModel=object))
    cfg = Config()
    w = cap.transcribe(clip, cfg, 23.3, 53.3)
    inside = cap.words_in_window(w, 23.3, 53.3)
    assert len(inside) >= 12, "words must exist all through the 30 s window"
    assert max(n for n, *_ in model.calls) < 40, "only the window (~33 s) is sent to the model, not the whole file"
    n_calls = len(model.calls)
    again = cap.transcribe(clip, cfg, 25.0, 50.0)          # inside the cached window: no new model work
    assert len(model.calls) == n_calls and again == w
    old = json.loads(clip.with_suffix(".words.json").read_text())
    assert old[0]["lo"] == pytest.approx(21.8) and old[0]["hi"] == pytest.approx(54.8)


def test_old_cache_format_is_ignored(tmp_path):
    f = tmp_path / "c.words.json"
    f.write_text(json.dumps([["hi", 1.0, 1.2]]))
    assert cap._read_cache(f) == []
