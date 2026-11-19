"""A paused stream (Twitch ad break) is not a dead stream: no restart loop, and streamlink's own words are kept for diagnosis."""
from autoclip.config import Config
from autoclip.recorder import ad_break_announced, last_words, should_restart, stream_opened, streamlink_cmd, streamlink_tail

CFG = Config()                                    # stall_s 45, ad_stall_s 240


def test_normal_stall_restarts_after_45_seconds():
    assert not should_restart(30, True, False, CFG)
    assert should_restart(50, True, False, CFG)


def test_ad_break_gets_patience_but_not_forever():
    assert not should_restart(50, True, True, CFG)       # paused for an ad: wait, do not start a new session (new pre-roll ad)
    assert not should_restart(200, True, True, CFG)
    assert should_restart(300, True, True, CFG)           # an "ad break" that never ends is a dead stream after all


def test_capture_that_never_produced_video():
    assert not should_restart(80, False, False, CFG)
    assert should_restart(100, False, False, CFG)
    assert not should_restart(150, False, True, CFG)      # pre-roll ad on a fresh session
    assert should_restart(260, False, True, CFG)


def test_streamlink_messages_are_read_for_ad_breaks_and_quoted(tmp_path):
    p = tmp_path / "source.log"
    assert not ad_break_announced(p) and last_words(p) == ""                    # no log yet: no crash
    p.write_text("[cli][info] Opening stream: 1080p60 (hls)\n[plugins.twitch][info] Detected advertisement break of 30 seconds\n")
    assert ad_break_announced(p)
    assert last_words(p).endswith("advertisement break of 30 seconds")
    p.write_text("[cli][info] Opening stream: 1080p60 (hls)\n[stream.hls][warning] Failed to reload playlist: Read timed out\n")
    assert not ad_break_announced(p) and "Read timed out" in last_words(p)
    p.write_text("[cli][info] Opening stream: 1080p60 (hls)\n[plugins.twitch][info] Will skip ad segments\n")      # the real streamlink 8.6.3 wording
    assert ad_break_announced(p)
    p.write_text("[cli][info] Loading a playlist; downloads and uploads fine\n")                                    # "ads" inside other words: no
    assert not ad_break_announced(p)
    p.write_text("x" * 50000 + "\n[twitch][info] Waiting for pre-roll ads to finish, be patient\n")
    assert ad_break_announced(p) and len(streamlink_tail(p)) <= 4000


def test_an_opened_stream_without_video_counts_as_a_pre_roll_ad(tmp_path):
    p = tmp_path / "source.log"
    p.write_text("[cli][info] Found matching plugin twitch for URL x\n")
    assert not stream_opened(p)                                   # stuck before the stream even opened: a real hang, restart is right
    p.write_text("[cli][info] Opening stream: 1080p60 (hls)\n")
    assert stream_opened(p) and not ad_break_announced(p)         # opened, no ad notice (yet): still an ad until proven otherwise


def test_streamlink_logs_at_info_level_so_ad_notices_are_not_lost():
    assert streamlink_cmd("https://twitch.tv/x", "best")[2:4] == ["--loglevel", "info"]


# ---- the real recorder loop against a source that pauses ---------------------------------------------------------
import subprocess
import sys
import threading
import time

import pytest

from autoclip.recorder import Recorder, ffmpeg_exe

needs_ffmpeg = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")

SOURCE = r"""
import subprocess, sys, time
ad = sys.argv[2] == "ad"
def burst():
    subprocess.run([sys.argv[1], "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25", "-t", "7",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-f", "mpegts", "pipe:1"], check=True)
burst()
sys.stdout.flush()
if ad:
    sys.stderr.write("[plugins.twitch][info] Detected advertisement break of 6 seconds\n"); sys.stderr.flush()
time.sleep(6)
burst()
time.sleep(30)
"""


def run_recorder(tmp_path, mode, seconds):
    stop = threading.Event()
    cfg = Config(buffer_dir=str(tmp_path), segment_s=1, stall_s=2, ad_stall_s=12)
    rec = Recorder(cfg, "x", stop, "t", source_cmd=[sys.executable, "-c", SOURCE, ffmpeg_exe(), mode])
    rec.defer_cleanup = True
    rec.start()
    saw_ad = False
    t0 = time.time()
    while time.time() - t0 < seconds:
        saw_ad = saw_ad or rec.paused_for_ad
        time.sleep(0.2)
    stop.set()
    rec.join(20)
    runs = [d for d in rec.root.glob("run_*") if d.is_dir()]
    segs = rec.segments_between(0, time.time() + 1)
    rec.cleanup()
    return runs, segs, saw_ad


