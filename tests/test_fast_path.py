import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from autoclip.captions import build_ass
from autoclip.config import Config
from autoclip.cutter import cut_draft, draft_offset, export_clip, probe_duration
from autoclip.recorder import Segment
from test_pipeline import make_segments

pytestmark = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")
WORDS = [("this", 1.0, 1.3), ("is", 1.3, 1.5), ("insane", 1.5, 2.2), ("no", 4.0, 4.2), ("way", 4.2, 4.6)]


def test_draft_is_instant_lossless_and_gap_aware(tmp_path):
    segs = make_segments(tmp_path, n=10)
    out = tmp_path / "d.mp4"
    t = time.perf_counter()
    res = cut_draft(segs, 1003.0, 1017.0, out)
    took = time.perf_counter() - t
    sel, d0 = res
    assert took < 3 and out.exists()                                  # stream copy: no encoding
    assert d0 == 1002.0 and len(sel) == 8                              # snapped out to whole segments
    assert abs(probe_duration(out) - 16.0) < 0.6
    assert draft_offset(sel, 1005.0) == pytest.approx(3.0)
    # an ad-break gap between segments must not shift later offsets
    gappy = [Segment(sel[0].path, 1000.0, 1002.0), Segment(sel[1].path, 1010.0, 1012.0)]
    assert draft_offset(gappy, 1011.0) == pytest.approx(3.0)
    assert draft_offset(gappy, 1005.0) == pytest.approx(2.0)           # inside the gap -> end of previous piece


def test_draft_refuses_uncovered_window(tmp_path):
    segs = make_segments(tmp_path, n=3)
    assert cut_draft(segs, 2000.0, 2030.0, tmp_path / "x.mp4") is None


def test_short_buffer_still_gives_a_clip_when_it_holds_enough_video(tmp_path):
    segs = make_segments(tmp_path, n=12)                               # 24 s of video (1000-1024) ...
    wide = (1000.0 - 60, 1024.0 + 20)                                  # ... for a 104 s window: 23% covered, the old rule skipped it
    out = tmp_path / "p.mp4"
    assert cut_draft(segs, *wide, out) is not None and out.exists()
    assert cut_draft(segs, *wide, tmp_path / "q.mp4", min_s=40) is None    # below the minimum: still skipped
    assert cut_draft(make_segments(tmp_path / "s", n=2) if (tmp_path / "s").mkdir() is None else None, 900.0, 1100.0, tmp_path / "r.mp4") is None  # 8 s: too little


def _draft(tmp_path):
    out = tmp_path / "d.mp4"
    cut_draft(make_segments(tmp_path, n=10), 1000.0, 1020.0, out)
    return out


def size(p):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
                          capture_output=True, text=True).stdout.strip()


