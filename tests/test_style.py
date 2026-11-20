"""Your style: the example log, continuous learning, the merge DEFAULTS <- learned <- taught, and how Autopilot / suggestions use it."""
import json
import time

import pytest

from autoclip import autopilot, describe, review, share, style, taste
from autoclip.config import Config

from clipfactory import GOOD_CH, meta, write_clip
from test_autopilot import Rig


def row(i, ch="fav", verdict="good", **kw):
    r = {"id": f"{ch}/s/c{i}", "t": 1000 + i, "ev": "keep", "channel": ch, "verdict": verdict, "by": "you", "title": f"title {i}",
         "title_src": "auto", "body": "", "body_edited": False, "hashtags": [], "captions": None, "had_speech": None,
         "trim_shift": None, "len_s": 30.0, "platforms": []}
    r.update(kw)
    return r


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def recorder_made(rig):
    """A clip whose 9:16 export came from the recorder (not from you)."""
    p = rig.clip()
    m = json.loads(p.read_text())
    m["exports"][0]["by"] = "recorder"
    p.write_text(json.dumps(m))
    return p


def wait_learned():
    deadline = time.time() + 10
    while style._running and time.time() < deadline:
        time.sleep(0.05)


# ------------------------------------------------------------------ the log
@pytest.fixture()
def store(tmp_path):
    return review.Store(tmp_path / "clips", Config())


def lines():
    return style.read_examples()


def test_keep_trash_undo_are_logged_with_a_full_snapshot(store):
    p = write_clip(store.root, meta(1))
    cid = json.loads(p.read_text())["id"]
    assert store.set_status(cid, "keep")
    r = lines()[-1]
    assert (r["ev"], r["verdict"], r["by"]) == ("keep", "good", "you")
    for k in ("t", "id", "channel", "cat", "chat_phrase", "top3", "title", "title_src", "body", "body_edited", "hashtags", "captions",
              "had_speech", "trim_shift", "len_s", "platforms", "rules_ver"):
        assert k in r
    assert r["chat_phrase"] == "KEKW" and r["cat"] == "funny" and r["hashtags"] == ["fyp"]
    assert [e["id"] for e in style.examples()] == [cid]
    store.set_status(cid, "new")                                                                # Undo withdraws it
    assert lines()[-1]["ev"] == "undo" and style.examples() == []
    store.set_status(cid, "trash")
    assert style.examples()[0]["verdict"] == "bad"


def test_purge_loses_nothing_because_the_line_was_written_at_decision_time(store):
    p = write_clip(store.root, meta(2))
    cid = json.loads(p.read_text())["id"]
    store.set_status(cid, "trash")
    store.purge()
    assert not p.exists() or json.loads(p.read_text()).get("status") == "trash"
    assert style.examples()[0]["id"] == cid and style.examples()[0]["title"].startswith("Title of")


def test_trash_after_your_own_post_is_not_bad_and_an_autopilot_post_you_trash_is_a_rule_failure(store):
    p = write_clip(store.root, meta(3))
    m = json.loads(p.read_text())
    m["shares"] = {"tiktok": {"status": "done", "actor": "you"}}
    p.write_text(json.dumps(m))
    store.set_status(m["id"], "trash")
    assert lines()[-1]["verdict"] == "none" and style.examples() == []

    p2 = write_clip(store.root, meta(4))
    m2 = json.loads(p2.read_text())
    m2["shares"] = {"tiktok": {"status": "done", "actor": "autopilot"}}
    m2["autopilot"] = {"state": "posted", "rules": "abc"}
    p2.write_text(json.dumps(m2))
    store.set_status(m2["id"], "trash")
    r = lines()[-1]
    assert r["verdict"] == "bad" and r["rule_fail"] is True and r["rules_ver"] == "abc"


def test_done_on_an_autopilot_only_post_is_no_judgement_of_yours(store):
    p = write_clip(store.root, meta(5))
    m = json.loads(p.read_text())
    m["shares"] = {"tiktok": {"status": "done", "actor": "autopilot"}}
    p.write_text(json.dumps(m))
    assert store.set_status(m["id"], "done")
    assert lines()[-1]["verdict"] == "none" and style.examples() == []


