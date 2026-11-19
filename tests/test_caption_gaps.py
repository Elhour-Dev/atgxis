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


# ------------------------------------------------------------------ no speech must not turn into text in random languages
class LangModel:
    """Remembers which `language` every call asked for. `detect` is what Whisper 'detects' when none is forced.
    `voice`: the strict pass hears words. `noise_segment`: what the relaxed passes invent from noise."""

    def __init__(self, detect="fr", prob=0.9, voice=True, noise_segment=None):
        self.detect, self.prob, self.voice, self.noise_segment, self.langs = detect, prob, voice, noise_segment, []

    def transcribe(self, audio, **kw):
        lang = kw.get("language")
        self.langs.append(lang)
        info = types.SimpleNamespace(language=lang or self.detect, language_probability=1.0 if lang else self.prob)
        strict = kw.get("no_speech_threshold", 0.6) is not None
        if strict and not self.voice:
            return iter([]), info
        if not strict and self.noise_segment:
            return iter([self.noise_segment]), info
        n = len(audio) / SR
        words = [types.SimpleNamespace(word=" salut", start=float(t), end=float(t) + 0.4) for t in np.arange(0.5, n - 0.5, 2.0)
                 if not (strict and n > 40 and 20 <= t < 50)]                  # the strict pass skips the middle, as in FakeModel
        return iter([types.SimpleNamespace(words=words)]), info


def test_detected_language_is_pinned_for_every_later_pass():
    m = LangModel(detect="fr")
    cap.recognise(m, tone(80))
    assert m.langs[0] is None and set(m.langs[1:]) == {"fr"} and len(m.langs) > 1      # first pass detects, the rest cannot drift


def test_configured_language_is_used_everywhere():
    m = LangModel(detect="fr")
    cap.recognise(m, tone(80), language="en")
    assert set(m.langs) == {"en"}


def test_no_speech_and_unsure_language_gives_nothing():
    m = LangModel(voice=False, prob=0.2, noise_segment=types.SimpleNamespace(
        words=[types.SimpleNamespace(word=" bonjour", start=0.1, end=0.5)], no_speech_prob=0.1, avg_logprob=-0.2))
    assert cap.recognise(m, tone(20)) == []            # noise: Whisper's language guess is not trusted, relaxed passes are not run
    assert len(m.langs) == 1


def test_invented_text_from_the_relaxed_pass_is_dropped():
    junk = types.SimpleNamespace(words=[types.SimpleNamespace(word=" Продолжение", start=0.1, end=0.5)], no_speech_prob=0.95, avg_logprob=-1.8)
    m = LangModel(voice=False, prob=0.9, noise_segment=junk)       # confident language, but the text itself is flagged as invented
    assert cap.recognise(m, tone(20)) == []
    real = types.SimpleNamespace(words=[types.SimpleNamespace(word=" salut", start=0.1, end=0.5)], no_speech_prob=0.95, avg_logprob=-0.4)
    m2 = LangModel(voice=False, prob=0.9, noise_segment=real)      # speech the strict pass skipped (game sound over a voice) is still kept
    assert [w[0] for w in cap.recognise(m2, tone(20))][:1] == ["salut"]
