import random
import re

import pytest

from autoclip.config import Config
from autoclip.detector import ChatEvent, SpikeDetector

T0 = 1_000_000.0


def run(cfg, script, seconds, seed=1):
    """script(sec) -> list[(user, text)] for that second. Returns spikes."""
    d, rnd, spikes = SpikeDetector(cfg), random.Random(seed), []
    d.advance(T0)                                   # start the clock
    for s in range(seconds):
        for user, text in script(s, rnd):
            d.add(ChatEvent(t=T0 + s + 0.5, user=user, text=text))
        spikes += d.advance(T0 + s + 1)
    sp = d.flush()
    return spikes + ([sp] if sp else []), d


def quiet(s, rnd):
    return [(f"u{rnd.randint(1, 6)}", "hello") ] if rnd.random() < 0.4 else []


def burst(lo, hi, users=40, text="KEKW"):
    def f(s, rnd):
        if lo <= s < hi:
            return [(f"v{rnd.randint(1, users)}", text) for _ in range(rnd.randint(15, 25))]
        return quiet(s, rnd)
    return f


def test_no_spike_in_normal_chat():
    spikes, _ = run(Config(), quiet, 400)
    assert spikes == []


def test_real_spike_found_once_with_sensible_window():
    spikes, _ = run(Config(), burst(200, 215), 400)
    assert len(spikes) == 1
    sp = spikes[0]
    assert T0 + 190 <= sp.start <= T0 + 210            # starts around the burst
    assert sp.end >= T0 + 212
    assert sp.peak >= 30 and sp.hype_frac > 0.9 and sp.score > 70


def test_no_clip_during_warmup():
    spikes, _ = run(Config(), burst(20, 35), 80)       # inside the 90s warmup
    assert spikes == []


def test_single_user_spam_does_not_trigger():
    def spam(s, rnd):
        return [("loud_guy", "AAAAAAA")] * 30 if 150 <= s < 170 else quiet(s, rnd)
    spikes, _ = run(Config(), spam, 300)
    assert spikes == []


def test_bots_and_commands_ignored():
    def bots(s, rnd):
        if 150 <= s < 170:
            return [(f"bot{i}", "hi") for i in range(3)] + [("nightbot", "welcome")] * 5 + \
                   [(f"v{i}", "!clip") for i in range(30)]
        return quiet(s, rnd)
    spikes, _ = run(Config(), bots, 300)
    assert spikes == []


def test_small_chat_needs_min_users():
    def tiny(s, rnd):
        return [(f"v{i}", "lol") for i in range(3)] if 150 <= s < 160 else quiet(s, rnd)
    spikes, _ = run(Config(), tiny, 300)               # 3 people < min_users=6
    assert spikes == []


def test_raid_is_ignored():
    def raid(s, rnd):
        if s == 150:
            return []
        return burst(151, 165)(s, rnd)

    d, rnd, spikes = SpikeDetector(Config()), random.Random(2), []
    d.advance(T0)
    for s in range(300):
        if s == 150:
            d.add(ChatEvent(t=T0 + s, kind="raid"))
        for u, t in raid(s, rnd):
            d.add(ChatEvent(t=T0 + s + .5, user=u, text=t))
        spikes += d.advance(T0 + s + 1)
    assert spikes == []


def test_two_close_waves_merge_into_one_clip():
    def two(s, rnd):
        return burst(150, 160)(s, rnd) if s < 175 else burst(185, 195)(s, rnd)
    spikes, _ = run(Config(), two, 400)
    assert len(spikes) == 1 and spikes[0].end >= T0 + 192


def test_far_apart_spikes_stay_separate():
    def two(s, rnd):
        return burst(150, 160)(s, rnd) if s < 250 else burst(300, 310)(s, rnd)
    spikes, _ = run(Config(), two, 450)
    assert len(spikes) == 2


def test_busy_channel_relative_threshold():
    """A channel with ~40 chatters normally must not fire at 45, but must at 120."""
    def busy(level):
        def f(s, rnd):
            n = level if 200 <= s < 215 else 40
            return [(f"v{rnd.randint(1, 5000)}", "hi") for _ in range(int(n * 0.6))]
        return f
    assert run(Config(), busy(45), 320)[0] == []
    assert len(run(Config(), busy(250), 320)[0]) == 1


def test_late_messages_are_counted_not_crashing():
    d = SpikeDetector(Config())
    d.advance(T0 + 100)
    d.add(ChatEvent(t=T0 + 10, user="a", text="late"))
    assert d.late == 1


