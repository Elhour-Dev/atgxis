"""Post-text suggestions (title options + description with credit and hashtags) for each clip.

This is rule-based, not AI: it reads what chat was spamming during the spike (KEKW = funny, WTF = shock, POG = hype ...)
and fills a title template. It is a starting point you edit in the review page, not finished copy.
"""
from __future__ import annotations

import re
import zlib

from .config import Config

CATEGORIES = {
    "funny": {"kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "haha", "hahaha", "icant", "dead", "rofl", "😂", "🤣", "💀"},
    "shock": {"wtf", "omg", "wow", "nooo", "noooo", "bruh", "no way", "what", "huh", "monkas", "😳", "😱", "😭"},
    "hype": {"pog", "pogchamp", "poggers", "pogu", "pogs", "w", "ww", "www", "gg", "ggs", "clip", "clipit", "insane",
             "clutch", "goat", "cracked", "🔥", "🐐"},
}
TITLES = {
    "funny": ["{n} had chat in TEARS 😂", "{n} said WHAT 💀", "Chat could not stop laughing at {n} 😂"],
    "shock": ["{n} left chat SPEECHLESS 😳", "Nobody expected this from {n} 😱", "{n} did WHAT?! 😳"],
    "hype": ["{n} just did the IMPOSSIBLE 🔥", "CHAT went CRAZY for {n} 🔥", "{n} is UNREAL 🔥"],
    "default": ["The moment chat EXPLODED on {n}'s stream 👀", "{n}'s stream goes CRAZY 👀", "You had to be there: {n} 👀"],
}
CATEGORY_TAGS = {"funny": ["funny", "lol"], "shock": ["omg", "shocked"], "hype": ["hype", "insane"], "default": ["moments"]}
_TOKEN = re.compile(r"[^\W_]+|[^\w\s]", re.UNICODE)


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


def suggest(meta: dict, cfg: Config) -> dict:
    """{'title': best title, 'alts': [3 titles], 'body': credit line + hashtags, 'category': ...}"""
    cat = classify((meta.get("spike") or {}).get("top"))
    name = display_name(str(meta.get("channel", "")), cfg)
    alts = [t.format(n=name) for t in TITLES[cat]]
    k = zlib.crc32(str(meta.get("id", "")).encode()) % len(alts)       # different clips start on different titles
    alts = alts[k:] + alts[:k]
    chan = re.sub(r"\W", "", str(meta.get("channel", "")).lower())
    tags = list(dict.fromkeys([chan, *CATEGORY_TAGS[cat], *cfg.hashtags]))[:9]
    lines = []
    if cfg.credit_line and meta.get("source_url"):
        lines.append(f"🎥 Clip from {name}: {meta['source_url']}")
    lines.append(" ".join("#" + t.lstrip("#") for t in tags if t))
    return {"title": alts[0], "alts": alts, "body": "\n\n".join(lines), "category": cat}
