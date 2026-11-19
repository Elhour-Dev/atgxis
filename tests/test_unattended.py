"""Leaving it running all day: disk guards, leftovers of unclean exits, survival of odd errors, status you can read afterwards."""
import os
import subprocess
import sys
import threading
import time

import pytest

from autoclip import audio, cli, disk
from autoclip.config import Config
from autoclip.detector import ChatEvent, Spike
from autoclip.lanes import Lanes
from autoclip.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
from autoclip.recorder import Recorder, Segment
from autoclip.worker import ChannelWorker

needs_ffmpeg = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")


def age(path, seconds):
    t = time.time() - seconds
    for f in [path, *path.rglob("*")]:
        os.utime(f, (t, t))


# ---- leftovers of a run that was killed ---------------------------------------------------------------------
def test_sweep_removes_only_dead_autoclip_buffers(tmp_path):
    dead = tmp_path / "autoclip_buf_dead" / "run_1"
    dead.mkdir(parents=True)
    (dead / "seg_000000.ts").write_bytes(b"x" * 1000)
    live = tmp_path / "autoclip_buf_live" / "run_2"
    live.mkdir(parents=True)
    (live / "seg_000000.ts").write_bytes(b"x")                        # written just now: a running stream
    other = tmp_path / "my_own_folder"                                 # not ours: never touched, however old
    other.mkdir()
    (other / "a.txt").write_text("keep")
    age(tmp_path / "autoclip_buf_dead", 3600)
    age(other, 3600)
    assert disk.sweep_stale_buffers(tmp_path) == 1
    assert not (tmp_path / "autoclip_buf_dead").exists()
    assert (live / "seg_000000.ts").exists() and (other / "a.txt").exists()
    assert disk.sweep_stale_buffers(tmp_path / "missing") == 0


def test_free_space_of_a_folder_that_does_not_exist_yet(tmp_path):
    assert disk.free_gb(tmp_path / "not" / "created" / "yet") > 0


# ---- disk guards ---------------------------------------------------------------------------------------------
def _worker(tmp_path, **kw):
    stop = threading.Event()
    cfg = Config(buffer_dir=str(tmp_path / "buf"), audio_assist=False, **kw)
    return ChannelWorker(cfg, "solo", tmp_path / "clips", stop, source_cmd=["x"], chat_factory=lambda s: threading.Thread(), lanes=Lanes(cfg)), cfg


def _spike():
    return Spike(start=100, end=110, onset=102, peak_t=105, peak=40, baseline=3, msgs=100, users=40, hype_frac=.4)


def test_low_disk_stops_cutting_instead_of_filling_the_drive(tmp_path, monkeypatch, caplog):
    w, cfg = _worker(tmp_path, min_free_gb=3.0)
    monkeypatch.setattr("autoclip.worker.free_gb", lambda p: 1.2)
    w._cut(70, 122, _spike())
    assert w.stats["skipped_disk"] == 1 and w.clips_made == 0 and not (tmp_path / "clips").exists()
    assert "only 1.2 GB free" in caplog.text
    monkeypatch.setattr("autoclip.worker.free_gb", lambda p: None)        # cannot read the free space: do not block cutting
    monkeypatch.setattr(w.recorder, "segments_between", lambda a, b: [])
    w._cut(70, 122, _spike())
    assert w.stats["skipped_disk"] == 1                                    # went on to try (and found no video: nothing to cut)
    w.lanes.shutdown()


def test_a_chat_spike_without_any_video_is_not_a_failed_cut(tmp_path, caplog):
    w, _ = _worker(tmp_path)
    caplog.set_level("INFO")
    w._cut(70, 122, _spike())                                              # the buffer is empty: the stream is offline
    assert w.stats["no_video"] == 1 and w.stats["failed_cuts"] == 0 and w.recent == []
    assert "[solo] chat spiked but there is no video" in caplog.text
    w.lanes.shutdown()


def test_full_clips_folder_stops_cutting(tmp_path, monkeypatch):
    w, cfg = _worker(tmp_path, max_clips_gb=1.0)
    monkeypatch.setattr("autoclip.worker.clips_gb", lambda p: 1.5)
    w._cut(70, 122, _spike())
    assert w.stats["skipped_disk"] == 1 and w.clips_made == 0
    w.lanes.shutdown()


