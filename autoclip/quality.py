"""Quality guard: how much each part of a 9:16 export enlarges the stream's own pixels, and the limits every reframe obeys.

  * A zoom may never make the picture bigger than 1.5x the stream's real pixels (MAX_UPSCALE). A panel that is already enlarged
    more than that by your layout gets no zoom at all.
  * Below 1080p the card warns: there is little room to zoom before the picture turns soft.
  * Everything is made in ONE pass from the original recording (the buffer is stream copy): lanczos scaling, a light unsharp mask
    only while zoomed, CRF 18 or lower, 30 fps (the measured default; `out_fps` in config.toml changes it).
"""
from __future__ import annotations

from . import layout as lay

MAX_UPSCALE = 1.5
CRF_MAX = 18
ZOOM_SHARPEN = 0.35            # luma unsharp amount while zoomed in (light: it only offsets the zoom's own softening)
LOW_RES = 1080


def crf(value: int) -> int:
    return max(0, min(int(value), CRF_MAX))


def cover_upscale(rect: dict | None, out_w: int, out_h: int, res: tuple[int, int]) -> float:
    """How much `rect` (fractions of the source; None = whole frame) is enlarged when it covers an out_w x out_h panel."""
    W, H = res
    r = rect or {"w": 1.0, "h": 1.0}
    return max(out_w / (r["w"] * W), out_h / (r["h"] * H))


def panels(layout: dict | None, res: tuple[int, int]) -> dict[str, float]:
    """{panel: enlargement} of a 9:16 layout without any zoom. Panels: 'face', 'game' (split), 'main' (blur / crop)."""
    mode = (layout or {}).get("mode", "blur")
    W, H = res
    if mode == "crop":
        return {"main": lay.OUT_H / H}
    if mode != "split":
        return {"main": lay.OUT_W / W}                              # whole frame fitted to the width
    h1, h2 = lay.heights(layout["split"])
    out = {"face": cover_upscale(layout["facecam"], lay.OUT_W, h1, res)}
    if layout.get("game_fit") == "blur":
        g = layout.get("game") or {"w": 1.0}
        out["game"] = lay.OUT_W / (g["w"] * W)
    else:
        out["game"] = cover_upscale(layout.get("game") or lay.default_game_rect(layout["split"], W / H), lay.OUT_W, h2, res)
    return out


def max_zoom(base: float) -> float:
    """The largest zoom a panel enlarged `base` times may get: base * zoom <= 1.5. 1.0 = no zoom allowed."""
    return max(1.0, MAX_UPSCALE / base) if base > 0 else 1.0


def report(layout: dict | None, res: tuple[int, int] | list | None) -> dict:
    """What the card shows: each panel's enlargement, its zoom room, and warnings."""
    if not res or len(res) < 2 or not res[0] or not res[1]:
        return {"res": None, "panels": {}, "zoom_room": {}, "warnings": ["Source size unknown: zooms are off for this clip."]}
    res = (int(res[0]), int(res[1]))
    p = panels(layout, res)
    warn = []
    if res[1] < LOW_RES:
        warn.append(f"The stream was recorded at {res[1]}p (below 1080p): zooms are small or off so the picture stays sharp.")
    if p and all(v * 1.0 > MAX_UPSCALE for v in p.values()):
        warn.append("Every panel is already enlarged more than 1.5x by the layout, so no zoom is used.")
    return {"res": list(res), "panels": {k: round(v, 2) for k, v in p.items()},
            "zoom_room": {k: round(max_zoom(v), 2) for k, v in p.items()}, "warnings": warn}