def test_very_long_spike_is_split():
    spikes, _ = run(Config(), burst(150, 280), 400)
    assert len(spikes) >= 2


def test_onset_is_much_tighter_than_window_start():
    spikes, _ = run(Config(), burst(200, 215), 400)
    sp = spikes[0]
    assert abs(sp.onset - (T0 + 200)) <= 1            # pinpoints when chat took off
    assert sp.onset > sp.start + 5                     # the old window-start guess was ~9s too early


# ---- audio assist, adaptive multiplier, near-miss reporting ----
def run_with_audio(cfg, chat_fn, audio_fn, seconds, seed=3):
    d, rnd, spikes = SpikeDetector(cfg), random.Random(seed), []
    d.advance(T0)
    for s in range(seconds):
        for user, text in chat_fn(s, rnd):
            d.add(ChatEvent(t=T0 + s + 0.5, user=user, text=text))
        if s % 4 == 0:                                   # one volume reading per 4 s video segment
            d.add(ChatEvent(t=T0 + s, kind="audio", db=audio_fn(s)))
        spikes += d.advance(T0 + s + 1)
    return spikes, d


def steady(level):
    """level distinct chatters per second, 5 messages each second."""
    def f(s, rnd):
        return [(f"v{rnd.randint(1, 100000)}", "hi") for _ in range(level)]
    return f


def rising(base, lo, hi, factor):
    def f(s, rnd):
        n = int(base * (factor if lo <= s < hi else 1))
        return [(f"v{rnd.randint(1, 100000)}", "hi") for _ in range(n)]
    return f


def test_loud_moment_lets_a_moderate_chat_rise_through():
    cfg = Config(min_users=5)
    chat = rising(10, 300, 315, 1.55)                           # chat only 1.55x normal: below the normal 2.0x bar
    quiet_audio = lambda s: -30.0
    loud_audio = lambda s: -16.0 if 296 <= s < 320 else -30.0   # streamer screams: +14 dB
    assert run_with_audio(cfg, chat, quiet_audio, 400)[0] == []
    spikes, _ = run_with_audio(cfg, chat, loud_audio, 400)
    assert len(spikes) == 1 and spikes[0].audio_db >= 8
    assert run_with_audio(Config(min_users=5, audio_assist=False), chat, loud_audio, 400)[0] == []   # switch really turns it off


def test_loud_audio_alone_never_makes_a_clip():
    loud_audio = lambda s: -10.0 if 296 <= s < 320 else -30.0
    spikes, _ = run_with_audio(Config(min_users=5), steady(10), loud_audio, 400)      # chat completely flat
    assert spikes == []
    spikes, _ = run_with_audio(Config(min_users=5), rising(10, 300, 315, 1.1), loud_audio, 400)    # barely above normal: still no
    assert spikes == []


def test_digital_silence_and_missing_audio_are_harmless():
    spikes, d = run_with_audio(Config(min_users=5), rising(10, 300, 315, 3), lambda s: -91.0, 400)
    assert len(spikes) == 1 and spikes[0].audio_db == 0                              # chat spike still works with a muted stream


def test_multiplier_eases_down_on_big_chats_but_never_below_1_8():
    d = SpikeDetector(Config())
    assert d._threshold(20) / 20 == pytest.approx(2.0)                                                  # small chat: full 2.0x
    assert d._threshold(800) / 800 == pytest.approx(1.8, abs=0.01)                                      # big chat: 1.8x
    assert SpikeDetector(Config(auto_mult=False))._threshold(800) / 800 == pytest.approx(2.0)           # can be turned off


def test_near_miss_is_reported_once_per_episode():
    cfg = Config(min_users=5)
    spikes, d = run_with_audio(cfg, rising(10, 300, 315, 1.7), lambda s: -30.0, 400)   # 1.7x: close to the 2.0x bar, not over
    notes = d.pop_notes()
    assert spikes == [] and len(notes) == 1 and "near-miss" in notes[0] and re.search(r"needed 1\.[89]x", notes[0]), notes
    assert d.pop_notes() == []                                                         # popped


def test_audio_meter_measures_real_segments(tmp_path):
    import subprocess
    from autoclip.audio import mean_volume
    def tone(name, vol):
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=400:duration=2,volume={vol}", "-c:a", "aac", str(p)], check=True)
        return mean_volume(p)
    quiet, loud = tone("q.m4a", 0.03), tone("l.m4a", 0.6)
    assert quiet is not None and loud is not None and loud - quiet > 20
    assert mean_volume(tmp_path / "missing.ts") is None
