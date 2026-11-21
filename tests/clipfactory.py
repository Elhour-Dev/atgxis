"""Synthetic clip records and a user with a (made-up) taste, shared by the learning and autopilot tests."""
from __future__ import annotations

import json
import random
import time
from datetime import datetime
from pathlib import Path

GOOD_CH, BAD_CH = "fav", "meh"


def meta(i: int, channel: str = GOOD_CH, *, rnd: random.Random | None = None, created: float | None = None, status: str = "new",
         phrase: str | None = None) -> dict:
    """A clip record shaped like the ones the worker writes. Good clips (channel `fav`) are big KEKW moments; `meh` ones are small and spammy."""
    rnd = rnd or random.Random(i)
    good = channel == GOOD_CH
    base = rnd.choice([8, 15, 40])
    peak = int(base * (rnd.uniform(3.5, 6) if good else rnd.uniform(1.8, 2.6)))
    users = peak + rnd.randint(0, 5)
    ph = phrase or ("KEKW" if good else "hello")
    when = created if created is not None else time.time() - 60
    return {"id": f"{channel}/s/c{i}", "file": f"c{i}.mp4", "channel": channel, "platform": "twitch", "source_url": f"https://twitch.tv/{channel}",
            "status": status, "score": rnd.uniform(60, 90) if good else rnd.uniform(25, 55), "chat_score": 50.0, "loudness_jump_db": rnd.uniform(0, 8),
            "created": datetime.fromtimestamp(when).isoformat(timespec="seconds"), "res": [1920, 1080], "duration_s": 60.0, "moment_s": 30.0,
            "clip_len_s": 30, "suggest_start_s": 10.0, "suggest_end_s": 40.0, "exports": [],
            "spike": {"start": 100, "end": 100 + rnd.randint(10, 30), "onset": 102, "peak_t": 105, "peak": peak, "baseline": base,
                      "msgs": int(users * (2 if good else 5)), "users": users, "hype_frac": 0.4 if good else 0.05, "audio_db": 6 if good else 1,
                      "top": [[ph, rnd.randint(10, 30)], ["lol", 4]]}}


def write_clip(root: Path, m: dict, export: bool = True) -> Path:
    """Put a clip on disk the way the program does: <root>/<channel>/<session>/<name>.clip.json + an mp4 + (optionally) a 9:16 export."""
    d = root / m["channel"] / "s"
    d.mkdir(parents=True, exist_ok=True)
    stem = m["file"][:-4]
    (d / m["file"]).write_bytes(b"draft")
    m = dict(m)
    if export:
        (d / f"{stem}_v.mp4").write_bytes(b"vertical clip")
        m["exports"] = [{"file": f"{stem}_v.mp4", "start": m["suggest_start_s"], "end": m["suggest_end_s"], "vertical": True, "captions": False,
                         "originality": {"hook": True, "voiceover": False}, "reframe": {"applied": True}}]                    # hook + reframe: Instagram may get it
    m["description"] = {"title": f"Title of {stem}", "body": "Body #fyp", "alts": [], "category": "funny"}
    p = d / f"{stem}.clip.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


def ffmpeg_leaked(pattern: str) -> bool:
    """Is an ffmpeg with `pattern` in its command line still running? pgrep where there is one, PowerShell on Windows."""
    import shutil
    import subprocess as sp
    if shutil.which("pgrep"):
        return sp.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0
    ps = ["powershell", "-NoProfile", "-Command",
          "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | Where-Object { $_.CommandLine -like '*" + pattern + "*' } | "
          "Measure-Object | Select-Object -ExpandProperty Count"]
    r = sp.run(ps, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        raise RuntimeError(f"could not list processes: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip() != "0"


def teach(taste, n_each: int = 60, seed: int = 1) -> dict:
    """Record the decisions of a user who keeps every `fav` clip and trashes every `meh` one, then learn from them."""
    rnd = random.Random(seed)
    for i in range(n_each):
        taste.record(meta(i, GOOD_CH, rnd=rnd), 1, "keep", when=1000 + i)
        taste.record(meta(i, BAD_CH, rnd=rnd), 0, "trash", when=1000 + i)
    return taste.train()
