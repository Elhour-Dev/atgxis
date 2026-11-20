"""Paid clipping campaigns: the rules file, what a campaign changes about a clip, the pre-post check, the post log and money,
the Autopilot's limits, and the voice-over. Nothing here posts anything: providers are fakes."""
import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from autoclip import autopilot, campaigns, review, share, style
from autoclip.campaigns import CampaignError
from autoclip.config import Config

from clipfactory import GOOD_CH, meta, write_clip
from test_autopilot import Rig, rig  # noqa: F401  (the autopilot test rig)
from test_share_settings import make_root

READY = {"id": "gta-clips", "disclosure": True, "must_show": None, "must_show_ok": True, "warn": None, "layout": "blur"}


def raw(**kw) -> dict:
    rules = kw.pop("rules", {})
    c = {"name": "GTA Clips", "url": "https://whop.com/gta", "channels": ["ch"], "platforms": ["tiktok", "youtube"], "rate_per_1k": 2.0,
         "min": 5, "cap": 100, "ends": None, "active": True,
         "rules": {"disclosure": "Ad", "hashtags": ["#gta6", "#ad"], "must_show": None, "min_len_s": 0, "max_len_s": 0}}
    c.update(kw)
    c["rules"] = {**c["rules"], **rules}
    return c


def save(*cs) -> list:
    return campaigns.save_text(json.dumps({"campaigns": list(cs)}))


class FakeAuto(share.Provider):
    name, label, automatic = "auto", "fake", True

    def __init__(self):
        self.jobs = []

    def publish(self, job):
        self.jobs.append(job)
        return share.Result(True, "done", "ok", f"https://www.tiktok.com/@me/video/{len(self.jobs)}")


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    """A clips folder with one exported clip of streamer `ch`, a fake automatic poster and a store."""
    root = make_root(tmp_path)
    prov = FakeAuto()
    monkeypatch.setitem(share.PROVIDERS, "auto", prov)
    return root, review.Store(root, Config()), prov


def make_ready(root: Path, **extra) -> None:
    p = root / "ch" / "s" / "c.clip.json"
    m = json.loads(p.read_text())
    m["exports"][0]["campaign"] = {**READY, **extra}
    p.write_text(json.dumps(m))


def req(platforms=("tiktok",), body="Funny moment #fyp"):
    return share.ShareRequest("ch/s/c", "c_v.mp4", list(platforms), {p: {"title": "Wow", "body": body} for p in platforms}, "auto")


# ---------------------------------------------------------------------------------------------- the rules file
def test_platform_names_match_the_sharing_module():
    assert campaigns.PLATFORMS == tuple(share.PLATFORMS) and set(campaigns.LABELS) == set(share.PLATFORMS) == set(campaigns.FLAG_HELP)


@pytest.mark.parametrize("change,msg", [
    ({"url": "javascript:alert(1)"}, "url must be a web link"),
    ({"name": ""}, "name is required"),
    ({"channels": []}, "channels must be"),
    ({"platforms": ["myspace"]}, "platforms must be"),
    ({"rate_per_1k": "lots"}, "rate_per_1k must be a number"),
    ({"cap": -1}, "cap"),
    ({"ends": "next week"}, "ends must be a date"),
    ({"ends": "9999-12-31"}, "ends must be a date"),
    ({"ends": "1960-01-01"}, "ends must be a date"),
    ({"url": "http://whop.com/gta"}, "starting with https://"),
    ({"active": "yes"}, "active must be true or false"),
    ({"rules": {"must_show": "logo"}}, "must_show must be"),
    ({"rules": {"hashtags": ["two words"]}}, "hashtags must be"),
    ({"rules": {"min_len_s": 70, "max_len_s": 60}}, "longer than"),
])
def test_a_bad_campaign_is_refused_in_plain_words_and_nothing_is_written(change, msg):
    with pytest.raises(CampaignError, match=msg) as e:
        save(raw(**change))
    assert "campaign 1" in str(e.value) and not campaigns.path().exists()


def test_a_campaign_is_off_until_you_switch_it_on():
    c = dict(raw())
    del c["active"]
    assert save(c)[0]["active"] is False and campaigns.for_channel("ch") is None


