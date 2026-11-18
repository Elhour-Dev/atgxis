"""Settings. Every value has a safe default; override any of them in a TOML file (see config.example.toml)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_BOTS = [
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot",
    "sery_bot", "soundalerts", "commanderroot", "lurxx", "nightdev", "stay_hydrated_bot",
]
DEFAULT_HYPE = [
    "kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "haha", "hahaha", "pog", "pogchamp",
    "poggers", "pogu", "pogs", "wtf", "wow", "omg", "insane", "clip", "clipit", "clutch",
    "gg", "ggs", "w", "ww", "www", "holy", "nooo", "noooo", "icant", "dead", "cracked",
    "😂", "🤣", "💀", "🔥", "😭",
]


@dataclass
class Config:
    # --- spike detection (all times in seconds) ---
    window_s: int = 10          # chat is measured over a sliding window this long
    baseline_s: int = 300       # "normal" chat level = median of the last N seconds
    warmup_s: int = 90          # learn the baseline this long before clipping anything
    mult: float = 2.5           # spike if unique chatters >= baseline * mult ...
    z: float = 3.0              # ... and >= baseline + z*sqrt(baseline) (statistical noise guard)
    min_users: int = 6          # ... and at least this many different chatters in the window
    exit_ratio: float = 1.5     # spike over when chatters < baseline*exit_ratio + 1 ...
    hold_s: int = 8             # ... for this many seconds in a row
    merge_gap_s: int = 30       # spikes closer than this become one clip
    max_active_s: int = 60      # force a clip split if a spike lasts this long
    cooldown_s: int = 20        # pause after a forced split
    raid_suppress_s: int = 90   # ignore spikes right after a raid
    chat_grace_s: float = 3.0   # wait this long for late chat messages (YouTube batches ~5s)

    # --- clip cutting ---
    pre_s: int = 30             # chat reacts AFTER the moment, so start this much earlier
    post_s: int = 12
    max_clip_s: int = 120
    encoder: str = "libx264"    # try h264_nvenc / h264_qsv / h264_amf for a hardware encoder
    preset: str = "veryfast"
    crf: int = 21
    cut_workers: int = 2
    vertical: str = "on_demand"  # "on_demand" (button in review page) | "auto" | "off"
    vertical_mode: str = "blur"  # "blur" (full frame on blurred bg) | "crop" (center crop)

    # --- recording ---
    quality: str = "720p60,720p,480p,best"   # lower than source = less disk, no CPU cost (stream copy)
    segment_s: int = 4
    buffer_s: int = 600         # rolling buffer length on disk
    buffer_dir: str = ""        # default: system temp folder
    out_dir: str = "clips"
    max_clips_gb: float = 20.0  # stop cutting if the clips folder exceeds this

    bots: list[str] = field(default_factory=lambda: list(DEFAULT_BOTS))
    hype_terms: list[str] = field(default_factory=lambda: list(DEFAULT_HYPE))


def load(path: str | Path | None) -> Config:
    cfg = Config()
    if not path:
        return cfg
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
    for k, v in data.items():
        setattr(cfg, k, v)
    return cfg
