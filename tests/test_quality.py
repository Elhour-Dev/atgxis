"""Quality guard: enlargement maths, zoom room, CRF cap, low-resolution warning, and the before / after stills."""
import json
import subprocess

import pytest

from autoclip import quality
from autoclip.config import Config
from autoclip.cutter import _ENC_CACHE, _vcodec

FACE = {"x": 0.78, "y": 0.65, "w": 0.2, "h": 0.31}
SPLIT = {"mode": "split", "split": 0.5, "game_fit": "cover", "facecam": FACE, "game": None}


def test_panel_enlargement_and_zoom_room():
    p = quality.panels(SPLIT, (1920, 1080))
    assert p["face"] == pytest.approx(max(1080 / (0.2 * 1920), 960 / (0.31 * 1080)))      # webcam: already > 1.5x
    assert p["game"] < 1.0                                                                  # gameplay is shrunk at 1080p
    assert quality.max_zoom(p["face"]) == 1.0 and quality.max_zoom(p["game"]) == pytest.approx(1.5 / p["game"])
    assert quality.panels({"mode": "blur"}, (1920, 1080))["main"] == pytest.approx(1080 / 1920)
    assert quality.panels({"mode": "crop"}, (1280, 720))["main"] == pytest.approx(1920 / 720)


def test_report_warns_below_1080p_and_when_no_panel_can_zoom():
    hi = quality.report(SPLIT, [1920, 1080])
    assert hi["warnings"] == [] and hi["zoom_room"]["face"] == 1.0 and hi["zoom_room"]["game"] > 1.0
    lo = quality.report({"mode": "crop"}, [1280, 720])
    assert any("720p" in w for w in lo["warnings"]) and any("already enlarged" in w for w in lo["warnings"])
    assert quality.report(SPLIT, None)["warnings"]


def test_crf_never_above_18_whatever_the_config_says(monkeypatch):
    monkeypatch.setitem(_ENC_CACHE, "auto", "libx264")
    args = _vcodec(Config(crf=28))
    assert args[args.index("-crf") + 1] == "18"
    args = _vcodec(Config(crf=16))
    assert args[args.index("-crf") + 1] == "16"
    monkeypatch.setitem(_ENC_CACHE, "auto", "h264_nvenc")
    args = _vcodec(Config(crf=28))
    assert args[args.index("-cq") + 1] == "18"


@pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")
def test_card_gets_a_report_and_before_after_stills(tmp_path):
    from autoclip import review
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / "c.mp4")], check=True)
    (d / "c.clip.json").write_text(json.dumps({"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "duration_s": 6, "status": "new",
                                               "res": [1280, 720], "moment_s": 4.0}))
    store = review.Store(tmp_path, Config())
    v = store.quality_view("ch/s/c", 1.0, 5.0, True)
    assert v["t"] == 4.0 and any("720p" in w for w in v["report"]["warnings"])
    assert store.quality_view("ch/s/c", 1.0, 3.0, True)["t"] < 3.0                          # the moment is kept inside the trim
    after = store.after_frame("ch/s/c", 4.0, 1.0, 5.0, True)
    assert after[:2] == b"\xff\xd8"
    w, h = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", "-"],
                          input=after, capture_output=True).stdout.decode().strip().split("x")
    assert (int(w), int(h)) == (1080, 1920)
    with pytest.raises(ValueError):
        store.quality_view("ch/s/c", 5.0, 1.0, True)
