"""Reframe: planning rules (pure), the filter graph, caption moving, and real renders through ffmpeg."""
import json
import random
import subprocess

import pytest

from autoclip import quality, reframe
from autoclip.config import Config
from autoclip.cutter import export_clip

FACE = {"x": 0.75, "y": 0.6, "w": 0.25, "h": 0.4}
SPLIT = {"mode": "split", "split": 0.5, "game_fit": "cover", "facecam": FACE, "game": None}
ALL = reframe.options(None)


def sig(dur=30.0, peaks_at=(8.0, 20.0), build=None, actx=None):
    n = int(dur / reframe.STEP)
    loud = [-40.0] * n
    face = [1.0] * n
    for t in peaks_at:
        i = int(t / reframe.STEP)
        loud[i - 1:i + 2] = [-14.0, -8.0, -14.0]
        face[i] = 9.0
    if build:
        a, b = build
        for i in range(int(a / reframe.STEP), int(b / reframe.STEP)):
            loud[i] = -40.0 + (i * reframe.STEP - a) / (b - a) * 25
    xs = actx or [None] * n
    return {"loud": loud, "face": face, "act": [3.0 if x is not None else 0.0 for x in xs], "actx": xs}


def test_max_two_zooms_in_any_30_seconds_whatever_the_order():
    rng = random.Random(1)
    for _ in range(200):
        acc = []
        for t in sorted((rng.uniform(0, 120) for _ in range(30)), key=lambda _: rng.random()):
            if reframe.allowed(acc, t):
                acc.append(t)
        acc.sort()
        assert all(sum(1 for x in acc if a <= x < a + 30) <= 2 for a in acc)


def test_plan_zooms_on_peaks_obeys_the_rules_and_is_repeatable():
    w = sig(60, peaks_at=(5, 12, 19, 26, 40, 50))
    opts = {**ALL, "layouts": False}
    p = reframe.plan(w, 60, SPLIT, (1920, 1080), opts, "clip-a")
    starts = [z["t0"] for z in p["zooms"]]
    assert 2 <= len(starts) <= 4 and all(sum(1 for x in starts if a <= x < a + 30) <= 2 for a in starts)
    room = quality.max_zoom(quality.panels(SPLIT, (1920, 1080))["game"])
    assert all(z["panel"] == "game" and 1.0 < z["amp"] <= room for z in p["zooms"])
    assert reframe.plan(w, 60, SPLIT, (1920, 1080), opts, "clip-a") == p                     # same clip, same choices
    assert any(reframe.plan(w, 60, SPLIT, (1920, 1080), opts, f"s{i}")["zooms"] != p["zooms"] for i in range(5))
    blur = reframe.plan(w, 60, {"mode": "blur"}, (1920, 1080), ALL, "clip-a")
    assert blur["zooms"] == [] and "Set layout" in blur["notes"][0]                           # webcam position unknown: no zoom


def test_no_zoom_when_the_layout_already_enlarges_too_much_or_on_a_low_res_crop():
    w = sig()
    assert reframe.plan(w, 30, {"mode": "crop"}, (1920, 1080), {**ALL, "track": False}, "x")["zooms"] == []   # 1.78x already
    p = reframe.plan(w, 30, SPLIT, (1280, 720), {**ALL, "layouts": False}, "x")
    assert all(z["panel"] == "game" for z in p["zooms"])                                      # the webcam panel is > 1.5x


def test_campaign_must_show_keeps_panels_whole():
    w = sig()
    p = reframe.plan(w, 30, {"mode": "blur"}, (1920, 1080), ALL, "x", must_show="overlay")
    assert p["zooms"] == [] and p["track"] is None and p["phases"] == []
    p = reframe.plan(w, 30, SPLIT, (3840, 2160), ALL, "x", must_show="facecam")
    assert all(z["panel"] != "face" for z in p["zooms"])


def test_layout_steps_webcam_big_split_webcam_big_and_zooms_stay_out_of_them():
    hits = both = 0
    for i in range(20):
        p = reframe.plan(sig(30, peaks_at=(22.0,)), 30, SPLIT, (1920, 1080), ALL, f"s{i}")
        both += bool(p["zooms"] and len(p["phases"]) == 2)                                  # the moment's zoom, then the payoff
        for a, b in p["phases"]:
            assert all(not (z["t0"] < b and z["t3"] > a) for z in p["zooms"])
        if p["phases"] and p["phases"][0][0] == 0.0:
            assert p["phases"][0][1] <= 2.5
            hits += 1
        if len(p["phases"]) == 2:
            assert p["phases"][1][0] > 22.0 and p["phases"][1][1] == 30.0 and 1.5 <= 30 - p["phases"][1][0] <= 4.5
    assert 10 <= hits < 20 and both >= 5                                                     # random per clip, mostly on
    assert reframe.plan(sig(), 30, {"mode": "blur"}, (1920, 1080), ALL, "x")["phases"] == []  # needs a webcam layout


