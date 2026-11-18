import subprocess
import time

import pytest

from autoclip.config import Config
from autoclip.cutter import loudness_jump, make_vertical, cut_clip
from autoclip.recorder import Segment, ffmpeg_exe

pytestmark = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")


def make_segments(tmp_path, n=10, seg=2, loud_from=6):
    """n fake 2s .ts segments, quiet then loud, stamped with fake wall times starting at 1000."""
    segs = []
    for i in range(n):
        p = tmp_path / f"seg_{i:06d}.ts"
        amp = 0.02 if i < loud_from else 0.8
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seg}",
                        "-f", "lavfi", "-i", f"sine=frequency=500:duration={seg},volume={amp}", "-c:v", "libx264", "-g", "25",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "mpegts", str(p)], check=True)
        segs.append(Segment(p, 1000.0 + i * seg, 1000.0 + (i + 1) * seg))
    return segs


def duration(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout
    return float(out)


def test_cut_vertical_and_loudness(tmp_path):
    segs = make_segments(tmp_path)
    cfg = Config(preset="ultrafast")
    out = tmp_path / "clip.mp4"
    assert cut_clip(segs, 1003.0, 1017.0, out, cfg)
    assert abs(duration(out) - 14.0) < 0.5               # exactly the requested window, not whole segments
    jump = loudness_jump(out, 9.0)                       # loud starts at wall 1012 = 9s into the clip
    assert jump is not None and jump > 10
    v = tmp_path / "v.mp4"
    assert make_vertical(out, v, cfg)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(v)], capture_output=True, text=True).stdout.strip()
    assert probe == "1080,1920"
    cfg.vertical_mode = "crop"
    assert make_vertical(out, tmp_path / "c.mp4", cfg)


def test_cut_refuses_when_buffer_does_not_cover_window(tmp_path):
    segs = make_segments(tmp_path, n=3)
    assert not cut_clip(segs, 1100.0, 1130.0, tmp_path / "x.mp4", Config())      # window long gone
    assert not cut_clip(segs, 990.0, 1100.0, tmp_path / "y.mp4", Config())       # only ~6% covered


def test_cut_survives_awkward_paths(tmp_path):
    d = tmp_path / "it's a folder with spaces"
    d.mkdir()
    segs = make_segments(d, n=4)
    assert cut_clip(segs, 1000.0, 1008.0, d / "o.mp4", Config(preset="ultrafast"))


def test_recorder_end_to_end_with_synthetic_source(tmp_path):
    """Real Recorder + real ffmpeg segmenter fed by a fake 'stream'; checks wall-clock stamping + buffer pruning + clean stop."""
    import threading
    from autoclip.recorder import Recorder
    src = [ffmpeg_exe(), "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-c:v", "libx264",
           "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-f", "mpegts", "pipe:1"]
    stop = threading.Event()
    cfg = Config(segment_s=1, buffer_s=6, buffer_dir=str(tmp_path))
    rec = Recorder(cfg, "x", stop, "t", source_cmd=src)
    rec.start()
    t0 = time.time()
    assert rec.live.wait(15)
    time.sleep(11)
    segs = rec.segments_between(t0, time.time() + 1)
    assert segs and all(s.t0 <= s.t1 for s in segs)
    assert max(s.t1 for s in segs) > time.time() - 4                # stamped with *now*, not stream-relative time
    assert min(s.t1 for s in segs) > time.time() - cfg.buffer_s - 3   # old segments pruned
    assert len(list(tmp_path.rglob("seg_*.ts"))) <= cfg.buffer_s + 6   # disk usage bounded
    stop.set()
    rec.join(timeout=15)
    assert not rec.is_alive()
    assert not any(tmp_path.iterdir()), "buffer folder should be deleted on stop"
    import subprocess as sp
    assert sp.run(["pgrep", "-f", "testsrc2=size=320x180"], capture_output=True).returncode != 0, "ffmpeg leaked"
