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
   pip install chat-downloader           # optional: a backup YouTube chat reader (YouTube chat is read without it)
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
* The first **90 s** of each run is spent learning the channel's normal chat level; nothing is clipped yet. These are 90 seconds of chat
  *actually heard*: seconds while the chat connection was down (timeouts) or during a raid do not count, so the warm-up simply takes longer.
  When it is done the log says, per channel, e.g. `READY: normal chat = 14 different chatters per 10s (learned from 90s of connected chat,
  1240 messages heard). A spike needs 28 chatters.` **How to check it did its job:** open that channel's chat; 14 is roughly how many different
  people write in 10 s on a calm stretch. A tiny number for a big channel, or very few messages heard, means chat was missing while it learned
  (the normal level also keeps correcting itself: it is the median of the last 5 minutes of calm chat).
* Leave it running during the stream. **Ctrl+C** stops it cleanly: a moment that was just found is still cut (it is not lost), renders
  already running are finished, and renders still waiting in line are skipped (their draft exists; press **Export** in the review page).
* Clips land in `clips\<channel>\<date_time>\` as **drafts**: ~40 s of context around the moment, made by stream copy in
  under a second (no CPU use, no quality loss). Nothing is encoded until you ask.
* The review page has tabs at the top: **To review · Kept · Trash · Done · All**, each with its count, plus the order (best score
  first by default). **📐 Layout** sets where the webcam and the game go in 9:16 clips (per streamer). Folders and posting accounts
  are under **⚙ Setup** (a dot on it means no posting account is connected yet). The top bar shrinks when you scroll down.
* The page never redraws under your hands. While you are working (typing, playing a video, or you clicked or scrolled in the last
  30 s) new clips and changes wait behind a green **"3 new clips · Show"** button; when you leave the page alone they appear by
  themselves. Your own actions (Keep, Trash, a finished export) show at once.
* Each clip card reads top to bottom: score, title, streamer and age; the video; a **trim bar** under it; what chat was saying; then
  **Keep / Trash** (always in the same place); then three steps: **① Export → ② Post text → ③ Share**. A step turns green with ✓ when
  it is done, and Share stays locked until the clip is exported.
* Each draft opens already trimmed to a **suggested 30 s window**. Drag the two handles on the trim bar (the yellow line is the moment
  chat exploded; click the bar to jump the player there), or use **Start here / End here** at the player position; **Fine-tune**
  has exact seconds, *◀ 5 s / 5 s ▶* and *30 s*. The player loops your trim. In **① Export** tick **Vertical 9:16** and/or
  **Captions** and press **Export clip**: ONE encode does the exact trim + layout + captions. Then **Keep / Trash**;
  "Delete trashed files" (on the Trash tab) frees disk.
* **Keyboard:** click a card (or use **← →**, or **J / L**) to pick it, then **K** keep, **T** trash, **U** undo, **Space** play/pause.
  After Keep or Trash the next clip is picked for you, so you can review a whole list without the mouse.
  Posted a clip already? Press **Done (free space)** instead of Trash: it deletes that clip's draft, exports and caption data (not the
  copy in your final clips folder), keeps it listed under "Done", and counts it as a Keep.
  Keep / Trash also teach the [🤖 Autopilot](#-autopilot-it-learns-what-you-keep-and-posts-the-good-ones-for-you), which can learn your taste and post the good clips for you.

## Several streams at once
`python -m autoclip run shroud xqc https://www.youtube.com/watch?v=VIDEOID` watches every stream at the same time (Twitch and YouTube can be
mixed). Each stream gets its own recorder, chat reader and baseline, so a quiet channel and a huge one are judged separately. What is
**shared** is the expensive part, so cost does not multiply with the number of streams:
* **Draft cuts never wait.** A draft is a stream copy (milliseconds), done in its own lane. Before, drafts and 9:16 renders shared one
  queue: when 4 streams spiked together, two drafts sat behind two long renders until their video had been deleted from the buffer, and
  **2 of 4 clips were lost** (measured). Now all 4 are saved at once.
