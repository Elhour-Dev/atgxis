"""Hook text: rules, languages, Groq with a fake transport, history, placement and the burned-in subtitle lines. No network."""
import json

import pytest

from autoclip import accounts, campaigns, hooks


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def test_rules_refuse_long_lying_and_repeated_hooks():
    assert hooks.problem("Wait for it") is None
    assert "7 words" in hooks.problem("one two three four five six seven eight")
    assert "prove" in hooks.problem("Streamer got arrested live")
    assert "prove" in hooks.problem("He made $10,000 here")
    assert "last 50" in hooks.problem("wait FOR it!", ["Wait for it"])
    assert hooks.problem("   ") == "empty"
    assert "{" not in hooks.clean("{\\b1}x") and "\\" not in hooks.clean("{\\b1}x")


def test_every_template_obeys_the_rules():
    for lang, items in hooks.TEMPLATES.items():
        for t in items + [q.format(q="no way bro") for q in hooks.QUOTE_TEMPLATES[lang]]:
            assert hooks.problem(t) is None, (lang, t)


def test_language_comes_from_the_setting_then_the_transcript():
    fr = [(w, 0, 0) for w in "non mais c'est pas possible je te jure".split()]
    assert hooks.language(fr) == "fr"
    assert hooks.language([("que", 0, 0), ("es", 0, 0), ("eso", 0, 0)]) == "es"
    assert hooks.language(fr, "de") == "de"
    assert hooks.language([]) == "en"


def test_templates_in_the_clip_language_and_never_the_last_50(monkeypatch):
    seen = []
    for i in range(len(hooks.TEMPLATES["fr"])):
        h = hooks.suggest(None, "fr", key="", seed="c1")
        assert h["source"] == "template" and h["lang"] == "fr" and h["text"] in hooks.TEMPLATES["fr"]
        assert h["text"] not in seen
        seen.append(h["text"])
        hooks.remember(h["text"], "pop")
    assert hooks.suggest(None, "fr", key="") is None                       # all used: none is invented
    assert hooks.suggest(None, "ja", key="") is None                       # no templates for this language
    assert len(hooks.history()) == len(seen)


def test_history_keeps_50_and_styles_take_turns():
    assert hooks.next_style() == "pop"
    for i in range(60):
        hooks.remember(f"hook {i}", hooks.STYLE_NAMES[i % 4])
    assert len(hooks.history()) == 50 and hooks.history()[0]["text"] == "hook 10"
    last = hooks.history()[-1]["style"]
    assert hooks.next_style() == hooks.STYLE_NAMES[(hooks.STYLE_NAMES.index(last) + 1) % 4]


def test_a_quote_the_streamer_said_can_lead_the_hook():
    words = [("no", 1.0, 1.2), ("way", 1.2, 1.4), ("bro", 1.4, 1.6), ("okay", 3.0, 3.2)]
    texts = set()
    for i in range(6):
        h = hooks.suggest(words, "en", avoid=list(texts), key="", seed="q")
        texts.add(h["text"])
    assert any('"no way bro"' in t for t in texts)


def test_groq_is_used_when_a_key_is_saved_and_bad_lines_are_skipped():
    calls = []

    def fake_http(method, url, headers=None, body=None, timeout=None):
        calls.append((url, headers, body))
        content = "1. Streamer got arrested on stream\n2. \"Wait until chat sees this\"\n3. Nope"
        return 200, {}, json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    h = hooks.suggest([("hello", 0, 1)], "en", key="gsk_test", http=fake_http)
    assert h == {"text": "Wait until chat sees this", "source": "groq", "lang": "en"}
    url, headers, body = calls[0]
    assert url == hooks.GROQ_URL and headers["Authorization"] == "Bearer gsk_test" and "'en'" in body["messages"][0]["content"]


def test_groq_failure_falls_back_to_templates_and_the_key_comes_from_accounts():
    def broken(*a, **k):
        raise accounts.ApiError("could not reach api.groq.com")
    h = hooks.suggest(None, "en", key="gsk_x", http=broken)
    assert h["source"] == "template"
    accounts.update("groq", api_key="gsk_saved")
    assert hooks.groq_key() == "gsk_saved"


def test_placement_avoids_the_facecam_captions_and_disclosure():
    H, W = 1920, 1080
    assert hooks.place(W, H, [], 96) == int(H * 0.09)                        # top-centre, below the platform's top bar
    split = campaigns.protected_bands({"mode": "split", "split": 0.4, "facecam": {"x": 0, "y": 0, "w": .3, "h": .3}}, True, True)
    y = hooks.place(W, H, split, 96)
    assert y >= H * 0.4 and all(y + 240 <= a or y >= b for a, b in split)  # below the webcam panel and clear of the seam captions
    assert y + 240 <= H * 0.78                                               # above the platform buttons


def test_burned_hook_is_bold_outlined_pops_in_and_lasts_2_5_s():
    ass = campaigns.overlay_ass(None, True, "Arial", 30, disclosure="Ad", hook="Wait for it", hook_style="yellow")
    style = next(x for x in ass.splitlines() if x.startswith("Style: Hook,"))
    f = style.split(",")
    assert f[3] == "&H0000F0FF" and f[7] == "-1" and int(f[16]) >= 5 and f[18] == "8"      # yellow, bold, outlined, top-centre
    hook = next(x for x in ass.splitlines() if ",Hook," in x)
    assert ",0:00:02.50,Hook," in hook and "\\fscx40" in hook and "\\t(0,140" in hook
    disc = next(x for x in ass.splitlines() if x.startswith("Style: Disc,"))
    assert disc.split(",")[18] == "7"                                        # the disclosure keeps its own corner


def test_auto_hook_is_burned_recorded_and_remembered(tmp_path, monkeypatch):
    from autoclip.config import Config
    import autoclip.review as rv
    store = rv.Store(tmp_path, Config())
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(b"x")
    (d / "c.clip.json").write_text(json.dumps({"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "duration_s": 30, "status": "new"}))
    got = {}

    def fake_export(src, out, start, end, cfg, vertical, ass, layout, errs, voiceover=None):
        got["ass"] = ass
        out.write_bytes(b"v")
        return out
    monkeypatch.setattr(rv, "export_clip", fake_export)
    name = store.export("ch/s/c", 1.0, 9.0, True, False, hook="auto", hook_style="red")
    m = json.loads((d / "c.clip.json").read_text())
    rec = m["exports"][-1]
    assert name.endswith("_hk.mp4") and rec["hook"]["style"] == "red" and rec["hook"]["source"] == "template"
    assert rec["originality"]["hook"] is True and rec["hook"]["text"] in got["ass"]
    assert hooks.recent_texts() == [rec["hook"]["text"]]
    assert hooks.next_style() == "ink"
