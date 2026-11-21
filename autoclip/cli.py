from __future__ import annotations

import argparse
import importlib.util
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config as cfgmod
from . import settings
from .cutter import pick_encoder
from .disk import clips_gb, free_gb, sweep_stale_buffers
from .lanes import Lanes
from .power import KeepAwake
from .worker import ChannelWorker, parse_target

log = logging.getLogger("autoclip")


def _setup_logging():
    for s in (sys.stdout, sys.stderr):           # Windows consoles default to cp1252 and choke on emotes
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")


def diagnose_stream(messages: str, got_bytes: int, seconds: float, first_video_s: float | None = None) -> list[str]:
    """What a test of one channel means, in plain words (pure: unit-tested)."""
    from .recorder import AD_WORDS
    low = messages.lower()
    if got_bytes > 200_000:
        out = ["OK: video arrives. Recording this channel works from this PC right now."]
        if first_video_s is not None and first_video_s > 12 and AD_WORDS.search(low):
            out.append(f"The first video came after {first_video_s:.0f} s: Twitch showed an ad first and streamlink skipped it. That is normal for big "
                       "channels without a Twitch login. The recorder waits such ads out; video missing during an ad cannot be clipped.")
        return out
    out = ["NO VIDEO arrived."]
    if "found matching plugin" in low and "available streams" not in low and "no playable streams" not in low:
        out.append("streamlink got STUCK asking Twitch about the channel (it never even listed the available streams), so this is not an "
                   "ad or an offline channel: the connection to Twitch's servers stalled. Often temporary. Run the check again with a longer wait "
                   "(--seconds 60); if it keeps happening, run it with --debug and send me the output, and test the connection: "
                   "Test-NetConnection gql.twitch.tv -Port 443  and  Test-NetConnection usher.ttvnw.net -Port 443")
    if re.search(r"no playable streams|offline|not found|unable to find|could not find", low):
        out.append("Twitch reports the channel as offline (or the name is wrong). Open the channel in your browser to check.")
    if AD_WORDS.search(low):
        out.append("Twitch is showing an AD BREAK first: streamlink skips the ad and sends no video until it ends (can be a minute or more on big "
                   "channels). Run the check again with a longer wait, e.g.  python -m autoclip check <channel> --seconds 120")
    if re.search(r"403|401|forbidden|unauthori[sz]ed|login|subscriber|geo|not available in your", low):
        out.append("Twitch refuses access (login/subscription needed, or this network or region is blocked).")
    if re.search(r"timed out|timeout|connection|name or service|getaddrinfo|resolve|network|ssl|reset by peer", low):
        out.append("Network problem between this PC and Twitch (timeouts / DNS). Same family as the chat timeouts: check a VPN, firewall or "
                   "antivirus web protection, try DNS 1.1.1.1, or test on another network (phone hotspot).")
    if len(out) == 1:
        out.append("No clear reason in streamlink's messages.")
    out.append("Also try updating streamlink (Twitch changes often, old versions break):  winget upgrade Streamlink.Streamlink")
    return out