* **9:16 renders share `cut_workers` slots (default 2) across ALL streams, best clip first.** If `max_encode_backlog` (6) renders are already
  waiting, new clips simply stay drafts (logged); press **Export** for them in the review page.
* **Cheaper renders:** frames are dropped to 30 fps *before* scaling/blurring instead of after (bit-identical picture, ~1.7x faster on a
  60 fps stream), and the blurred background is computed at quarter size (~2.8x faster again; visually the same). Measured on 15 s of
  1080p60 (4-core box, real defaults): the blurred layout went from **50.8 s to 10.4 s**. The webcam/gameplay *split* layout has no big blur,
  so it only gains ~7% (10.2 s to 9.5 s).
* Every `status_every_s` seconds (60) you get one overview: which streams are live, chat rate vs normal, spikes/clips, render queue.
* The same channel listed twice is watched once; a typo in any target is reported before anything starts.
* At start with 2+ streams it prints the disk (rolling buffer, ~0.7 GB per stream per 10 min) and bandwidth (~8 Mbit/s per stream at
  `best`) you are asking for, and warns when the buffer drive is too small. Put `buffer_dir` on a big drive.
* Idle cost is small: about 2% of one core per stream (almost all of it measuring audio loudness of each 4 s segment).
Rule of thumb: the limits are your download speed and disk, not the program. Add `quality = "720p60,720p,best"` to `config.toml` to halve both
(clips will be softer).

## Leaving it running all day (school / work)
* **Start it, check the first minutes, then leave it.** After about 90 s the status block shows each stream's chat level; spikes and clips
  appear in it with the time of the last clip, plus the disk state. Everything is also in `autoclip.log` in the clips folder.
* **The PC must stay awake.** On Windows the program asks Windows not to sleep from idleness (`keep_awake = true`). It cannot stop sleep that
  you cause: closing the laptop lid or choosing Sleep stops all recording. Set Power options > "When plugged in, put the computer to sleep" to
  *Never* for the day, and keep a laptop plugged in.
* **Stop it with Ctrl+C**, not by closing the window. Closing the window kills it instantly; the video buffer of that run (about 0.7 GB per
  stream) is left in your temp folder. The next start deletes such leftovers automatically (only `autoclip_buf_*` folders nothing has written
  to for 15 minutes).
* **Disk.** A busy day can make hundreds of clips (a 1080p60 clip is about 40 MB as a draft plus 25 MB as 9:16). The program stops cutting, and
  says so in the status block and the log, when the clips folder passes `max_clips_gb` (20) or the drive has less than `min_free_gb` (3) free.
  Nothing is deleted for you. Raise the limit, or Trash clips in the review page and press "Delete trashed files".
* **Chat drops are handled.** If the Twitch chat connection breaks (`twitch chat <name>: timed out (retry in Ns)` in the log), the program
  reconnects by itself: quickly after a connection that had been healthy (the wait only grows while attempts keep failing), alternating between
  Twitch's ports 6697 and 443, and it pings a quiet connection so a dead one is noticed within about 90 s. While chat is down the status
  block says `CHAT RECONNECTING`: the video keeps recording, but spikes cannot be detected, and the channel's "normal" level is not
  learned from that silence. When chat returns the log says `back after N s`. Occasional drops on Wi-Fi are normal; if they are constant, check the connection.
* **`video: waiting` in the status block** means no video is being recorded for that channel. The log line `capture ended without any video
  (streamlink said: ...)` gives the reason. To test one channel directly on your PC, run `python -m autoclip check kaicenat`: it asks
  streamlink for 20 s of video, shows what streamlink says and explains it (channel offline, ad break, network/DNS problem, access refused).
  `python -m autoclip doctor` shows your streamlink version; Twitch changes often, so keep it current (`winget upgrade Streamlink.Streamlink`).
  While Twitch shows an **ad break**, streamlink pauses on purpose; the recorder now waits for it (up to `ad_stall_s` = 240 s) instead of
  restarting the capture, which would only start a new session with a new pre-roll ad.