def test_a_good_campaign_is_cleaned_and_names_must_be_unique():
    c = save(raw(channels=["KaiCenat"], rules={"hashtags": ["gta6", "#ad"]}))[0]
    assert c["id"] == "gta-clips" and c["channels"] == ["kaicenat"] and c["rules"]["hashtags"] == ["#gta6", "#ad"]
    assert c["rules"]["need_speech"] is False and c["budget_empty"] is False
    with pytest.raises(CampaignError, match="same name"):
        save(raw(), raw())
    with pytest.raises(CampaignError, match="not valid JSON"):
        campaigns.save_text("{ nope")


def test_a_damaged_campaign_in_a_hand_edited_file_is_left_out_and_named():
    campaigns.path().parent.mkdir(parents=True, exist_ok=True)
    campaigns.path().write_text(json.dumps({"campaigns": [raw(), raw(name="Broken", platforms=[])]}), encoding="utf-8")
    camps, problems = campaigns.load_all()
    assert [c["name"] for c in camps] == ["GTA Clips"] and len(problems) == 1 and "Broken" in problems[0]
    campaigns.path().write_text("{ not json", encoding="utf-8")
    assert campaigns.load() == [] and "not valid JSON" in campaigns.load_all()[1][0]


def test_no_campaigns_file_means_nothing_changes():
    assert campaigns.load() == [] and campaigns.for_channel("ch") is None
    assert campaigns.plan({"channel": "ch"}, None, True, Config()) == {"campaign": None, "layout": None, "record": None}
    assert "Example campaign" in campaigns.raw_text()


# ---------------------------------------------------------------------------------------------- live or not + money
def test_a_campaign_stops_being_live_when_off_ended_out_of_budget_or_capped():
    now = time.time()
    c = save(raw())[0]
    assert campaigns.live_reason(c, now) is None and campaigns.for_channel("CH", now)["id"] == "gta-clips"
    assert "switched off" in campaigns.live_reason(save(raw(active=False))[0], now)
    assert "budget" in campaigns.live_reason(save(raw(budget_empty=True))[0], now)
    ended = save(raw(ends="2020-01-01"))[0]
    assert "ended" in campaigns.live_reason(ended, now) and campaigns.for_channel("ch", now) is None
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    assert campaigns.live_reason(save(raw(ends=today))[0], now) is None              # a date ends at the END of that day
    c = save(raw(cap=10))[0]
    campaigns.add_post(c, "ch/s/c", "tiktok", "https://www.tiktok.com/@me/video/1")
    campaigns.update_post("ch/s/c:tiktok", views=4000)                                # 4000 views x $2 / 1000 = $8 < $10 cap
    assert campaigns.live_reason(c, now) is None
    campaigns.update_post("ch/s/c:tiktok", views=6000)
    assert "cap" in campaigns.live_reason(c, now) and campaigns.for_channel("ch", now) is None


def test_earnings_follow_the_formula_with_cap_fee_and_minimum():
    assert campaigns.earnings(10_000, 2, 100, 5) == 18.0                             # 20 * 0.9
    assert campaigns.earnings(1_000_000, 2, 100, 5) == 90.0                          # capped at 100, then the fee
    assert campaigns.earnings(1_000, 2, 100, 5) == 0.0                               # 1.80 is under the $5 minimum
    assert campaigns.earnings(1_000_000, 2, 0, 0) == 1800.0                          # cap 0 = no cap
    c = save(raw())[0]
    for pl, v, st in [("tiktok", 10_000, "approved"), ("youtube", 5_000, "submitted")]:
        campaigns.add_post(c, "ch/s/c", pl, None)
        campaigns.update_post(f"ch/s/c:{pl}", status=st, views=v)
    s = campaigns.stats(c)
    assert (s["posted"], s["submitted"], s["approved"], s["views"]) == (2, 2, 1, 15_000)
    assert s["earnings"] == 18.0 and s["earnings_if_approved"] == 27.0


