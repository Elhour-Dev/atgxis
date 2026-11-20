"""Instagram only gets original clips (hook + reframe or voice-over), and you are warned when a moment was already posted."""
import json

import pytest

from autoclip import review, share
from autoclip.config import Config


class Fake(share.Provider):
    name, label, automatic = "fake", "Fake", True

    def __init__(self):
        self.calls = []

    def publish(self, job):
        self.calls.append(job.platform)
        return share.Result(True, "done", "", f"https://{job.platform}.example/p")


@pytest.fixture
def world(tmp_path, monkeypatch):
    prov = Fake()
    monkeypatch.setitem(share.PROVIDERS, "fake", prov)
    root = tmp_path / "clips"

    def clip(stem, created="2026-11-20T20:00:00", start=0.0, end=30.0, **ex):
        d = root / "ch" / "s"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}_v.mp4").write_bytes(b"v")
        m = {"id": f"ch/s/{stem}", "file": f"{stem}.mp4", "channel": "ch", "status": "new", "duration_s": 60.0, "created": created,
             "source_url": "https://twitch.tv/ch", "spike": {}, "description": {"title": f"T {stem}", "body": "B"},
             "suggest_start_s": start, "suggest_end_s": end, "clip_len_s": end - start, "moment_s": start + 5,
             "exports": [{"file": f"{stem}_v.mp4", "start": start, "end": end, "vertical": True, "captions": False, **ex}]}
        (d / f"{stem}.clip.json").write_text(json.dumps(m))
        return m["id"], f"{stem}_v.mp4"
    return review.Store(root, Config()), prov, clip


def req(cid, f, plats):
    return share.ShareRequest(cid, f, plats, {p: {"title": "T", "body": "B"} for p in plats}, "fake")


HOOK = {"originality": {"hook": True, "voiceover": False}}


def test_rules():
    assert "hook" in share.instagram_problem({})
    assert "reframe" in share.instagram_problem(HOOK)
    assert share.instagram_problem({**HOOK, "reframe": {"applied": True}}) is None
    assert share.instagram_problem({"originality": {"hook": True, "voiceover": True}}) is None
    assert not share.original({"originality": {"voiceover": True}}) and share.original({**HOOK, "reframe": {"applied": True}})


def test_instagram_is_blocked_without_originality_and_nothing_is_posted(world):
    store, prov, clip = world
    cid, f = clip("a", **HOOK)
    with pytest.raises(share.ShareError, match="Instagram Reels: needs a reframe"):
        share.run(store, req(cid, f, ["tiktok", "instagram"]))
    assert prov.calls == []                                                           # all or nothing
    assert share.run(store, req(cid, f, ["tiktok"]))["tiktok"]["status"] == "done"   # the others still work


def test_same_moment_already_posted_is_a_warning_not_a_block(world):
    store, prov, clip = world
    ok = {**HOOK, "reframe": {"applied": True}}
    a, fa = clip("a", start=10.0, end=40.0, **ok)
    b, fb = clip("b", created="2026-11-20T20:00:20", start=0.0, end=25.0, **ok)      # 20:00:20-20:00:45 overlaps a (20:00:10-40)
    c, fc = clip("c", created="2026-11-20T21:00:00", **ok)                            # an hour later: a different moment
    share.run(store, req(a, fa, ["tiktok"]))
    pre = share.precheck(store, b, fb, ["instagram"])
    assert pre["block"] is None and pre["original"] and len(pre["same_moment"]) == 1 and "TikTok" in pre["same_moment"][0]
    assert share.precheck(store, c, fc, ["instagram"])["same_moment"] == []
    assert share.precheck(store, a, fa, ["instagram"])["same_moment"]                # the same clip on TikTok counts too
    res = share.run(store, req(b, fb, ["instagram"]))
    assert res["instagram"]["status"] == "done" and "already posted" in res["instagram"]["message"]
    assert share.precheck(store, b, fb, ["tiktok"]) == {"block": None, "same_moment": [], "original": True}


def test_autopilot_posts_to_the_others_and_skips_instagram(world, monkeypatch):
    from autoclip import autopilot
    store, prov, clip = world
    cid, f = clip("a", **HOOK)
    events = []
    monkeypatch.setattr(autopilot, "log_event", lambda **kw: events.append(kw))
    ap = autopilot.Autopilot(store)
    p, m = store.find(cid)
    monkeypatch.setattr(autopilot, "_latest_vertical", lambda *a, **k: m["exports"][0])
    out = ap._post(p, m, 0.99, ["tiktok", "instagram"], {**autopilot.load(), "provider": "fake"}, 1e9)
    assert out["action"] == "posted" and prov.calls == ["tiktok"]
    assert any(e.get("action") == "skip_platform" and "Instagram" in e["message"] for e in events)
