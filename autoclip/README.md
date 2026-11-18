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
* Clips land in `clips\<channel>\<date_time>\`. The review page ranks them best-first; **Keep / Trash / Make vertical 9:16**,
  and "Delete trashed files" frees disk. Nothing is deleted until you press that.

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
| CPU load on a gaming PC | The buffer is **never re-encoded**; only the ~40 s clip is encoded, 2 at a time. Optional GPU encoder. Vertical copies only on demand. |
| Windows quirks | No shell tricks, list-style subprocess args, paths with spaces/apostrophes tested, UTF-8 console so emotes cannot crash it. |
| Orphaned ffmpeg processes | Shutdown is verified by tests: no leftover processes, buffer folder removed. |
| Review page safety | Listens on `127.0.0.1` only, serves only `.mp4` inside the clips folder, rejects other websites' requests. |

Loudness bonus: a clip scores up to +10 if the audio is louder after the moment than before it
(streamer/game reacting), which filters chat-only false alarms.

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
