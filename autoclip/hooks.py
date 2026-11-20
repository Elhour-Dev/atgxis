"""Hook text: a short line burned big at the top of the first 2.5 seconds, so people stop scrolling.

  * Made from the clip's transcript by Groq (free key, optional) or, without it, from templates in the clip's language.
  * Rules for every hook, whoever wrote it: at most 7 words, makes people curious, claims nothing that is not in the clip
    (no "died / arrested / leaked / official..."), and is not one of your last 50 hooks.
  * Placed top-centre inside the platforms' safe zones, never over the facecam panel, the captions or the ad disclosure.
  * Styles rotate (4 bold outlined looks, all with a pop-in) so your clips do not all look the same.

Standard library only. History lives next to your style files (%APPDATA%/autoclip/style/hooks.json)."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
from pathlib import Path

log = logging.getLogger("autoclip.hooks")

MAX_WORDS = 7
HISTORY = 50
HOOK_S = 2.5
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

TEMPLATES = {
    "en": ["Wait for it…", "Watch what happens next", "Chat could not believe this", "Keep watching until the end",
           "This is why chat exploded", "How did this even happen?", "You have to see this", "This moment broke the chat",
           "Look closely at this", "Nobody in chat saw this coming"],
    "fr": ["Attends la suite…", "Regarde bien ce qui arrive", "Le chat n'en revenait pas", "Reste jusqu'à la fin",
           "Comment c'est possible ?", "Il faut voir ça", "Ce moment a fait exploser le chat", "Regarde bien ça"],
    "es": ["Espera a ver esto…", "Mira lo que pasa después", "El chat no lo podía creer", "Quédate hasta el final",
           "¿Cómo pasó esto?", "Tienes que ver esto", "Este momento hizo explotar el chat", "Fíjate bien en esto"],
    "de": ["Wartet ab…", "Schau, was dann passiert", "Der Chat konnte es nicht glauben", "Bleib bis zum Ende",
           "Wie ist das passiert?", "Das musst du sehen", "Dieser Moment sprengte den Chat", "Schau genau hin"],
    "pt": ["Espera só…", "Olha o que acontece depois", "O chat não acreditou nisso", "Fica até o final",
           "Como isso aconteceu?", "Você precisa ver isso", "Esse momento explodiu o chat", "Repara bem nisso"],
}
QUOTE_TEMPLATES = {          # {q} = a few words the streamer really said
    "en": ['"{q}"… then this happened', 'Wait until "{q}"'],
    "fr": ['"{q}"… et là', 'Attends le "{q}"'],
    "es": ['"{q}"… y entonces', 'Espera al "{q}"'],
    "de": ['"{q}"… und dann', 'Wartet auf "{q}"'],
    "pt": ['"{q}"… e aí', 'Espera o "{q}"'],
}

# words that state facts a clip cannot prove: a hook that uses them could be a lie, so it is refused
_CLAIMS = re.compile(r"(?i)\b(died|dies|dead|death|killed|arrested|banned|leaked|exposed|confirmed|official|breaking|cancel+ed|"
                     r"fired|divorce|pregnant|lawsuit|sued|hacked|scam|million|billion|mort|arrêté|banni|officiel|muerto|"
                     r"arrestado|oficial|tot|verhaftet|gesperrt|offiziell|morto|preso|banido)\b|\$|100\s?%|#1\b")

_STOP = {
    "en": "the you and is it that what this i to of was he she they my your so just no yeah oh".split(),
    "fr": "le la les et est c'est que je tu il elle on pas de des un une mais quoi oui non ça".split(),
    "es": "el la los las y es que yo tú no de un una pero qué sí eso esto muy".split(),
    "de": "der die das und ist ich du nicht was ein eine aber ja nein so mit auf".split(),
    "pt": "o a os as e é que eu você não de um uma mas sim isso muito tá".split(),
}

# style -> (text colour, outline colour) in ASS &HBBGGRR&; every style is bold, outlined and pops in
STYLES = {"pop": ("&H00FFFFFF", "&H00000000"), "yellow": ("&H0000F0FF", "&H00000000"),
          "red": ("&H00FFFFFF", "&H002020E0"), "ink": ("&H00101010", "&H00FFFFFF")}
STYLE_NAMES = tuple(STYLES)

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------------- rules
def norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").lower()).split())


def clean(text: str) -> str:
    """One line, no characters that the subtitle renderer would read as commands."""
    return " ".join(re.sub(r"[{}\\\r\n]", " ", str(text or "")).split()).strip(" -–—")[:120]


def problem(text: str, recent: list[str] | tuple = ()) -> str | None:
    """Why this hook may not be used, or None when it is fine."""
    t = clean(text)
    if not t:
        return "empty"
    if len(t.split()) > MAX_WORDS:
        return f"more than {MAX_WORDS} words"
    if _CLAIMS.search(t):
        return "states something the clip cannot prove"
    if norm(t) in {norm(r) for r in recent}:
        return "used in one of your last 50 hooks"
    return None


def language(words: list | None, forced: str | None = None, text: str | None = None) -> str:
    """The clip's language: the caption language you set, else guessed from the transcript's common words, else English."""
    if forced:
        return forced.lower()[:2]
    toks = [norm(w[0] if isinstance(w, (list, tuple)) else w) for w in (words or [])]
    toks = " ".join(toks + [norm(text or "")]).split()
    if not toks:
        return "en"
    score = {k: sum(t in s for t in toks) for k, s in ((k, set(v)) for k, v in _STOP.items())}
    best = max(score, key=score.get)
    return best if score[best] >= 2 else "en"


# ---------------------------------------------------------------------------------------------------- history
def _path() -> Path:
    from . import style
    return style.folder() / "hooks.json"


def history() -> list[dict]:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return [h for h in d.get("recent", []) if isinstance(h, dict) and h.get("text")][-HISTORY:]
    except (OSError, ValueError, AttributeError):
        return []


def recent_texts() -> list[str]:
    return [h["text"] for h in history()]


def remember(text: str, style_name: str, source: str = "") -> None:
    """A hook that was burned into a clip: it may not come back for your next 50 hooks."""
    with _LOCK:
        h = history() + [{"text": clean(text), "style": style_name, "source": source}]
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"recent": h[-HISTORY:]}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)


def next_style() -> str:
    """Styles take turns: the one after the style of your last hook."""
    h = history()
    last = h[-1].get("style") if h else None
    return STYLE_NAMES[(STYLE_NAMES.index(last) + 1) % len(STYLE_NAMES)] if last in STYLE_NAMES else STYLE_NAMES[0]


# ---------------------------------------------------------------------------------------------------- making one
def groq_key() -> str:
    from . import accounts
    return str(accounts.get("groq").get("api_key") or os.environ.get("GROQ_API_KEY") or "").strip()


def _groq(transcript: str, lang: str, avoid: list[str], key: str, http=None) -> list[str]:
    """Up to 6 candidate hooks from Groq. Raises on any network / API problem (the caller falls back to templates)."""
    from . import accounts
    http = http or accounts.http
    prompt = (f"Transcript of a short livestream clip:\n{transcript[:1500] or '(no speech)'}\n\n"
              f"Write 6 different hooks for this clip in the language with code '{lang}'. Each hook: at most {MAX_WORDS} words, "
              "makes viewers curious about what happens, and says NOTHING that the transcript does not support "
              "(no invented facts, names, numbers, deaths, bans or news). No hashtags, no emojis. One hook per line, nothing else."
              + ("\nDo not reuse these: " + " | ".join(avoid[-50:]) if avoid else ""))
    st, _, raw = http("POST", GROQ_URL, headers={"Authorization": "Bearer " + key},
                      body={"model": GROQ_MODEL, "temperature": 0.9, "max_tokens": 200,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=15)
    if st != 200:
        raise RuntimeError(f"Groq answered {st}")
    text = json.loads(raw)["choices"][0]["message"]["content"]
    return [re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", ln).strip().strip('"').strip() for ln in str(text).splitlines() if ln.strip()]


def _quote(words: list) -> str | None:
    """A short bit the streamer said (2-4 words), taken from the loudest-looking sentence: the longest run without a pause."""
    runs, cur, last = [], [], None
    for w in words or []:
        t, a, b = w
        if cur and last is not None and a - last > 0.6:
            runs.append(cur)
            cur = []
        cur.append(clean(t).strip(".,!?"))
        last = b
    if cur:
        runs.append(cur)
    runs = [r for r in runs if 2 <= len(r) <= 4 and all(r)]
    return " ".join(runs[0]) if runs else None


def suggest(words: list | None, lang: str, avoid: list[str] | None = None, seed: str = "", extra: list[str] | None = None,
            key: str | None = None, http=None) -> dict | None:
    """{'text', 'source': 'groq'|'template'|'yours', 'lang'} or None when no allowed hook exists for this language.
    `avoid` = texts already shown for this clip (Regenerate); your last 50 hooks are always avoided too.
    `extra` = your own hook lines (from your style), used before the built-in templates."""
    recent = recent_texts() + list(avoid or [])
    key = groq_key() if key is None else key
    if key:
        try:
            transcript = " ".join(clean(w[0]) for w in words or [])
            for h in _groq(transcript, lang, recent, key, http):
                if not problem(h, recent):
                    return {"text": clean(h), "source": "groq", "lang": lang}
        except Exception as e:                                   # never log the key; the reason is enough
            log.info("Groq hook failed (%s): using templates", type(e).__name__)
    rng = random.Random(f"{seed}|{len(recent)}")
    yours = [x for x in extra or [] if language(None, None, x) == lang]
    q = _quote(words or [])
    built = [t.format(q=q) for t in QUOTE_TEMPLATES.get(lang, [])] if q else []
    pool = list(TEMPLATES.get(lang, []))
    rng.shuffle(built)
    rng.shuffle(pool)
    for src, items in (("yours", yours), ("template", built + pool)):
        for h in items:
            if not problem(h, recent):
                return {"text": clean(h), "source": src, "lang": lang}
    return None


# ---------------------------------------------------------------------------------------------------- burning it in
def place(W: int, H: int, protected: list[tuple[float, float]], size: int) -> int:
    """Top y (pixels) for a hook block of 2 lines at `size`: the highest spot inside the safe zone (below the platform's top bar
    and above its bottom buttons) that does not touch any protected band (y0, y1): facecam, captions, disclosure, handle."""
    block = int(size * 2.5)
    top, bottom = int(H * 0.09), int(H * 0.78)
    for y in range(top, bottom - block, max(4, H // 200)):
        if all(y + block <= a or y >= b for a, b in protected):
            return y
    return top


def ass_event(text: str, style_name: str, W: int, H: int, protected: list[tuple[float, float]], dur: float,
              font: str = "Arial") -> tuple[str, str]:
    """(style line, dialogue line) for the hook: bold, outlined, top-centre, pop-in, 0-2.5 s."""
    fill, edge = STYLES.get(style_name, STYLES["pop"])
    size = round(H * (0.05 if H > W else 0.065))
    y = place(W, H, protected, size)
    style = (f"Style: Hook,{font},{size},{fill},{fill},{edge},&H64000000,-1,0,0,0,100,100,0,0,1,{max(5, size // 9)},2,8,"
             f"{round(W * 0.06)},{round(W * 0.06)},0,1\n")
    pop = "{\\an8\\pos(%d,%d)\\fscx40\\fscy40\\t(0,140,\\fscx112\\fscy112)\\t(140,240,\\fscx100\\fscy100)\\fad(0,180)}" % (W // 2, y)
    end = min(HOOK_S, max(dur, 0.5))
    s = f"{int(end // 3600)}:{int(end % 3600 // 60):02d}:{end % 60:05.2f}"
    return style, f"Dialogue: 3,0:00:00.00,{s},Hook,,0,0,0,,{pop}{clean(text)}\n"
