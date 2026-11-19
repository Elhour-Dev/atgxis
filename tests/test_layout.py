import json
import re
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from autoclip import layout as lay
from autoclip.captions import build_ass
from autoclip.config import Config
from autoclip.cutter import _ENC_CACHE, export_clip, grab_frame, pick_encoder, probe_duration

pytestmark = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")
FACE = {"x": 1500 / 1920, "y": 700 / 1080, "w": 380 / 1920, "h": 340 / 1080}      # the red patch in the synthetic stream
SPLIT = {"mode": "split", "split": 0.5, "game_fit": "cover", "facecam": FACE, "game": None}


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    """1080p60 stream: blue 'gameplay' everywhere, a red 'webcam' patch bottom-right, with audio."""
    p = tmp_path_factory.mktemp("src") / "stream.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=0x2060e0:s=1920x1080:r=60:d=4,drawbox=x=1500:y=700:w=380:h=340:color=red:t=fill",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


def pixel(video, x, y, at=1.0):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1", "-vf",
                        f"crop=8:8:{x}:{y},scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    return tuple(r.stdout[:3])


def probe(video, entry):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", f"stream={entry}", "-of", "csv=p=0",
                           str(video)], capture_output=True, text=True).stdout.strip()


def test_validate_accepts_good_and_rejects_bad():
    assert lay.validate(SPLIT)["facecam"]["w"] == pytest.approx(FACE["w"], abs=1e-4)
    assert lay.validate({"mode": "blur"}) == {"mode": "blur"}
    for bad in [None, {"mode": "evil"}, {"mode": "split"}, {**SPLIT, "split": 0.9}, {**SPLIT, "game_fit": "x"},
                {**SPLIT, "facecam": {"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}},               # outside the frame
                {**SPLIT, "facecam": {"x": "0);drawtext=", "y": 0, "w": 0.2, "h": 0.2}},     # injection attempt
                {**SPLIT, "facecam": {"x": 0, "y": 0, "w": 0.001, "h": 0.2}}]:
        with pytest.raises(lay.LayoutError):
            lay.validate(bad)


def test_split_layout_puts_webcam_on_top_gameplay_below(stream, tmp_path):
    out = tmp_path / "v.mp4"
    assert export_clip(stream, out, 0.5, 3.0, Config(preset="ultrafast"), vertical=True, layout=SPLIT)
    assert probe(out, "width,height") == "1080,1920"
    assert pixel(out, 540, 480) == (255, 0, 0) or all(abs(a - b) < 12 for a, b in zip(pixel(out, 540, 480), (255, 0, 0)))   # top: red webcam
    bottom = pixel(out, 540, 1440)
    assert bottom[2] > 150 and bottom[0] < 80                      # bottom: blue gameplay (not swapped, no red leaking in)
    assert pixel(out, 5, 5)[0] > 200 and pixel(out, 1070, 940)[0] > 200      # webcam fills its whole half, edge to edge


def test_every_mode_and_game_fit_renders(stream, tmp_path):
    cfg = Config(preset="ultrafast")
    for i, L in enumerate([{"mode": "blur"}, {"mode": "crop"}, {**SPLIT, "game_fit": "blur"},
                           {**SPLIT, "split": 0.4, "game": {"x": 0.2, "y": 0.1, "w": 0.4, "h": 0.5}}]):
        out = tmp_path / f"m{i}.mp4"
        assert export_clip(stream, out, 0, 2, cfg, vertical=True, layout=L), L
        assert probe(out, "width,height") == "1080,1920"


def test_output_is_30fps_with_normalized_audio(stream, tmp_path):
    out = tmp_path / "o.mp4"
    assert export_clip(stream, out, 0, 3, Config(preset="ultrafast"), vertical=True, layout=SPLIT)
    assert probe(out, "r_frame_rate") == "30/1"                    # source was 60 fps
    assert abs(probe_duration(out) - 3.0) < 0.4
    a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True).stdout.strip()
    assert a == "aac,48000"


def test_auto_encoder_falls_back_to_cpu_when_no_gpu():
    _ENC_CACHE.clear()
    assert pick_encoder(Config()) in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf")
    assert pick_encoder(Config(encoder="libx264")) == "libx264"           # explicit setting always wins
    # ffmpeg lists nvenc/qsv in this sandbox but there is no GPU; the real probe must NOT pick them
    assert pick_encoder(Config()) == "libx264"


def test_captions_sit_on_the_seam_in_split_layout():
    def where(ass):
        d = next(l for l in ass.splitlines() if l.startswith("Dialogue"))
        an, x, y = re.search(r"\\an(\d)\\pos\((\d+),(\d+)\)", d).groups()
        return int(an), int(x), int(y)
    words = [("hello", 0.0, 0.5)]
    assert where(build_ass(words, 0, 2, True, seam=True)) == (5, 540, 960)          # middle of the 1080x1920 frame = the seam
    an, x, y = where(build_ass(words, 0, 2, True, seam=False))
    assert an == 2 and x == 540 and y == 1920 - int(1920 * 0.30)                    # plain vertical: lower third, bottom-anchored


def test_layout_store_roundtrip_and_atomic(tmp_path):
    st = lay.LayoutStore(tmp_path)
    assert st.get("kaicenat") is None
    st.save("kaicenat", SPLIT)
    assert st.get("kaicenat")["mode"] == "split" and lay.LayoutStore(tmp_path).get("kaicenat")["split"] == 0.5
    with pytest.raises(lay.LayoutError):
        st.save("kaicenat", {"mode": "split"})
    assert st.get("kaicenat")["mode"] == "split"                          # bad save did not corrupt the good one


def test_detect_facecam_finds_a_face_in_a_corner_overlay(tmp_path):
    cv2 = pytest.importorskip("cv2")
    skd = pytest.importorskip("skimage.data")
    import numpy as np
    from autoclip.facecam import detect_facecam
    frame = np.full((1080, 1920, 3), (60, 90, 40), np.uint8)
    face = cv2.cvtColor(cv2.resize(skd.astronaut(), (420, 420)), cv2.COLOR_RGB2BGR)
    frame[620:1040, 1440:1860] = face                                      # webcam overlay, bottom-right
    png = tmp_path / "f.png"
    cv2.imwrite(str(png), frame)
    vid = tmp_path / "f.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png), "-t", "4", "-r", "30", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(vid)], check=True)
    r = detect_facecam(vid, 4.0)
    assert r, "no face found"
    cx, cy = r["x"] + r["w"] / 2, r["y"] + r["h"] / 2
    assert 1440 / 1920 < cx < 1860 / 1920 and 620 / 1080 < cy < 1040 / 1080     # box centered inside the overlay
    assert 0 <= r["x"] and r["x"] + r["w"] <= 1.0001 and r["y"] + r["h"] <= 1.0001


def test_detect_facecam_returns_none_when_no_face(stream):
    pytest.importorskip("cv2")
    from autoclip.facecam import detect_facecam
    assert detect_facecam(stream, 4.0) is None


def test_http_frame_preview_layout_autodetect(stream, tmp_path):
    from autoclip import review
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(stream.read_bytes())
    meta = {"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 1, "duration_s": 4.0, "moment_s": 1,
            "suggest_start_s": 0, "suggest_end_s": 3, "exports": [], "created": "2026-01-01T00:00:00", "source_url": "x",
            "spike": {"peak": 1, "baseline": 1, "msgs": 1, "top": []}}
    (d / "c.clip.json").write_text(json.dumps(meta))
    cfg = Config(preset="ultrafast")
    srv = review.ThreadingHTTPServer(("127.0.0.1", 0), review.make_handler(review.Store(tmp_path, cfg)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    code, img = call("/api/frame?id=ch/s/c&t=1.0")
    assert code == 200 and img[:2] == b"\xff\xd8"                                      # JPEG
    code, img = call("/api/preview", {"id": "ch/s/c", "t": 1.0, "layout": SPLIT})
    assert code == 200 and img[:2] == b"\xff\xd8"
    (tmp_path / "pv.jpg").write_bytes(img)
    assert probe(tmp_path / "pv.jpg", "width,height") == "1080,1920"                   # the preview IS the final composition
    assert call("/api/preview", {"id": "ch/s/c", "t": 1.0, "layout": {"mode": "x"}})[0] == 400
    assert call("/api/layout", {"channel": "ch", "layout": SPLIT})[0] == 200
    assert call("/api/layout", {"channel": "../evil", "layout": SPLIT})[0] == 400      # channel name validated
    assert call("/api/layout", {"channel": "ch", "layout": {"mode": "split"}})[0] == 400
    assert json.loads(call("/api/layouts")[1])["ch"]["mode"] == "split"
    code, out = call("/api/export", {"id": "ch/s/c", "start": 0, "end": 3, "vertical": True, "captions": False})
    assert code == 200
    assert pixel(d / json.loads(out)["file"], 540, 480)[0] > 200                       # export used the SAVED layout (red on top)
    code, msg = call("/api/autodetect", {"id": "ch/s/c"})
    assert code == 400                                                                 # no face in the synthetic clip -> clear message
    srv.shutdown()
