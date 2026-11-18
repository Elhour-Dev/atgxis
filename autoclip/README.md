# autoclip

Watches **live** Twitch / YouTube streams, notices when chat explodes, and automatically cuts a clip
around each moment. You review the clips afterwards in a local web page. Free, runs on your PC, no accounts or API keys.

## Install (Windows, ~5 minutes)

1. Install Python 3.11+ from python.org (tick **Add python.exe to PATH**).
2. In PowerShell:
   ```powershell
   winget install Gyan.FFmpeg
   winget install Streamlink.Streamlink
   winget install yt-dlp.yt-dlp          # only needed for YouTube channel / @handle links
   pip install chat-downloader           # only needed for YouTube
   ```
   Close and reopen PowerShell so the new tools are on PATH.
3. In the project folder:
   ```powershell
   python -m autoclip doctor     # lists anything missing
   python -m autoclip demo       # 1-minute offline self-test with fake video + chat
   ```
   If `demo` says PASSED, ffmpeg, cutting, detection and the review page all work on your machine.

## Use

```powershell
python -m autoclip run shroud                               # Twitch channel (name or link)
python -m autoclip run shroud xqc https://www.youtube.com/watch?v=VIDEOID   # several at once
python -m autoclip review                                   # later: opens the review page
```
* The first **90 s** of each run is spent learning the channel's normal chat level; nothing is clipped yet.
* Leave it running during the stream. **Ctrl+C** stops it cleanly (clips in progress are finished).
* Clips land in `clips\<channel>\<date_time>\` as **drafts**: ~40 s of context around the moment, made by stream copy in
  under a second (no CPU use, no quality loss). Nothing is encoded until you ask.
* In the review page (ranked best-first) each draft opens already trimmed to a **suggested 30 s window**. Fine-tune with the
  *start = player* / *end = player* buttons (the player loops your trim), tick **Vertical 9:16** and/or **Captions**, and press
  **Export clip**: ONE encode does the exact trim + layout + captions. Then **Keep / Trash**; "Delete trashed files" frees disk.

## How it avoids the usual problems

| Problem | What it does |
|---|---|
| One person, a bot, or `!command` spam fakes a spike | Counts **different chatters**, not messages. Ignores known bots and `!commands`. |
| Raids | A raid notice mutes spike detection for 90 s and is excluded from the baseline. |
| Quiet chats fire constantly | Needs a minimum number of chatters **and** `baseline + 3*sqrt(baseline)` (statistical noise guard). |
| Huge chats never "spike" | Threshold is relative to that channel's own median, not a fixed number. |
| Chat reacts after the moment | Clip starts 30 s early (`pre_s`). |
| One long event = 5 duplicate clips | Spikes within 30 s merge into one clip; a very long spike is split at 60 s. |
| Video and chat drift apart (ads, drops, reconnects) | Every video segment and chat message is stamped with **wall-clock** time, never stream-relative time. |
| Stream drops / goes offline | Recorder reconnects with back-off and polls politely while offline; chat readers reconnect forever. |
| Disk fills up | Rolling 10-minute buffer is auto-pruned (stream copy, no re-encode); stops cutting past `max_clips_gb`. |
| CPU load on a gaming PC | Buffer and drafts are **never re-encoded** (a 48 s draft takes 0.2 s vs 16 s to encode: 70x faster). Only clips you export are encoded, one pass, bounded across all streams. Optional GPU encoder. |
| Clips start too early / too late | Finds the second chat actually took off (not the window start: ~9 s tighter), then places a fixed 30 s window around it (lead-in + the chat peak always inside); you fine-tune in the player. |
| Same moment clipped twice | A new clip overlapping an existing one by 60%+ is skipped. |
| Windows quirks | No shell tricks, list-style subprocess args, paths with spaces/apostrophes tested, UTF-8 console so emotes cannot crash it. |
| Orphaned ffmpeg processes | Shutdown is verified by tests: no leftover processes, buffer folder removed. |
| Review page safety | Listens on `127.0.0.1` only, serves only `.mp4` inside the clips folder, rejects other websites' requests. |

Loudness bonus: a clip scores up to +10 if the audio is louder after the moment than before it
(streamer/game reacting), which filters chat-only false alarms.

## Vertical clips: webcam on top, gameplay below (Reels / TikTok / Shorts)
Every clip is exported **automatically as a ready 1080x1920, 30 fps, exactly 30 s video**. Until you teach it where a streamer's
webcam is, it uses a blurred full frame. Do this **once per streamer** (takes ~20 seconds):
1. `python -m autoclip review`, then press **Set layout for <channel>** on any of that channel's clips.
2. Drag the **red box** over the webcam and the **blue box** over the part of the game you want shown
   (boxes keep the right shape automatically; drag the corner square to resize). Optional: **Auto-detect webcam**
   (`pip install "opencv-python-headless>=4.8,<5"`).
3. Press **Preview** to see the finished vertical frame, then **Save layout**. All future clips for that channel use it.
   Press **Export clip** on older clips to re-render them with the new layout.
Other options: whole screen on a blurred background, plain center crop, adjustable webcam/gameplay split (40-60%).

**Subtitle position:** in the same editor, drag the **green box** on the vertical preview to where subtitles should appear
(its height sets the text size; the sample text inside shows how it will look and wrap). Saved per streamer along with the layout.
Without a saved box, captions sit on the line between webcam and gameplay.

## Post text (title + description) for every clip
Open **Post text** on any clip: you get a suggested title (press ↻ for other options), plus a description with a credit line to
the streamer and hashtags. Edit freely (it autosaves) and press **Copy title + description**. These are rule-based suggestions
built from what chat was spamming (KEKW = funny, WTF = shock, POG = hype), not AI-written copy: treat them as a starting point.
Set nicer names in `config.toml`:  `display_names = { kaicenat = "Kai Cenat" }`,  `hashtags = ["fyp", "gaming"]`,  `credit_line = false`.

**Speed:** the encoder is chosen automatically (NVIDIA / Intel / AMD hardware encoder if it really works, else CPU), runs at
below-normal priority so your game/stream never stutters, and loudness is normalized for social platforms.

## Captions (optional, one-time setup)
`pip install faster-whisper`, then tick **Captions** when exporting. First use downloads the speech model (~500 MB for `small`);
a GPU is used automatically if available. Style: big bold uppercase, 1-3 words at a time, current word highlighted in yellow.
If nobody speaks in the clip you get a clear message instead of an empty caption track. Set `whisper_model = "base"` for speed
or `"medium"` for accuracy.

## Tuning
See `config.example.toml`. Too many clips -> raise `mult` and `min_users`. Missing moments -> lower them.
Clips start too late/early -> change `pre_s`. Always check the first few streams and adjust.

## Limits (be aware)
* Chat-based: moments nobody reacts to in chat are missed; clip *quality* still needs your review.
* **YouTube chat uses an unofficial feed** (`chat-downloader`) and may break when YouTube changes it.
* Twitch ads can briefly interrupt the video; only streamlink's behaviour applies. If the channel requires login/subscription, it can't be recorded.
* Needs a live stream (no VODs) — for VODs use the chat-replay download instead.

## Rules for other people's streams
Recording for private review is fine; **publishing needs the streamer's permission** (Twitch/YouTube terms, and copyrighted
music in streams can get your upload claimed). Credit the streamer, and check whether they allow clipping before you post.
