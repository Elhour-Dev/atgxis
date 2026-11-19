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
    mult: float = 2.0           # spike if unique chatters >= baseline * mult ...
    z: float = 3.0              # ... and >= baseline + z*sqrt(baseline) (statistical noise guard)
    auto_mult: bool = True      # big chats are steadier, so the multiplier eases down to 1.8 as chat gets busier
    min_users: int = 6          # ... and at least this many different chatters in the window
    exit_ratio: float = 1.5     # spike over when chatters < baseline*exit_ratio + 1 ...
    hold_s: int = 8             # ... for this many seconds in a row
    merge_gap_s: int = 30       # spikes closer than this become one clip
    max_active_s: int = 60      # force a clip split if a spike lasts this long
    cooldown_s: int = 20        # pause after a forced split
    raid_suppress_s: int = 90   # ignore spikes right after a raid
    chat_grace_s: float = 3.0   # wait this long for late chat messages (YouTube batches ~5s)
    # a loud moment (streamer screaming, game explosion) is a second signal: it lowers the chat threshold, never fires alone
    audio_assist: bool = True
    audio_boost_db: float = 8.0  # "loud" = this many dB above the recent normal volume
    audio_mult: float = 1.35     # chat only needs to be this many times normal when the audio is loud

    # --- clip cutting ---
    # Drafts are cut by STREAM COPY (instant, no CPU, no quality loss) with generous context.
    # You trim in the review page; only the clips you export get one precise encode.
    cut_mode: str = "copy"      # "copy" (fast draft) | "encode" (exact window, ~10s of CPU per clip)
    clip_len_s: int = 30        # length of every finished clip (the suggested trim / automatic vertical export)
    pre_s: int = 30             # draft context before the spike (free in copy mode; lets you slide the window earlier)
    post_s: int = 12
    max_clip_s: int = 120
    reaction_lag_s: int = 10    # chat reacts this long after the moment; used to place the 30 s window
    lead_in_s: int = 5          # plus this much lead-in before the moment
    dup_overlap: float = 0.6    # skip a clip that overlaps an existing one by this much
    encoder: str = "auto"       # "auto" = NVIDIA/Intel/AMD hardware encoder if it really works, else CPU (x264)
    preset: str = "medium"      # CPU encoder speed/quality (exports are one-off, so quality beats speed)
    crf: int = 17               # quality (lower = better/larger; 17 is visually lossless for social media)
    sharpen: float = 0.0        # 0 = off. 0.3-0.8 sharpens the enlarged webcam panel (try it if your webcam looks soft)
    out_fps: int = 30           # social platforms use 30; halves encode time vs 60. 0 = keep source fps
    normalize_audio: bool = True  # loudness-normalize to social-media level (-14 LUFS)
    low_priority: bool = True   # encode at below-normal priority so your game / stream never stutters
    cut_workers: int = 2        # total simultaneous encodes across ALL streams
    draft_workers: int = 4      # total simultaneous draft cuts across ALL streams (stream copy: light, and never queued behind encodes)
    max_encode_backlog: int = 6  # if this many 9:16 renders are already waiting, new clips stay drafts (export them in the review page)
    status_every_s: int = 60    # with several streams: print a one-block overview this often (0 = never)
    vertical: str = "auto"      # "auto" = also export a ready 9:16 of every suggested trim | "on_demand" | "off"
    vertical_mode: str = "blur"  # layout used when a channel has no saved layout: "blur" | "crop"

    # --- captions (optional: pip install faster-whisper) ---
    whisper_model: str = "small"    # tiny/base/small/medium; "small" is a good speed/accuracy balance
    whisper_device: str = "auto"    # auto picks the GPU if CUDA is available
    whisper_language: str = ""      # "" = detect automatically; "en", "fr", ... forces a language (more reliable if you know it)
    caption_font: str = "Arial"

    # --- recording ---
    quality: str = "best"       # the stream's top quality (usually 1080p60). Recording is stream copy: no CPU cost, ~0.7 GB per 10 min
    segment_s: int = 4
    buffer_s: int = 600         # rolling buffer length on disk
    buffer_dir: str = ""        # default: system temp folder
    out_dir: str = "clips"      # drafts + review page live here
    export_dir: str = ""        # optional: finished clips are ALSO copied here (e.g. D:/Clips). Set it in the review page too
    max_clips_gb: float = 20.0  # stop cutting if the clips folder exceeds this

    # --- post text suggestions (review page) ---
    hashtags: list[str] = field(default_factory=lambda: ["fyp", "viral", "streamer", "twitch", "clips"])
    credit_line: bool = True    # add "Clip from <streamer>: <link>" to the suggested description
    display_names: dict[str, str] = field(default_factory=dict)   # e.g. { kaicenat = "Kai Cenat" }

    bots: list[str] = field(default_factory=lambda: list(DEFAULT_BOTS))
    hype_terms: list[str] = field(default_factory=lambda: list(DEFAULT_HYPE))

OLD_QUALITY = "720p60,720p,480p,best"


def load(path: str | Path | None, auto: bool = True) -> Config:
    """Load settings. With no path given, a `config.toml` in the current folder is used automatically, so you never
    have to remember the --config flag (forgetting it silently ran on less sensitive defaults)."""
    cfg = Config()
    if not path and auto and Path("config.toml").is_file():
        path = "config.toml"
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
    if cfg.quality.replace(" ", "") == OLD_QUALITY:          # copied from an old config.example.toml: that cap made every clip blurry
        cfg.quality = "best"
    return cfg
