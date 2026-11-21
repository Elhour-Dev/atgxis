"""Several streams at once: shared lanes, no lost clips under load, clean shutdown, cheaper renders."""
import json
import random
import subprocess
import threading
import time
from pathlib import Path

import pytest

from autoclip import worker as worker_mod
from autoclip.cli import plan_targets, resource_notes, status_block
from autoclip.config import Config
from autoclip.cutter import _filters, export_clip, probe_duration
from autoclip.detector import ChatEvent
from autoclip.lanes import Lanes, PriorityPool
from autoclip.recorder import Recorder, ffmpeg_exe
from autoclip.disk import clips_gb
from autoclip.worker import ChannelWorker

needs_ffmpeg = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")


# ---- lanes (pure python) -----------------------------------------------------------------------
def test_priority_pool_runs_best_first_and_bounds_concurrency():
    pool, gate, order = PriorityPool(1, "t"), threading.Event(), []
    pool.submit(1, gate.wait)                                  # occupies the only worker
    time.sleep(0.2)
    for score in (30, 90, 60, 90):
        pool.submit(score, order.append, score)
    assert pool.running == 1 and pool.waiting == 4
    gate.set()
    pool.shutdown(wait=True)
    assert order == [90, 90, 60, 30]                           # best moment first, first come first served among equals
    assert pool.waiting == 0 and pool.running == 0
    assert pool.submit(5, order.append, 5) is False            # closed pools refuse work instead of hiding it


def test_priority_pool_cancel_queued_keeps_running_job():
    pool, gate, done = PriorityPool(1, "t"), threading.Event(), []
    pool.submit(9, lambda: (gate.wait(), done.append("running")))
    time.sleep(0.2)
    for i in range(3):
        pool.submit(1, done.append, f"queued{i}")
    threading.Timer(0.3, gate.set).start()
    assert pool.shutdown(wait=True, cancel_queued=True) == 3
    assert done == ["running"]                                 # the render in progress is finished, the waiting ones are skipped


def test_priority_pool_survives_a_failing_job():
    pool, out = PriorityPool(1, "t"), []
    pool.submit(2, lambda: 1 / 0)
    pool.submit(1, out.append, "after")
    pool.shutdown(wait=True)
    assert out == ["after"]


def test_drafts_never_wait_behind_encodes():
    """The bug this guards: with one shared pool, 2 long encodes made every other stream's draft wait until the buffer was gone."""
    lanes, gate = Lanes(Config(cut_workers=1, draft_workers=2)), threading.Event()
    lanes.encodes.submit(50, gate.wait)                        # the encode lane is stuck on a long render
    lanes.encodes.submit(60, gate.wait)
    t = time.time()
    futs = [lanes.drafts.submit(lambda i=i: i) for i in range(5)]
    assert [f.result(timeout=2) for f in futs] == list(range(5))
    assert time.time() - t < 1.5
    gate.set()
    lanes.shutdown()


# ---- recorder ----------------------------------------------------------------------------------
def test_recorder_reads_only_new_complete_lines(tmp_path):
    csv = tmp_path / "list.csv"
    csv.write_text("seg_000000.ts,0.000000,4.000000\nseg_000001.ts,4.000000,8.0")      # last line still being written
    rows, off = Recorder._read_new(csv, 0)
    assert rows == [("seg_000000.ts", 0.0, 4.0)]               # the half-written line is NOT misread as 8.0 -> 8
    with open(csv, "a") as f:
        f.write("00000\nseg_000002.ts,8.000000,12.000000\n")
    rows, off2 = Recorder._read_new(csv, off)
    assert rows == [("seg_000001.ts", 4.0, 8.0), ("seg_000002.ts", 8.0, 12.0)]
    assert Recorder._read_new(csv, off2) == ([], off2)         # nothing new: nothing re-parsed
    assert Recorder._read_new(tmp_path / "missing.csv", 0) == ([], 0)