def test_the_to_submit_list_turns_late_after_25_minutes():
    c = save(raw())[0]
    t0 = time.time()
    campaigns.add_post(c, "ch/s/c", "tiktok", "https://www.tiktok.com/@me/video/1", now=t0)
    campaigns.add_post(c, "ch/s/c", "tiktok", None, now=t0 + 5)                       # the same post again: no second row
    assert len(campaigns.posts()) == 1
    x = campaigns.to_submit(now=t0 + 10 * 60)[0]
    assert x["late"] is False and x["left_s"] == 20 * 60 and x["campaign_url"] == "https://whop.com/gta"
    assert campaigns.to_submit(now=t0 + 26 * 60)[0]["late"] is True
    campaigns.update_post("ch/s/c:tiktok", status="submitted")
    assert campaigns.to_submit() == []
    with pytest.raises(CampaignError, match="why it was rejected"):
        campaigns.update_post("ch/s/c:tiktok", status="rejected")
    with pytest.raises(CampaignError, match="web link"):
        campaigns.update_post("ch/s/c:tiktok", url="ftp://x")


def test_to_submit_is_one_row_per_clip_timed_from_its_earliest_post():
    c = save(raw(platforms=["tiktok", "youtube", "instagram"]))[0]
    t0 = time.time()
    campaigns.add_post(c, "ch/s/c", "youtube", "https://youtube.com/shorts/A", "Wow", now=t0 + 60)
    campaigns.add_post(c, "ch/s/c", "tiktok", "https://www.tiktok.com/@me/video/1", "Wow", now=t0)
    campaigns.add_post(c, "ch/s/c", "instagram", None, "Wow", now=t0 + 120)
    campaigns.add_post(c, "ch/s/d", "tiktok", "https://www.tiktok.com/@me/video/2", "Other", now=t0 + 300)
    g = campaigns.to_submit_groups(now=t0 + 26 * 60)
    assert [x["clip"] for x in g] == ["ch/s/c", "ch/s/d"]
    first = g[0]
    assert [x["platform"] for x in first["posts"]] == ["tiktok", "youtube", "instagram"]
    assert first["links"] == "https://www.tiktok.com/@me/video/1\nhttps://youtube.com/shorts/A"     # one per line, links you have
    assert first["late"] is True and first["left_s"] == 4 * 60 and g[1]["late"] is False           # the earliest post starts the clock
    campaigns.update_post("ch/s/c:tiktok", status="submitted")
    assert [x["platform"] for x in campaigns.to_submit_groups(now=t0)[0]["posts"]] == ["youtube", "instagram"]


def test_a_lock_left_by_a_crashed_process_is_taken_over_and_never_blocks_for_good():
    c = save(raw())[0]
    lock = campaigns.posts_path().with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    old = time.time() - 60
    import os
    os.utime(lock, (old, old))
    campaigns.add_post(c, "ch/s/c", "tiktok", None)                                    # stale lock: taken over
    assert len(campaigns.posts()) == 1 and not lock.exists()
    lock.write_text("")                                                                 # a fresh lock: wait, then a plain error
    import pytest as _p
    from unittest import mock
    with mock.patch.object(campaigns, "STALE_LOCK_S", 999), mock.patch.object(campaigns.time, "sleep", lambda s: None):
        real = campaigns.time.time
        ticks = iter(range(0, 10_000, 5))
        with mock.patch.object(campaigns.time, "time", lambda: real() + next(ticks)):
            with _p.raises(CampaignError, match="busy"):
                campaigns.update_post("ch/s/c:tiktok", status="submitted")
    lock.unlink()
    assert campaigns.update_post("ch/s/c:tiktok", status="submitted")["status"] == "submitted"   # the thread lock was released