def test_untouched_autopilot_output_is_never_logged_as_yours(store, monkeypatch):
    class P(share.Provider):
        name, label, automatic = "auto", "fake", True

        def configured(self):
            return True

        def problems(self, platform):
            return None

        def publish(self, job):
            return share.Result(True, "done", "ok", "https://x")
    monkeypatch.setitem(share.PROVIDERS, "auto", P())
    p = write_clip(store.root, meta(6))
    m = json.loads(p.read_text())
    share.run(store, share.ShareRequest(m["id"], m["exports"][0]["file"], ["tiktok"], {"tiktok": {"title": "t", "body": "b"}}, "auto",
                                        False, actor="autopilot"))
    r = lines()[-1]
    assert (r["ev"], r["by"], r["verdict"]) == ("post", "autopilot", "none") and r["platforms"] == []
    assert style.examples() == []
    share.run(store, share.ShareRequest(m["id"], m["exports"][0]["file"], ["youtube"], {"youtube": {"title": "t", "body": "b"}}, "auto"))
    r = lines()[-1]
    assert (r["by"], r["verdict"], r["platforms"]) == ("you", "good", ["youtube"])


def test_title_source_and_body_edits_are_tracked(store):
    p = write_clip(store.root, meta(7))
    m = json.loads(p.read_text())
    d = {"title": "auto title", "body": "auto body", "auto_body": "auto body", "alts": ["auto title", "Alt two"], "seen": []}
    m["description"] = d
    p.write_text(json.dumps(m))
    assert store.set_description(m["id"], "auto title", "auto body") and lines() == []          # nothing changed: nothing logged
    store.set_description(m["id"], "Alt two", "auto body")
    assert lines()[-1]["title_src"] == "picked" and lines()[-1]["body_edited"] is False
    store.set_description(m["id"], "my own words", "my body #mine")
    assert lines()[-1]["title_src"] == "typed" and lines()[-1]["body_edited"] is True and lines()[-1]["hashtags"] == ["mine"]


def test_mark_as_posted_is_logged_as_good(store):
    p = write_clip(store.root, meta(8))
    cid = json.loads(p.read_text())["id"]
    assert share.mark(store, cid, "tiktok", "done")
    assert lines()[-1]["ev"] == "mark" and lines()[-1]["verdict"] == "good" and lines()[-1]["platforms"] == ["tiktok"]