def test_push_in_on_a_buildup():
    kinds = {z["kind"] for i in range(12) for z in reframe.plan(sig(30, peaks_at=(15.0,), build=(10.0, 14.5)), 30, SPLIT,
                                                                     (1920, 1080), {**ALL, "layouts": False}, f"b{i}")["zooms"]}
    assert kinds == {"push", "punch"}


def test_gameplay_crop_follows_the_action():
    n = 120
    actx = [0.9] * n                                                                         # all the action on the right
    p = reframe.plan(sig(30, peaks_at=(), actx=actx), 30, {"mode": "crop"}, (1920, 1080), ALL, "t")
    keys = p["track"]["keys"]
    assert keys[0][1] <= 0.65 and keys[-1][1] == 1.0                                         # pans right, smoothly
    assert all(b - a <= 0.1 / (1 - reframe.track_window({"mode": "crop"}, (1920, 1080))) + 2e-3 for (_, a), (_, b) in zip(keys, keys[1:]))
    assert reframe.plan(sig(30, peaks_at=()), 30, {"mode": "crop"}, (1920, 1080), ALL, "t")["track"] is None


def test_graph_never_mirrors_and_offsets_time_for_previews():
    p = reframe.plan(sig(), 30, SPLIT, (1920, 1080), ALL, "g")
    g = reframe.graph(SPLIT, p, (1920, 1080), 30, offset=4.5)
    assert "hflip" not in g and "vflip" not in g and "perspective=" in g and "(in/30+4.500)" in g and g.endswith("[v]")
    assert reframe.graph(SPLIT, {"zooms": [], "phases": [], "track": None}, (1920, 1080), 30) is None


def test_captions_move_below_the_big_webcam_and_hook_avoids_it():
    from autoclip.captions import build_ass
    ass = build_ass([("hey", 1.0, 1.4), ("later", 10.0, 10.4)], 0.0, 12.0, True, seam=True)
    p = {"phases": [[0.0, 2.5]]}
    moved = reframe.move_captions(ass, p)
    lines = [x for x in moved.splitlines() if ",Cap," in x]
    assert f",{int(1920 * reframe.CAPTION_Y)})" in lines[0] and ",960)" in lines[1]
    assert reframe.hook_bands(p) == [(1920 * reframe.FACE_BOX[0], 1920 * reframe.FACE_BOX[1])]


ffmpeg_ok = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def loud_clip(tmp_path_factory):
    """12 s 1080p: a moving white box (the action) and a loud burst at 6 s."""
    p = tmp_path_factory.mktemp("rf") / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=0x204080:s=1920x1080:r=30:d=12[bg];color=c=white:s=160x160:r=30:d=12[box];"
                    "[bg][box]overlay=x='200+t*120':y=500",
                    "-f", "lavfi", "-i", "sine=f=300:d=12,volume='if(between(t,5.8,6.6),1,0.02)':eval=frame",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


@pytest.mark.skipif(not ffmpeg_ok, reason="no ffmpeg")
def test_analyze_finds_the_loud_moment_and_the_action(loud_clip):
    s = reframe.cached_signals(loud_clip)
    assert loud_clip.with_suffix(".signals.json").is_file()
    w = reframe.window(s, 0, 12, None, None)
    loudest = max(range(len(w["loud"])), key=lambda i: w["loud"][i]) * reframe.STEP
    assert 5.5 <= loudest <= 6.75
    xs = [x for x in w["actx"] if x is not None]
    assert xs and xs[-1] > xs[0]                                                           # the box moves right


@pytest.mark.skipif(not ffmpeg_ok, reason="no ffmpeg")
@pytest.mark.parametrize("layout", [{"mode": "blur"}, {"mode": "crop"}, SPLIT])
def test_real_render_one_pass_1080x1920_same_length(loud_clip, tmp_path, layout):
    meta = {"id": "ch/s/c", "res": [1920, 1080]}
    rf = reframe.make(loud_clip, 0.0, 12.0, layout, meta, None, {"freeze": True}, 30.0)
    assert rf and rf["graph"]
    out = tmp_path / "o.mp4"
    errs = []
    assert export_clip(loud_clip, out, 0.0, 12.0, Config(), True, None, layout, errs, motion=rf["graph"]), errs
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration", "-of", "json", str(out)],
                          capture_output=True, text=True).stdout
    d = json.loads(info)
    assert (d["streams"][0]["width"], d["streams"][0]["height"]) == (1080, 1920)
    assert abs(float(d["format"]["duration"]) - 12.0) < 0.3                                  # freeze keeps the length