def psnr(a, b, at):
    """Similarity of the frame at `at` seconds in two videos (dB). Identical ~ 50+, a drawn caption drops it a lot."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(at), "-i", str(a), "-ss", str(at), "-i", str(b),
                        "-lavfi", "psnr", "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True)
    return float(r.stderr.split("average:")[1].split()[0])


def test_export_trim_vertical_and_captions_in_one_pass(tmp_path):
    d, cfg = _draft(tmp_path), Config(preset="ultrafast")
    plain = tmp_path / "plain.mp4"
    assert export_clip(d, plain, 2.0, 12.5, cfg)
    assert abs(probe_duration(plain) - 10.5) < 0.3                     # frame-accurate trim, not segment-snapped
    vert, cc = tmp_path / "v.mp4", tmp_path / "cc.mp4"
    assert export_clip(d, vert, 2.0, 12.5, cfg, vertical=True) and size(vert) == "1080,1920"
    assert export_clip(d, cc, 0.0, 6.0, cfg, vertical=True, ass_text=build_ass(WORDS, 0.0, 6.0, True))
    assert size(cc) == "1080,1920"
    # captions really are drawn: frame at 1.7s (word on screen) differs from the same frame without captions
    nocc = tmp_path / "nocc.mp4"
    assert export_clip(d, nocc, 0.0, 6.0, cfg, vertical=True)
    assert psnr(cc, nocc, 1.7) < 30                                     # word on screen: frames clearly differ
    assert psnr(cc, nocc, 3.2) > 38                                     # nobody speaking: no caption drawn


def test_export_works_with_awkward_folder_names(tmp_path):
    d = tmp_path / "C colon it's here"
    d.mkdir()
    src = _draft(d)
    assert export_clip(src, d / "o.mp4", 1, 5, Config(preset="ultrafast"), True, build_ass(WORDS, 1, 5, True))


def test_build_ass_shifts_groups_and_highlights():
    ass = build_ass(WORDS, 1.0, 5.0, vertical=True)
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert len(lines) == 5 and "PlayResX: 1080" in ass
    assert lines[0].startswith("Dialogue: 0,0:00:00.00")                # shifted so the trim start is time 0
    assert "{\\c&H00FFFF&}THIS{\\c&HFFFFFF&} IS INSANE" in lines[0]    # current word highlighted, 3-word group
    assert "NO" in lines[3] and "THIS" not in lines[3]                  # pause starts a new group
    assert build_ass([("{evil}\\n", 0, 1)], 0, 2, False).count("{evil}") == 0   # override tags cannot be injected


def test_review_store_and_http_export(tmp_path, monkeypatch):
    from autoclip import review
    d = _draft(tmp_path)
    meta = {"id": "ch/s/c1", "file": "d.mp4", "channel": "ch", "status": "new", "score": 80, "duration_s": probe_duration(d),
            "moment_s": 8, "suggest_start_s": 2, "suggest_end_s": 12, "exports": [], "created": "2026-01-01T00:00:00",
            "source_url": "x", "spike": {"peak": 30, "baseline": 3, "msgs": 99, "top": []}}
    (tmp_path / "d.clip.json").write_text(json.dumps(meta))
    cfg = Config(preset="ultrafast")
    store = review.Store(tmp_path, cfg)
    srv = review.ThreadingHTTPServer(("127.0.0.1", 0), review.make_handler(store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    code, body = post("/api/export", {"id": "ch/s/c1", "start": 2, "end": 9, "vertical": True, "captions": False})
    assert code == 200 and (tmp_path / json.loads(body)["file"]).exists()
    assert post("/api/export", {"id": "ch/s/c1", "start": 9, "end": 2})[0] == 400          # reversed trim
    assert post("/api/export", {"id": "nope", "start": 1, "end": 2})[0] == 400
    assert post("/api/export", {"id": "ch/s/c1", "start": 0, "end": 5000})[0] == 400       # longer than the draft
    code, msg = post("/api/export", {"id": "ch/s/c1", "start": 1, "end": 6, "captions": True})
    import importlib.util
    assert code == 400 and ("faster-whisper" in msg if importlib.util.find_spec("faster_whisper") is None
                            else "speech" in msg)                                            # clear message, no crash
    monkeypatch.setattr(review, "transcribe", lambda p, c, a=None, b=None: WORDS)                           # pretend whisper ran
    code, body = post("/api/export", {"id": "ch/s/c1", "start": 0, "end": 6, "vertical": True, "captions": True})
    assert code == 200 and "_cc" in body
    exports = json.loads((tmp_path / "d.clip.json").read_text())["exports"]
    assert len(exports) == 2
    # trash + purge removes drafts AND exports
    store.set_status("ch/s/c1", "trash")
    store.purge()
    assert not list(tmp_path.glob("*.mp4")) and not (tmp_path / "d.clip.json").exists()
    srv.shutdown()


def test_duplicate_clip_detection():
    from autoclip.worker import ChannelWorker
    w = ChannelWorker.__new__(ChannelWorker)
    w.cfg, w.recent = Config(), [(1000.0, 1100.0)]
    assert w._duplicate(1020.0, 1110.0)          # >60% overlap -> skip
    assert not w._duplicate(1085.0, 1200.0)      # follow-on clip -> keep


def test_suggested_window_is_always_exactly_30s_and_contains_the_reaction():
    from autoclip.detector import Spike
    from autoclip.worker import fit_window, suggest_window
    cfg = Config()

    def sp(onset, peak, end):
        return Spike(start=onset - 9, end=end, onset=onset, peak_t=peak, peak=50, baseline=5, msgs=100, users=50, hype_frac=.5)

    a, b = suggest_window(sp(1000, 1008, 1015), cfg)
    assert b - a == 30 and a == 1000 - 10 - 5                             # 15 s lead-in before the chat onset
    assert a <= 1000 and b >= 1008 + 5                                    # moment and reaction both inside
    a, b = suggest_window(sp(1000, 1055, 1060), cfg)                      # long spike, peak late: window slides to the peak
    assert b - a == 30 and a <= 1055 <= b - 5
    assert fit_window(-5.0, 40.0, 30) == (0.0, 30.0)                      # clamp to the draft start
    assert fit_window(25.0, 40.0, 30) == (10.0, 40.0)                     # clamp to the draft end, still 30 s
    assert fit_window(2.0, 18.0, 30) == (0.0, 18.0)                       # short draft: use all of it


def test_transcribe_gives_whisper_decoded_audio_not_a_file_path(tmp_path, monkeypatch):
    """Regression: faster-whisper decoding a path via PyAV crashed with "unexpected keyword argument 'metadata_errors'"
    on some installs. We must hand it a ready float32 array (decoded by ffmpeg) so PyAV is never involved."""
    import sys
    import types
    import numpy as np
    from autoclip import captions
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)], check=True)
    seen = {}

    class Word:
        def __init__(self, w, a, b):
            self.word, self.start, self.end = w, a, b

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, **kw):
            seen["audio"], seen["kw"] = audio, kw
            seg = types.SimpleNamespace(words=[Word(" hello", 0.5, 0.9), Word(" world", 1.0, 1.4)])
            return [seg], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    captions._MODEL.clear()
    words = captions.transcribe(clip, Config())
    assert words == [("hello", 0.5, 0.9), ("world", 1.0, 1.4)]
    assert seen["kw"]["vad_filter"] is True
    assert isinstance(seen["audio"], np.ndarray) and seen["audio"].dtype == np.float32     # array, not a path
    assert abs(len(seen["audio"]) - 3 * 16000) < 1600                                      # 3 s at 16 kHz
    assert seen["kw"]["word_timestamps"] is True
    assert clip.with_suffix(".words.json").exists()                                        # cached for the next export


def test_transcribe_clear_error_for_clip_without_audio(tmp_path, monkeypatch):
    import sys
    import types
    from autoclip import captions
    clip = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2", "-c:v", "libx264", str(clip)], check=True)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=lambda *a, **k: object()))
    captions._MODEL.clear()
    with pytest.raises(captions.CaptionsUnavailable, match="no audio"):
        captions.transcribe(clip, Config())


def test_server_ignores_browser_aborts_and_serves_ranges_correctly(tmp_path, capfd):
    """WinError 10053 regression: a browser that cancels a video download must not spam tracebacks or break the server."""
    import socket
    from autoclip import review
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    big = d / "c.mp4"
    big.write_bytes(bytes(range(256)) * 40000)                      # ~10 MB
    srv = review.QuietServer(("127.0.0.1", 0), review.make_handler(review.Store(tmp_path, Config())))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    for _ in range(5):                                              # ask for the whole file, read a bit, then slam the door (RST)
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"GET /files/ch/s/c.mp4 HTTP/1.1\r\nHost: x\r\n\r\n")
        s.recv(1000)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()
    time.sleep(0.5)

    def get(rng=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/files/ch/s/c.mp4", headers={"Range": rng} if rng else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, b"", e.headers

    data = big.read_bytes()
    code, body, _ = get()
    assert code == 200 and body == data                              # server still fine after the aborts, file intact
    code, body, h = get("bytes=100-199")
    assert code == 206 and body == data[100:200] and h["Content-Range"] == f"bytes 100-199/{len(data)}"
    code, body, _ = get("bytes=-500")
    assert code == 206 and body == data[-500:]                       # suffix range = LAST 500 bytes (used to be wrong)
    code, body, _ = get(f"bytes={len(data)}-")
    assert code == 416                                                # past the end
    assert get("bytes=abc-")[0] == 200                               # garbage Range header: just send the file
    srv.shutdown()
    out = capfd.readouterr()
    assert "Traceback" not in out.err and "10053" not in out.err and "Broken pipe" not in out.err


def test_export_survives_a_locked_output_file(tmp_path, monkeypatch):
    """Windows: the browser is playing the previous export, so the file name is locked (WinError 32). The export must still succeed."""
    import os
    from autoclip import cutter
    src = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "3", "-c:v", "libx264", str(src)], check=True)
    out = tmp_path / "clip_v.mp4"
    out.write_bytes(b"old export that a browser has open")
    real, locked = os.replace, {out}

    def replace(a, b):
        if Path(b) in locked:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(a, b)
    monkeypatch.setattr(cutter.os, "replace", replace)
    monkeypatch.setattr(cutter.time, "sleep", lambda s: None)
    placed = cutter.export_clip(src, out, 0, 2, Config(preset="ultrafast"))
    assert placed == tmp_path / "clip_v_2.mp4" and placed.stat().st_size > 1000
    assert out.read_bytes() == b"old export that a browser has open"            # the locked file is untouched
    assert not list(tmp_path.glob("*.part.mp4"))                                  # no temp leftovers
    locked.add(placed)                                                            # everything locked: a clear error, not a crash
    errs = []
    monkeypatch.setattr(cutter.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(32, "busy")))
    assert cutter.export_clip(src, out, 0, 2, Config(preset="ultrafast"), errors=errs) is False
    assert "could not save" in errs[0] and not list(tmp_path.glob("*.part.mp4"))


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc to list open files")
def test_streaming_a_video_never_keeps_the_file_open(tmp_path):
    """A browser that pauses mid-download used to keep the file open, which on Windows blocks replacing it (WinError 32)."""
    import os
    import socket
    from autoclip import review
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    big = d / "big.mp4"
    big.write_bytes(b"\0" * (24 * 1024 * 1024))
    srv = review.QuietServer(("127.0.0.1", 0), review.make_handler(review.Store(tmp_path, Config())))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = socket.create_connection(srv.server_address)
    s.sendall(b"GET /files/ch/s/big.mp4 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert len(s.recv(2000)) > 0                                   # the download started; now the "browser" stops reading
    time.sleep(0.5)                                                # the server thread is blocked writing into a full socket
    open_files = [os.readlink(f"/proc/self/fd/{n}") for n in os.listdir("/proc/self/fd") if os.path.exists(f"/proc/self/fd/{n}")]
    assert str(big.resolve()) not in open_files
    s.close()
    srv.shutdown()