def test_duplicate_history_stays_short_and_still_works(tmp_path):
    w, _ = _worker(tmp_path)
    for i in range(500):
        assert w._claim(i * 1000.0, i * 1000.0 + 60)
    assert len(w.recent) == 200
    assert not w._claim(499_000.0 + 10, 499_000.0 + 70)                  # the newest clips are still checked
    w.lanes.shutdown()


def test_status_shows_disk_and_when_the_last_clip_was(tmp_path, monkeypatch):
    w, cfg = _worker(tmp_path, max_clips_gb=10.0, out_dir=str(tmp_path / "clips"))
    w.snapshot = {"label": "solo", "video": "live", "msgs_per_min": 120, "normal": 9.0, "warmup_left": 0, "spikes": 3, "clips": 2,
                  "last_clip": "14:05"}
    assert "(last 14:05)" in w.status_line()
    out = tmp_path / "clips"
    out.mkdir()
    (out / "a.mp4").write_bytes(b"0" * 1000)
    monkeypatch.setattr(disk, "free_gb", lambda p: 120.0)
    monkeypatch.setattr("autoclip.cli.free_gb", lambda p: 120.0)
    assert "clips 0.0/10 GB, 120 GB free" in cli.disk_line(cfg)
    monkeypatch.setattr("autoclip.cli.clips_gb", lambda p: 10.5)
    assert "FULL: NOT CUTTING" in cli.disk_line(cfg)
    monkeypatch.setattr("autoclip.cli.clips_gb", lambda p: 8.5)
    assert "almost full" in cli.disk_line(cfg)
    monkeypatch.setattr("autoclip.cli.clips_gb", lambda p: 1.0)
    monkeypatch.setattr("autoclip.cli.free_gb", lambda p: 2.0)
    assert "LOW DISK" in cli.disk_line(cfg)
    assert "disk:" in cli.status_block([w], w.lanes, cfg)
    w.lanes.shutdown()


# ---- survival -------------------------------------------------------------------------------------------------
def test_audio_meter_survives_an_unexpected_error(tmp_path, monkeypatch):
    got, stop = [], threading.Event()
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return -20.0

    monkeypatch.setattr(audio, "mean_volume", flaky)
    m = audio.AudioMeter(got.append, stop)
    m.start()
    m.submit([Segment(tmp_path / "a.ts", 0, 4), Segment(tmp_path / "b.ts", 4, 8)])
    end = time.time() + 5
    while not got and time.time() < end:
        time.sleep(0.1)
    stop.set()
    m.join(5)
    assert len(got) == 1 and got[0].kind == "audio" and got[0].db == -20.0       # the 2nd segment was still measured


@needs_ffmpeg
def test_an_offline_check_leaves_no_folder_behind(tmp_path):
    stop = threading.Event()
    rec = Recorder(Config(buffer_dir=str(tmp_path), segment_s=1), "http://x", stop, "off", source_cmd=[sys.executable, "-c", "pass"])
    rec.root.mkdir(parents=True)
    assert rec._run_once() == 0                                          # streamlink-style: exits at once, stream is offline
    assert list(rec.root.iterdir()) == []                                # no run_* folder with empty logs per check, all day long


# ---- keep the PC awake ---------------------------------------------------------------------------------------
def test_keep_awake_asks_and_gives_back():
    calls = []
    k = KeepAwake(True, set_state=lambda flags: calls.append(flags) or 1)
    assert k.start() is True
    k.stop()
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]
    k.stop()
    assert len(calls) == 2                                               # stopping twice does nothing more


def test_keep_awake_can_be_off_and_never_breaks_other_systems():
    calls = []
    assert KeepAwake(False, set_state=lambda f: calls.append(f)).start() is False and calls == []
    assert KeepAwake(True, set_state=lambda f: 0).start() is False       # the system refused: reported, not an error
    if sys.platform != "win32":
        assert KeepAwake(True).start() is False                          # no such call on this system: silently does nothing
