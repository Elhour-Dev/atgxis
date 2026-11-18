import random

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
