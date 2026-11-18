import io
import json
import re
import subprocess
import threading
import urllib.request

import pytest

from autoclip import layout as lay
from autoclip.captions import build_ass, caption_geometry
from autoclip.config import Config
from autoclip.cutter import export_clip
from autoclip.describe import classify, suggest

pytestmark = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")
# Realistic Whisper output: neighbouring words overlap (insane ends 2.38 > next word starts 2.25) - this triggered the jump
OVERLAP = [("this", 1.0, 1.4), ("is", 1.4, 1.6), ("insane", 1.6, 2.38), ("no", 2.25, 2.5), ("way", 2.5, 3.0), ("bro", 3.0, 3.4)]


def events(ass):
    out = []
    for l in ass.splitlines():
        if l.startswith("Dialogue"):
            f = l.split(",", 9)
            t = lambda x: sum(float(p) * m for p, m in zip(x.split(":"), (3600, 60, 1)))
            out.append((t(f[1]), t(f[2]), f[9]))
    return out


def test_captions_never_overlap_in_time_and_all_share_one_position():
    ass = build_ass(OVERLAP, 0.0, 5.0, True)
    ev = events(ass)
    assert len(ev) == 6
    for a, b in zip(ev, ev[1:]):
        assert a[1] <= b[0] + 1e-9, "two captions on screen at once would be stacked and pushed up by the renderer"
    pos = {re.search(r"\\pos\(([\d.]+),([\d.]+)\)", e[2]).groups() for e in ev}
    assert len(pos) == 1                                          # every caption pinned to the same place


def test_caption_box_geometry_and_validation():
    g = caption_geometry(True, False, {"x": 0.1, "y": 0.2, "w": 0.8, "h": 0.1})
    assert (g["cx"], g["cy"]) == (540, 0.25 * 1920) and g["ml"] == 108 and g["mr"] == 108 and g["an"] == 5
    assert g["size"] == int(0.1 * 1920 * 0.6)                      # text height follows box height
    assert "\\pos(540,480)" in build_ass(OVERLAP, 0, 5, True, box={"x": 0.1, "y": 0.2, "w": 0.8, "h": 0.1})
    assert caption_geometry(True, True, None)["cy"] == 960          # split default: on the seam
    assert lay.validate({"mode": "blur", "captions": {"x": 0.1, "y": 0.7, "w": 0.8, "h": 0.1}})["captions"]["y"] == 0.7
    assert lay.validate({"mode": "split", "facecam": {"x": 0.7, "y": 0.6, "w": 0.2, "h": 0.3}, "captions": {"x": 0, "y": 0.5, "w": 1, "h": 0.1}})
    for bad in [{"x": 0.5, "y": 0.5, "w": 0.9, "h": 0.1}, {"x": 0, "y": 0, "w": 0.05, "h": 0.1}, {"x": 0, "y": 0, "w": 0.5, "h": 0.9},
                {"x": "1)", "y": 0, "w": 0.5, "h": 0.1}, {"x": 0}]:
        with pytest.raises(lay.LayoutError):
            lay.validate({"mode": "blur", "captions": bad})


@pytest.fixture(scope="module")
def blue(tmp_path_factory):
    p = tmp_path_factory.mktemp("c") / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x2060e0:s=1280x720:r=30:d=5", "-f", "lavfi", "-i", "sine=d=5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


def caption_rows(video, at):
    """(top, bottom) pixel rows of white/yellow caption text at time `at`, or None."""
    Image = pytest.importorskip("PIL.Image")
    png = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True).stdout
    im = Image.open(io.BytesIO(png)).convert("RGB")
    px, (W, H) = im.load(), im.size
    rows = [y for y in range(H) if any(min(px[x, y]) > 200 or (px[x, y][0] > 200 and px[x, y][1] > 200 and px[x, y][2] < 90) for x in range(0, W, 3))]
    return (min(rows), max(rows)) if rows else None


def test_captions_do_not_jump_when_words_overlap(blue, tmp_path):
    """The reported bug: caption appears low, then jumps up. Before the fix rows went (1273,1327) -> (1173,1327) at t=2.3s."""
    out = tmp_path / "c.mp4"
    assert export_clip(blue, out, 0, 5, Config(preset="ultrafast"), vertical=True, ass_text=build_ass(OVERLAP, 0.0, 5.0, True))
    seen = [caption_rows(out, t) for t in (1.2, 1.9, 2.1, 2.27, 2.35, 2.8, 3.2)]
    assert all(seen) and len(set(seen)) == 1, seen
    assert caption_rows(out, 0.5) is None and caption_rows(out, 4.5) is None      # nothing drawn while nobody speaks


def test_captions_land_inside_the_box_the_user_placed(blue, tmp_path):
    box = {"x": 0.1, "y": 0.2, "w": 0.8, "h": 0.08}                              # near the top: y from 384 to 538 px
    out = tmp_path / "b.mp4"
    assert export_clip(blue, out, 0, 5, Config(preset="ultrafast"), vertical=True, ass_text=build_ass(OVERLAP, 0.0, 5.0, True, box=box),
                       layout={"mode": "blur", "captions": box})
    top, bottom = caption_rows(out, 1.9)
    assert 0.2 * 1920 - 10 <= top and bottom <= 0.28 * 1920 + 40                  # inside (allowing outline), nowhere near the default spot (~1300)