# ---------------------------------------------------------------------------------------------- what a campaign changes in a clip
def test_the_layout_never_hides_what_must_show():
    split = {"mode": "split", "split": 0.4, "game_fit": "cover", "facecam": {"x": 0, "y": 0, "w": 0.2, "h": 0.2}, "game": None,
             "captions": {"x": 0.05, "y": 0.7, "w": 0.9, "h": 0.1}}
    assert campaigns.fit_layout(split, None) == (split, None, True)
    assert campaigns.fit_layout(split, "facecam") == (split, None, True)              # the webcam is the top panel
    lay, warn, ok = campaigns.fit_layout(split, "overlay")
    assert lay == {"mode": "blur", "captions": split["captions"]} and "overlay" in warn and ok
    whole = {**split, "game_fit": "blur"}
    assert campaigns.fit_layout(whole, "handle") == (whole, None, True)               # the whole frame is already visible
    lay, warn, _ = campaigns.fit_layout({"mode": "crop"}, "facecam")
    assert lay["mode"] == "blur" and "crop" in warn
    assert "Check" in campaigns.fit_layout({"mode": "blur"}, "facecam")[1]           # not sure there is a webcam: say so


def test_disclosure_hook_and_handle_are_burned_in_and_merged_with_captions():
    ass = campaigns.overlay_ass(None, True, "Arial", 30, disclosure="Ad", hook="{\\b1}Wait\nfor it", handle="@fav")
    assert "PlayResY: 1920" in ass and "Dialogue: 2,0:00:00.00,0:00:03.00,Disc,,0,0,0,,Ad" in ass
    hook = next(line for line in ass.splitlines() if ",Hook," in line)
    typed = hook.split("}", 1)[1]                                                     # after our own pop-in tags
    assert typed == "b1Wait for it" and hook.count("{") == 1 and ",0:00:02.50,Hook," in hook   # no ASS override tricks from typed text
    assert "Dialogue: 1,0:00:00.00,0:00:30.00,Handle" in ass
    from autoclip.captions import build_ass
    cap = build_ass([("hello", 11.0, 11.5), ("there", 11.5, 12.0)], 10.0, 20.0, True)
    both = campaigns.overlay_ass(cap, True, "Arial", 10, disclosure="Ad")
    head, events = both.split("[Events]")
    assert "Style: Cap," in head and "Style: Disc," in head and ",Cap,," in events and ",Disc,,0,0,0,,Ad" in events
    assert campaigns.overlay_ass(cap, True, "Arial", 10) == cap                       # nothing to add: captions untouched
    assert campaigns.overlay_ass(None, True, "Arial", 10) is None


def test_the_post_text_gets_ad_first_the_hashtags_and_one_credit_line():
    c = save(raw())[0]
    m = {"channel": "ch", "source_url": "https://twitch.tv/ch"}
    t, x = share.compose("tiktok", "Wow", "Funny moment\n\n@ch #fyp #ad")
    t, x = campaigns.finalize_text("tiktok", t, x, c, m, Config())
    assert x.startswith("#ad Wow") and x.lower().count("#ad") == 1 and x.count("#gta6") == 1
    assert x.count("🎥 Clip from") == 1 and x.splitlines()[-1].startswith("@ch #fyp")
    assert campaigns.finalize_text("tiktok", t, x, c, m, Config()) == (t, x)          # a second pass changes nothing
    assert campaigns.check(c, m, {"vertical": True, "start": 0, "end": 30, "campaign": READY}, "tiktok", t, x) == []


def test_the_pre_post_check_names_every_problem():
    c = save(raw(rules={"must_show": "facecam", "min_len_s": 40, "max_len_s": 60}))[0]
    plain = {"vertical": True, "start": 0, "end": 30}
    errs = " | ".join(campaigns.check(c, {}, plain, "instagram", "Wow", "Wow no tags"))
    for bit in ["Instagram Reels is not allowed", "no burned-in \"Ad\" disclosure", "must start with \"#ad\"", "missing hashtags: #gta6 #ad",
                "at least 40 s", "facecam must be visible"]:
        assert bit in errs
    assert "at most 60 s" in " ".join(campaigns.check(c, {}, {**plain, "end": 90}, "tiktok", "", ""))
    off = save(raw(active=False))[0]
    assert "not live" in campaigns.check(off, {}, plain, "tiktok", "", "")[0]


def test_trim_is_fitted_to_the_length_rules():
    c = save(raw(rules={"min_len_s": 20, "max_len_s": 25}))[0]
    assert campaigns.fit_trim(10, 40, 60, c) == (10, 35)
    assert campaigns.fit_trim(10, 20, 60, c) == (10, 30)
    assert campaigns.fit_trim(50, 58, 60, c) == (40, 60)
    assert campaigns.fit_trim(0, 10, 12, c) is None                                    # the draft itself is too short