@needs_ffmpeg
def test_ad_break_is_waited_out_instead_of_restarting_in_a_loop(tmp_path, caplog):
    caplog.set_level("INFO")
    runs, segs, saw_ad = run_recorder(tmp_path, "ad", 22)
    assert len(runs) == 1, "a paused (ad break) stream must keep its capture, not start a new session"
    assert saw_ad and "waiting for it to end" in caplog.text
    assert "restarting capture" not in caplog.text
    assert len(segs) >= 8                                   # video from BEFORE and AFTER the pause was recorded
    assert max(s.t1 for s in segs) - min(s.t0 for s in segs) > 10       # wall-clock stamps span the pause honestly


@needs_ffmpeg
def test_silence_without_an_ad_break_still_restarts_the_capture(tmp_path, caplog):
    caplog.set_level("INFO")
    runs, segs, saw_ad = run_recorder(tmp_path, "silent", 14)
    assert not saw_ad                                       # no ad was announced...
    assert "no new video for" in caplog.text and "restarting capture" in caplog.text     # ...so a dead stream is replaced as before
    assert "stream interrupted; reconnecting" in caplog.text


# ---- "video: waiting" must explain itself ---------------------------------------------------------------------------
import os
import stat

from autoclip import cli


def test_diagnose_stream_says_what_it_means():
    ok = cli.diagnose_stream("[cli][info] Opening stream: 1080p60 (hls)", 5_000_000, 20)
    assert ok[0].startswith("OK: video arrives") and len(ok) == 1
    off = cli.diagnose_stream("error: No playable streams found on this URL: https://twitch.tv/x", 0, 20)
    assert off[0] == "NO VIDEO arrived." and any("offline" in x for x in off)
    net = cli.diagnose_stream("[stream.hls][error] Unable to open URL: HTTPSConnectionPool: Read timed out", 0, 20)
    assert any("Network problem" in x and "1.1.1.1" in x for x in net)
    ad = cli.diagnose_stream("[plugins.twitch][info] Will skip ad segments", 0, 20)             # what a real Kai Cenat check printed
    assert any("AD BREAK" in x and "--seconds 120" in x for x in ad)
    late = cli.diagnose_stream("[plugins.twitch][info] Will skip ad segments", 5_000_000, 45, first_video_s=31)
    assert late[0].startswith("OK") and "31 s" in late[1] and "ad" in late[1]
    assert any("403 Client Error: Forbidden" and "refuses access" in x for x in cli.diagnose_stream("403 Client Error: Forbidden for url", 0, 20))
    stuck = cli.diagnose_stream("[cli][info] Found matching plugin twitch for URL https://www.twitch.tv/ishowspeed", 0, 20)   # a real check
    assert any("STUCK" in x and "--seconds 60" in x and "gql.twitch.tv" in x for x in stuck)
    listed = cli.diagnose_stream("[cli][info] Found matching plugin twitch for URL x\n[cli][info] Available streams: 720p60, 1080p60 (best)", 0, 20)
    assert not any("STUCK" in x for x in listed)                    # got as far as the stream list: a different problem
    nothing = cli.diagnose_stream("", 0, 20)
    assert "No clear reason" in " ".join(nothing) and any("winget upgrade Streamlink" in x for x in nothing)