def test_describe_reads_the_chat_reaction():
    assert classify([["kekw", 46], ["lul", 38], ["wtf", 10]]) == "funny"
    assert classify([["wtf", 50], ["no way", 30], ["kekw", 5]]) == "shock"
    assert classify([["pog", 40], ["w", 30]]) == "hype"
    assert classify([["hello", 9]]) == "default" and classify([]) == "default"
    cfg = Config(display_names={"kaicenat": "Kai Cenat"})
    m = {"id": "kaicenat/s/c1", "channel": "kaicenat", "source_url": "https://www.twitch.tv/kaicenat", "spike": {"top": [["kekw", 9]]}}
    d = suggest(m, cfg)
    assert "Kai Cenat" in d["title"] and len(d["alts"]) == 6 and d["title"] in d["alts"]
    assert "twitch.tv/kaicenat" in d["body"] and "#kaicenat" in d["body"] and "#funny" in d["body"] and "#fyp" in d["body"]
    assert "Clip from" not in suggest(m, Config(credit_line=False))["body"]
    firsts = {suggest({**m, "id": f"kaicenat/s/c{i}"}, cfg)["title"] for i in range(12)}
    assert len(firsts) > 1                                                       # different clips start on different titles


def test_description_api_roundtrip_and_backfill_for_old_clips(tmp_path):
    from autoclip import review
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(b"x")
    meta = {"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 1, "duration_s": 4, "moment_s": 1, "suggest_start_s": 0,
            "suggest_end_s": 3, "exports": [], "created": "2026-01-01T00:00:00", "source_url": "https://twitch.tv/ch",
            "spike": {"peak": 1, "baseline": 1, "msgs": 1, "top": [["pog", 5]]}}                   # an OLD clip: no description field
    (d / "c.clip.json").write_text(json.dumps(meta))
    srv = review.QuietServer(("127.0.0.1", 0), review.make_handler(review.Store(tmp_path, Config())))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, None

    clip = call("/api/clips")[1][0]
    assert clip["description"]["category"] == "hype" and clip["description"]["title"]          # old clip got a suggestion
    assert call("/api/describe", {"id": "ch/s/c", "title": "My title", "body": "my body #x"})[0] == 200
    assert call("/api/clips")[1][0]["description"]["title"] == "My title"                      # edit persisted
    assert call("/api/clips")[1][0]["description"]["alts"]                                      # alternatives kept
    assert call("/api/describe", {"id": "nope", "title": "a", "body": "b"})[0] == 400
    assert call("/api/describe", {"id": "ch/s/c", "title": "t" * 400, "body": "b"})[0] == 400  # size limit
    srv.shutdown()


def test_review_page_javascript_is_syntactically_valid(tmp_path):
    """A stray escape in the page template once produced 'Invalid or unexpected token' and an empty page."""
    import re
    import shutil
    from autoclip import review
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = re.search(r"<script>(.*)</script>", review.PAGE, re.S).group(1)
    f = tmp_path / "page.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _fake_whisper(monkeypatch, behaviour):
    """behaviour(vad) -> list of (word, start, end); records every call."""
    import sys
    import types
    from autoclip import captions
    calls = []

    class W:
        def __init__(self, w, a, b):
            self.word, self.start, self.end = w, a, b

    class Model:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, vad_filter=True, **kw):
            calls.append(vad_filter)
            return [types.SimpleNamespace(words=[W(*x) for x in behaviour(vad_filter)])], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    captions._MODEL.clear()
    return calls


def _clip_with_audio(tmp_path):
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=3", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)], check=True)
    return clip


def test_whisper_voice_filter_is_retried_without_vad(tmp_path, monkeypatch):
    """Loud game audio can make the voice-activity filter discard real speech: must retry unfiltered."""
    from autoclip import captions
    calls = _fake_whisper(monkeypatch, lambda vad: [] if vad else [("finally", 0.5, 1.0)])
    clip = _clip_with_audio(tmp_path)
    assert captions.transcribe(clip, Config()) == [("finally", 0.5, 1.0)]
    assert calls[0] is True and calls[-1] is False                  # strict pass, relaxed pass, then no voice filter at all


def test_empty_transcript_is_never_cached_so_a_clip_is_not_stuck_forever(tmp_path, monkeypatch):
    from autoclip import captions
    clip = _clip_with_audio(tmp_path)
    _fake_whisper(monkeypatch, lambda vad: [])
    with pytest.raises(captions.CaptionsUnavailable, match="no speech"):
        captions.transcribe(clip, Config())
    assert not clip.with_suffix(".words.json").exists()                              # nothing cached
    clip.with_suffix(".words.json").write_text("[]")                                  # simulate the OLD bug's leftover empty cache
    _fake_whisper(monkeypatch, lambda vad: [("hi", 0.1, 0.4)])
    assert captions.transcribe(clip, Config()) == [("hi", 0.1, 0.4)]                  # recovers by itself


def test_export_explains_when_trim_misses_the_speech_and_shows_ffmpeg_errors(tmp_path, monkeypatch):
    from autoclip import review
    from autoclip.cutter import probe_duration
    src = _clip_with_audio(tmp_path)
    meta = {"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 1, "duration_s": probe_duration(src), "exports": [],
            "created": "x", "source_url": "x", "spike": {}}
    (tmp_path / "c.clip.json").write_text(json.dumps(meta))
    store = review.Store(tmp_path, Config(preset="ultrafast"))
    monkeypatch.setattr(review, "transcribe", lambda p, c, a=None, b=None: [("hello", 2.0, 2.6)])
    with pytest.raises(ValueError, match=r"no speech was recognised between 0.0s and 1.0s"):
        store.export("ch/s/c", 0.0, 1.0, True, True)                                  # speech is at 2.0s, trim is 0-1s
    assert store.export("ch/s/c", 0.0, 2.9, True, True)                               # trim covering it works
    monkeypatch.setattr(review, "build_ass", lambda *a, **k: "garbage that is not a subtitle file")
    with pytest.raises(RuntimeError, match="ffmpeg failed: .+"):                       # a bad subtitle file: the reason reaches the UI
        store.export("ch/s/c", 0.0, 2.9, False, True)