@pytest.mark.skipif(not ffmpeg_ok, reason="no ffmpeg")
def test_store_export_records_the_reframe_and_preview_matches(loud_clip, tmp_path):
    import shutil
    from autoclip import review
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    shutil.copy(loud_clip, d / "c.mp4")
    (d / "c.clip.json").write_text(json.dumps({"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "duration_s": 12, "status": "new",
                                               "res": [1920, 1080], "moment_s": 6.0}))
    from autoclip.layout import LayoutStore
    LayoutStore(tmp_path).save("ch", SPLIT)
    store = review.Store(tmp_path, Config())
    v = store.quality_view("ch/s/c", 0.0, 12.0, True)
    assert v["report"]["plan"]["zooms"] >= 1 and 5.0 < v["t"] < 8.0
    assert store.after_frame("ch/s/c", v["t"], 0.0, 12.0, True)[:2] == b"\xff\xd8"
    name = store.export("ch/s/c", 0.0, 12.0, True, False)
    rec = json.loads((d / "c.clip.json").read_text())["exports"][-1]
    assert "_rf" in name and rec["reframe"]["applied"] and rec["reframe"]["zooms"] >= 1
    assert store.set_reframe("ch/s/c", {"on": False})["on"] is False
    name2 = store.export("ch/s/c", 0.0, 12.0, True, False)
    assert "_rf" not in name2 and json.loads((d / "c.clip.json").read_text())["exports"][-1]["reframe"]["applied"] is False
    old = store.set_reframe("ch/s/c", {"on": True})["seed"]
    assert store.set_reframe("ch/s/c", {"shuffle": True})["seed"] != old


def test_facecam_must_show_on_a_full_frame_layout_means_no_zoom_at_all():
    for mode in ("blur", "crop"):
        p = reframe.plan(sig(30, peaks_at=(8.0, 20.0)), 30, {"mode": mode}, (1920, 1080), ALL, "x", must_show="facecam")
        assert p["zooms"] == [] and p["track"] is None


def test_hook_over_a_webcam_only_intro_sits_above_the_webcam_and_off_the_moved_captions():
    from autoclip import campaigns, hooks
    p = {"phases": [[0.0, 2.5]]}
    screen = reframe.hook_screen(p, True)
    info = {}
    ass = campaigns.overlay_ass(None, True, "Arial", 30, hook="Wait for it", protected=screen, info=info)
    line = next(x for x in ass.splitlines() if ",Hook," in x)
    y = int(line.split("\pos(")[1].split(",")[1].split(")")[0])
    size = round(1920 * 0.05)
    assert info["hook"] and y + size * 2.5 <= 1920 * reframe.FACE_BOX[0]                    # above the webcam, so above captions
    assert reframe.hook_screen({"phases": [[20.0, 30.0]]}, True) is None
    assert reframe.plan(sig(30, peaks_at=(22.0,)), 30, SPLIT, (1920, 1080), ALL, "s1")["phases"][0] == [0.0, hooks.HOOK_S]


@pytest.mark.skipif(not ffmpeg_ok, reason="no ffmpeg")
def test_a_zoom_is_really_visible_in_the_export(loud_clip, tmp_path):
    p = {"seed": "z", "mode": "split", "phases": [], "track": None, "freeze": None, "notes": [],
         "zooms": [{"panel": "game", "kind": "punch", "t0": 5.0, "t1": 5.2, "t2": 7.0, "t3": 7.3, "amp": 1.4, "fx": 0.5, "fy": 0.5}]}
    g = reframe.graph(SPLIT, p, (1920, 1080), 30.0)
    out = tmp_path / "z.mp4"
    assert export_clip(loud_clip, out, 0.0, 9.0, Config(), True, None, SPLIT, [], motion=g)

    def box_width(at):                                                                      # the white box, bottom (gameplay) panel
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(out), "-frames:v", "1", "-vf", "crop=1080:960:0:960,format=gray",
                            "-f", "rawvideo", "-"], capture_output=True).stdout
        rows = [r[y * 1080:(y + 1) * 1080] for y in range(960)]
        return max(sum(1 for v in row if v > 200) for row in rows)
    assert box_width(6.2) > box_width(3.0) * 1.25                                           # 1.4x zoom: the box looks bigger