# ---------------------------------------------------------------------------------------------- posting through share.run
def test_without_a_campaign_posting_is_unchanged(shop):
    root, store, prov = shop
    share.run(store, req())
    assert prov.jobs[0].paid is False and not prov.jobs[0].text.startswith("#ad") and campaigns.posts() == []


def test_a_campaign_clip_is_blocked_until_its_export_and_platform_pass(shop):
    root, store, prov = shop
    save(raw())
    with pytest.raises(share.ShareError, match="disclosure"):
        share.run(store, req())
    make_ready(root)
    with pytest.raises(share.ShareError, match="Instagram Reels is not allowed"):
        share.run(store, req(("tiktok", "instagram")))
    assert prov.jobs == []                                                             # nothing goes out when any platform fails


def test_a_campaign_post_is_marked_paid_logged_and_listed_to_submit(shop):
    root, store, prov = shop
    save(raw())
    make_ready(root)
    res = share.run(store, req())
    job = prov.jobs[0]
    assert job.paid is True and job.text.startswith("#ad Wow") and "#gta6" in job.text and "🎥" in job.text
    assert res["tiktok"]["paid_flag"] == campaigns.FLAG_HELP["tiktok"]                 # the fake could not set it: the card says so
    sh = json.loads((root / "ch" / "s" / "c.clip.json").read_text())["shares"]["tiktok"]
    assert sh["campaign"] == "gta-clips" and sh["paid_flag"].startswith("not set")
    todo = campaigns.to_submit()
    assert [(x["clip"], x["platform"], x["url"]) for x in todo] == [("ch/s/c", "tiktok", "https://www.tiktok.com/@me/video/1")]


def test_a_draft_waits_for_you_to_mark_it_posted_before_the_30_minutes_start(shop, monkeypatch):
    root, store, prov = shop
    save(raw())
    make_ready(root)
    monkeypatch.setattr(prov, "publish", lambda job: share.Result(True, "draft", "in your inbox"))
    share.run(store, req())
    assert campaigns.posts() == []
    assert share.mark(store, "ch/s/c", "tiktok", "done") and [x["platform"] for x in campaigns.to_submit()] == ["tiktok"]


def test_a_word_like_credit_in_the_title_is_not_taken_for_the_credit_line():
    c = save(raw())[0]
    m = {"channel": "ch", "source_url": "https://twitch.tv/ch"}
    for title in ["He maxed his credit card", "🎥 crazy moment"]:
        _, x = campaigns.finalize_text("tiktok", title, title + "\n\nlol", c, m, Config())
        assert "🎥 Clip from Ch: https://twitch.tv/ch" in x


def test_only_an_export_of_the_right_length_counts():
    c = save(raw(rules={"min_len_s": 20, "max_len_s": 30}))[0]
    assert campaigns.length_ok({"start": 0, "end": 25}, c) and not campaigns.length_ok({"start": 0, "end": 40}, c)
    assert not campaigns.length_ok({"start": 0, "end": 10}, c) and not campaigns.length_ok({}, c)


def test_a_post_you_made_yourself_goes_on_the_list_when_you_mark_it(shop):
    root, store, _ = shop
    save(raw())
    assert share.mark(store, "ch/s/c", "youtube", "done")
    assert [(x["platform"], x["url"]) for x in campaigns.to_submit()] == [("youtube", None)]
    assert share.mark(store, "ch/s/c", "instagram", "done") and len(campaigns.posts()) == 1      # not a campaign platform


def test_a_rejection_is_kept_as_a_bad_style_example_with_its_reason(shop):
    root, store, _ = shop
    save(raw())
    campaigns.add_post(campaigns.load()[0], "ch/s/c", "tiktok", None)
    with pytest.raises(CampaignError):
        store.update_campaign_post("ch/s/c:tiktok", {"status": "rejected"})
    store.update_campaign_post("ch/s/c:tiktok", {"status": "rejected", "reason": "Facecam not visible"})
    rows = [r for r in style.read_examples() if r["ev"] == "campaign_reject"]
    assert len(rows) == 1 and rows[0]["verdict"] == "bad" and rows[0]["reason"] == "gta-clips: Facecam not visible"
    store.update_campaign_post("ch/s/c:tiktok", {"views": 1200})                       # editing views later adds no second example
    assert len([r for r in style.read_examples() if r["ev"] == "campaign_reject"]) == 1


