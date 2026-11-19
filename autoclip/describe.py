"""Post-text suggestions (title options + description with credit and hashtags) for each clip.

Rule-based, no AI service and nothing leaves your PC. Titles are built from what is specific to THIS clip:
  * what chat was spamming during the spike (the exact phrase, e.g. "NO WAY"), and which reaction it was (funny / shock / hype),
  * how big the spike was (chatters at once, times normal),
  * whether the streamer got loud,
  * optionally what the streamer SAID (speech-to-text of the clip, when you press "Base on what's said" or after captions).
Every call can return a fresh set (`seed`), never repeating titles already shown for the clip.
It is a starting point you edit in the review page, not finished copy.
"""
from __future__ import annotations

import random
import re
import unicodedata
import zlib
from collections import Counter

from .config import Config

CATEGORIES = {
    "funny": {"kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "haha", "hahaha", "icant", "dead", "rofl", "😂", "🤣", "💀", "kekl", "pepelaugh"},
    "shock": {"wtf", "omg", "wow", "nooo", "noooo", "bruh", "no way", "what", "huh", "monkas", "😳", "😱", "😭", "noway", "nowayyy", "ayo"},
    "hype": {"pog", "pogchamp", "poggers", "pogu", "pogs", "w", "ww", "www", "gg", "ggs", "clip", "clipit", "insane",
             "clutch", "goat", "cracked", "🔥", "🐐", "letsgo", "lets go", "hype", "sheesh"},
}
CATEGORY_TAGS = {"funny": ["funny", "lol", "comedy"], "shock": ["omg", "shocked", "unexpected"], "hype": ["hype", "insane", "goated"],
                 "default": ["moments", "livestream"]}

# every template is (needs, text). `needs` lists the fields that must be available: n=streamer, say=chat phrase, quote=streamer quote
T = {
    "chat": [
        ("say", 'Chat spammed "{say}" at {n} 😭'), ("say", '{n} made all of chat type "{say}"'), ("say", 'Why is chat typing "{say}"? 👀'),
        ("say", '"{say}" - chat, the second {n} did that'), ("say", 'The moment chat started spamming "{say}" 💀'),
        ("say", '{n} broke chat: "{say}" x{count}'), ("say", 'Everybody in {n}\'s chat said "{say}" at once'),
    ],
    "speech": [
        ("quote", '"{quote}" 😭 {n}'), ("quote", '{n}: "{quote}"'), ("quote", '{n} said "{quote}" and chat LOST it'),
        ("quote", 'The way {n} said "{quote}" 💀'), ("quote", '"{quote}" - {n} live on stream 👀'),
        ("quote", 'Chat could not believe {n} said "{quote}"'),
    ],
    "funny": [
        ("n", "{n} had chat in TEARS 😂"), ("n", "{n} said WHAT 💀"), ("n", "Chat could not stop laughing at {n} 😂"),
        ("n", "I cannot breathe, {n} is too funny 💀"), ("n", "{n} did it again 😭 chat is DEAD"), ("n", "This {n} moment is actually unfair 😂"),
        ("n", "POV: you are in {n}'s chat right now 😂"), ("n", "{n} just ended chat with ONE line 💀"),
    ],
    "shock": [
        ("n", "{n} left chat SPEECHLESS 😳"), ("n", "Nobody expected this from {n} 😱"), ("n", "{n} did WHAT?! 😳"),
        ("n", "Wait for it... {n} 😳"), ("n", "Chat is in SHOCK after what {n} just did"), ("n", "{n} really just did that live 😱"),
        ("n", "Bro what did {n} just do 😭"), ("n", "{n} shocked the whole stream"),
    ],
    "hype": [
        ("n", "{n} just did the IMPOSSIBLE 🔥"), ("n", "CHAT went CRAZY for {n} 🔥"), ("n", "{n} is UNREAL 🔥"),
        ("n", "This {n} play is INSANE 🐐"), ("n", "{n} cooked and chat KNOWS it 🔥"), ("n", "Clip it NOW: {n} 🔥"),
        ("n", "{n} making history on stream 🐐"), ("n", "The whole chat stood up for {n} 🔥"),
    ],
    "default": [
        ("n", "The moment chat EXPLODED on {n}'s stream 👀"), ("n", "{n}'s stream goes CRAZY 👀"), ("n", "You had to be there: {n} 👀"),
        ("n", "This is why people watch {n} 👀"), ("n", "{n} had no idea what was coming 👀"), ("n", "Something just happened on {n}'s stream"),
    ],
    "big": [
        ("peak", "{peak} people typed at once in {n}'s chat 🤯"), ("ratio", "{n}'s chat went {ratio}x louder in seconds 🤯"),
        ("peak", "{peak} chatters, one reaction: {n} 👀"),
    ],
    "loud": [
        ("n", "{n} SCREAMED and chat went insane 🔊"), ("n", "The loudest moment of {n}'s stream 🔊"), ("n", "{n} lost it on stream 🔊"),
    ],
    "hook": [
        ("n", "You will not believe what {n} just did"), ("n", "This is why chat is obsessed with {n}"),
        ("n", "I can't believe {n} did this live"), ("n", "Watch till the end: {n} 👀"), ("n", "{n} clip of the day 🎬"),
    ],
}
HOOK_BODY = ["Wait for it...", "Watch till the end 👀", "Chat went crazy 😭", "You had to be there.", "Turn the sound on 🔊", "This one is a lot."]
_TOKEN = re.compile(r"[^\W_]+|[^\w\s]", re.UNICODE)
_BAD = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|nigg\w*|fag\w*|cunt|retard\w*|rape\w*|kill yourself|kys)\b", re.I)
_SKIP_MSG = re.compile(r"^(\W*|[a-z]{1,2}|[+\-]\d+|\d+)$", re.I)