* **An offline streamer** costs almost nothing: it is checked about once a minute and leaves no files behind. If chat spikes while there is no
  video, that is logged as "no video" (not a failed cut).
* Measured on a synthetic run (2 streams, repeated spikes, 9:16 renders on): memory stayed flat (about 22 MB), threads and open files stayed
  constant, the rolling buffer stayed bounded, no cut failed. The review page lists 1,500 clips in about 75 ms.
* The 9:16 renders use the layout you saved for that streamer when the render *starts*. Clips made before you set a layout use the blurred
  full-frame look; press **Export clip** on them later to re-render with the layout.

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
| CPU load on a gaming PC | Buffer and drafts are **never re-encoded** (a 48 s draft takes 0.2 s vs 16 s to encode: 70x faster). Only clips you export (and the optional automatic 9:16 renders) are encoded, one pass, bounded across all streams. Optional GPU encoder. |
| Clips start too early / too late | Finds the second chat actually took off (not the window start: ~9 s tighter), then places a fixed 30 s window around it (lead-in + the chat peak always inside); you fine-tune in the player. |
| Same moment clipped twice | A new clip overlapping an existing one by 60%+ is skipped. |
| Windows quirks | No shell tricks, list-style subprocess args, paths with spaces/apostrophes tested, UTF-8 console so emotes cannot crash it. |
| Orphaned ffmpeg processes | Shutdown is verified by tests: no leftover processes, buffer folder removed. |
| Review page safety | Listens on `127.0.0.1` only, serves only `.mp4` inside the clips folder, rejects other websites' requests. |

Loudness bonus: a clip scores up to +10 if the audio is louder after the moment than before it
(streamer/game reacting), which filters chat-only false alarms.

## Finding the moments (sensitivity)
* A `config.toml` in the project folder is **loaded automatically** (no `--config` flag). The first line printed at start shows
  which settings are in use; if it says "built-in defaults" your `config.toml` was not found.
* **Chat spike**: unique chatters in 10 s vs the channel's own normal level. The multiplier is 2.0 and eases down to 1.8 on big chats.
* **Loud moments help**: when the stream gets 8+ dB louder than normal (streamer screaming, game explosion), chat only needs to
  be 1.35x normal. Loud audio alone never makes a clip (`audio_assist = false` turns this off).
* **Polls are skipped**: when most of the top chat messages are poll answers (numbers like `2651` or `21+`, yes/no, left/right),
  chat is answering a question, not reacting to a moment, so no clip is made (`skipped: chat was answering a poll` in the log).
  W / L / GG floods still count: those are real reactions. `poll_frac = 0` turns this off.
* **Big channels**: raise `min_users` (for example 20) so tiny chat bumps in quiet stretches do not become clips.
* **Near-misses are logged** (`near-miss: chat 1.7x normal (needed 1.9x)`): if you see many for moments you wanted, lower `mult`.
  A session summary at the end counts spikes, near-misses, clips, skipped duplicates and failed cuts.
* Everything is also written to `autoclip.log` in the clips folder: send that file when something looks wrong.
* Window position: the 30 s window starts 15 s before chat took off. Drag it on the trim bar, or use **◀ 5 s / 5 s ▶** under *Fine-tune*, to slide it.

## Folders
**⚙ Setup → 📁 Folders** in the review page: pick where **finished clips** are copied (any folder, with a Browse dialog) and where drafts live.
Saved in your user settings (`%APPDATA%\autoclip\settings.json`), so `git pull` never overwrites them. **📂** next to an export shows it in
Explorer.

## Sharing to TikTok / YouTube Shorts / Instagram Reels
Export a clip (vertical, trimmed, captions as you like), open **Share**, pick a **method**, tick the platforms, **Share selected...**.
You always get a confirmation first; nothing is opened or uploaded before you press OK. Safety rules: only exported clips can be shared,
one invalid or not-connected platform blocks the whole request (nothing is uploaded), an already-posted platform is not posted again
unless you tick "post again", and each result is recorded.

