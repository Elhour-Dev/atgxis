"""Learning from Keep / Trash: what counts as a decision, what the model can and cannot learn, and when it may be trusted."""
import json
import random
import threading
import time

import pytest

from autoclip import review, taste
from autoclip.config import Config

from clipfactory import BAD_CH, GOOD_CH, meta, teach, write_clip


# ------------------------------------------------------------------ features
def test_sample_survives_missing_and_odd_data():
    for m in ({}, {"id": "x"}, {"spike": None}, {"spike": {"top": "garbage", "peak": "n/a"}}, {"loudness_jump_db": None, "res": [None, None]},
              {"spike": {"top": [[None, 3], [], 5]}, "score": float("nan")}):
        s = taste.sample(m)
        assert all(isinstance(v, float) and v == v for v in s["x"].values()), m             # finite numbers only (no NaN poisoning the fit)
        json.dumps(s)                                                                         # and storable


def test_chat_terms_keep_words_and_short_phrases_but_not_noise():
    t = taste.chat_terms([["NO WAY", 9], ["KEKW", 5], ["!!!", 3], ["12345", 2], ["this sentence is far too long to be a spammed phrase", 2]])
    assert {"t:no", "t:way", "p:no way", "t:kekw"} <= set(t)
    assert not [x for x in t if x in ("t:!", "t:12345")]
    assert not [x for x in t if x.startswith("p:this")]


# ------------------------------------------------------------------ the decision log
def test_latest_decision_wins_and_undo_withdraws_it():
    m = meta(1)
    taste.record(m, 1, "keep", when=1)
    taste.record(m, 0, "trash", when=2)
    assert [r["label"] for r in taste.labels()] == [0]                                        # changed her mind: Trash counts
    taste.record(m, None, "new", when=3)
    assert taste.labels() == [] and taste.counts() == {"total": 0, "keeps": 0, "trashes": 0}   # Undo: no decision at all


def test_posting_it_yourself_is_a_keep_but_never_overrules_a_trash():
    a, b = meta(1), meta(2)
    taste.record(a, 1, "posted", when=1)
    assert [r["label"] for r in taste.labels()] == [1]
    taste.record(b, 0, "trash", when=1)
    taste.record(b, 1, "posted", when=2)                                                      # posted AFTER trashing: the explicit Trash stays
    assert sorted((r["clip"], r["label"]) for r in taste.labels()) == [(a["id"], 1), (b["id"], 0)]
    taste.record(b, None, "new", when=3)                                                      # Undo of the Trash: now the post counts
    assert {r["clip"]: r["label"] for r in taste.labels()}[b["id"]] == 1


def test_a_damaged_log_never_breaks_learning():
    taste.record(meta(1), 1, "keep")
    with open(taste.feedback_path(), "a", encoding="utf-8") as f:
        f.write('{"clip": "half written\n')                                                  # power cut in the middle of a line
        f.write("not json at all\n")
        f.write("[1, 2, 3]\n")
    taste.record(meta(2), 0, "trash")
    assert taste.counts()["total"] == 2
    taste.record({"id": ""}, 1, "keep")                                                       # a record with no id is ignored, not stored
    assert taste.counts()["total"] == 2


def test_cannot_write_the_log_does_not_stop_the_click(monkeypatch, capsys):
    monkeypatch.setattr(taste, "folder", lambda: taste.Path("/proc/definitely/not/writable"))
    taste.record(meta(1), 1, "keep")                                                          # must not raise
    assert "could not save" in capsys.readouterr().out


def make_store(tmp_path, n_good=0, n_bad=0, **kw):
    root = tmp_path / "clips"
    paths = [write_clip(root, meta(i, GOOD_CH, **kw)) for i in range(n_good)] + [write_clip(root, meta(i, BAD_CH, **kw)) for i in range(n_bad)]
    return review.Store(root, Config()), paths


def test_keep_and_trash_in_the_review_page_are_remembered_and_survive_deleting_the_files(tmp_path):
    store, _ = make_store(tmp_path, 1, 1)
    assert store.set_status("fav/s/c0", "keep") and store.set_status("meh/s/c0", "trash")
    assert taste.counts() == {"total": 2, "keeps": 1, "trashes": 1}
    assert store.purge() == 1                                                                  # "Delete trashed files"
    assert not (tmp_path / "clips" / "meh" / "s" / "c0.clip.json").exists()                    # really gone from disk
    assert (tmp_path / "clips" / "fav" / "s" / "c0.clip.json").exists()                        # the kept one untouched
    assert taste.counts() == {"total": 2, "keeps": 1, "trashes": 1}                            # the decision outlives the clip
    assert store.set_status("fav/s/c0", "new")                                                 # Undo
    assert taste.counts() == {"total": 1, "keeps": 0, "trashes": 1}


