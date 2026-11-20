"""Autopilot: it must post the clips the learned taste is sure about, and must NOT post in every situation where posting would be a mistake."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from autoclip import autopilot, cli, review, share, taste
from autoclip.config import Config
from autoclip.share import Result

from clipfactory import BAD_CH, GOOD_CH, meta, teach, write_clip

MIN = 60


class FakeAuto(share.Provider):
    """Stands in for Quick connect / the direct accounts: records what it was asked to publish."""
    name, label, automatic = "auto", "fake", True

    def __init__(self):
        self.calls, self.fail, self.not_connected = [], False, set()

    def configured(self):
        return True

    def problems(self, platform):
        return f"{platform} not connected" if platform in self.not_connected else None

    def publish(self, job):
        self.calls.append((job.platform, job.video.name, job.title))
        return Result(False, "failed", "boom") if self.fail else Result(True, "done", "ok", f"https://example.com/{job.video.stem}")


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.root, self.now = tmp_path / "clips", time.time()
        self.provider = FakeAuto()
        monkeypatch.setitem(share.PROVIDERS, "auto", self.provider)
        self.model = teach(taste)
        assert taste.readiness(self.model, 0.8)["ok"], "the made-up user is clear enough that the gate opens"
        self.store = review.Store(self.root, Config())
        self.ap = autopilot.Autopilot(self.store)
        self.n = 100
        autopilot.save({"mode": "post", "platforms": ["tiktok"], "channels": [GOOD_CH], "delay_min": 10, "min_precision": 0.8,
                        "min_score": 0, "trash_below": 0})                              # score rules have their own tests

    def clip(self, channel=GOOD_CH, *, age_s=60, export=True, **kw):
        self.n += 1
        return write_clip(self.root, meta(self.n, channel, created=self.now - age_s, **kw), export=export)

    def read(self, path):
        for i in range(20):                                    # Windows: the autopilot thread may be replacing the file right now
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (PermissionError, ValueError):
                if i == 19:
                    raise
                time.sleep(0.02)

    def tick(self, minutes=0):
        return self.ap.tick(self.now + minutes * MIN)

    def ap_state(self, path):
        return (self.read(path).get("autopilot") or {}).get("state")


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# ------------------------------------------------------------------ when it acts
def test_it_queues_a_good_clip_first_and_posts_it_only_after_your_veto_time(rig):
    p = rig.clip()
    assert rig.tick(0) == [{"action": "queued", "clip": rig.read(p)["id"], "p": rig.tick.__self__.read(p)["autopilot"]["p"]}]
    assert rig.ap_state(p) == "queued" and rig.provider.calls == []
    assert rig.tick(5) == [] and rig.provider.calls == []                                     # still inside the veto window
    acts = rig.tick(11)
    assert [a["action"] for a in acts] == ["posted"] and rig.provider.calls == [("tiktok", p.name.replace(".clip.json", "_v.mp4"), "Title of " + p.name[:-10])]
    m = rig.read(p)
    assert m["autopilot"]["state"] == "posted" and m["shares"]["tiktok"]["status"] == "done" and m["shares"]["tiktok"]["actor"] == "autopilot"
    assert [e["action"] for e in autopilot.read_log()] == ["queued", "attempt", "posted"]      # the attempt is written BEFORE sending
    assert rig.tick(60) == [] and len(rig.provider.calls) == 1                                # never twice


def test_off_and_suggest_modes_never_post_or_even_queue(rig):
    p = rig.clip()
    for mode in ("off", "suggest"):
        autopilot.save({"mode": mode})
        assert rig.tick(0) == [] and rig.tick(60) == []
    assert rig.provider.calls == [] and "autopilot" not in rig.read(p)


def test_a_model_that_has_not_proved_itself_posts_nothing(rig, tmp_path):
    for f in taste.folder().glob("*"):
        f.unlink()                                                                            # forget everything it learned
    taste._cache.clear(); taste._model_cache.clear()
    for i in range(15):
        taste.record(meta(i, GOOD_CH), 1, "keep")
        taste.record(meta(i, BAD_CH), 0, "trash")                                              # 30 decisions: guessing, but not yet allowed to act
    taste.train()
    rig.clip()
    assert rig.tick(0) == [] and rig.tick(30) == [] and rig.provider.calls == []


def test_it_needs_a_platform_and_a_streamer_ticked(rig):
    rig.clip()
    autopilot.save({"platforms": []})
    assert rig.tick(0) == [] and rig.tick(30) == []
    autopilot.save({"platforms": ["tiktok"], "channels": []})
    assert rig.tick(0) == [] and rig.tick(30) == [] and rig.provider.calls == []


def test_clips_it_would_not_keep_and_streamers_you_did_not_tick_are_left_alone(rig):
    mine = rig.clip(BAD_CH)                                                                    # ticked streamer? no: and also a clip it dislikes
    autopilot.save({"channels": [GOOD_CH, BAD_CH]})
    dislike = rig.clip(BAD_CH)
    other = rig.clip(GOOD_CH)
    autopilot.save({"channels": [BAD_CH]})                                                     # only the streamer it dislikes is ticked
    assert rig.tick(0) == [] and rig.tick(30) == []
    assert all("autopilot" not in rig.read(x) for x in (mine, dislike, other)) and rig.provider.calls == []


def test_a_decision_of_yours_always_beats_autopilot(rig):
    kept, trashed, later = rig.clip(), rig.clip(), rig.clip()
    for path, st in ((kept, "keep"), (trashed, "trash")):
        m = rig.read(path); m["status"] = st; path.write_text(json.dumps(m))
    rig.tick(0)
    assert rig.ap_state(kept) is None and rig.ap_state(trashed) is None                        # you decided: hands off
    assert rig.ap_state(later) == "queued"
    rig.store.set_status(rig.read(later)["id"], "trash")                                       # trashed inside the veto window
    rig.tick(11)
    assert rig.ap_state(later) == "cancelled" and rig.provider.calls == []


def test_it_never_posts_where_the_clip_already_is(rig):
    autopilot.save({"platforms": ["tiktok", "instagram"]})
    p = rig.clip()
    m = rig.read(p)
    m["shares"] = {"tiktok": {"status": "done", "provider": "manual"}}                         # you posted it to TikTok yourself
    p.write_text(json.dumps(m))
    rig.tick(0)
    rig.tick(11)
    assert [c[0] for c in rig.provider.calls] == ["instagram"]
    done = rig.clip()
    m = rig.read(done); m["shares"] = {k: {"status": "opened"} for k in ("tiktok", "instagram")}; done.write_text(json.dumps(m))
    assert rig.tick(30) == [] and len(rig.provider.calls) == 1


def test_a_platform_that_is_not_connected_is_skipped_not_forced(rig):
    autopilot.save({"platforms": ["tiktok", "instagram"]})
    rig.provider.not_connected = {"instagram"}
    p = rig.clip()
    rig.tick(0); rig.tick(11)
    assert [c[0] for c in rig.provider.calls] == ["tiktok"] and rig.read(p)["autopilot"]["platforms"] == ["tiktok"]


def test_only_automatic_methods_are_allowed(rig):
    with pytest.raises(ValueError, match="posts by itself"):
        autopilot.save({"provider": "assistant"})                                              # the Share assistant needs a person at the keyboard


# ------------------------------------------------------------------ limits
def test_daily_cap_and_spacing_between_posts(rig):
    autopilot.save({"max_per_day": 2, "gap_min": 60, "max_age_h": 100, "delay_min": 0})
    a, b, c = rig.clip(), rig.clip(), rig.clip()
    rig.tick(0)                                                                                # queued
    assert [x["action"] for x in rig.tick(1)] == ["posted"]
    assert rig.tick(2) == [] and len(rig.provider.calls) == 1                                  # too soon after the last one
    assert [x["action"] for x in rig.tick(62)] == ["posted"]
    assert rig.tick(130) == [] and len(rig.provider.calls) == 2                                # cap of 2 per 24 h reached
    assert [x["action"] for x in rig.tick(24 * 60 + 70)] == ["posted"] and len(rig.provider.calls) == 3   # a day later


def test_old_clips_are_not_posted(rig):
    autopilot.save({"max_age_h": 6})
    old = rig.clip(age_s=7 * 3600)
    fresh = rig.clip()
    rig.tick(0); rig.tick(11)
    assert rig.ap_state(old) is None and rig.ap_state(fresh) == "posted"
    q = rig.clip(age_s=5.7 * 3600)
    rig.tick(12)                                                                               # queued while still fresh enough...
    assert rig.ap_state(q) == "queued"
    rig.tick(12 + 20)                                                                          # ...but it turned stale before its turn
    assert rig.ap_state(q) == "expired"


# ------------------------------------------------------------------ the vertical export
def test_it_waits_for_the_recorders_vertical_and_makes_one_itself_when_that_is_not_coming(rig, monkeypatch):
    made = []

    def fake_export(cid, start, end, vertical, captions, by="you", **kw):
        p, m = rig.store.find(cid)
        (p.parent / "made_v.mp4").write_bytes(b"x")
        with rig.store.lock:
            m["exports"] = [{"file": "made_v.mp4", "start": start, "end": end, "vertical": vertical, "captions": captions}]
            review._write_json(p, m)
        made.append((cid, start, end, vertical, captions))
        return "made_v.mp4"

    monkeypatch.setattr(rig.store, "export", fake_export)
    p = rig.clip(export=False, age_s=60)                                                       # the recorder is probably still rendering it
    rig.tick(0)
    assert rig.tick(11) == [] and made == [] and rig.provider.calls == []
    assert rig.ap_state(p) == "queued"
    assert [x["action"] for x in rig.tick(20)] == ["posted"]                                   # 20 minutes old now: nobody else is making it
    assert made == [(rig.read(p)["id"], 10.0, 40.0, True, False)]                             # the suggested trim, vertical, no captions
    assert rig.provider.calls[0][1] == "made_v.mp4"


def test_a_non_vertical_export_is_never_posted(rig):
    p = rig.clip()
    m = rig.read(p); m["exports"][0]["vertical"] = False; p.write_text(json.dumps(m))
    rig.tick(0)
    rig.store.export = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg failed: test"))
    rig.tick(30)                                                                               # old enough to export itself, but that fails
    assert rig.provider.calls == [] and rig.ap_state(p) == "retry"


# ------------------------------------------------------------------ failures
def test_a_failed_post_is_retried_once_later_and_then_given_up(rig):
    rig.provider.fail = True
    p = rig.clip()
    rig.tick(0)
    assert rig.tick(11)[0]["action"] == "failed" and rig.ap_state(p) == "retry"
    assert rig.tick(15) == [] and len(rig.provider.calls) == 1                                 # not before the retry time
    assert rig.tick(11 + 16)[0]["action"] == "failed" and rig.ap_state(p) == "failed"
    assert rig.tick(200) == [] and len(rig.provider.calls) == 2                                # two tries, never a third
    assert [e["action"] for e in autopilot.read_log()].count("failed") == 2
    assert "boom" in rig.read(p)["autopilot"]["message"]


def test_a_post_that_may_have_gone_out_is_never_repeated(rig):
    p = rig.clip()
    m = rig.read(p)
    m["autopilot"] = {"state": "posting", "attempts": 1}                                       # the program died in the middle of sending
    p.write_text(json.dumps(m))
    assert rig.tick(0) == [] and rig.tick(60) == [] and rig.provider.calls == []


def test_dont_post_this_one(rig):
    p = rig.clip()
    rig.tick(0)
    assert rig.ap.hold(rig.read(p)["id"])
    rig.tick(11)
    assert rig.ap_state(p) == "held" and rig.provider.calls == [] and rig.read(p)["status"] == "new"   # not a Trash: it teaches nothing
    assert taste.counts()["total"] == 120


# ------------------------------------------------------------------ safety net + learning loop
def posted_clip(rig, status, i):
    p = rig.clip(status=status)
    m = rig.read(p); m["autopilot"] = {"state": "posted", "at": rig.now - 1000 + i}; p.write_text(json.dumps(m))


def test_it_pauses_itself_when_you_trash_its_posts(rig):
    for i, st in enumerate(["keep", "trash", "keep", "trash", "trash", "keep"]):
        posted_clip(rig, st, i)
    p = rig.clip()
    acts = rig.tick(0)
    assert acts and acts[0]["action"] == "paused"
    s = autopilot.load()
    assert s["mode"] == "suggest" and "trashed 3 of its last 6" in s["paused_reason"]
    assert rig.tick(30) == [] and rig.provider.calls == [] and rig.ap_state(p) is None
    assert autopilot.save({"mode": "post"})["paused_reason"] == ""                              # switching it back on yourself clears the pause


def test_a_few_unhappy_judgements_are_not_enough_to_pause(rig):
    for i, st in enumerate(["keep", "trash", "keep", "keep"]):
        posted_clip(rig, st, i)                                                                # only 4 judged
    rig.clip()
    assert rig.tick(0)[0]["action"] == "queued" and autopilot.load()["mode"] == "post"


def test_its_own_posts_are_not_your_taste_but_your_own_posts_are(rig):
    before = taste.counts()["total"]
    rig.clip(); rig.tick(0); rig.tick(11)
    assert len(rig.provider.calls) == 1 and taste.counts()["total"] == before                  # autopilot agreeing with itself teaches nothing
    own = rig.clip(BAD_CH)
    m = rig.read(own)
    r = share.run(rig.store, share.ShareRequest(m["id"], m["exports"][0]["file"], ["tiktok"],
                                                {"tiktok": {"title": "t", "body": "b"}}, "auto"))
    assert r["tiktok"]["status"] == "done" and taste.counts()["total"] == before + 1           # you pressed Share yourself: a Keep
    assert taste.labels()[-1]["src"] == "posted" and taste.labels()[-1]["label"] == 1


def test_judging_its_posts_does_teach_it(rig):
    p = rig.clip(); rig.tick(0); rig.tick(11)
    before = taste.counts()
    rig.store.set_status(rig.read(p)["id"], "trash")                                            # "that was a bad pick"
    assert taste.counts()["trashes"] == before["trashes"] + 1


def test_the_model_is_refreshed_by_the_autopilot_when_decisions_changed(rig):
    taste.record(meta(900, BAD_CH), 0, "trash")
    assert taste.is_stale()
    rig.tick(0)
    assert not taste.is_stale()


# ------------------------------------------------------------------ settings
def test_settings_are_validated_in_plain_words():
    for bad, why in [({"mode": "yolo"}, "off, suggest or post"), ({"platforms": ["myspace"]}, "unknown platform"),
                     ({"channels": ["no spaces/slashes"]}, "bad channel"), ({"max_per_day": 0}, "posts per 24 hours must be between 1 and 20"),
                     ({"min_precision": 0.2}, "required accuracy must be between 60% and 95%"), ({"delay_min": "soon"}, "minutes before posting: use a number")]:
        with pytest.raises(ValueError, match=why):
            autopilot.save(bad)
    assert autopilot.load() == autopilot.DEFAULTS and autopilot.DEFAULTS["mode"] == "off"      # nothing was half-saved; and it starts OFF


def test_a_hand_edited_settings_file_falls_back_to_safe_values_one_setting_at_a_time():
    autopilot.path().parent.mkdir(parents=True, exist_ok=True)
    autopilot.path().write_text(json.dumps({"mode": "post", "max_per_day": 9999, "platforms": ["tiktok"], "provider": "assistant"}))
    s = autopilot.load()
    assert s["mode"] == "post" and s["platforms"] == ["tiktok"]                                # the good parts survive...
    assert s["max_per_day"] == autopilot.DEFAULTS["max_per_day"] and s["provider"] == "auto"    # ...the bad ones become the safe default
    autopilot.path().write_text("{ not json")
    assert autopilot.load()["mode"] == "off"


def test_channel_names_are_case_insensitive():
    assert autopilot.save({"channels": ["KaiCenat", "kaicenat", "xqc"]})["channels"] == ["kaicenat", "xqc"]


# ------------------------------------------------------------------ what the page and the command line show
def test_status_report_and_cli_text(rig):
    rig.clip(); rig.tick(0)
    st = autopilot.status(rig.store)
    json.dumps(st)                                                                              # the page gets this as JSON
    assert st["ready"]["ok"] and st["queue"][0]["channel"] == GOOD_CH and GOOD_CH in st["channels"] and st["counts"]["total"] == 120
    text = "\n".join(cli.autopilot_report(st))
    assert "Learned from 120 decisions" in text and "Ready to post: yes" in text and "Mode: post" in text and "waiting:" in text
    for f in taste.folder().glob("*"):
        f.unlink()
    taste._cache.clear(); taste._model_cache.clear()
    assert "Ready to post: no" in "\n".join(cli.autopilot_report(autopilot.status(rig.store)))


def test_cli_status_runs(rig, capsys):
    rc = cli.main(["autopilot", "status", "--out", str(rig.root)])
    out = capsys.readouterr().out
    assert rc == 0 and "Learned from" in out and "Mode: post" in out


def test_the_background_loop_really_runs_and_stops(rig):
    autopilot.save({"delay_min": 0, "max_per_day": 5})
    p = rig.clip()
    stop = threading.Event()
    t = autopilot.Autopilot(rig.store, stop).start(every=0.05)                                  # the thread `run` starts, ticking fast
    deadline = time.time() + 10
    while rig.ap_state(p) != "posted" and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    t.join(5)
    assert rig.ap_state(p) == "posted" and not t.is_alive() and len(rig.provider.calls) == 1


def test_run_starts_the_autopilot_thread_and_says_what_mode_it_is_in(rig, capsys):
    stop = threading.Event()
    cli.start_autopilot(Config(out_dir=str(rig.root)), stop)
    out = capsys.readouterr().out
    stop.set()
    assert "Autopilot: post" in out and "within the limits you set" in out
    autopilot.save({"mode": "off"})
    stop2 = threading.Event()
    cli.start_autopilot(Config(out_dir=str(rig.root)), stop2)
    stop2.set()
    assert "Autopilot: off" in capsys.readouterr().out


def server(rig):
    srv = review.QuietServer(("127.0.0.1", 0), review.make_handler(rig.store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    return srv, call


def test_review_page_api(rig):
    good, bad = rig.clip(GOOD_CH), rig.clip(BAD_CH)
    srv, call = server(rig)
    try:
        code, body = call("/")
        assert code == 200 and "openAutopilot" in body and "function apBadge" in body and "/*__AP__*/" not in body
        clips = {c["id"]: c for c in json.loads(call("/api/clips")[1])}
        g, b = clips[rig.read(good)["id"]], clips[rig.read(bad)["id"]]
        assert g["taste"]["pick"] is True and b["taste"]["pick"] is False and g["taste"]["p"] > b["taste"]["p"]
        code, body = call("/api/autopilot")
        assert code == 200 and json.loads(body)["settings"]["mode"] == "post"
        code, body = call("/api/autopilot/settings", {"mode": "banana"})
        assert code == 400 and "off, suggest or post" in body and autopilot.load()["mode"] == "post"
        assert call("/api/autopilot/settings", {"mode": "suggest", "max_per_day": 5})[0] == 200
        assert autopilot.load()["mode"] == "suggest" and autopilot.load()["max_per_day"] == 5
        assert call("/api/autopilot/settings", {"mode": "post", "paused_reason": "x"})[0] == 200
        assert autopilot.load()["paused_reason"] == ""                                          # the page cannot fake a pause message
        assert call("/api/autopilot/train", {})[0] == 200
        assert call("/api/autopilot/hold", {"id": rig.read(good)["id"]})[0] == 200 and rig.ap_state(good) == "held"
        assert call("/api/autopilot/hold", {"id": "nope"})[0] == 400
        before = taste.counts()["total"]
        assert call("/api/status", {"id": rig.read(bad)["id"], "status": "trash"})[0] == 200
        assert taste.counts()["total"] == before + 1                                            # the Trash button teaches it
    finally:
        srv.shutdown()


def test_the_page_script_is_valid_javascript():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    import re
    script = re.search(r"<script>(.*)</script>", review.PAGE, re.S).group(1)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "page.js"
        f.write_text(script, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_done_is_neither_regret_nor_a_clip_to_post(rig):
    for i, st in enumerate(["done", "done", "done", "trash", "keep", "keep"]):
        posted_clip(rig, st, i)
    metas = list(rig.store._metas())
    assert rig.ap.regret(metas) == (1, 3)                                                     # freeing space is not a judgement
    p = rig.clip(status="done")
    assert all(a["action"] != "paused" for a in rig.tick(0))
    assert rig.ap_state(p) is None and rig.tick(30) is not None and rig.provider.calls == []  # a done clip is never queued or posted


# ------------------------------------------------------------------ score rules: post 70+, leave 40-69 for you, trash under 40
def scored(rig, score, **kw):
    p = rig.clip(**kw)
    m = rig.read(p)
    m["score"] = score
    for e in m["exports"]:
        e["by"] = "recorder"                                                                 # the recorder's 9:16, not yours
    from autoclip.describe import suggest
    d = suggest(m, Config())                                                                 # exactly what the recorder writes
    m["description"] = {**d, "auto_body": d["body"], "title_src": "auto", "body_edited": False}
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def test_score_rules_post_only_high_scores_and_trash_low_ones(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    hi, mid, low = scored(rig, 85), scored(rig, 55), scored(rig, 20)
    acts = rig.tick(0)
    assert {a["action"] for a in acts} == {"queued", "trashed"}
    assert rig.ap_state(hi) == "queued" and rig.ap_state(mid) is None
    assert rig.read(low)["status"] == "trash" and rig.ap_state(low) == "trashed"
    assert rig.read(mid)["status"] == "new"                                                  # left for you to check
    rig.tick(11)
    assert [c[1] for c in rig.provider.calls] == [hi.name.replace(".clip.json", "_v.mp4")]
    rig.tick(200)
    assert len(rig.provider.calls) == 1                                                      # the 55 is never posted
    assert any(e["action"] == "trashed" for e in autopilot.read_log())


def test_its_trash_is_not_your_taste_and_undo_sticks(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    low = scored(rig, 10)
    before = taste.counts()["total"]
    rig.tick(0)
    assert rig.read(low)["status"] == "trash"
    assert taste.counts()["total"] == before                                                  # not learned as your Trash
    assert taste.import_existing(rig.root) == 0                                               # not even when old decisions are imported
    cid = rig.read(low)["id"]
    assert rig.store.set_status(cid, "new")                                                   # you bring it back
    rig.tick(1)
    assert rig.read(low)["status"] == "new"                                                   # and it stays back


def test_score_rules_spare_clips_you_worked_on_and_streamers_you_did_not_tick(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    mine = scored(rig, 10)
    m = rig.read(mine)
    m["description"]["title_src"] = "typed"
    mine.write_text(json.dumps(m), encoding="utf-8")
    other = scored(rig, 10, channel=BAD_CH)
    low = scored(rig, 10)
    rig.tick(0)
    assert rig.read(low)["status"] == "trash"                                                 # the rule did run
    assert rig.read(mine)["status"] == "new" and rig.read(other)["status"] == "new"


def test_score_rules_do_nothing_unless_posting_is_on(rig):
    autopilot.save({"mode": "suggest", "min_score": 70, "trash_below": 40})
    low = scored(rig, 5)
    rig.tick(0)
    assert rig.read(low)["status"] == "new"


def test_score_settings_are_checked():
    with pytest.raises(ValueError):
        autopilot.save({"min_score": 50, "trash_below": 60})
    with pytest.raises(ValueError):
        autopilot.save({"min_score": 101})
    assert autopilot.load()["min_score"] == 70 and autopilot.load()["trash_below"] == 40


def test_the_page_says_it_would_post_only_above_the_score_floor(rig):
    autopilot.save({"min_score": 70, "trash_below": 0})
    scored(rig, 90)
    scored(rig, 50)
    picks = {round(c["score"]): (c.get("taste") or {}).get("pick") for c in rig.store.list()}
    assert picks[50] is False


def test_a_trash_waits_out_your_veto_time_and_leaves_old_backlog_alone(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 10, "max_age_h": 24})
    fresh, old = scored(rig, 10), scored(rig, 10, age_s=3 * 86400)
    rig.tick(0)
    assert rig.read(fresh)["status"] == "new"                                                 # still inside your veto time
    rig.tick(11)
    assert rig.read(fresh)["status"] == "trash" and rig.read(old)["status"] == "new"


def test_a_missing_or_broken_score_is_never_trashed_and_never_crashes(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    a, b = scored(rig, None), scored(rig, "abc")
    rig.tick(0)
    rig.tick(30)
    assert rig.read(a)["status"] == "new" and rig.read(b)["status"] == "new" and rig.provider.calls == []
    assert len(rig.store.list()) == 2                                                         # the review page still works


def test_a_title_you_typed_before_this_update_protects_the_clip(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    p = scored(rig, 10)
    m = rig.read(p)
    m["description"] = {"title": "my own words", "body": "x", "alts": ["a suggestion"]}       # no markers: an older record
    p.write_text(json.dumps(m), encoding="utf-8")
    rig.tick(0)
    assert rig.read(p)["status"] == "new"


def test_a_keep_pressed_at_the_same_moment_wins(rig, monkeypatch):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    p = scored(rig, 10)
    real = autopilot._touched

    def keep_in_between(m):                                                                   # the page writes the file mid-check
        q = rig.read(p)
        q["status"] = "keep"
        time.sleep(0.02)
        p.write_text(json.dumps(q), encoding="utf-8")
        return real(m)
    orig = rig.ap._auto_trash

    def racing(path, score, limit, now):
        monkeypatch.setattr(autopilot, "_touched", keep_in_between)
        return orig(path, score, limit, now)
    rig.ap._auto_trash = racing
    rig.tick(0)
    assert rig.read(p)["status"] == "keep"


def test_after_undo_your_own_trash_is_yours(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    p = scored(rig, 10)
    rig.tick(0)
    cid = rig.read(p)["id"]
    assert rig.read(p)["status"] == "trash"
    rig.store.set_status(cid, "new")
    assert rig.ap_state(p) == "restored"
    rig.store.set_status(cid, "trash")
    rig.tick(1)
    assert rig.read(p)["status"] == "trash" and rig.ap_state(p) == "restored"


def test_a_hand_edited_trash_score_above_the_post_score_is_capped(rig):
    autopilot.path().write_text(json.dumps({"min_score": 70, "trash_below": 90}), encoding="utf-8")
    assert autopilot.load()["trash_below"] == 70


def test_scores_are_judged_as_the_card_shows_them(rig):
    autopilot.save({"min_score": 70, "trash_below": 40, "delay_min": 0})
    shown_40, shown_70 = scored(rig, 39.6), scored(rig, 69.6)
    rig.tick(0)
    assert rig.read(shown_40)["status"] == "new" and rig.ap_state(shown_70) == "queued"
    assert autopilot.clip_score({"score": 39.4}) == 39 and autopilot.clip_score({"score": float("inf")}) is None
    assert autopilot.clip_score({"score": 10 ** 400}) is None                                  # a corrupted file never crashes it


def test_every_post_tags_the_streamer_even_with_an_old_description(rig):
    p = rig.clip()
    m = rig.read(p)
    sent = []
    rig.provider.publish = lambda job: sent.append(job.text) or Result(True, "done", "ok", "https://x")
    share.run(rig.store, share.ShareRequest(m["id"], m["exports"][0]["file"], ["tiktok"], {"tiktok": {"title": "t", "body": "Old text\n\n#fyp #funny"}}, "auto"))
    assert "@fav #fyp #funny" in sent[-1]
    share.run(rig.store, share.ShareRequest(m["id"], m["exports"][0]["file"], ["youtube"], {"youtube": {"title": "t", "body": "Hi @FAV look"}}, "auto"))
    assert sent[-1].lower().count("@fav") == 1                                                   # never twice