def classify(top: list) -> str:
    """Which reaction dominated chat during the spike. `top` = [(message, count), ...]."""
    score = {k: 0 for k in CATEGORIES}
    for text, count in top or []:
        low, toks = str(text).lower(), set(_TOKEN.findall(str(text).lower()))
        for cat, words in CATEGORIES.items():
            if toks & words or any(" " in w and w in low for w in words):
                score[cat] += int(count)
    best = max(score, key=score.get)
    return best if score[best] > 0 else "default"


def display_name(channel: str, cfg: Config) -> str:
    names = {k.lower(): v for k, v in (cfg.display_names or {}).items()}
    return names.get(channel.lower()) or channel[:1].upper() + channel[1:]


def chat_phrase(top: list) -> tuple[str | None, int]:
    """The most-spammed message that reads as a phrase (not a single letter, a number or a bot command)."""
    best, n = None, 0
    for text, count in top or []:
        t = re.sub(r"\s+", " ", str(text)).strip()
        if len(t) < 3 or len(t) > 28 or _SKIP_MSG.match(t) or t.startswith(("!", "@", "http")) or _BAD.search(t):
            continue
        if int(count) > n:
            best, n = t, int(count)
    if best and best.isupper() is False and len(best.split()) <= 2:
        best = best.upper()                              # "no way" -> "NO WAY": reads as chat spam
    return best, n


def best_quote(words: list, lo: float, hi: float, moment: float) -> str | None:
    """A short, punchy sentence the streamer said near the moment (from the transcript words)."""
    ws = [w for w in words if w[2] > lo and w[1] < hi]
    if len(ws) < 3:
        return None
    sents, cur = [], []
    for w in ws:
        cur.append(w)
        if re.search(r"[.!?]$", w[0]) or len(cur) >= 9:
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    best, score = None, -1.0
    for s in sents:
        text = " ".join(x[0] for x in s).strip(" ,;:-")
        n = len(s)
        if n < 3 or n > 10 or _BAD.search(text):
            continue
        mid = (s[0][1] + s[-1][2]) / 2
        sc = 3 - abs(mid - moment) / 8 + (1.5 if text.endswith(("!", "?")) else 0) + (1 if 4 <= n <= 7 else 0)
        if sc > score:
            best, score = text, sc
    if not best:
        return None
    best = re.sub(r"[.,;:]+$", "", best)
    return best[0].upper() + best[1:] if best else None


def features(meta: dict, cfg: Config, words: list | None = None, window: tuple[float, float] | None = None) -> dict:
    sp = meta.get("spike") or {}
    top = sp.get("top") or []
    say, count = chat_phrase(top)
    base = float(sp.get("baseline") or 0) or 1.0
    peak = int(sp.get("peak") or 0)
    quote = None
    if words:
        lo, hi = window or (meta.get("suggest_start_s", 0), meta.get("suggest_end_s", meta.get("duration_s", 0)))
        quote = best_quote(words, lo, hi, float(meta.get("moment_s", (lo + hi) / 2)))
    return {"n": display_name(str(meta.get("channel", "")), cfg), "cat": classify(top), "say": say, "count": count, "quote": quote,
            "peak": peak, "ratio": round(peak / base, 1) if peak and base else 0,
            "loud": (meta.get("loudness_jump_db") or 0) >= 6}