def cmd_check(args) -> int:
    """Ask streamlink for a few seconds of one channel, exactly like the recorder does, and say what happened."""
    from .recorder import AD_WORDS, kill, streamlink_cmd
    cfg = _load_cfg(args)
    try:
        _, label, url = parse_target(args.target)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2
    exe = shutil.which("streamlink")
    if not exe:
        print("streamlink is MISSING -> winget install Streamlink.Streamlink  (then reopen PowerShell)")
        return 1
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True, errors="replace").stdout.strip()
    print(f"{ver or 'streamlink (version unknown)'}\nTesting {label}: asking for {args.seconds:g} s of video (quality {cfg.quality}) ...")
    proc = subprocess.Popen(streamlink_cmd(url, cfg.quality, "debug" if args.debug else "info"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    got, lines, first = [0], [], [None]

    def pump_out():
        while chunk := proc.stdout.read(65536):
            got[0] += len(chunk)
            if first[0] is None:
                first[0] = time.time()

    def pump_err():
        for raw in iter(proc.stderr.readline, b""):
            lines.append(raw.decode("utf-8", "replace").rstrip())

    pumps = [threading.Thread(target=pump_out, daemon=True), threading.Thread(target=pump_err, daemon=True)]
    for t in pumps:
        t.start()
    t0 = time.time()
    ad_wait, noted = max(args.seconds, 120.0), False
    while proc.poll() is None and got[0] < 200_000:                  # enough video arrived = proven, stop early
        elapsed = time.time() - t0
        saw_ad = got[0] == 0 and any(AD_WORDS.search(ln) for ln in list(lines))
        if saw_ad and not noted:
            noted = True
            print(f"Twitch is showing an ad first (streamlink skips it, so no video arrives until it ends). Waiting up to {ad_wait:.0f} s ...")
        if elapsed >= (ad_wait if saw_ad else args.seconds):
            break
        time.sleep(0.2)
    kill(proc)
    for t in pumps:
        t.join(3)
    took = max(time.time() - t0, 0.5)
    msgs = "\n".join(lines)
    first_s = (first[0] - t0) if first[0] else None
    print("\nstreamlink said:\n" + ("\n".join("  " + ln for ln in lines[-(40 if args.debug else 12):]) if lines else "  (nothing)"))
    print(f"\nreceived {got[0] / 1e6:.2f} MB in {took:.0f} s" + (f", first video after {first_s:.0f} s" if first_s is not None else "") + "\n")
    verdict = diagnose_stream(msgs, got[0], took, first_s)
    print("\n".join(verdict))
    return 0 if got[0] > 200_000 else 1


def cmd_doctor(_args) -> int:
    ok = True
    print(f"Python {sys.version.split()[0]}", "OK" if sys.version_info >= (3, 11) else "NEEDS 3.11+")
    ok &= sys.version_info >= (3, 11)
    for exe, need, hint in [("ffmpeg", True, "winget install Gyan.FFmpeg"),
                            ("streamlink", True, "winget install Streamlink.Streamlink"),
                            ("yt-dlp", False, "winget install yt-dlp.yt-dlp   (only needed for YouTube channel/@handle links)")]:
        p = shutil.which(exe)
        print(f"{exe:11s}", p or f"MISSING -> {hint}")
        if p and exe == "streamlink":
            v = subprocess.run([p, "--version"], capture_output=True, text=True, errors="replace").stdout.strip()
            print(f"{'':11s}", v or "(version unknown)", "  <- if recording fails, update: winget upgrade Streamlink.Streamlink")
        ok &= bool(p) or not need
    has = importlib.util.find_spec("chat_downloader") is not None
    print(f"{'chat-downloader':11s}", "OK (backup YouTube chat reader)" if has else "not installed (optional backup for YouTube chat): pip install chat-downloader")
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        hw = [e for e in ("h264_nvenc", "h264_qsv", "h264_amf") if e in enc]
        print("hardware encoders:", ", ".join(hw) or "none found (CPU encoding is fine)")
        filt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        print("captions (libass):", "OK" if " ass " in filt else "MISSING - install the full FFmpeg build (winget Gyan.FFmpeg has it)")
    print("faster-whisper:", "OK" if importlib.util.find_spec("faster_whisper") else "not installed (only for captions): pip install faster-whisper")
    from . import accounts
    have = [p for p in PLATFORM_CHOICES if accounts.connected(p)]
    print("auto-posting accounts:", ", ".join(have) if have else "none connected (optional): python -m autoclip connect")
    from . import autopilot, taste
    c, a = taste.counts(), autopilot.load()
    print(f"autopilot: {a['mode']} | learned from {c['total']} decisions ({c['keeps']} kept, {c['trashes']} trashed)")
    print("\nAll required tools found." if ok else "\nFix the MISSING items above, then run doctor again.")
    return 0 if ok else 1


def _load_cfg(args):
    cfg = cfgmod.load(args.config)
    settings.apply(cfg)                      # folders chosen in the review page
    if getattr(args, "out", None):
        cfg.out_dir = args.out               # command line wins
    return cfg


def _log_to_file(folder: Path) -> Path:
    """Keep a log next to the clips so any problem can be sent for diagnosis."""
    from logging.handlers import RotatingFileHandler
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "autoclip.log"
    h = RotatingFileHandler(f, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    return f


def plan_targets(targets: list[str]) -> list[str]:
    """Check every target BEFORE anything starts, and watch each stream once: the same channel listed twice would run two
    recorders in one buffer folder and clip every moment twice. Raises ValueError (with the offending text) for rubbish."""
    seen: dict[str, str] = {}
    for t in targets:
        _, label, _ = parse_target(t)
        if label in seen:
            print(f"NOTE: {t!r} is the same stream as {seen[label]!r}: watching it once.")
            continue
        seen[label] = t
    return list(seen.values())


def resource_notes(cfg, n: int) -> list[str]:
    """What N streams at once will ask of this PC (rough numbers, so nobody finds out when the disk is full)."""
    if n < 2:
        return []
    base = buffer_base(cfg)
    need = n * 0.7 * cfg.buffer_s / 600                      # ~0.7 GB per 10 min of 1080p60
    notes = [f"{n} streams at once: about {need:.1f} GB of rolling buffer in {base} and ~{n * 8} Mbit/s download at best quality. "
             f"All 9:16 renders share {cfg.cut_workers} slot(s), best clip first; draft cuts are never held up by them."]
    try:
        free = shutil.disk_usage(base).free / 1e9
        if free < need * 1.5:
            notes.append(f"WARNING: only {free:.1f} GB free there. Set buffer_dir in config.toml to a bigger drive, or watch fewer streams.")
    except OSError:
        pass
    return notes


def buffer_base(cfg) -> Path:
    return Path(cfg.buffer_dir) if cfg.buffer_dir else Path(tempfile.gettempdir())


def disk_line(cfg) -> str:
    """Disk state for the status block, with a plain warning when clips are (about to be) no longer cut."""
    out = Path(cfg.out_dir)
    used, free = clips_gb(out), free_gb(out)
    line = f"disk: clips {used:.1f}/{cfg.max_clips_gb:g} GB" + (f", {free:.0f} GB free" if free is not None else "")
    if used >= cfg.max_clips_gb:
        line += "  !! FULL: NOT CUTTING. Delete trashed clips in the review page or raise max_clips_gb"
    elif free is not None and free < cfg.min_free_gb:
        line += f"  !! LOW DISK (under {cfg.min_free_gb:g} GB): NOT CUTTING. Free up space"
    elif used >= 0.8 * cfg.max_clips_gb:
        line += "  (almost full)"
    bfree = free_gb(buffer_base(cfg))
    if bfree is not None and bfree < 1.0:
        line += f"  !! only {bfree:.1f} GB free for the video buffer"
    return line


def status_block(workers: list[ChannelWorker], lanes: Lanes, cfg) -> str:
    head = f"status: {sum(1 for w in workers if w.is_alive())}/{len(workers)} streams running"
    if cfg.vertical == "auto":
        head += f" | 9:16 renders: {lanes.encodes.running} running, {lanes.encodes.waiting} waiting"
    return "\n".join([head] + ["  " + w.status_line() for w in workers] + ["  " + disk_line(cfg)])


def cmd_run(args) -> int:
    cfg = _load_cfg(args)
    try:
        targets = plan_targets(args.targets)
    except ValueError as e:
        print(f"ERROR: {e}\nUse a Twitch name or link, or a YouTube live link.")
        return 2
    logfile = _log_to_file(Path(cfg.out_dir))
    src = "config.toml" if (args.config or Path("config.toml").is_file()) else "built-in defaults (no config.toml found)"
    print(f"Settings: {src} | sensitivity: mult {cfg.mult}{' (eases to 1.8 on big chats)' if cfg.auto_mult else ''}, "
          f"min chatters {cfg.min_users}, audio assist {'on' if cfg.audio_assist else 'off'}")
    if cfg.min_users >= 30:
        print(f"NOTE: min chatters is {cfg.min_users:g}. Chat quieter than that can never trigger a clip, "
              f"so spikes on a calm stretch are skipped. 6-15 is typical; set min_users in config.toml.")
    print(f"Clips: {Path(cfg.out_dir).resolve()}" + (f"  | finished clips also copied to: {cfg.export_dir}" if cfg.export_dir else "")
          + f"\nLog: {logfile}")
    for note in resource_notes(cfg, len(targets)):
        print(note)
    if cfg.vertical == "auto":
        pick_encoder(cfg)                         # find the hardware encoder once now, not during the first clip
    stale = sweep_stale_buffers(buffer_base(cfg))  # leftovers of a run that was killed (console window closed, crash, power cut)
    if stale:
        print(f"Removed {stale} leftover video buffer folder(s) from an earlier run that did not shut down cleanly.")
    awake = KeepAwake(cfg.keep_awake)
    if awake.start():
        print("Keeping this PC awake while clipping (closing the lid or choosing Sleep still stops it).")
    stop = threading.Event()
    lanes = Lanes(cfg)                            # shared by all streams: bounded encodes, draft cuts that never wait
    workers = [ChannelWorker(cfg, t, Path(cfg.out_dir), stop, lanes=lanes) for t in targets]
    for w in workers:
        w.start()
        time.sleep(1)                             # stagger the connections (chat servers and streamlink dislike a burst)
    print(f"Clipping {len(workers)} stream(s).  Review later with:  python -m autoclip review\nCtrl+C to stop.")
    start_autopilot(cfg, stop)
    next_status = time.time() + max(cfg.status_every_s, 1)
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(0.5)
            if cfg.status_every_s > 0 and time.time() >= next_status:
                next_status = time.time() + cfg.status_every_s
                log.info("%s", status_block(workers, lanes, cfg))
    except KeyboardInterrupt:
        print("\nStopping (finishing clips in progress)...")
        stop.set()
    finally:
        awake.stop()
    for w in workers:
        w.join(timeout=120)
    # On Ctrl+C the renders still waiting in line are skipped (the drafts exist; Export in the review page makes them);
    # the ones already running are finished. Otherwise stopping could take minutes.
    dropped = lanes.shutdown(cancel_queued_encodes=stop.is_set())
    if dropped:
        print(f"{dropped} queued 9:16 render(s) were skipped: those clips are still there as drafts, press Export in the review page.")
    return 0


def start_autopilot(cfg, stop: threading.Event):
    """The autopilot thread lives inside `run`, so a clip can be posted while you are away. It idles when the mode is off, and picks up a
    change made in the review page within 30 s."""
    from . import autopilot
    from .review import Store
    mode = autopilot.load()["mode"]
    autopilot.Autopilot(Store(Path(cfg.out_dir), cfg), stop).start()
    print(f"Autopilot: {mode}" + {"off": " (it only learns from your Keep / Trash; switch it on in the review page: 🤖 Autopilot)",
                                   "suggest": " (it marks the clips it would post; nothing is posted)",
                                   "post": " (it posts the clips it is sure about, within the limits you set)"}[mode])


def autopilot_report(st: dict) -> list[str]:
    """Plain-language status of the learning and the autopilot (pure: unit-tested)."""
    c, s, r = st["counts"], st["settings"], st["ready"]
    out = [f"Learned from {c['total']} decisions: {c['keeps']} kept, {c['trashes']} trashed."]
    if st["model"] and st["model"].get("auc") is not None:
        out.append(f"On clips it had not seen while learning, it ranks a kept clip above a trashed one {st['model']['auc']:.0%} of the time (50% = guessing).")
    for x in st["curve"]:
        if x["picked"]:
            out.append(f"  sure >= {x['at']:.0%}: would pick {x['picked']} of your clips, you agreed with {x['agreed']} ({x['precision']:.0%})")
    out.append(f"Ready to post: yes, from {r['threshold']:.0%} sure (it was right {r['hits']}/{r['support']} on past clips)" if r["ok"]
               else f"Ready to post: no. {r['reason']}")
    out.append(f"Mode: {s['mode']}" + (f"  (PAUSED: {s['paused_reason']})" if s["paused_reason"] else ""))
    if s["mode"] == "post" or s["platforms"] or s["channels"]:
        out.append(f"  platforms: {', '.join(s['platforms']) or 'none ticked'} via {s['provider']} | streamers: {', '.join(s['channels']) or 'none ticked'}")
        out.append(f"  limits: {s['max_per_day']} per 24 h, {s['gap_min']} min apart, {s['delay_min']} min veto time | posted in the last 24 h: {st['posted_24h']}")
    for q in st["queue"]:
        out.append(f"  waiting: {q['channel']} {q['id']} ({q['p']:.0%} sure)")
    return out


def cmd_autopilot(args) -> int:
    from . import autopilot, taste
    from .review import Store
    cfg = _load_cfg(args)
    store = Store(Path(cfg.out_dir), cfg)
    n = taste.import_existing(store.root)
    if n:
        print(f"Took over {n} earlier Keep / Trash decision(s) from your clip files.")
    if args.action == "train" or (taste.is_stale() and args.action != "run"):
        m = taste.train()
        print(f"Learned from {m['n']} decisions." if m["state"] != "none" else m["reason"])
    if args.action in ("status", "train"):
        print("\n".join(autopilot_report(autopilot.status(store))))
        return 0
    ap = autopilot.Autopilot(store)
    if args.action == "once":
        acts = ap.tick()
        print("\n".join(f"{a['action']}: {a.get('clip', '')} {a.get('message', '')}".strip() for a in acts) or "Nothing to do right now.")
        return 0
    print(f"Autopilot ({autopilot.load()['mode']}) is running on {store.root}. Change its settings in the review page. Ctrl+C to stop.")
    try:
        ap.loop(30.0)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_review(args) -> int:
    from .review import serve
    cfg = _load_cfg(args)
    serve(Path(cfg.out_dir), cfg, args.port, not args.no_browser)
    return 0


PLATFORM_CHOICES = ("youtube", "instagram", "tiktok")


def cmd_connect(args) -> int:
    """Open the review page straight on the Accounts window (guided setup for automatic posting)."""
    from .review import serve
    cfg = _load_cfg(args)
    print("Opening Connect accounts" +(f" for {args.platform}" if args.platform else "") +
          ". Follow the numbered steps there; press Ctrl+C here when you are done.")
    serve(Path(cfg.out_dir), cfg, args.port, not args.no_browser, open_path="#connect=" + (args.platform or ""))
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


START_HERE = """autoclip: start here

  1. Check your PC:      python -m autoclip doctor
  2. Watch a stream:     python -m autoclip run <channel>
  3. Review your clips:  python -m autoclip review

Posting, layouts and Autopilot are all on the review page.
Something not working?  python -m autoclip check <channel>
All commands:           python -m autoclip --help"""


def main(argv=None) -> int:
    _setup_logging()
    ap = argparse.ArgumentParser(prog="autoclip", description="Auto-clip live streams when chat spikes.")
    ap.add_argument("--config", help="TOML settings file (see config.example.toml)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="check that required tools are installed")
    r = sub.add_parser("run", help="watch one or more live streams and clip them")
    r.add_argument("targets", nargs="+", help="twitch name / twitch.tv link / youtube live link")
    r.add_argument("--out")
    v = sub.add_parser("review", help="open the review page")
    v.add_argument("--out")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-browser", action="store_true")
    k = sub.add_parser("check", help="test one channel: can streamlink get video from it on this PC right now?")
    k.add_argument("target", help="twitch name / twitch.tv link / youtube live link")
    k.add_argument("--seconds", type=float, default=20)
    k.add_argument("--debug", action="store_true", help="show streamlink's detailed log (each web request), to see where it gets stuck")
    k.add_argument("--out")
    c = sub.add_parser("connect", help="guided setup for automatic posting to YouTube / Instagram / TikTok")
    c.add_argument("platform", nargs="?", choices=PLATFORM_CHOICES, help="open the guide for this platform (default: all)")
    c.add_argument("--out")
    c.add_argument("--port", type=int, default=8765)
    c.add_argument("--no-browser", action="store_true")
    a = sub.add_parser("autopilot", help="what it learned from your Keep / Trash, and run the auto-poster without the clipper")
    a.add_argument("action", nargs="?", default="status", choices=["status", "train", "run", "once"],
                   help="status (default), train now, run (keep posting), once (one pass)")
    a.add_argument("--out")
    d = sub.add_parser("demo", help="offline self-test with fake video and chat")
    d.add_argument("--out")
    args = ap.parse_args(argv)
    if not args.cmd:
        if args.config is not None:
            ap.error("--config needs a command, for example: python -m autoclip --config my.toml run <channel>")
        print(START_HERE)
        return 0
    return {"doctor": cmd_doctor, "run": cmd_run, "review": cmd_review, "connect": cmd_connect, "check": cmd_check, "autopilot": cmd_autopilot, "demo": cmd_demo}[args.cmd](args)