def test_the_card_shows_the_campaign_and_what_blocks_post(shop):
    root, store, _ = shop
    assert store.list()[0]["campaign"] is None
    save(raw(rules={"need_speech": True}))
    k = store.list()[0]["campaign"]
    assert k["live"] and not k["ready"] and "disclosure" in " ".join(k["problems"]["tiktok"]) and k["warn"]
    make_ready(root)
    k = store.list()[0]["campaign"]
    assert k["ready"] and k["problems"] == {"tiktok": [], "youtube": []}


def test_review_page_campaign_api(shop):
    from test_share_settings import server
    root, store, _ = shop
    srv, call = server(root)
    try:
        code, body = call("/api/campaigns/save", {"text": json.dumps({"campaigns": [raw(platforms=[])]})})
        assert code == 400 and "platforms must be" in body
        assert call("/api/campaigns/save", {"text": json.dumps({"campaigns": [raw()]})})[0] == 200
        code, v = call("/api/campaigns")
        assert code == 200 and v["campaigns"][0]["id"] == "gta-clips" and v["live"]["gta-clips"] is None and "gta-clips" in v["stats"]
        campaigns.add_post(campaigns.load()[0], "ch/s/c", "tiktok", None)
        code, todo = call("/api/campaigns/todo")
        assert code == 200 and [x["platform"] for x in todo[0]["posts"]] == ["tiktok"]
        code, row = call("/api/campaigns/post", {"id": "ch/s/c:tiktok", "status": "submitted", "url": "https://www.tiktok.com/@me/video/9"})
        assert code == 200 and row["status"] == "submitted" and row["url"].endswith("/9")
        assert call("/api/campaigns/post", {"id": "nope", "status": "submitted"})[0] == 400
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------------------------- the Autopilot
def campaign_export(rig, monkeypatch):
    """Stand-in for Store.export: a campaign-ready 9:16 file, as the real export records it."""
    made = []

    def fake_export(cid, start, end, vertical, captions, by="you", hook=None, voiceover=False):
        p, m = rig.store.find(cid)
        cp = campaigns.plan(m, None, vertical, rig.store.cfg)
        (p.parent / "made_v_ad.mp4").write_bytes(b"x")
        with rig.store.lock:
            m["exports"] = [{"file": "made_v_ad.mp4", "start": start, "end": end, "vertical": vertical, "captions": captions,
                             **({"campaign": cp["record"]} if cp["record"] else {})}]
            review._write_json(p, m)
        made.append((start, end))
        return "made_v_ad.mp4"

    monkeypatch.setattr(rig.store, "export", fake_export)
    return made


def test_autopilot_posts_for_a_live_campaign_streamer_you_did_not_tick(rig, monkeypatch):
    made = campaign_export(rig, monkeypatch)
    autopilot.save({"channels": []})
    save(raw(channels=[GOOD_CH], platforms=["tiktok"], rules={"max_len_s": 20}))
    p = rig.clip()                                                                     # its plain 9:16 has no disclosure: never used
    assert [a["action"] for a in rig.tick(0)] == ["queued"]
    assert [a["action"] for a in rig.tick(11)] == ["posted"]
    assert made == [(10.0, 30.0)] and rig.provider.calls[0][1] == "made_v_ad.mp4"     # trimmed to the 20 s maximum
    assert [(x["clip"], x["platform"]) for x in campaigns.posts()] == [(rig.read(p)["id"], "tiktok")]


def test_autopilot_never_trashes_clips_of_a_streamer_you_did_not_tick(rig):
    autopilot.save({"channels": [], "trash_below": 40, "min_score": 70})
    save(raw(channels=[GOOD_CH], platforms=["tiktok"]))
    p = write_clip(rig.root, {**meta(500, GOOD_CH, created=rig.now - 20 * 60), "score": 10})
    rig.tick(0)
    assert rig.read(p)["status"] == "new"