def _fits(need: str, f: dict) -> bool:
    if need == "n":
        return True
    if need in ("peak", "ratio"):
        return (f["peak"] >= 25 and f["ratio"] >= 2.5)
    return bool(f.get(need))


def candidates(f: dict) -> list[tuple[str, str]]:
    """[(family, title)] for every template this clip has the data for."""
    fams = ["speech", "chat", f["cat"], "big", "hook"] + (["loud"] if f["loud"] else [])
    out = []
    for fam in fams:
        for need, tpl in T[fam]:
            if _fits(need, f):
                title = tpl.format(n=f["n"], say=f["say"] or "", quote=f["quote"] or "", count=f["count"], peak=f["peak"], ratio=f["ratio"])
                if len(title) <= 95:
                    out.append((fam, title))
    return out


def pick(f: dict, rng: random.Random, seen: set[str], k: int = 6, extra: list | None = None, order: list | None = None) -> list[str]:
    """k varied titles: at most two per family, clip-specific families first, never one already shown.
    `extra` = more (family, title) candidates (your taught templates); `order` = your preferred family order (style rules)."""
    cands = [c for c in (extra or []) + candidates(f) if c[1] not in seen]
    rng.shuffle(cands)
    prio = {"speech": 0, "chat": 1, f["cat"]: 2, "loud": 3, "big": 4, "hook": 5}
    if order:
        fam = {"category": f["cat"]}
        prio = {fam.get(x, x): i for i, x in enumerate(order)}
    cands.sort(key=lambda c: prio.get(c[0], 9) + rng.random() * 1.6)      # specific first, but never the same order twice
    out, per = [], Counter()
    for fam, title in cands:
        if per[fam] < 2 and title not in out:
            out.append(title)
            per[fam] += 1
        if len(out) == k:
            break
    for fam, title in cands:                              # not enough variety: fill with whatever is left
        if len(out) >= k:
            break
        if title not in out:
            out.append(title)
    return out


def hashtags(f: dict, meta: dict, cfg: Config, body: dict | None = None) -> list[str]:
    chan = re.sub(r"\W", "", str(meta.get("channel", "")).lower())
    emote = re.sub(r"\W", "", (f["say"] or "").lower()) if f["say"] and len(f["say"].split()) == 1 else ""
    if body is None:
        tags = [chan, *CATEGORY_TAGS[f["cat"]], *([emote] if emote and len(emote) <= 14 else []), *cfg.hashtags]
        return list(dict.fromkeys(t.lstrip("#") for t in tags if t))[:9]
    tags = [*body.get("hashtags_fixed", []), *([chan] if body.get("channel_tag", True) else []), *CATEGORY_TAGS[f["cat"]],
            *([emote] if emote and len(emote) <= 14 else []), *cfg.hashtags]
    return list(dict.fromkeys(t.lstrip("#") for t in tags if t))[:max(0, int(body.get("hashtags_max", 9)))]


def body_for(f: dict, meta: dict, cfg: Config, rng: random.Random, body: dict | None = None) -> str:
    lines = []
    hooks = [h for h in (body or {}).get("hooks") or [] if not _BAD.search(h)] or HOOK_BODY
    lead = f'"{f["quote"]}"' if f["quote"] else rng.choice(hooks)
    lines.append(lead)
    credit = cfg.credit_line if (body or {}).get("credit_line") is None else body["credit_line"]
    if credit and meta.get("source_url"):
        lines.append(f"🎥 Clip from {f['n']}: {meta['source_url']}")
    tags = hashtags(f, meta, cfg, body)
    at = mention(meta, cfg)                              # the streamer is always tagged, next to the hashtags
    if tags or at:
        lines.append(" ".join(([at] if at else []) + ["#" + t for t in tags]))
    return "\n\n".join(lines)


def with_mention(body: str, meta: dict, cfg: Config) -> str:
    """The description with the @streamer tag: added in front of the hashtag line (or at the end) when it is not there yet."""
    at = mention(meta, cfg)
    if not at or re.search(r"(?<![\w@])" + re.escape(at) + r"(?!\w)(?!\.\w)", body, re.I):
        return body
    lines = body.rstrip().split("\n")
    if lines and lines[-1].lstrip().startswith("#"):
        lines[-1] = at + " " + lines[-1].lstrip()
        return "\n".join(lines)
    return (body.rstrip() + "\n\n" + at) if body.strip() else at