# ---- cli ---------------------------------------------------------------------------------------
def test_plan_targets_dedupes_and_validates(capsys):
    got = plan_targets(["Shroud", "https://www.twitch.tv/shroud", "xqc", "https://www.youtube.com/watch?v=abcdefghijk"])
    assert got == ["Shroud", "xqc", "https://www.youtube.com/watch?v=abcdefghijk"]
    assert "same stream" in capsys.readouterr().out
    with pytest.raises(ValueError):
        plan_targets(["shroud", "not a channel!!"])           # reported before anything starts


def test_resource_notes_only_for_several_streams(tmp_path):
    cfg = Config(buffer_dir=str(tmp_path))
    assert resource_notes(cfg, 1) == []
    notes = resource_notes(cfg, 4)
    assert "4 streams" in notes[0] and "2.8 GB" in notes[0]


def test_clips_gb_is_cached(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"0" * 1000)
    first = clips_gb(tmp_path, max_age=60)
    (tmp_path / "b.bin").write_bytes(b"0" * 5_000_000)
    assert clips_gb(tmp_path, max_age=60) == first             # no second folder walk within the window
    assert clips_gb(tmp_path, max_age=0) > first


# ---- render cost -------------------------------------------------------------------------------
def test_fps_is_applied_before_the_expensive_layout_graph():
    cfg = Config()
    for layout in (None, {"mode": "crop"}, {"mode": "blur"},
                   {"mode": "split", "facecam": {"x": .7, "y": .05, "w": .25, "h": .3}, "game_fit": "blur"}):
        args = _filters(cfg, True, "x.ass", layout)
        graph = args[args.index("-filter_complex") + 1]
        assert graph.startswith("[0:v]fps=30[f];[f]") and graph.count("[0:v]") == 1
        assert graph.endswith("[v]ass=x.ass[vo]")              # captions still drawn last, on the finished frame
    assert _filters(Config(out_fps=0), True, None, {"mode": "crop"})[1].startswith("[0:v]scale")     # 0 = keep the source rate
    assert _filters(cfg, False, None) == ["-vf", "fps=30"]