def test_decisions_from_before_learning_existed_are_taken_over_once(tmp_path):
    root = tmp_path / "clips"
    write_clip(root, meta(0, GOOD_CH, status="keep"))
    write_clip(root, meta(1, BAD_CH, status="trash"))
    write_clip(root, meta(2, GOOD_CH, status="new"))                                           # undecided: nothing to learn
    assert taste.import_existing(root) == 2
    assert taste.import_existing(root) == 0                                                    # not twice
    assert taste.counts() == {"total": 2, "keeps": 1, "trashes": 1}
    m = meta(0, GOOD_CH, status="keep")
    taste.record(m, None, "new")                                                               # she undid it in the new version...
    assert taste.import_existing(root) == 0                                                    # ...the old file status must not resurrect it


def test_every_clip_in_the_list_carries_the_models_opinion(tmp_path):
    teach(taste)
    store, _ = make_store(tmp_path, 1, 1)
    by_id = {c["id"]: c for c in store.list()}
    assert by_id["fav/s/c0"]["taste"]["p"] > by_id["meh/s/c0"]["taste"]["p"]
    assert set(by_id["fav/s/c0"]["taste"]) >= {"p", "state", "why", "pick"}


# ------------------------------------------------------------------ the model
def test_no_guessing_before_there_is_something_to_guess_from():
    m = taste.train()
    assert m["state"] == "none" and taste.predict(meta(1)) is None
    for i in range(12):
        taste.record(meta(i, GOOD_CH), 1, "keep")                                              # only ever Keep: nothing to compare with
    m = taste.train()
    assert m["state"] == "none" and "10 clips" in m["reason"]
    assert not taste.readiness(m)["ok"]


def test_it_learns_what_the_user_likes_from_the_decisions_alone():
    m = teach(taste, n_each=40)
    assert m["state"] == "ready" and m["auc"] > 0.95
    rnd = random.Random(99)
    good = [taste.predict(meta(500 + i, GOOD_CH, rnd=rnd))["p"] for i in range(30)]           # clips it has never seen
    bad = [taste.predict(meta(500 + i, BAD_CH, rnd=rnd))["p"] for i in range(30)]
    assert min(good) > max(bad) and sum(good) / 30 > 0.8 and sum(bad) / 30 < 0.2


def test_it_can_explain_a_guess():
    teach(taste, n_each=40)
    why = taste.predict(meta(777, GOOD_CH))["why"]
    assert why and all(isinstance(w["text"], str) and w["text"] for w in why)
    assert any("fav" in w["text"] or "kekw" in w["text"] or "hype" in w["text"] or "chat" in w["text"] for w in why)


def test_early_guesses_are_shown_but_do_not_unlock_posting():
    for i in range(7):
        taste.record(meta(i, GOOD_CH), 1, "keep")
        taste.record(meta(i, BAD_CH), 0, "trash")
    m = taste.train()
    assert m["state"] == "early" and taste.predict(meta(50, GOOD_CH))["state"] == "early"
    r = taste.readiness(m)
    assert not r["ok"] and "more decisions" in r["reason"]


def test_a_user_whose_choices_follow_no_pattern_is_never_unlocked():
    rnd = random.Random(5)
    for i in range(160):                                                                       # coin flips, whatever the clip looks like
        taste.record(meta(i, rnd.choice([GOOD_CH, BAD_CH]), rnd=rnd), rnd.randint(0, 1), "keep")
    m = taste.train()
    assert not taste.readiness(m, 0.6)["ok"]


def test_training_is_deterministic_and_quick():
    for i in range(400):
        rnd = random.Random(i)
        taste.record(meta(i, rnd.choice([GOOD_CH, BAD_CH]), rnd=rnd), rnd.randint(0, 1), "keep", when=i)
    t0 = time.time()
    a = taste.train()
    took = time.time() - t0
    b = taste.train()
    assert a["core"]["w"] == b["core"]["w"] and a["oof"] == b["oof"]
    assert took < 10, f"training took {took:.1f}s"


def test_new_decisions_make_the_model_stale_and_training_fixes_it():
    assert not taste.is_stale()
    for i in range(12):
        taste.record(meta(i, GOOD_CH if i % 2 else BAD_CH), i % 2, "keep")
    assert taste.is_stale()
    taste.train()
    assert not taste.is_stale()
    taste.record(meta(0, BAD_CH), 1, "keep")                                                   # same number of clips, a flipped decision
    assert taste.is_stale()


def test_background_retraining_runs_after_a_click(monkeypatch):
    monkeypatch.setattr(taste, "RETRAIN_DELAY_S", 0.05)
    for i in range(12):
        taste.record(meta(i, GOOD_CH if i % 2 else BAD_CH), i % 2, "keep")
    taste.retrain_soon()
    taste.retrain_soon()                                                                       # quick clicks share one training run
    deadline = time.time() + 15
    while taste.is_stale() and time.time() < deadline:
        time.sleep(0.1)
    assert not taste.is_stale() and taste.model()["n"] == 12