* **Share assistant** (no setup): opens each platform's upload page, selects the file, copies the caption; you press Post.
* **⚡ Quick connect + "Post automatically"** (free, fastest to set up): the **⚡ Quick connect** button opens a 4-step guide. You create a
  free [Zernio](https://zernio.com) account, connect Instagram + TikTok there, paste one API key here. No developer apps, no approval waits.
  Zernio's free plan covers your first 2 connected accounts with unlimited posts, so keep YouTube on the direct method below. "Post
  automatically" then uses Zernio for Instagram/TikTok and your own account for YouTube. TikTok can post publicly or go to your drafts.
* **Post directly to my connected accounts** (free, official APIs): uploads for you. Set up once with a guided window:
  `python -m autoclip connect` (or `connect youtube` / `instagram` / `tiktok`), or press **🔗 Accounts** in the review page, or press the
  **Connect ...** button that appears in a clip's Share box when a platform is missing. Logins are kept in `%APPDATA%\autoclip\accounts.json` on your PC only.

### Connecting an account (what is easier now, and what cannot be)
Every platform requires you to create a free developer app on your own account; no program can skip that. The window makes each step one click:
* Numbered steps, each with a button that opens **the exact console page**, a **Copy** button for values you must paste into the platform
  (TikTok's redirect address already contains this page's port), and tick boxes that remember how far you got.
* **YouTube: drop the JSON file** Google lets you download for the OAuth client (Download JSON) instead of copying two long strings. A
  "Web application" client, an API key, a service-account key, swapped boxes, stray spaces/quotes/line breaks or a pasted `Bearer ` prefix are
  recognised and fixed or explained in words.
* **Test connection** checks the saved login with the platform (posts nothing) and shows which account is connected (Instagram `@name`,
  TikTok display name). `python -m autoclip doctor` lists which accounts are connected.
* When Google/TikTok refuse a sign-in, the page you land on says what to change (for example "add your account under Audience > Test users"),
  and **Something went wrong?** under each platform lists the usual errors with their fix.
* If your browser blocks the sign-in tab, a link appears so you can open it yourself.

Platform consoles change their wording often; if a step no longer matches what you see, the link still opens the right area and the
Something-went-wrong list still applies. **Instagram caveat (not verified against a live account):** Meta's documentation says resumable Reel
uploads are limited to apps using Facebook Login for Business and some sources say only Business (not Creator) accounts can publish through
the API. If Instagram refuses the upload, use the Share assistant for Instagram.

What each platform really allows (so there are no surprises):
| | what happens | catch |
|---|---|---|
| **YouTube** | uploaded and published as a Short | Google can lock uploads from a brand-new API project to *private* until it audits the project; the app detects this and tells you, with a link to publish it in Studio. Set the consent screen to "In production" or the login expires after 7 days. Roughly 6 uploads/day on the default quota. |
| **Instagram** | published as a Reel | needs a Professional (Creator/Business) account and a free Meta developer app; works on your own account without Meta app review. |
| **TikTok** | **draft** in your TikTok inbox (you open the app and press Post) or a direct post | TikTok only allows *private* direct posts for apps it has not audited, so Draft is the default. TikTok setup is the least predictable of the three; the Share assistant is always there as a fallback. |

These three uploaders were built from the platforms' published API behaviour and tested against a simulated server, not against your live
accounts. If one of them reports an error, the message says why; use the Share assistant for that platform and tell me the message.

## 🤖 Autopilot: it learns what you keep and posts the good ones for you
Every **Keep** and **Trash** you press in the review page teaches it what you like. Once it has proved it can tell your good clips from
your bad ones, it can post the clips it is sure about **without you pressing anything**. It starts **off**.

**What it learns from.** Keep (a good clip), Trash (a bad one), and clips you post yourself (counted as a Keep, but a Trash always wins).
*Undo* withdraws a decision. It never learns from its own posts (it would only agree with itself), but it *does* learn when you judge them.
Decisions are kept in your settings folder (`%APPDATA%\autoclip\taste\`), not with the clips, so **Delete trashed files does not make it
forget**. Keeps and Trashes you made before this feature existed are taken over automatically the first time.
**Done (free space)** on a clip you posted counts as a Keep. A Trash on a clip you posted yourself also counts as a Keep (posting it
already said it was good; before the Done button, Trash was the only way to free the space). For the Autopilot's own posts, only your
Trash counts against it: Done there is neither good nor bad and teaches nothing. If Done answers "failed", a video is still playing in
the page or an export is running: pause it or wait, then press Done again (nothing was marked).

**What it looks at.** Only what was measured about the clip: how big the chat spike was against normal, how loud the stream got, the
streamer, which reaction it was, and which words chat was spamming (e.g. it can learn "I keep KEKW clips from X and trash the rest"). It
**cannot see the video**: if you choose by what happens on screen, it can only follow that as far as chat reacts to it.

**Using it.** Press **🤖 Autopilot** in the review page.
1. Keep reviewing. Each card shows `🤖 87%` (hover for *why*: streamer, chat words, loudness). **Autopilot pick first** sorts by it.
2. The window shows what it learned and a table of what each confidence level *would have done on clips you already judged* (clips it did not
   learn from), so you see the real hit rate before trusting it.
3. Pick **Suggest only** first: it marks the clips it would post, posts nothing, and you can check its taste against yours.
4. Then **Post automatically**: tick the platforms, the **streamers** (only ones whose clips you are allowed to post: publishing someone
   else's stream needs their permission), and press Save. It posts while `python -m autoclip run` is running, or on its own with
   `python -m autoclip autopilot run`. `python -m autoclip autopilot status` explains where it stands in plain words.

**When it is allowed to post.** All of these, checked on predictions for clips the model had never seen while learning
(cross-validation):
* at least 30 decisions, 8 or more of each kind; it must rank your kept clips above your trashed ones clearly better than chance;
* at some confidence level it must have been right at least **85%** of the time (you can change this) on **at least 20** such clips, judged
  by the *lower* confidence bound (a lucky streak is not proof) **and** its own average predicted chance must agree;
* the clip must be at least that confident. If no level passes, it says why and does nothing.

These numbers come from simulations I ran with made-up users (not real people), so treat them as a guide: a user with a clear taste
unlocked after roughly 300-600 decisions and was right 81-91% of the time on new clips; a user whose choices followed no pattern was **never**
unlocked (0 of 120 runs). The first version of the gate was fooled by short lucky streaks (12 right out of 12); that is why it needs 20+ clips
and two agreeing checks. Lowering "required accuracy" in the window unlocks it sooner and makes wrong posts more likely.

**Safety rules (enforced in the program, not just the page).**
* A waiting time (default 10 min) before each post: it is marked on the card ("posts at 14:32 unless you Trash it") with a **Don't post** button.
* At most 3 posts per 24 h and 60 min apart (both adjustable); clips older than 24 h are never posted.
* Only the channels and platforms you ticked, only through a *connected* account, only a 9:16 export of the suggested trim (it makes one if
  the recorder has not). The Share assistant (which needs you) is never used.
* Never twice: a platform that already has the clip (posted by you or earlier) is skipped. If the program dies *while* sending, that clip is
  **not** retried by itself, because it may have gone out. A failed post is retried once after 15 min, then given up on.
* **Circuit breaker:** if you trash 30% or more of its last posts that you judged (at least 5), it switches itself to *Suggest only* and says why.
* **Score rules** (in the Autopilot window, default 70 / 40): it posts only clips scoring **70 or more**; clips scoring **40-69** are
  left for you to check; clips scoring **under 40** from the streamers you ticked are moved to **Trash** while posting is on (after the
  same waiting time as a post, and only clips from the last 24 h, so switching it on does not sweep your whole backlog). Trash, not
  deleted: **Undo** brings a clip back (and it stays back), and its files go when you press "Delete trashed files". A clip you already
  worked on (exported, posted, or edited its text) is never trashed. These trashes are the Autopilot's, so they never count as your
  taste or style. Keep judging some 40-69 clips yourself: that is what teaches it which ones are worth posting.
* Everything it does is recorded in `autopilot_log.jsonl` next to your settings, and on each clip's card.

**Limits.** It posts through the same code as the Share button: that code has been tested against simulated platform servers, not your live
accounts, and the autopilot itself is tested with a stand-in provider. Check the first posts yourself. Posting other people's streams without
their permission can get you struck or banned whatever the program thinks of the clip.

### It posts in your style

Taste decides *which* clips go out; style decides *how*: the trim, captions, title, description and platforms, the way **you** do them.

* **Examples.** Every Keep, Trash, Done, Undo, title or description edit, export and post of yours adds one line to
  `<settings folder>\style\examples.jsonl`, with everything about the clip at that moment, so deleting files loses nothing.
  Good = kept, Done or posted by you. Bad = trashed and never posted by you. An Autopilot post you trash later is bad *and* counts as a
  rule failure. The Autopilot's own untouched titles, trims and posts are logged as its own and **never** learned as yours.
* **Learning.** After each new example, `learned.json` is rebuilt in the background from your last 50 examples, per streamer and for all
  streamers together. It learns title case, word limit and emoji habit; fixed hashtags, hashtag count and credit line; captions
  (always / never / only with speech); your usual trim shift and maximum length; your usual platforms; and the clip lengths you post.
  **A rule changes only when at least 5 examples agree at least 70% of the time.** Otherwise the previous value stays.
* **Teaching.** The `autoclip-teacher` Claude skill can write `taught.json`: title templates (`{streamer} {phrase} {quote} {count}`),
  description hooks, the order of title sources, and skip rules (`skip_terms`, `need_speech`). It may not write anything else.
  Rules are merged as *defaults, then learned, then taught*. A broken file or field is ignored, and the Autopilot window lists the problem.
* **What Autopilot does with it.** It trims by your shifts (kept inside the draft), and it never adds captions to a clip without speech.
  It makes a fresh 9:16 export when the latest one does not match. It writes the title and description your way unless you already
  edited them. It posts only to your usual platforms *among the ones you ticked*. Clips your pick rules rule out are **skipped** (shown on
  the card and in the log). Style can only shape or skip a post: it never raises a limit, adds a platform or streamer, or lowers a threshold.
* The review page's title suggestions follow the same rules. The rules version used is stored on each posted clip and in the log, so you
  can tell which rules made a post.
* Autopilot window, **My style**: number of examples, last update, and each rule with *learned* or *taught*. Untick **Use my style** to
  go back to the plain suggestions.

## 💰 Paid clipping campaigns (Content Rewards and the like)
Press **💰 Campaigns** in the review page, then **+ Add campaign**: a form asks for the campaign's name, its page link, the
streamers (click one you already clip, or paste their twitch.tv link), the platforms it pays for, the pay, the end date and its rules
(disclosure text, hashtags, what must stay visible, clip length). **Edit** next to a campaign changes it (renaming keeps its posts and
stats); **Delete campaign** removes it. Everything is checked before it is saved, and the form says what to fix.
It is stored in `campaigns.json` in your settings folder (`%APPDATA%\autoclip\campaigns.json`, so `git pull` never touches it);
**Advanced: edit campaigns.json directly** shows the file itself. The fields, as the file names them:

| Field | Meaning |
|---|---|
| `name`, `url` | campaign name and its page (the `https://` link you submit posts on) |
| `channels`, `platforms` | the streamers it pays for; `tiktok` / `youtube` / `instagram` |
| `rate_per_1k`, `min`, `cap` | $ per 1000 views, minimum payout, maximum payout (`0` = no cap). **`min` and `cap` are per campaign.** |
| `ends`, `active`, `budget_empty` | end date (`"2026-12-31"` = until the end of that day, or `null`); `active: true` starts it (it is off until you say so); set `budget_empty` when the campaign says its budget is used up |
| `rules.disclosure` | text burned into the first 3 seconds (`"Ad"`), or `null` |
| `rules.hashtags` | hashtags every post must carry |
| `rules.must_show` | `"facecam"`, `"overlay"`, `"handle"` (the streamer's @ is also burned in) or `null` |
| `rules.min_len_s`, `rules.max_len_s` | clip length limits (`0` = none) |
| `rules.need_speech`, `rules.no_ai`, `rules.notes` | reminders shown on the card (the Autopilot skips clips where nobody talks when `need_speech` is on) |

While a campaign is **live** (active, not ended, budget not marked empty, cap not reached) every clip of its streamers:
* keeps `must_show` visible: a split or crop layout that could cut it off is replaced by the full frame over a blurred fill. When the
  app cannot be sure (e.g. "facecam" without a saved webcam layout) the card says so;
* gets the disclosure burned in for the first 3 seconds (exports made for a campaign get `_ad` in their file name);
* gets **"#ad" first** in the post text, every campaign hashtag and one credit line (never added twice);
* can only be posted when the **pre-post check** passes. The card lists what blocks Post per platform: no disclosure in this export
  (export it again), "#ad" not first, missing hashtags, too short / too long, a platform the campaign does not pay for, must_show not
  kept, account not connected. The same check runs inside the posting code, for you and for the Autopilot.

**Paid-promotion flags** are set where the official API allows it: TikTok direct posts that are not private get
`brand_content_toggle` (Paid partnership); YouTube gets `paidProductPlacementDetails` and it is shown as set only when YouTube confirms
it (if YouTube refuses the field, the video is uploaded without it). TikTok drafts, private TikTok posts (it is not sent there),
Instagram, Zernio and the Share assistant do not set it: the card lists each flag that was not set, so you switch it on in the app.

**After posting**: every campaign post is saved with its link (paste it when the platform gives none) and goes on the **To submit**
list. The list has **one row per clip**: **Copy all links** (one per line, ready to paste into the campaign), **Open campaign**, and a
**Submitted** tick per link. A post that only reached your drafts / inbox (or the Share assistant) goes on the list when you press
**Mark as posted**, so the 30 minutes start when it is really public. You get a desktop notification when it is posted; the row turns
red 25 minutes after the clip's **earliest** post (most campaigns want the link within 30), and the page warns you once more if you
allowed its alerts. In the posts list below it, mark each post
**Submitted / Approved / Rejected**; a rejection needs the campaign's reason and is saved as a *bad* example in your style log
(`examples.jsonl`, with that reason) so the teacher skill can learn from it.

**Stats per campaign**: posted, submitted, approved, rejected, views and estimated earnings:
`min(views / 1000 × rate, cap) × 0.9`, and **$0 while that is under `min`**. Earnings count approved posts; the page also shows what
it would be if the pending ones are approved. **Refresh views (YouTube)** asks YouTube for the view counts; this needs a YouTube login
that may read statistics, so if it says it cannot, type the views in (TikTok and Instagram views are always typed in).

**Autopilot**: streamers of a live campaign count as ticked for posting (never for auto-trash), on the campaign's platforms that you
ticked. It only posts an export made for that campaign, trimmed to its length limits, and only when the pre-post check passes. It stops
for that streamer the moment the campaign ends, reaches its cap or you set `budget_empty`. Every other Autopilot limit stays the same.

### Your voice-over and hook text (originality)
Platform programmes pay more for clips with your own touch. In **Export**, open **Make it original**: type a **hook** (big text for the
first 3 seconds) and **🎙 Record** a voice-over in the page or **Pick a file** (webm / ogg / mp3 / wav / m4a, up to 25 MB). Tick
**Use my voice-over** and export: your voice starts with the clip and the stream sound goes quieter while you talk. Clips that have them
are tagged on the card (🎙 / 🪝). The voice-over file is kept next to the clip and deleted with it (Done / Delete trashed files).

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
Open **② Post text** on any clip. Titles are built from what is specific to that clip: the phrase chat was spamming ("NO WAY", "KEKW"),
how big the spike was, whether the streamer got loud, and which reaction it was (funny / shock / hype). Buttons:
**↻ Other title** steps through six options, and fetches a brand-new set when they run out; **🔄 New ideas** makes a new set right away
(titles already shown for that clip are never repeated); **🎧 From what is said** listens to the clip (needs `faster-whisper`, see Captions)
and quotes the streamer's own words in the titles. The description gets a hook line (or the quote), a credit line, and the streamer's **@tag** followed by the hashtags (the @tag is always there); text you
edited yourself is never overwritten. These are rule-based suggestions, not AI-written copy.
Set nicer names in `config.toml`:  `display_names = { kaicenat = "Kai Cenat" }`,  `handles = { kaicenat = "kaicenat" }` (the @tag, when their TikTok / Instagram handle differs from the channel name; for YouTube the @handle comes from an
`youtube.com/@name` link, and a plain video link gets no @tag unless you add it here, so a stranger is never tagged),  `hashtags = ["fyp", "gaming"]`,  `credit_line = false`.

## Video quality
The biggest factor is the quality the stream is **recorded** at: the default is now the stream's best (usually 1080p60), because a webcam
is a small part of the screen and gets enlarged ~2-3x in a vertical clip. Measured on a test pattern: recording at 1080p instead of 720p
raised the sharpness score of the webcam panel from 0.839 to 0.887 (SSIM). Exports use x264 `medium`, CRF 17. Each card shows the source
resolution (clips recorded at 720p show a warning), and the layout editor shows how much each box is enlarged ("2.1x (ok)"; red above 2x:
make the box bigger). New layouts show the whole gameplay frame (nothing cut off) instead of a tight crop; you can switch back in the editor.
Optional `sharpen = 0.5` in `config.toml` sharpens the webcam panel (off by default: it did not improve the measured score, judge by eye).
If you copied the old `config.example.toml`, the old `quality = "720p60,720p,480p,best"` line is replaced by `best` automatically.

**Speed:** the encoder is chosen automatically (NVIDIA / Intel / AMD hardware encoder if it really works, else CPU), runs at
below-normal priority so your game/stream never stutters, and loudness is normalized for social platforms.

## Captions (optional, one-time setup)
`pip install faster-whisper`, then tick **Captions** when exporting. First use downloads the speech model (~500 MB for `small`);
a GPU is used automatically if available. Style: big bold uppercase, 1-3 words at a time, current word highlighted in yellow.
Only the part you are exporting is transcribed, and stretches Whisper skipped although they are audible are retried with a more sensitive setting. If really nobody speaks there you get a clear message. Set `whisper_model = "base"` for speed
or `"medium"` for accuracy.

## Tuning
See `config.example.toml`. Too many clips -> raise `mult` and `min_users`. Missing moments -> lower them.
Clips start too late/early -> change `pre_s`. Always check the first few streams and adjust.

## Limits (be aware)
* Chat-based: moments nobody reacts to in chat are missed; clip *quality* still needs your review.
* **YouTube chat uses an unofficial feed** (the one YouTube's own page uses; `chat-downloader`, if installed, is tried when it fails)
  and may break when YouTube changes it. On a slow connection the first connect can take ~20 s (the watch page is large).
* Twitch ads can briefly interrupt the video; only streamlink's behaviour applies. If the channel requires login/subscription, it can't be recorded.
* Needs a live stream (no VODs) — for VODs use the chat-replay download instead.

## Rules for other people's streams
Recording for private review is fine; **publishing needs the streamer's permission** (Twitch/YouTube terms, and copyrighted
music in streams can get your upload claimed). Credit the streamer, and check whether they allow clipping before you post.