def fake_streamlink(tmp_path, body):
    exe = tmp_path / "bin" / "streamlink"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe.parent


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake streamlink")
def test_check_command_reports_working_and_broken_channels(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    good = fake_streamlink(tmp_path, '[ "$1" = "--version" ] && { echo "streamlink 9.9.9"; exit 0; }\nhead -c 3000000 /dev/zero\n')
    monkeypatch.setenv("PATH", f"{good}{os.pathsep}{os.environ['PATH']}")
    assert cli.main(["check", "kaicenat", "--seconds", "5", "--out", str(tmp_path / "c")]) == 0
    out = capsys.readouterr().out
    assert "streamlink 9.9.9" in out and "OK: video arrives" in out

    bad = fake_streamlink(tmp_path / "b", '[ "$1" = "--version" ] && { echo "streamlink 1.0.0"; exit 0; }\n'
                                          'echo "error: No playable streams found on this URL" >&2\nexit 1\n')
    monkeypatch.setenv("PATH", f"{bad}{os.pathsep}{os.environ['PATH']}")
    assert cli.main(["check", "kaicenat", "--seconds", "5", "--out", str(tmp_path / "c")]) == 1
    out = capsys.readouterr().out
    assert "No playable streams" in out and "NO VIDEO arrived." in out and "offline" in out


def test_check_command_rejects_nonsense_target(capsys):
    assert cli.main(["check", "not a channel!!"]) == 2


@needs_ffmpeg
def test_a_capture_that_dies_at_once_logs_what_streamlink_said(tmp_path, caplog):
    caplog.set_level("INFO")
    script = "import sys; sys.stderr.write('error: Unable to open URL: Read timed out\\n'); sys.exit(1)"
    rec = Recorder(Config(buffer_dir=str(tmp_path), segment_s=1), "x", threading.Event(), "t", source_cmd=[sys.executable, "-c", script])
    rec.root.mkdir(parents=True)
    assert rec._run_once() == 0
    assert "capture ended without any video (streamlink said: error: Unable to open URL: Read timed out)" in caplog.text
    assert list(rec.root.iterdir()) == []                        # and still no leftover folder


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake streamlink")
def test_check_waits_out_an_ad_break_and_reports_how_long_it_took(tmp_path, monkeypatch, capsys):
    """The real case: 'Will skip ad segments', no video for a while, then video. A fixed 20 s test called this broken."""
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    exe = fake_streamlink(tmp_path, '[ "$1" = "--version" ] && { echo "streamlink 8.6.3"; exit 0; }\n'
                                    'echo "[plugins.twitch][info] Will skip ad segments" >&2\nsleep 4\nhead -c 3000000 /dev/zero\n')
    monkeypatch.setenv("PATH", f"{exe}{os.pathsep}{os.environ['PATH']}")
    assert cli.main(["check", "kaicenat", "--seconds", "1", "--out", str(tmp_path / "c")]) == 0      # asked for 1 s, waited the ad out
    out = capsys.readouterr().out
    assert "Twitch is showing an ad first" in out and "first video after" in out and "OK: video arrives" in out


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake streamlink")
def test_check_debug_asks_streamlink_for_its_detailed_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    exe = fake_streamlink(tmp_path, '[ "$1" = "--version" ] && { echo "streamlink 8.6.3"; exit 0; }\n'
                                    'echo "args: $*" >&2\necho "[cli][info] Found matching plugin twitch for URL x" >&2\nsleep 3\n')
    monkeypatch.setenv("PATH", f"{exe}{os.pathsep}{os.environ['PATH']}")
    assert cli.main(["check", "ishowspeed", "--seconds", "1", "--debug", "--out", str(tmp_path / "c")]) == 1
    out = capsys.readouterr().out
    assert "--loglevel debug" in out and "STUCK" in out and "Test-NetConnection gql.twitch.tv" in out


SILENT_OPEN = "import sys, time; sys.stderr.write('[cli][info] Opening stream: 1080p60 (hls)\\n'); sys.stderr.flush(); time.sleep(60)"


@needs_ffmpeg
def test_opened_stream_with_no_video_is_not_abandoned_after_the_start_wait(tmp_path, caplog, monkeypatch):
    import autoclip.recorder as r
    monkeypatch.setattr(r, "START_WAIT_S", 2)
    caplog.set_level("INFO")
    stop = threading.Event()
    rec = Recorder(Config(buffer_dir=str(tmp_path), segment_s=1, stall_s=2, ad_stall_s=60), "x", stop, "t", source_cmd=[sys.executable, "-c", SILENT_OPEN])
    rec.defer_cleanup = True
    rec.start()
    time.sleep(6)
    stop.set(); rec.join(20); rec.cleanup()
    assert "most likely the pre-roll ad" in caplog.text
    assert "capture ended without any video" not in caplog.text.split("most likely the pre-roll ad")[0]