# ------------------------------------------------------------------ the safety gate
def test_auc_matches_hand_calculation():
    assert taste.auc([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]) == 1.0
    assert taste.auc([(0.1, 1), (0.2, 1), (0.8, 0), (0.9, 0)]) == 0.0
    assert taste.auc([(0.5, 1), (0.5, 0)]) == 0.5                                              # ties
    assert taste.auc([(0.5, 1), (0.6, 1)]) is None                                             # one class only


def test_wilson_bound_distrusts_small_lucky_samples():
    assert taste.wilson_lower(5, 5) < 0.8                                                      # 5 out of 5 is not proof
    assert taste.wilson_lower(100, 100) > 0.97
    assert taste.wilson_lower(0, 0) == 0.0


def oof(n_hits, n_miss, p=0.95):
    """Out-of-sample results: `n_hits` clips it was `p` sure about and the user kept, then `n_miss` it was just as sure about and did not."""
    return [[p, 1]] * n_hits + [[p - 0.001, 0]] * n_miss + [[0.3, 0]] * 40


def test_a_streak_on_a_handful_of_clips_does_not_unlock():
    assert taste.choose_threshold(oof(12, 0), 0.85) is None                                    # 12 out of 12 on 12 clips: found in simulation to be luck
    got = taste.choose_threshold(oof(30, 0), 0.85)
    assert got and got["support"] == 30 and got["hits"] == 30


def test_a_model_that_is_not_as_sure_as_the_streak_suggests_does_not_unlock():
    # 22/22 kept, but the model itself only claimed ~70%: the streak is more likely luck than skill
    assert taste.choose_threshold([[0.70, 1]] * 22 + [[0.3, 0]] * 40, 0.85) is None


def test_mediocre_precision_does_not_unlock_but_a_lower_target_can():
    # 30 clips it is about equally sure of (0.900-0.915); it was wrong on every fifth one: right 80% of the time, no cut-off does better
    data = [[0.9 + 0.0005 * i, 0 if i % 5 == 0 else 1] for i in range(30)] + [[0.3, 0]] * 40
    assert taste.choose_threshold(data, 0.85) is None
    assert taste.choose_threshold(data, 0.6) is not None


def test_readiness_follows_the_target_and_the_extra_confidence_you_ask_for():
    m = teach(taste, n_each=40)
    base = taste.readiness(m, 0.8)
    assert base["ok"] and base["threshold"] >= taste.FLOOR and base["support"] >= taste.MIN_SUPPORT
    strict = taste.readiness(m, 0.8, min_confidence=0.97)
    assert strict["threshold"] == 0.97 and strict["ok"]                                        # extra confidence can only raise the bar
    assert taste.readiness(m, 0.8, min_confidence=0.1)["threshold"] == base["threshold"]       # and never lower it


def test_curve_reports_what_each_level_would_have_done():
    rows = taste.curve([[0.95, 1], [0.9, 1], [0.8, 0], [0.55, 1], [0.2, 0]])
    at = {x["at"]: x for x in rows}
    assert (at[0.9]["picked"], at[0.9]["agreed"]) == (2, 2) and at[0.5]["picked"] == 4 and at[0.5]["agreed"] == 3
    assert taste.curve([])[0]["precision"] is None


def test_the_model_is_written_atomically_and_a_broken_file_is_ignored():
    teach(taste, n_each=15)
    assert taste.model()["n"] == 30
    taste.model_path().write_text("{ broken", encoding="utf-8")
    assert taste.model() is None and taste.predict(meta(1)) is None
    assert not list(taste.folder().glob("*.tmp"))


def test_a_click_that_arrives_while_training_finishes_is_not_lost(monkeypatch):
    monkeypatch.setattr(taste, "RETRAIN_DELAY_S", 0.05)
    for i in range(12):
        taste.record(meta(i, GOOD_CH if i % 2 else BAD_CH), i % 2, "keep")
    taste.retrain_soon()
    deadline = time.time() + 15
    while taste.is_stale() and time.time() < deadline:
        time.sleep(0.02)
    assert not taste.is_stale()
    for round_ in range(5):                                                                    # decisions arriving one after another, each right after a run
        taste.record(meta(100 + round_, GOOD_CH), 1, "keep")
        taste.retrain_soon()
        time.sleep(0.07)                                                                       # lands around the moment a run ends
    deadline = time.time() + 15
    while (taste.is_stale() or taste._running) and time.time() < deadline:
        time.sleep(0.02)
    assert not taste.is_stale() and taste.model()["n"] == 17