@needs_ffmpeg
def test_vertical_export_of_a_60fps_source_comes_out_30fps_1080x1920(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=60:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    for layout in (None, {"mode": "crop"}):
        out = tmp_path / f"o_{(layout or {}).get('mode', 'blur')}.mp4"
        assert export_clip(src, out, 0.5, 3.5, Config(preset="ultrafast"), vertical=True, layout=layout)
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,avg_frame_rate",
                               "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
        assert info == "1080,1920,30/1"
        assert abs(probe_duration(out) - 3.0) < 0.3


# ---- several real streams through the whole pipeline ---------------------------------------------
def _source():
    return [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", "pipe:1"]


class FakeChat(threading.Thread):
    """Quiet chat, then a burst of 40 different viewers 12-15 s after start."""

    def __init__(self, sink, stop, seed):
        super().__init__(daemon=True)
        self.sink, self.stop, self.seed, self.messages = sink, stop, seed, 0

    def run(self):
        t0, rnd = time.time(), random.Random(self.seed)
        while not self.stop.is_set():
            hot = 12 <= time.time() - t0 < 15
            for _ in range(rnd.randint(20, 30) if hot else rnd.randint(0, 1)):
                user = f"viewer{rnd.randint(1, 40) if hot else rnd.randint(1, 3)}"
                self.sink(ChatEvent(t=time.time(), user=user, text=rnd.choice(["KEKW", "WTF", "POG"]) if hot else "hello"))
                self.messages += 1
            self.stop.wait(1.0)


def _cfg(tmp_path, **kw):
    base = dict(warmup_s=8, baseline_s=30, pre_s=6, post_s=3, hold_s=3, merge_gap_s=4, window_s=6, segment_s=1, min_users=5,
                chat_grace_s=1.0, audio_assist=False, buffer_dir=str(tmp_path / "buf"), cut_workers=1, vertical="auto",
                preset="ultrafast")
    base.update(kw)
    return Config(**base)


def _wait(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False


@needs_ffmpeg
def test_three_streams_spike_together_every_clip_is_saved_while_encodes_are_busy(tmp_path, monkeypatch):
    gate = threading.Event()
    started = []

    def slow_export(src, out, start, end, cfg, vertical=False, ass_text=None, layout=None, errors=None, **kw):
        started.append(out.name)
        gate.wait(120)                                           # a long 9:16 render that holds the (single) encode slot
        out.write_bytes(b"fake")
        return out

    monkeypatch.setattr(worker_mod, "export_clip", slow_export)
    cfg, out, stop = _cfg(tmp_path), tmp_path / "clips", threading.Event()
    lanes = Lanes(cfg)
    ws = [ChannelWorker(cfg, f"chan{i}", out, stop, source_cmd=_source(), lanes=lanes,
                        chat_factory=lambda sink, i=i: FakeChat(sink, stop, i)) for i in range(3)]
    try:
        for w in ws:
            w.start()
        # all three streams spiked at once; ONE encode slot is stuck, yet every stream still gets its draft clip
        assert _wait(lambda: all(w.clips_made == 1 for w in ws), 90), [w.stats for w in ws]
        assert _wait(lambda: lanes.encodes.running == 1, 10)
        assert lanes.encodes.waiting == 2                        # the other two renders are queued, not lost
        metas = sorted(out.rglob("*.clip.json"))
        assert len(metas) == 3 and all((m.parent / json.loads(m.read_text(encoding="utf-8"))["file"]).exists() for m in metas)
        # you press Keep in the review page while your clip's render is still queued ...
        victim = metas[0]
        data = json.loads(victim.read_text(encoding="utf-8"))
        data["status"] = "keep"
        victim.write_text(json.dumps(data), encoding="utf-8")
        gate.set()
        assert _wait(lambda: sum(w.stats["verticals"] for w in ws) == 3, 30), [w.stats for w in ws]
        # ... and the render finishing must not undo it, and must add its export to the record
        after = json.loads(victim.read_text(encoding="utf-8"))
        assert after["status"] == "keep" and len(after["exports"]) == 1 and after["exports"][0]["vertical"] is True
        from autoclip import autopilot
        fresh = json.loads(metas[1].read_text(encoding="utf-8"))
        assert not autopilot._touched(fresh), "a clip nobody touched must not count as yours (or the score rules never trash)"
        assert "streams running" in status_block(ws, lanes, cfg)
    finally:
        gate.set()
        stop.set()
        for w in ws:
            w.join(timeout=60)
        lanes.shutdown()
    assert not any((tmp_path / "buf").iterdir()), "buffers must be deleted on stop"
    from clipfactory import ffmpeg_leaked
    assert not ffmpeg_leaked("testsrc2=size=320x180"), "ffmpeg leaked"


@needs_ffmpeg
def test_ctrl_c_while_a_spike_waits_for_its_post_roll_still_saves_the_clip(tmp_path):
    cfg, out, stop = _cfg(tmp_path, post_s=25, vertical="on_demand"), tmp_path / "clips", threading.Event()
    w = ChannelWorker(cfg, "solo", out, stop, source_cmd=_source(), chat_factory=lambda sink: FakeChat(sink, stop, 7))
    w.start()
    assert _wait(lambda: bool(w.pending), 90), "spike never detected"
    assert w.clips_made == 0                                     # still waiting for the 25 s of video after the moment
    stop.set()
    w.join(timeout=60)
    assert w.clips_made == 1 and len(list(out.rglob("*.clip.json"))) == 1       # used to be dropped
    assert not any((tmp_path / "buf").iterdir())                 # buffer deleted AFTER the cut, not before


def test_a_crashing_second_does_not_end_the_stream(tmp_path, monkeypatch):
    cfg, stop = _cfg(tmp_path), threading.Event()
    w = ChannelWorker(cfg, "flaky", tmp_path / "c", stop, source_cmd=["python3", "-c", "import time; time.sleep(30)"],
                      chat_factory=lambda sink: FakeChat(sink, stop, 1))
    calls = []
    real = w._tick

    def tick():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        real()

    monkeypatch.setattr(w, "_tick", tick)
    w.start()
    assert _wait(lambda: len(calls) >= 4, 15)                    # kept going after the exception
    assert w.is_alive()
    stop.set()
    w.join(timeout=30)
    assert not w.is_alive()
