from __future__ import annotations

import argparse
import importlib.util
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as cfgmod
from .worker import ChannelWorker


def _setup_logging():
    for s in (sys.stdout, sys.stderr):           # Windows consoles default to cp1252 and choke on emotes
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")


def cmd_doctor(_args) -> int:
    ok = True
    print(f"Python {sys.version.split()[0]}", "OK" if sys.version_info >= (3, 11) else "NEEDS 3.11+")
    ok &= sys.version_info >= (3, 11)
    for exe, need, hint in [("ffmpeg", True, "winget install Gyan.FFmpeg"),
                            ("streamlink", True, "winget install Streamlink.Streamlink"),
                            ("yt-dlp", False, "winget install yt-dlp.yt-dlp   (only needed for YouTube channel/@handle links)")]:
        p = shutil.which(exe)
        print(f"{exe:11s}", p or f"MISSING -> {hint}")
        ok &= bool(p) or not need
    has = importlib.util.find_spec("chat_downloader") is not None
    print(f"{'chat-downloader':11s}", "OK" if has else "missing (only needed for YouTube): pip install chat-downloader")
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        hw = [e for e in ("h264_nvenc", "h264_qsv", "h264_amf") if e in enc]
        print("hardware encoders:", ", ".join(hw) or "none found (CPU encoding is fine)")
        filt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        print("captions (libass):", "OK" if " ass " in filt else "MISSING - install the full FFmpeg build (winget Gyan.FFmpeg has it)")
    print("faster-whisper:", "OK" if importlib.util.find_spec("faster_whisper") else "not installed (only for captions): pip install faster-whisper")
    print("\nAll required tools found." if ok else "\nFix the MISSING items above, then run doctor again.")
    return 0 if ok else 1


def cmd_run(args) -> int:
    cfg = cfgmod.load(args.config)
    if args.out:
        cfg.out_dir = args.out
    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=cfg.cut_workers, thread_name_prefix="cut")
    workers = [ChannelWorker(cfg, t, Path(cfg.out_dir), stop, pool=pool) for t in args.targets]
    for w in workers:
        w.start()
        time.sleep(1)
    print(f"Clipping {len(workers)} stream(s) into {Path(cfg.out_dir).resolve()}.  Review later with:  python -m autoclip review\nCtrl+C to stop.")
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping (finishing clips in progress)...")
        stop.set()
        for w in workers:
            w.join(timeout=120)
    pool.shutdown(wait=True)
    return 0


def cmd_review(args) -> int:
    from .review import serve
    cfg = cfgmod.load(args.config)
    serve(Path(args.out or cfg.out_dir), cfg, args.port, not args.no_browser)
    return 0


def cmd_demo(args) -> int:
    """Synthetic video + synthetic chat: proves ffmpeg, cutting, detection and the review page work, offline."""
    import random
    from .chat import Sink
    from .detector import ChatEvent
    from .recorder import ffmpeg_exe
    cfg = cfgmod.Config(warmup_s=12, baseline_s=40, pre_s=10, post_s=4, hold_s=4, merge_gap_s=6,
                        window_s=8, segment_s=2, min_users=5, chat_grace_s=1.0)
    out = Path(args.out or tempfile.mkdtemp(prefix="autoclip_demo_"))
    src = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-c:v", "libx264", "-preset", "ultrafast",
           "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", "pipe:1"]
    stop = threading.Event()

    class FakeChat(threading.Thread):
        def __init__(self, sink: Sink):
            super().__init__(daemon=True)
            self.sink, self.messages = sink, 0

        def run(self):
            t0, rnd = time.time(), random.Random(1)
            while not stop.is_set():
                el = time.time() - t0
                hot = 22 <= el < 30                                   # the 'exciting moment'
                n = rnd.randint(18, 30) if hot else (1 if rnd.random() < .5 else 0)
                for _ in range(n):
                    u = f"viewer{rnd.randint(1, 40) if hot else rnd.randint(1, 3)}"
                    self.sink(ChatEvent(t=time.time(), user=u, text=rnd.choice(["KEKW", "LUL", "WTF", "clip it", "POG"]) if hot else "hello"))
                    self.messages += 1
                stop.wait(1.0)

    w = ChannelWorker(cfg, "demochannel", out, stop, source_cmd=src, chat_factory=lambda sink: FakeChat(sink))
    w.start()
    print(f"Demo running ~55s (fake video + fake chat spike). Output: {out}")
    t0 = time.time()
    while time.time() - t0 < 55 and w.clips_made == 0:
        time.sleep(1)
    time.sleep(2)
    stop.set()
    w.join(timeout=60)
    clips = list(out.rglob("*.mp4"))
    print(f"\nDEMO {'PASSED' if clips else 'FAILED'}: {len(clips)} clip(s) in {out}")
    if clips:
        print(f"See it in the review page:  python -m autoclip review --out \"{out}\"")
    return 0 if clips else 1


def main(argv=None) -> int:
    _setup_logging()
    ap = argparse.ArgumentParser(prog="autoclip", description="Auto-clip live streams when chat spikes.")
    ap.add_argument("--config", help="TOML settings file (see config.example.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check that required tools are installed")
    r = sub.add_parser("run", help="watch one or more live streams and clip them")
    r.add_argument("targets", nargs="+", help="twitch name / twitch.tv link / youtube live link")
    r.add_argument("--out")
    v = sub.add_parser("review", help="open the review page")
    v.add_argument("--out")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-browser", action="store_true")
    d = sub.add_parser("demo", help="offline self-test with fake video and chat")
    d.add_argument("--out")
    args = ap.parse_args(argv)
    return {"doctor": cmd_doctor, "run": cmd_run, "review": cmd_review, "demo": cmd_demo}[args.cmd](args)