def test_background_training_stays_in_the_folder_it_was_scheduled_for(monkeypatch, tmp_path):
    monkeypatch.setattr(taste, "RETRAIN_DELAY_S", 0.2)
    for i in range(12):
        taste.record(meta(i, GOOD_CH if i % 2 else BAD_CH), i % 2, "keep")
    mine = taste.folder()
    taste.retrain_soon()
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "somewhere_else"))               # the world changes before the thread wakes up
    deadline = time.time() + 15
    while not (mine / "model.json").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert (mine / "model.json").exists() and not (tmp_path / "somewhere_else").exists()


# ------------------------------------------------------------------ "Done (free space)": posted clips whose files you deleted
def test_trash_after_you_posted_it_counts_as_keep():
    a, b = meta(1), meta(2)
    taste.record(a, 1, "posted", when=1)
    taste.record(a, 0, "trash", when=2)                                                       # trashed only to free disk space
    taste.record(b, 0, "trash", when=1)                                                       # never posted: a real Trash
    assert {r["clip"]: r["label"] for r in taste.labels()} == {a["id"]: 1, b["id"]: 0}


def test_done_frees_the_files_and_counts_as_keep(tmp_path):
    root = tmp_path / "clips"
    m = meta(1)
    m["shares"] = {"tiktok": {"status": "done", "actor": "you", "file": "c1_v.mp4"}}
    p = write_clip(root, m)
    (p.parent / "c1.words.json").write_text("[]", encoding="utf-8")
    copy = tmp_path / "final" / "c1_v.mp4"
    copy.parent.mkdir()
    copy.write_bytes(b"kept")
    meta_on_disk = json.loads(p.read_text(encoding="utf-8"))
    meta_on_disk["exports"][0]["copy"] = str(copy)
    p.write_text(json.dumps(meta_on_disk), encoding="utf-8")
    store = review.Store(root, Config())
    assert store.set_status(m["id"], "done")
    assert sorted(x.name for x in p.parent.iterdir()) == [p.name]                              # draft, export and words file are gone
    assert copy.read_bytes() == b"kept"                                                        # the copy in the final clips folder is not
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["status"] == "done" and saved["files_deleted"] is True
    assert [(r["label"], r["src"]) for r in taste.labels()] == [(1, "done")]
    assert not store.set_status(m["id"], "new")                                                # its files are gone: no way back to review
    assert store.purge() == 0 and p.exists()                                                   # "Delete trashed files" leaves it alone


def test_done_needs_a_shared_clip(tmp_path):
    root = tmp_path / "clips"
    p = write_clip(root, meta(1))
    store = review.Store(root, Config())
    assert not store.set_status(meta(1)["id"], "done")
    assert (p.parent / "c1.mp4").exists() and taste.labels() == []


def test_import_repairs_trash_on_clips_you_posted(tmp_path):
    root = tmp_path / "clips"
    mine, auto, done, plain = meta(1, status="trash"), meta(2, status="trash"), meta(3, status="done"), meta(4, status="trash")
    auto_done = meta(5, status="done")
    auto_done["shares"] = {"tiktok": {"status": "done", "actor": "autopilot"}}
    write_clip(root, auto_done)
    done["shares"] = {"youtube": {"status": "done"}}                                            # a manual "Mark as posted": yours
    mine["shares"] = {"youtube": {"status": "done", "actor": "you"}}
    auto["shares"] = {"tiktok": {"status": "done", "actor": "autopilot"}}                     # its post, your Trash: a real Trash
    for m in (mine, auto, done, plain):
        write_clip(root, m)
    assert taste.import_existing(root) == 4
    assert {r["clip"]: r["label"] for r in taste.labels()} == {mine["id"]: 1, auto["id"]: 0, done["id"]: 1, plain["id"]: 0}


def test_done_keeps_everything_when_a_file_is_locked_and_autopilot_posts_teach_nothing(tmp_path, monkeypatch):
    root = tmp_path / "clips"
    m = meta(1)
    m["shares"] = {"tiktok": {"status": "done", "actor": "autopilot"}, "youtube": {"status": "opened", "actor": "you"}}
    p = write_clip(root, m)
    store = review.Store(root, Config())
    real = taste.Path.unlink

    def locked(self, missing_ok=False):
        if self.name == "c1.mp4":
            raise PermissionError("in use")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(taste.Path, "unlink", locked)
    assert not store.set_status(m["id"], "done")                                              # nothing claimed, can be pressed again
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["status"] == "new" and saved["exports"] == []                                  # the export that did go is not listed any more
    monkeypatch.setattr(taste.Path, "unlink", real)
    assert store.set_status(m["id"], "done")
    assert taste.labels() == []                                                               # Done on the Autopilot's own post: not your taste
