"""Vertical (9:16) layouts. Rectangles are stored as FRACTIONS of the source frame (0-1), so a layout keeps
working whether the stream is 480p, 720p or 1080p.

  mode "split": webcam crop on top, gameplay on the bottom (the standard Reels / TikTok / Shorts streamer format)
  mode "blur" : whole frame centered over a blurred copy of itself
  mode "crop" : plain center crop
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
MODES = ("split", "blur", "crop")


class LayoutError(ValueError):
    pass


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def heights(split: float) -> tuple[int, int]:
    top = _even(OUT_H * split)
    return top, OUT_H - top


def default_game_rect(split: float = 0.5, src_aspect: float = 16 / 9) -> dict:
    """Center crop of the source with the same shape as the bottom panel (full height, as wide as needed)."""
    _, h2 = heights(split)
    w = min(1.0, (OUT_W / h2) / src_aspect)
    return {"x": round((1 - w) / 2, 4), "y": 0.0, "w": round(w, 4), "h": 1.0}


def validate(layout: dict) -> dict:
    """Return a cleaned copy or raise LayoutError. Never trust values that end up inside an ffmpeg filter string."""
    if not isinstance(layout, dict):
        raise LayoutError("layout must be an object")
    mode = layout.get("mode", "split")
    if mode not in MODES:
        raise LayoutError(f"mode must be one of {MODES}")
    out: dict = {"mode": mode}
    cap = layout.get("captions")
    if cap is not None:                                  # where the subtitles go, as fractions of the 1080x1920 output
        try:
            x, y, w, h = (float(cap[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise LayoutError("captions needs numeric x, y, w, h") from None
        if not (0 <= x < 1 and 0 <= y < 1 and 0.2 <= w <= 1 and 0.03 <= h <= 0.5 and x + w <= 1.001 and y + h <= 1.001):
            raise LayoutError("captions box is outside the frame or too small")
        out["captions"] = {k: round(v, 5) for k, v in zip("xywh", (x, y, w, h))}
    if mode != "split":
        return out

    def rect(name: str, required: bool):
        r = layout.get(name)
        if r is None:
            if required:
                raise LayoutError(f"{name} rectangle is required")
            return None
        try:
            x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise LayoutError(f"{name} needs numeric x, y, w, h") from None
        if not (0 <= x < 1 and 0 <= y < 1 and 0.04 <= w <= 1 and 0.04 <= h <= 1 and x + w <= 1.001 and y + h <= 1.001):
            raise LayoutError(f"{name} rectangle is outside the frame or too small")
        return {k: round(v, 5) for k, v in zip("xywh", (x, y, w, h))}

    split = float(layout.get("split", 0.5))
    if not 0.3 <= split <= 0.7:
        raise LayoutError("split must be between 0.3 and 0.7")
    fit = layout.get("game_fit", "cover")
    if fit not in ("cover", "blur"):
        raise LayoutError("game_fit must be 'cover' or 'blur'")
    out.update(split=round(split, 3), game_fit=fit, facecam=rect("facecam", True), game=rect("game", False))
    return out


def _crop(r: dict) -> str:
    # even sizes, integer positions; plain expressions with no commas so no quoting is needed
    return (f"crop=trunc(iw*{r['w']}/2)*2:trunc(ih*{r['h']}/2)*2:trunc(iw*{r['x']}):trunc(ih*{r['y']})")


def _cover(r: dict | None, w: int, h: int) -> str:
    return (f"{_crop(r)}," if r else "") + f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def chain(layout: dict | None) -> str:
    """ffmpeg filter_complex text from input [0:v] to output label [v]: 1080x1920."""
    mode = (layout or {}).get("mode", "blur")
    if mode == "crop":
        return f"[0:v]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H}[v]"
    if mode != "split":
        return (f"[0:v]split[a][b];[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},boxblur=25:5[bg];"
                f"[b]scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v]")
    lay = validate(layout)
    h1, h2 = heights(lay["split"])
    top = f"[a]{_cover(lay['facecam'], OUT_W, h1)}[top]"
    if lay["game_fit"] == "blur":      # whole gameplay area visible, blurred fill above and below
        g = lay["game"]
        fg = f"{_crop(g)}," if g else ""
        bottom = (f"[b]split[b1][b2];[b1]scale={OUT_W}:{h2}:force_original_aspect_ratio=increase,crop={OUT_W}:{h2},boxblur=25:5[bg];"
                  f"[b2]{fg}scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[bot]")
    else:
        bottom = f"[b]{_cover(lay['game'] or default_game_rect(lay['split']), OUT_W, h2)}[bot]"
    return f"[0:v]split[a][b];{top};{bottom};[top][bot]vstack=inputs=2[v]"


class LayoutStore:
    """layouts.json inside the clips folder: {channel: layout}. Written atomically; safe with the review page open."""

    def __init__(self, root: Path):
        self.path = Path(root) / "layouts.json"
        self._lock = threading.Lock()

    def all(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, channel: str) -> dict | None:
        return self.all().get(channel)

    def save(self, channel: str, layout: dict) -> dict:
        clean = validate(layout)
        with self._lock:
            data = self.all()
            data[channel] = clean
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        return clean
