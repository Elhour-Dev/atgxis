"""Optional helper: guess where the streamer's webcam is, so you only have to nudge the box.
Needs:  pip install "opencv-python-headless>=4.8,<5"   (face detector ships inside the package)."""
from __future__ import annotations

import statistics
import subprocess
import tempfile
from pathlib import Path

from .layout import OUT_W, heights
from .recorder import ffmpeg_exe


class FacecamUnavailable(RuntimeError):
    pass


def detect_facecam(video: Path, duration: float, split: float = 0.5, samples: int = 8) -> dict | None:
    """Return a normalized facecam rect (shaped for the top panel) or None if no face is found consistently."""
    try:
        import cv2
    except ImportError as e:
        raise FacecamUnavailable('Auto-detect needs:  pip install "opencv-python-headless>=4.8,<5"') from e
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise FacecamUnavailable("OpenCV face model missing; reinstall opencv-python-headless (version 4.x)")
    faces = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(samples):
            t = duration * (i + 0.5) / samples
            png = Path(td) / f"f{i}.png"
            subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=1280:-2", str(png)], capture_output=True, timeout=60)
            img = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            H, W = img.shape
            found = cascade.detectMultiScale(img, scaleFactor=1.08, minNeighbors=6, minSize=(int(W * 0.03), int(W * 0.03)))
            if len(found):
                x, y, w, h = max(found, key=lambda f: f[2] * f[3])        # biggest face in this frame
                faces.append(((x + w / 2) / W, (y + h / 2) / H, w / W, h / H, W / H))
    if len(faces) < max(2, samples // 4):
        return None
    cx, cy, fw, fh, aspect = (statistics.median(f[k] for f in faces) for k in range(5))
    h1, _ = heights(split)
    panel = OUT_W / h1                                    # width/height of the top panel
    bh = fh * 2.6                                         # a webcam frame is ~2.6x the height of a face
    bw = bh * panel / aspect                              # keep the panel's shape, in normalized units
    if bw > 1 or bh > 1:
        s = max(bw, bh)
        bw, bh = bw / s, bh / s
    x = min(max(cx - bw / 2, 0), 1 - bw)
    y = min(max(cy - bh / 2, 0), 1 - bh)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(bw, 4), "h": round(bh, 4)}