def test_autopilot_stops_when_the_campaign_ends_and_keeps_to_its_platforms(rig, monkeypatch):
    campaign_export(rig, monkeypatch)
    autopilot.save({"channels": []})
    save(raw(channels=[GOOD_CH], platforms=["youtube"]))                             # you only ticked TikTok
    rig.clip()
    assert rig.tick(0) == [] and rig.tick(30) == []
    save(raw(channels=[GOOD_CH], platforms=["tiktok"]))
    p = rig.clip()
    rig.tick(0)
    assert rig.ap_state(p) == "queued"
    save(raw(channels=[GOOD_CH], platforms=["tiktok"], budget_empty=True))           # you marked the budget empty
    assert rig.tick(11) == [] and rig.ap_state(p) == "cancelled" and rig.provider.calls == []


def test_autopilot_skips_a_clip_that_cannot_meet_the_length_rule(rig):
    autopilot.save({"channels": []})
    save(raw(channels=[GOOD_CH], platforms=["tiktok"], rules={"min_len_s": 90}))     # drafts are 60 s
    p = rig.clip()
    acts = rig.tick(0)
    assert acts[0]["action"] == "skipped" and "90 s" in acts[0]["message"] and rig.ap_state(p) == "skipped"


def test_autopilot_reuses_a_ready_campaign_export(rig, monkeypatch):
    made = campaign_export(rig, monkeypatch)
    save(raw(channels=[GOOD_CH], platforms=["tiktok"]))
    p = rig.clip()
    m = rig.read(p)
    m["exports"][0]["campaign"] = dict(READY)
    p.write_text(json.dumps(m))
    rig.tick(0)
    assert [a["action"] for a in rig.tick(11)] == ["posted"] and made == [] and rig.provider.calls[0][1].endswith("_v.mp4")


# ---------------------------------------------------------------------------------------------- real ffmpeg: export + voice-over
def _ffmpeg_with_ass() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return r.returncode == 0 and " ass " in r.stdout


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_with_ass(), reason="needs ffmpeg with the ass filter")


def _draft(root: Path, audio: bool = True, dur: int = 6) -> Path:
    d = root / "ch" / "s"
    d.mkdir(parents=True, exist_ok=True)
    src = d / "c.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={dur}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={dur}", "-ac", "2", "-c:a", "aac", "-shortest"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    m = {"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 50, "duration_s": float(dur), "exports": [],
         "created": "2026-01-01T00:00:00", "source_url": "https://twitch.tv/ch", "spike": {}, "suggest_start_s": 0, "suggest_end_s": dur}
    (d / "c.clip.json").write_text(json.dumps(m))
    return d