def test_a_log_that_cannot_be_written_never_stops_the_click(monkeypatch, capsys, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(style, "folder", lambda: blocker / "sub")                                 # a file where a folder should be
    style.record(meta(1), "keep", "good")
    assert "could not save" in capsys.readouterr().out


# ------------------------------------------------------------------ learning
def test_learning_is_debounced_and_runs_in_the_background(monkeypatch):
    runs = []
    real = style.learn
    monkeypatch.setattr(style, "learn", lambda rows=None: runs.append(1) or real(rows))
    monkeypatch.setattr(style, "RETRAIN_DELAY_S", 0.3)
    for i in range(8):
        style.record(meta(i), "keep", "good")
    wait_learned()
    assert 1 <= len(runs) <= 2 and style.learned_path().exists()
    assert json.loads(style.learned_path().read_text())["meta"]["examples"] == 8


def test_a_field_changes_only_with_5_examples_and_70_percent_agreement():
    up = [row(i, title=f"BIG WORDS {i}", title_src="typed") for i in range(4)]
    assert "title.case" not in style.learn_fields(style.examples(up))                           # 4 examples: not enough
    five = up + [row(9, title="small words here", title_src="typed")]
    assert style.learn_fields(style.examples(five))["title.case"] == "upper"                     # 4 of 5 = 80%
    split = up[:3] + [row(i, title=f"small words {i}", title_src="typed") for i in range(5, 7)]
    assert "title.case" not in style.learn_fields(style.examples(split))                         # 3 of 5 = 60%


def test_an_unsettled_field_keeps_its_previous_value():
    style.learn([row(i, title=f"LOUD {i}", title_src="typed") for i in range(5)])
    assert style.load("fav")["title"]["case"] == "upper"
    mixed = [row(i, title=f"LOUD {i}", title_src="typed") for i in range(5)] + [row(i, title=f"quiet one {i}", title_src="typed") for i in range(5, 10)]
    style.learn(mixed)
    assert style.load("fav")["title"]["case"] == "upper"                                          # 50/50 now: the old rule stays


def test_auto_titles_and_bad_clips_teach_no_title_rules():
    rows = [row(i, title=f"LOUD {i}", title_src="auto") for i in range(10)] + [row(i, verdict="bad", title=f"LOUD {i}", title_src="typed") for i in range(10, 20)]
    assert "title.case" not in style.learn_fields(style.examples(rows))


def test_learns_trim_captions_platforms_hashtags_and_lengths():
    rows = [row(i, trim_shift=[-2.0, 3.0 + (i % 2) * 0.5], len_s=35.0, sug_len_s=35.0, captions=True, had_speech=True, platforms=["tiktok"],
                body="x #mine #fyp", body_edited=True, hashtags=["mine", "fyp"]) for i in range(6)]
    f = style.learn_fields(style.examples(rows))
    assert f["trim.start_shift_s"] == -2.0 and 3.0 <= f["trim.end_shift_s"] <= 3.5
    assert f["captions.mode"] == "speech" and f["platforms"] == ["tiktok"] and f["body.hashtags_fixed"] == ["fyp", "mine"]
    assert f["trim.max_len_s"] == 35.0 and f["pick.min_len_s"] < 35 < f["pick.max_len_s"]
    assert f["body.credit_line"] is False


def test_per_channel_and_default_and_last_50():
    rows = [row(i, ch="a", title=f"LOUD {i}", title_src="typed") for i in range(60)] + \
           [row(100 + i, ch="b", title=f"quiet words {i}", title_src="typed") for i in range(5)]
    out = style.learn(rows)
    assert out["a"]["examples"] == 50 and out["b"]["examples"] == 5
    assert style.load("a")["title"]["case"] == "upper" and style.load("b")["title"]["case"] == "lower"


# ------------------------------------------------------------------ merge + broken files
def test_merge_order_defaults_then_learned_then_taught():
    write_json(style.learned_path(), {"default": {"title": {"case": "lower", "max_words": 8}}, "fav": {"title": {"case": "upper"}},
                                      "meta": {"version": 3}})
    write_json(style.taught_path(), {"default": {"body": {"hooks": ["Wait..."]}}, "fav": {"title": {"templates": ["{streamer} again"]}}})
    r = style.load("fav")
    assert r["title"]["case"] == "upper" and r["title"]["max_words"] == 8 and r["body"]["hooks"] == ["Wait..."]
    assert r["title"]["templates"] == ["{streamer} again"] and r["meta"]["sources"]["title.case"] == "learned"
    assert r["meta"]["sources"]["title.templates"] == "taught"
    other = style.load("other")
    assert other["title"]["case"] == "lower" and other["title"]["templates"] == []
    assert r["pick"] == style.DEFAULTS["pick"]


def test_the_teacher_may_only_write_taught_fields():
    write_json(style.taught_path(), {"fav": {"title": {"case": "upper"}, "platforms": ["youtube"]}})
    r = style.load("fav")
    assert r["title"]["case"] is None and r["platforms"] == []
    assert any("title.case is not allowed" in p for p in style.check())
    assert any("platforms is not allowed" in p for p in style.check())


def test_broken_files_and_fields_are_ignored_never_raised():
    write_json(style.learned_path(), "{not json")
    write_json(style.taught_path(), {"fav": {"title": {"templates": ["ok {streamer}", "bad {nope}"]}, "pick": {"need_speech": True}}})
    r = style.load("fav")
    assert r["title"]["templates"] == [] and r["pick"]["need_speech"] is True                     # bad field dropped, good one kept
    probs = style.check()
    assert any("learned.json" in p and "JSON" in p for p in probs) and any("placeholder" in p for p in probs)
    write_json(style.taught_path(), [1, 2])
    assert style.load("fav")["title"]["templates"] == []
    write_json(style.learned_path(), {"fav": {"trim": {"start_shift_s": "lots"}, "captions": {"mode": "loud"}}, "meta": "x"})
    assert style.load("fav")["trim"]["start_shift_s"] == 0.0 and style.load("fav")["captions"]["mode"] is None


def test_version_changes_only_when_the_rules_change():
    v1 = style.load("fav")["meta"]["version"]
    style.learn([row(i) for i in range(3)])
    assert style.load("fav")["meta"]["version"] == v1
    write_json(style.taught_path(), {"fav": {"body": {"hooks": ["new hook"]}}})
    assert style.load("fav")["meta"]["version"] != v1


# ------------------------------------------------------------------ never loosens
def test_platform_rules_only_narrow_the_ticked_ones():
    rules = style.load("x")
    assert style.platforms_for(rules, ["tiktok"]) == ["tiktok"]
    rules["platforms"] = ["youtube", "tiktok"]
    assert style.platforms_for(rules, ["tiktok"]) == ["tiktok"]
    rules["platforms"] = ["youtube"]
    assert style.platforms_for(rules, ["tiktok"]) == []


def test_trim_is_clamped_and_falls_back_when_unusable():
    m = {"suggest_start_s": 10.0, "suggest_end_s": 40.0, "duration_s": 45.0}
    r = style.load("x")
    r["trim"].update(start_shift_s=-20.0, end_shift_s=20.0)
    assert style.trim_for(m, r) == (0.0, 45.0)
    r["trim"].update(start_shift_s=29.0, end_shift_s=-29.0)
    assert style.trim_for(m, r) == (10.0, 40.0)
    r["trim"].update(start_shift_s=0.0, end_shift_s=0.0, max_len_s=20.0)
    assert style.trim_for(m, r) == (10.0, 30.0)


def test_captions_never_without_speech():
    r = style.load("x")
    for mode in (None, "always", "speech", "never"):
        r["captions"]["mode"] = mode
        assert style.captions_for(r, False, True) is False
    r["captions"]["mode"] = "always"
    assert style.captions_for(r, None, False) is True


def test_style_has_no_say_over_limits():
    write_json(style.learned_path(), {"default": {"max_per_day": 99, "pick": {"min_len_s": 0}}})
    assert "max_per_day" not in style.load("x") and autopilot.load()["max_per_day"] == autopilot.DEFAULTS["max_per_day"]


# ------------------------------------------------------------------ describe follows the rules
def test_suggestions_follow_your_rules_and_stay_the_same_without_them():
    cfg = Config()
    m = meta(1)
    assert describe.suggest(m, cfg, seed=3) == describe.suggest(m, cfg, seed=3, rules=None)
    r = style.load("fav")
    r["title"].update(templates=["{streamer} vs chat: {phrase}"], case="upper")
    r["body"].update(hooks=["My hook"], hashtags_fixed=["clips"], hashtags_max=3, channel_tag=False, credit_line=False)
    out = describe.suggest(m, cfg, rules=r)
    assert out["title"] == "FAV VS CHAT: KEKW" and all(t == t.upper() for t in out["alts"])
    lead, tags = out["body"].split("\n\n")
    assert lead == "My hook" and tags.split() == ["@fav", "#clips", "#funny", "#lol"]


def test_template_with_missing_data_is_skipped_and_word_limit_applies():
    r = style.load("fav")
    r["title"].update(templates=['"{quote}"'], max_words=6)
    out = describe.suggest(meta(1), Config(), rules=r)
    assert not out["title"].startswith('"') and out["alts"] and all(len(t.split()) <= 6 for t in out["alts"])


def test_review_suggestions_use_your_style_unless_switched_off(tmp_path):
    write_json(style.learned_path(), {"default": {"title": {"case": "upper"}}})
    st = review.Store(tmp_path / "c", Config())
    m = meta(1)
    assert st._fresh_description(tmp_path, {**m, "file": ""})["title"].isupper()
    autopilot.save({"use_style": False})
    assert not st._fresh_description(tmp_path, {**m, "file": ""})["title"].isupper()


# ------------------------------------------------------------------ autopilot uses the rules
@pytest.fixture()
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_autopilot_writes_untouched_titles_your_way_but_keeps_yours(rig):
    write_json(style.learned_path(), {"default": {"title": {"case": "upper"}}})
    p = recorder_made(rig)
    m = rig.read(p)
    m["description"] = {"title": "plain suggestion", "body": "auto", "auto_body": "auto", "alts": ["plain suggestion"]}
    p.write_text(json.dumps(m))
    mine = rig.clip()                                                                            # title_src "typed": yours
    rig.tick(0)
    rig.tick(11)
    rig.tick(80)
    titles = {c[1]: c[2] for c in rig.provider.calls}
    assert titles[p.name.replace(".clip.json", "_v.mp4")].isupper()
    assert titles[mine.name.replace(".clip.json", "_v.mp4")] == "Title of " + mine.name[:-10]
    ver = style.load(GOOD_CH)["meta"]["version"]
    assert rig.read(p)["autopilot"]["rules"] == ver
    assert any(e.get("rules") == ver for e in autopilot.read_log() if e.get("action") == "posted")


def test_autopilot_re_exports_with_your_trim_and_never_captions_without_speech(rig, monkeypatch):
    monkeypatch.setattr(autopilot, "_can_caption", lambda: True)                               # as if faster-whisper is installed
    write_json(style.learned_path(), {"default": {"trim": {"start_shift_s": -2.0, "end_shift_s": 1.0}, "captions": {"mode": "always"}}})
    made = []

    def fake_export(cid, start, end, vertical, captions, by="you", **kw):
        p, m = rig.store.find(cid)
        (p.parent / "styled_v.mp4").write_bytes(b"x")
        with rig.store.lock:
            m["exports"].append({"file": "styled_v.mp4", "start": start, "end": end, "vertical": vertical, "captions": captions, "by": by})
            review._write_json(p, m)
        made.append((start, end, captions, by))
        return "styled_v.mp4"
    monkeypatch.setattr(rig.store, "export", fake_export)
    monkeypatch.setattr(autopilot, "_words", lambda p, m: [])                                     # no transcript: speech unknown
    p = recorder_made(rig)
    rig.tick(0)
    rig.tick(11)
    assert made == [(8.0, 41.0, True, "autopilot")] and rig.provider.calls[0][1] == "styled_v.mp4"
    monkeypatch.setattr(autopilot, "_words", lambda p, m: [("hi", 50.0, 50.2)])                   # transcribed, but nothing said in the clip
    p2 = recorder_made(rig)
    rig.tick(80)
    rig.tick(200)
    assert made[-1] == (8.0, 41.0, False, "autopilot")


def test_autopilot_skips_what_your_pick_rules_say_and_logs_it(rig):
    write_json(style.taught_path(), {"default": {"pick": {"skip_terms": ["kekw"]}}})
    p = rig.clip()
    acts = rig.tick(0)
    assert acts[0]["action"] == "skipped" and rig.ap_state(p) == "skipped" and "kekw" in acts[0]["message"]
    assert rig.tick(30) == [] and rig.provider.calls == []                                     # logged once, not every pass
    assert sum(1 for e in autopilot.read_log() if e["action"] == "skipped") == 1
    write_json(style.taught_path(), {})                                                         # the rule is gone: the clip comes back
    assert [a["action"] for a in rig.tick(31)] == ["queued"]


def test_autopilot_never_adds_a_platform_from_your_style(rig):
    write_json(style.learned_path(), {"default": {"platforms": ["youtube"]}})                    # you post to YouTube, but ticked only TikTok
    p = rig.clip()
    rig.tick(0)
    rig.tick(30)
    assert rig.provider.calls == [] and rig.ap_state(p) is None                                  # not skipped for good: tick YouTube and it comes back


def test_switching_style_off_gives_the_old_behaviour(rig):
    write_json(style.taught_path(), {"default": {"pick": {"skip_terms": ["kekw"]}}})
    autopilot.save({"use_style": False})
    p = rig.clip()
    rig.tick(0)
    assert rig.ap_state(p) == "queued"


def test_status_shows_examples_and_where_each_field_came_from(rig):
    write_json(style.taught_path(), {"default": {"body": {"hooks": ["x"]}}})
    st = autopilot.status(rig.store)["style"]
    src = {f["field"]: f["source"] for f in st["fields"]}
    assert src["body.hooks"] == "taught" and src["title.case"] == "default" and st["examples"] == 0


# ------------------------------------------------------------------ regressions found by the checker
def test_new_titles_are_the_apps_not_yours(store):
    p = write_clip(store.root, meta(20))                                                         # title_src "typed"
    cid = json.loads(p.read_text())["id"]
    store.new_titles(cid)
    store.set_status(cid, "keep")
    assert lines()[-1]["title_src"] == "auto"


def test_a_recorder_export_with_a_numbered_name_is_not_yours():
    assert style.export_by({"file": "c1_vertical_2.mp4"}) == "recorder"
    assert style.export_by({"file": "c1_100-300_v.mp4"}) == "you"


def test_pick_lengths_compare_like_with_like():
    rows = [row(i, trim_shift=[2.0, -16.0], len_s=12.0, sug_len_s=30.0) for i in range(6)]
    f = style.learn_fields(style.examples(rows))
    r = style.load("x")
    r["pick"].update(min_len_s=f["pick.min_len_s"], max_len_s=f["pick.max_len_s"])
    assert style.skip_reason({"suggest_start_s": 5.0, "suggest_end_s": 35.0}, r) is None


def test_a_huge_number_spoils_only_its_own_field():
    style.learned_path().parent.mkdir(parents=True, exist_ok=True)
    style.learned_path().write_text('{"default": {"trim": {"start_shift_s": 1' + "0" * 400 + '}}}', encoding="utf-8")
    write_json(style.taught_path(), {"default": {"body": {"hooks": ["kept"]}}})
    assert style.load("x")["body"]["hooks"] == ["kept"] and style.load("x")["trim"]["start_shift_s"] == 0.0
    assert any("start_shift_s" in p for p in style.check())


def test_bad_clip_data_fails_the_clip_not_the_autopilot(rig):
    p = rig.clip()
    rig.tick(0)
    m = rig.read(p)
    m["suggest_start_s"] = None
    p.write_text(json.dumps(m))
    acts = rig.tick(11)                                                                          # must not raise
    assert acts and acts[-1]["action"] == "failed"


def test_new_titles_with_a_case_rule_do_not_repeat():
    r = style.load("fav")
    r["title"]["case"] = "upper"
    seen, shown = [], []
    for seed in range(3):
        out = describe.suggest(meta(1), Config(), seed=seed, seen=seen, rules=r)
        seen, shown = out["seen"], shown + out["alts"]
    assert len(shown) == len(set(shown))


def test_never_transcribed_is_unknown_not_silent(rig, monkeypatch):
    monkeypatch.setattr(autopilot, "_can_caption", lambda: True)                               # as if faster-whisper is installed
    write_json(style.taught_path(), {"default": {"pick": {"need_speech": True}}})
    write_json(style.learned_path(), {"default": {"captions": {"mode": "speech"}}})
    made = []

    def fake_export(cid, start, end, vertical, captions, by="you", **kw):
        p, m = rig.store.find(cid)
        (p.parent / "cc_v.mp4").write_bytes(b"x")
        with rig.store.lock:
            m["exports"].append({"file": "cc_v.mp4", "start": start, "end": end, "vertical": True, "captions": captions, "by": by})
            review._write_json(p, m)
        made.append(captions)
        return "cc_v.mp4"
    monkeypatch.setattr(rig.store, "export", fake_export)
    p = recorder_made(rig)                                                                               # no .words.json on disk at all
    rig.tick(0)
    assert rig.ap_state(p) == "queued"                                                           # not skipped
    rig.tick(11)
    assert made == [True] and rig.provider.calls                                                 # captions tried


def test_older_descriptions_without_markers_are_still_yours(rig):
    assert style.title_source({"title": "mine", "alts": ["a"]}) == "typed"
    assert style.title_source({"title": "a", "alts": ["a"]}) == "auto"
    assert style.body_edited({"body": "x"}) and not style.body_edited({"body": "x", "auto_body": "x"})


def test_your_own_vertical_export_beats_the_trim_rule(rig, monkeypatch):
    write_json(style.learned_path(), {"default": {"trim": {"start_shift_s": -5.0}}})
    monkeypatch.setattr(rig.store, "export", lambda *a, **k: pytest.fail("must not re-export"))
    p = rig.clip()                                                                               # its export has no "by": an older one of yours
    rig.tick(0)
    rig.tick(11)
    assert rig.provider.calls and rig.provider.calls[0][1].endswith("_v.mp4")


def test_with_nothing_learned_everything_is_as_before(rig, monkeypatch):
    m = meta(1)
    assert describe.suggest(m, Config(), seed=2, rules=style.load(GOOD_CH)) == describe.suggest(m, Config(), seed=2)
    autopilot.save({"captions": True})
    monkeypatch.setattr(rig.store, "export", lambda *a, **k: pytest.fail("must not re-export"))
    p = rig.clip()
    rig.tick(0)
    rig.tick(11)
    assert rig.provider.calls


def test_titles_typed_in_the_share_window_are_logged_as_yours(store, monkeypatch):
    class P(share.Provider):
        name, label, automatic = "auto", "fake", True

        def configured(self):
            return True

        def problems(self, platform):
            return None

        def publish(self, job):
            return share.Result(True, "done", "ok", "https://x")
    monkeypatch.setitem(share.PROVIDERS, "auto", P())
    p = write_clip(store.root, meta(30))
    m = json.loads(p.read_text())
    share.run(store, share.ShareRequest(m["id"], m["exports"][0]["file"], ["tiktok"], {"tiktok": {"title": "Typed in share", "body": "b #x"}}, "auto"))
    r = lines()[-1]
    assert r["title"] == "Typed in share" and r["title_src"] == "typed" and r["hashtags"] == ["x"] and r["rules_ver"]


def test_without_speech_to_text_no_captions_export_is_even_tried(rig, monkeypatch):
    write_json(style.learned_path(), {"default": {"captions": {"mode": "always"}}})
    monkeypatch.setattr(autopilot, "_can_caption", lambda: False)
    made = []
    monkeypatch.setattr(rig.store, "export", lambda cid, s, e, v, c, by="you": made.append(c) or pytest.fail("one plain export is enough"))
    p = recorder_made(rig)                                                                       # the recorder's plain 9:16 already fits
    rig.tick(0)
    rig.tick(11)
    assert made == [] and rig.provider.calls


def test_taking_back_mark_as_posted_withdraws_its_keep(store):
    p = write_clip(store.root, meta(40))
    cid = json.loads(p.read_text())["id"]
    share.mark(store, cid, "tiktok", "done")
    assert style.examples()[0]["verdict"] == "good"
    share.mark(store, cid, "tiktok", "reset")
    assert style.examples() == []
    store.set_status(cid, "keep")
    share.mark(store, cid, "tiktok", "reset")                                                     # a real Keep is not taken back
    assert style.examples()[0]["verdict"] == "good"


def test_taking_back_a_mark_never_loses_other_evidence():
    def ev(t, e, v, plats):
        return row(1, ev=e, verdict=v, platforms=plats, t=t)
    assert style.examples([ev(1, "keep", "good", []), ev(2, "mark", "good", ["tiktok"]), ev(3, "mark_reset", "none", [])])
    assert style.examples([ev(1, "mark", "good", ["tiktok"]), ev(2, "mark", "good", ["tiktok", "youtube"]),
                           ev(3, "mark_reset", "none", ["youtube"])])                            # YouTube is still posted
    assert not style.examples([ev(1, "mark", "good", ["tiktok"]), ev(2, "mark_reset", "none", [])])


def test_an_older_clip_without_recorded_auto_text_does_not_teach_its_body_as_yours(store):
    p = write_clip(store.root, meta(50))
    m = json.loads(p.read_text())
    m["description"] = {"title": "a", "alts": ["a"], "body": "app text #fyp"}                  # written by the recorder before the update
    p.write_text(json.dumps(m))
    store.set_status(m["id"], "keep")
    assert lines()[-1]["body_edited"] is False
