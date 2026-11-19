import json

import pytest

from autoclip import config as cfgmod
from autoclip.config import Config
from autoclip.describe import best_quote, chat_phrase, suggest
from test_share_settings import make_root, server

META = {"id": "kai/s/1", "channel": "kaicenat", "source_url": "https://twitch.tv/kaicenat", "moment_s": 14, "suggest_start_s": 0,
        "suggest_end_s": 30, "loudness_jump_db": 9,
        "spike": {"peak": 210, "baseline": 60, "top": [["KEKW", 80], ["no way", 30], ["a", 50], ["!clip", 40]]}}


def test_titles_use_what_is_specific_to_the_clip():
    d = suggest(META, Config())
    text = " | ".join(d["alts"])
    assert 'KEKW' in text and "Kaicenat" in text                      # the actual chat phrase and the streamer
    assert len(d["alts"]) == 6 and len(set(d["alts"])) == 6
    assert all(len(t) <= 95 for t in d["alts"])
    assert d["category"] == "funny"
    assert chat_phrase([["a", 90], ["!clip", 50], ["12", 40], ["no way", 30]]) == ("NO WAY", 30)   # skips letters, commands, numbers


def test_different_clips_and_new_seeds_give_different_titles():
    other = dict(META, id="kai/s/2", spike={"peak": 40, "baseline": 20, "top": [["WTF", 20], ["LUL", 9]]})
    assert suggest(META, Config())["alts"] != suggest(other, Config())["alts"]
    first = suggest(META, Config())
    again = suggest(META, Config(), seed=1, seen=first["seen"])
    assert not set(first["alts"]) & set(again["alts"]), "titles already shown must not come back"
    assert suggest(META, Config())["alts"] == first["alts"]            # deterministic for the same seed


def test_quote_comes_from_speech_near_the_moment_and_profanity_is_skipped():
    words = [("bro", 10, 10.3), ("he", 10.3, 10.5), ("just", 10.5, 10.8), ("cooked", 10.8, 11.2), ("me!", 11.2, 11.6),
             ("what", 40, 40.2), ("the", 40.2, 40.4), ("fuck", 40.4, 40.7), ("man!", 40.7, 41)]
    assert best_quote(words, 0, 30, 14) == "Bro he just cooked me!"
    assert best_quote([("what", 1, 1.2), ("the", 1.2, 1.4), ("fuck", 1.4, 1.7), ("man!", 1.7, 2)], 0, 5, 1) is None
    d = suggest(META, Config(), words=words)
    assert d["used_speech"] and any("Bro he just cooked me!" in t for t in d["alts"])
    assert "Bro he just cooked me!" in d["body"]


def test_clip_with_no_chat_data_still_gets_titles():
    d = suggest({"id": "x", "channel": "foo", "spike": {}}, Config())
    assert len(d["alts"]) >= 4 and all("Foo" in t for t in d["alts"])


def test_old_quality_line_is_upgraded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('quality = "720p60,720p,480p,best"\n')
    assert cfgmod.load(None).quality == "best"
    (tmp_path / "config.toml").write_text('quality = "1080p60,best"\n')
    assert cfgmod.load(None).quality == "1080p60,best"                  # a deliberate choice is respected


def test_new_titles_endpoint_never_dead_ends_and_keeps_your_text(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = make_root(tmp_path)
    mp = root / "ch" / "s" / "c.clip.json"
    m = json.loads(mp.read_text())
    m["description"] = {"title": "my title", "body": "my own words"}     # the broken state: no alternatives stored
    m["spike"] = {"peak": 120, "baseline": 40, "top": [["KEKW", 40]]}
    mp.write_text(json.dumps(m))
    srv, call = server(root)
    seen = set()
    for _ in range(3):
        code, d = call("/api/describe/new", {"id": "ch/s/c"})
        assert code == 200 and len(d["alts"]) >= 4
        assert not seen & set(d["alts"]), "no repeats across requests"
        seen |= set(d["alts"])
        assert d["body"] == "my own words"
    code, d = call("/api/describe/new", {"id": "ch/s/c", "listen": True})   # faster-whisper not installed here: falls back, says why
    assert code == 200 and ("faster-whisper" in d["note"] or "Could not listen" in d["note"])
    assert call("/api/describe/new", {"id": "nope"})[0] == 400
    srv.shutdown()


def test_the_streamer_is_always_tagged_with_the_hashtags():
    from autoclip import describe, style
    from autoclip.config import Config
    from clipfactory import meta
    tags = describe.suggest(meta(1), Config())["body"].splitlines()[-1].split()
    assert tags[0] == "@fav" and all(t.startswith("#") for t in tags[1:])
    rules = style.load("fav")
    rules["body"].update(channel_tag=False, hashtags_max=0)                                     # even when your style drops every hashtag
    assert describe.suggest(meta(1), Config(), rules=rules)["body"].splitlines()[-1] == "@fav"
    cfg = Config()
    cfg.handles = {"FAV": "@fav.official"}                                                      # a different handle on TikTok / Instagram
    assert describe.suggest(meta(1), cfg)["body"].splitlines()[-1].split()[0] == "@fav.official"


def test_youtube_streamers_are_tagged_only_when_their_handle_is_known():
    from autoclip import describe
    from autoclip.config import Config
    yt = {"channel": "yt_dQw4w9WgXcQ", "platform": "youtube", "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    assert describe.mention(yt, Config()) == ""                                                 # a video link: unknown, no wrong @
    assert describe.mention({**yt, "channel": "yt_MrBeast", "source_url": "https://www.youtube.com/@MrBeast/live"}, Config()) == "@MrBeast"
    cfg = Config()
    cfg.handles = {"yt_dQw4w9WgXcQ": "mrbeast", "fav": "!!!"}
    assert describe.mention(yt, cfg) == "@mrbeast"
    assert describe.mention({"channel": "fav"}, cfg) == "@fav"                                 # a handle that cleans to nothing falls back
    assert describe.mention({"channel": "x" * 40}, Config()) == ""                             # too long to be a real handle: no tag
    assert describe.mention({**yt, "channel": "yt_Some-Name", "source_url": "https://www.youtube.com/@Some-Name/live"}, Config()) == ""
    cfg.handles = ["not", "a", "table"]
    assert describe.mention({"channel": "fav"}, cfg) == "@fav"                                 # a broken setting is ignored


def test_the_tag_is_never_added_twice():
    from autoclip import describe
    from autoclip.config import Config
    for body in ("thanks @xqc.", "thanks @XQC!", "@xqc #fyp"):
        assert describe.with_mention(body, {"channel": "xqc"}, Config()) == body
    assert describe.with_mention("hi @xqcow", {"channel": "xqc"}, Config()).endswith("@xqc")