def _audio(f: Path) -> tuple[int, float]:
    """(channels, seconds) of the first audio stream, from the decoded samples (a container length can lie)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0", str(f)],
                       capture_output=True, text=True)
    ch = int(r.stdout.strip() or 0)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(f), "-map", "0:a:0", "-f", "null", "-"], capture_output=True, text=True)
    import re
    t = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    h, m, sec = t[-1] if t else ("0", "0", "0")
    return ch, int(h) * 3600 + int(m) * 60 + float(sec)


def _wav(tmp_path: Path) -> str:
    w = tmp_path / "vo.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=800:duration=2", str(w)], check=True)
    return base64.b64encode(w.read_bytes()).decode()


@needs_ffmpeg
def test_a_campaign_export_burns_the_disclosure_and_is_recorded(tmp_path):
    root = tmp_path / "clips"
    d = _draft(root)
    save(raw(rules={"must_show": "overlay"}))
    store = review.Store(root, Config(out_fps=0))
    name = store.export("ch/s/c", 1.0, 4.0, True, False)
    assert name.endswith("_v_ad.mp4") and (d / name).is_file()
    e = json.loads((d / "c.clip.json").read_text())["exports"][-1]
    assert e["campaign"]["id"] == "gta-clips" and e["campaign"]["disclosure"] and e["layout"] == "blur"
    assert campaigns.export_ready(e, campaigns.load()[0])


@needs_ffmpeg
@pytest.mark.parametrize("audio", [True, False])
def test_voice_over_and_hook_are_mixed_in_and_tagged(tmp_path, audio):
    root = tmp_path / "clips"
    d = _draft(root, audio, dur=12)
    store = review.Store(root, Config(out_fps=0))
    with pytest.raises(ValueError, match="voice-over first"):
        store.export("ch/s/c", 1.0, 4.0, True, False, voiceover=True)
    with pytest.raises(ValueError, match="must be a"):
        store.save_voiceover("ch/s/c", _wav(tmp_path), "exe")
    with pytest.raises(ValueError, match="one piece"):
        store.save_voiceover("ch/s/c", "%%%not base64", "wav")
    vo = store.save_voiceover("ch/s/c", _wav(tmp_path), "wav")
    assert vo["file"] == "c_vo.wav" and (d / "c_vo.wav").is_file()
    name = store.export("ch/s/c", 1.0, 9.0, True, False, hook="Wait for it", voiceover=True)
    assert name.endswith("_hk_vo.mp4")
    ch, secs = _audio(d / name)                        # a 2 s voice-over must not cut the 8 s clip's sound short, or make it mono
    assert ch == 2 and abs(secs - 8.0) < 0.3, (ch, secs)
    assert json.loads((d / "c.clip.json").read_text())["exports"][-1]["originality"] == {"voiceover": True, "hook": True}
    store.set_status("ch/s/c", "trash")
    store.purge()
    assert not (d / "c_vo.wav").exists()                                               # deleting trashed files takes the voice-over too


# ---------------------------------------------------------------------------------------------- damaged files, odd input
def test_a_damaged_post_log_is_kept_aside_never_overwritten():
    c = save(raw())[0]
    campaigns.posts_path().write_text('{"posts": [{"id": "a:tiktok", "campaign": "gta', encoding="utf-8")
    campaigns.add_post(c, "ch/s/c", "tiktok", None)
    kept = list(campaigns.posts_path().parent.glob("campaign_posts.damaged-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8").startswith('{"posts"') and len(campaigns.posts()) == 1


def test_a_hand_damaged_post_row_never_breaks_the_lists():
    save(raw())
    campaigns.posts_path().write_text(json.dumps({"posts": [
        {"id": "x:tiktok", "campaign": "gta-clips", "clip": "x", "platform": "tiktok", "status": "to_submit", "posted_at": "abc", "views": "lots"},
        {"id": "y:tiktok", "campaign": "gta-clips", "clip": "y", "status": "to_submit", "posted_at": 1},
        {"id": "z:tiktok", "campaign": "gta-clips", "clip": "z", "platform": "tiktok", "status": "to_submit", "posted_at": time.time()}]}),
        encoding="utf-8")
    assert [g["clip"] for g in campaigns.to_submit_groups()] == ["z"]
    assert campaigns.stats(campaigns.load()[0])["views"] == 0


def test_a_private_youtube_upload_is_listed_with_its_public_link():
    c = save(raw())[0]
    row = campaigns.add_post(c, "ch/s/c", "youtube", "https://studio.youtube.com/video/VID12345/edit")
    assert row["url"] == "https://www.youtube.com/shorts/VID12345"


@pytest.mark.parametrize("ends,ok", [("2026-12-31", True), ("2026-12-31T18:00", True), ("20261231", False), (20261231, False),
                                     ("2026-W01", False), ("1999-12-31", False), ("2200-12-31", True)])
def test_only_the_two_date_forms_are_accepted(ends, ok):
    if ok:
        assert save(raw(ends=ends))[0]["ends"] == str(ends)
    else:
        with pytest.raises(CampaignError, match="ends must be a date"):
            save(raw(ends=ends))


def test_a_youtube_description_that_starts_with_a_credit_gets_no_second_one():
    c = save(raw())[0]
    t, x = share.compose("youtube", "Wow", "Credit: @kai\n\n#kai")
    _, x = campaigns.finalize_text("youtube", t, x, c, {"channel": "kai"}, Config())
    assert x.lower().count("credit") == 1 and x.startswith("#ad Credit: @kai")