def _handle(v) -> str:
    h = str(v or "").strip().lstrip("@")
    return h if re.fullmatch(r"[\w.]{1,30}", h, re.ASCII) else ""   # never change a name to fit: that could tag somebody else


def mention(meta: dict, cfg: Config) -> str:
    """@streamer: their handle from `handles` in the config (it may differ on TikTok / Instagram), else the Twitch name, else the
    @handle in a YouTube link. No tag when the handle is not known (a YouTube video link): a wrong @ would tag a stranger."""
    chan = str(meta.get("channel") or "")
    raw = getattr(cfg, "handles", None)
    handles = {str(k).lower(): v for k, v in raw.items()} if isinstance(raw, dict) else {}
    h = _handle(handles.get(chan.lower()))
    if h:
        return "@" + h
    if meta.get("platform") == "youtube" or chan.startswith("yt_"):
        m = re.search(r"youtube\.com/@([\w.-]+)", str(meta.get("source_url") or ""))
        return "@" + _handle(m.group(1)) if m and _handle(m.group(1)) else ""
    h = _handle(chan)
    return "@" + h if h else ""


def _fill(tpl: str, f: dict) -> str | None:
    """A taught template with {streamer} {phrase} {quote} {count}; None when the clip lacks something it needs. Plain replacement, no format()."""
    vals = {"streamer": f["n"], "phrase": f["say"], "quote": f["quote"], "count": str(f["count"]) if f["count"] else None}
    out = tpl
    for k, v in vals.items():
        if "{" + k + "}" in out:
            if not v:
                return None
            out = out.replace("{" + k + "}", v)
    out = out.strip()
    return out if out and len(out) <= 95 and not _BAD.search(out) and "{" not in out else None


def _ends_emoji(t: str) -> bool:
    t = t.rstrip()
    return bool(t) and (unicodedata.category(t[-1]) == "So" or t[-1] == "\ufe0f")


def _recase(t: str, case: str | None) -> str:
    if case == "upper":
        return t.upper()
    if case == "lower":
        return t.lower()
    if case == "title":
        return re.sub(r"(?<![\w'])([^\W\d_])", lambda m_: m_.group(1).upper(), t)
    if case == "sentence":
        return re.sub(r"[^\W\d_]", lambda m_: m_.group(0).upper(), t, count=1)
    return t


def styled_titles(titles: list[str], tr: dict) -> list[str]:
    """Your title rules: drop titles that break your word limit / emoji habit (unless that leaves none), then apply your case."""
    keep = titles
    if tr.get("max_words"):
        keep = [t for t in keep if len(t.split()) <= tr["max_words"]] or keep
    if tr.get("emoji_end") is not None:
        keep = [t for t in keep if _ends_emoji(t) == tr["emoji_end"]] or keep
    return list(dict.fromkeys(_recase(t, tr.get("case")) for t in keep))


def suggest(meta: dict, cfg: Config, words: list | None = None, window: tuple[float, float] | None = None,
            seed: int = 0, seen: list[str] | None = None, rules: dict | None = None) -> dict:
    """{'title', 'alts' (6 fresh titles), 'body', 'category', 'seen' (every title shown so far), 'used_speech': bool}.
    Different `seed` (and the `seen` list) give different titles; the same clip + seed always gives the same ones.
    `rules` = your style (style.load): your templates, family order, case, word limit, emoji habit, hooks and hashtags."""
    f = features(meta, cfg, words, window)
    rng = random.Random(zlib.crc32(f"{meta.get('id', '')}:{seed}".encode()))
    seen_set = set(seen or [])
    tr = (rules or {}).get("title") or {}
    extra = [("taught", t) for t in (_fill(x, f) for x in tr.get("templates") or []) if t]
    order = tr.get("source_order") if rules else None
    alts = pick(f, rng, seen_set, extra=extra, order=order) or pick(f, rng, set(), extra=extra, order=order)
    shown = list(dict.fromkeys([*(seen or []), *alts]))[-60:]     # the raw titles: that is what pick() filters on
    if rules:
        alts = styled_titles(alts, tr)
    body_rules = rules.get("body") if rules else None
    return {"title": alts[0], "alts": alts, "body": body_for(f, meta, cfg, rng, body_rules), "category": f["cat"], "seed": seed,
            "seen": shown, "used_speech": bool(f["quote"]), "chat_phrase": f["say"]}
