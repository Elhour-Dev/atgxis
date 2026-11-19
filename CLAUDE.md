# atgxis / autoclip

A free desktop program (Python 3.11+, mainly used on Windows) that watches **live** Twitch / YouTube streams, notices when chat
explodes, and automatically cuts a clip around each moment. The user reviews the clips on a local web page, trims and exports them
as vertical 9:16 videos with captions, and can post them to TikTok / YouTube Shorts / Instagram Reels. Everything runs on the
user's own PC; there is no server.

Repo: `Elhour-Dev/atgxis`. The code lives in `autoclip/`. The full user documentation is `autoclip/README.md`; read it before changing
behaviour, because it describes the design decisions and the measured numbers behind them.

## Run it

```powershell
python -m autoclip doctor      # lists missing tools (ffmpeg, streamlink, yt-dlp, chat-downloader)
python -m autoclip demo        # offline self-test with fake video + chat; must print PASSED
python -m autoclip run <channel> [<channel> ...]   # watch streams (Twitch name/link or YouTube link)
python -m autoclip review      # open the review page (127.0.0.1 only)
python -m autoclip check <channel>   # can streamlink get video from this channel right now?
python -m autoclip connect [youtube|instagram|tiktok]   # guided setup for automatic posting
python -m autoclip autopilot [status|run]                # auto-poster that learns from Keep/Trash
```

External tools needed: `ffmpeg`, `streamlink`; `yt-dlp` and `chat-downloader` only for YouTube. Python dependencies are in
`requirements.txt`; the core uses only the standard library, so keep new core code stdlib-only.

## Tests

```
pip install pytest
python -m pytest -q            # needs ffmpeg on PATH; the full suite is slow (it cuts real video)
python -m pytest tests/test_detector.py -q   # fast, pure logic
```

`tests/clipfactory.py` builds fake video and chat for the tests. Run the relevant test file after every change, and the whole
suite before you commit. (The full suite was not completed in the cloud session that wrote this file, because it was slow, so
check its result yourself.)

## Layout of `autoclip/`

| Module | Role |
|---|---|
| `cli.py`, `__main__.py` | Commands (`run`, `review`, `doctor`, `check`, `connect`, `autopilot`, `demo`) |
| `config.py`, `config.example.toml` | All settings with safe defaults. A `config.toml` in the project folder is loaded automatically |
| `settings.py` | Per-user settings outside the repo (`%APPDATA%\autoclip\`), so `git pull` never overwrites them |
| `worker.py` | One `ChannelWorker` per stream: recorder + chat reader + detector + cutting |
| `lanes.py` | Work shared by all streams at once (draft cuts never wait behind renders; render slots are shared) |
| `recorder.py` | streamlink -> ffmpeg stream copy into a rolling buffer of short `.ts` segments |
| `chat.py` | Twitch IRC and YouTube chat readers; reconnect forever |
| `detector.py` | Chat-spike detector: pure logic, driven by timestamps, no I/O |
| `audio.py` | Loudness of each segment (a second "something happened" signal) |
| `cutter.py` | Draft cuts (stream copy, no re-encode) and the one-pass export (trim + layout + captions) |
| `layout.py`, `facecam.py` | 9:16 layouts stored as fractions of the frame; optional webcam auto-detect (OpenCV) |
| `captions.py`, `describe.py` | Burned-in captions; title / description suggestions |
| `review.py`, `autopilot_page.py` | The local review web page (one big Python string, so no backslashes in `review.PAGE`) |
| `share.py`, `publish.py`, `accounts.py`, `zernio.py` | Sharing and posting: share assistant, official APIs, OAuth logins, Zernio "Quick connect" |
| `taste.py`, `autopilot.py` | Learns Keep/Trash and posts the clips it is sure about, with safety rules |
| `disk.py`, `power.py` | Disk guards and stale-buffer cleanup; keep-awake for long unattended runs |

`docs/` is the GitHub Pages site (privacy policy, terms of service, TikTok app review files). The TikTok verification `.txt` file
and `docs/tiktok/` must stay in place.

## Rules that matter

- **Timestamps are wall-clock.** Every video segment and chat message is stamped with real time, never stream-relative time, so
  video and chat stay aligned across ads, drops and reconnects. Do not change this.
- **Never re-encode the buffer or the drafts.** Only exported clips (and the optional automatic 9:16 renders) are encoded, in one
  pass.
- **Count different chatters, not messages**, and judge each channel against its own baseline.
- **The review page listens on `127.0.0.1` only**, serves only `.mp4` files inside the clips folder, and rejects requests from
  other websites. Keep it that way.
- **Posting is opt-in and cautious.** Autopilot starts off; it has a waiting time, a daily cap and a spacing limit; it posts only
  to channels and platforms the user ticked. These limits are enforced in code, not only in the page. Do not weaken them.
- **Secrets stay on the user's PC** (`%APPDATA%\autoclip\accounts.json`). Never commit tokens, API keys or `config.toml`, and never
  log them.
- **Windows first:** use list-style subprocess arguments (no shell), test paths with spaces and apostrophes, and keep console output
  safe for UTF-8 emotes.
- `clips/`, `__pycache__/` and `.pytest_cache/` are git-ignored.

## Recent work (newest first)

- Autopilot: learns from Keep/Trash and posts the clips it would keep (`taste.py`, `autopilot.py`).
- Zernio "Quick connect": post to Instagram + TikTok with one API key; fixes for typed titles and the Zernio `profileId` error.
- Export stays busy until the clip exists; no invented subtitles on non-speech; caption language setting.
- Recording hardening: ad-break patience, the `check` command, a clip is kept when the buffer holds at least `min_clip_s`.
- Chat resilience: fast reconnects, dead-link detection, port fallback (6697 / 443); warm-up counts only chat actually heard.
- Unattended-run hardening: disk guards, stale-buffer sweep, keep-awake.

## Working on this project

- Develop on a feature branch and open a pull request; do not push straight to `main`. The cloud sessions used the branch
  `ccr-f4c77297-ufdo88`. Create a pull request only when the user asks.
- Update `autoclip/README.md` when behaviour the user can see changes. It is the user's manual.
- Commit messages in this repo are short and describe the user-visible change.
