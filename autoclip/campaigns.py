"""Paid clipping campaigns (Content Rewards and the like): clips of a streamer you are paid to clip are made ready to submit.

campaigns.json (in your settings folder, filled by hand or in the 💰 Campaigns window) lists each campaign:
  {name, url, channels[], platforms[], rate_per_1k, min, cap, ends, active, budget_empty,
   rules{disclosure, hashtags[], must_show, min_len_s, max_len_s, need_speech, no_ai, notes}}

While a campaign is LIVE (active, not ended, budget not marked empty, cap not reached) every clip of its channels:
  * keeps `must_show` visible in the 9:16 layout (a full-frame, blurred-fill layout when the split one could crop it);
  * gets the disclosure ("Ad") burned in for the first 3 seconds, and "#ad" first in the post text;
  * gets the campaign hashtags and a credit line in the post text;
  * may only be posted when the pre-post check passes (share.run enforces it, for you and for the Autopilot).
Posts are logged in campaign_posts.json: you submit each link to the campaign within 30 minutes, then mark it
submitted / approved / rejected. Earnings are an estimate: min(views / 1000 * rate, cap) * 0.9, and 0 under `min`
(both `min` and `cap` are per campaign).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import hooks, settings

log = logging.getLogger("autoclip.campaigns")

PLATFORMS = ("tiktok", "youtube", "instagram")            # same keys as share.PLATFORMS (a test keeps them equal)
LABELS = {"tiktok": "TikTok", "youtube": "YouTube Shorts", "instagram": "Instagram Reels"}
MUST_SHOW = ("overlay", "facecam", "handle")
STATUSES = ("to_submit", "submitted", "approved", "rejected")
STALE_LOCK_S = 5.0
DEADLINE_S = 30 * 60                                      # campaigns want the link within 30 minutes of posting
WARN_S = 25 * 60                                          # the page turns the row red from here
FEE = 0.9                                                 # the platform keeps 10%
DISCLOSURE_S = 3.0
AD_TAG = "#ad"
FLAG_HELP = {"tiktok": "not set: switch on Content disclosure > Paid partnership in TikTok",
             "youtube": "not set: tick 'Includes paid promotion' in YouTube Studio",
             "instagram": "not set: add the Paid partnership label in Instagram"}
RULE_DEFAULTS = {"disclosure": "Ad", "hashtags": [], "must_show": None, "min_len_s": 0.0, "max_len_s": 0.0, "need_speech": False,
                 "no_ai": False, "notes": ""}
EXAMPLE = {"campaigns": [{
    "name": "Example campaign", "url": "https://whop.com/", "channels": ["kaicenat"], "platforms": ["tiktok", "youtube"],
    "rate_per_1k": 1.0, "min": 10, "cap": 500, "ends": "2026-12-31", "active": False, "budget_empty": False,
    "rules": {"disclosure": "Ad", "hashtags": ["#ad", "#kaicenat"], "must_show": "facecam", "min_len_s": 10, "max_len_s": 60,
              "need_speech": False, "no_ai": True, "notes": "Facecam must be visible."}}]}

_CHAN = re.compile(r"[\w.-]{1,60}")
_HASHTAG = re.compile(r"#?(\w{1,50})", re.UNICODE)
_lock = threading.Lock()


class CampaignError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------------- files
def path() -> Path:
    return settings.config_dir() / "campaigns.json"


def posts_path() -> Path:
    return settings.config_dir() / "campaign_posts.json"


def _write_atomic(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    for i in range(40):                                    # Windows: the review page (another process) may be reading it this instant
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if i == 39:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


class _PostsLock:
    """Thread lock + a lock file, because the recorder's Autopilot and the review page are separate processes that both write the
    post log (each write takes milliseconds). A lock file older than STALE_LOCK_S is left over from a crash and is taken over."""

    def __enter__(self):
        _lock.acquire()
        self.f = posts_path().with_suffix(".lock")
        self.f.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 10
        try:
            while True:
                try:
                    os.close(os.open(self.f, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    return self
                except (FileExistsError, PermissionError):     # Windows says PermissionError while the other side deletes it
                    try:
                        if time.time() - self.f.stat().st_mtime > STALE_LOCK_S:
                            self.f.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.time() > deadline:
                        raise CampaignError("the post log is busy (another window is saving it): try again") from None
                    time.sleep(0.05)
        except BaseException:
            _lock.release()                                    # never leave this process locked out of its own post log
            raise

    def __exit__(self, *exc):
        try:
            self.f.unlink(missing_ok=True)
        finally:
            _lock.release()


# ---------------------------------------------------------------------------------------------------- validation
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "campaign"


def _number(v, what: str, lo: float = 0.0, hi: float = 1e9) -> float:
    if isinstance(v, bool):
        raise CampaignError(f"{what} must be a number")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise CampaignError(f"{what} must be a number") from None
    if x != x or not lo <= x <= hi:
        raise CampaignError(f"{what} must be between {lo:g} and {hi:g}")
    return x


def _bool(v, what: str) -> bool:
    if not isinstance(v, bool):
        raise CampaignError(f"{what} must be true or false")
    return v


def ends_ts(ends: str | None) -> float | None:
    """'2026-12-31' = the end of that day (your local time); a full ISO date-time = that moment; None = no end."""
    if not ends:
        return None
    s = str(ends).strip()
    try:
        t = datetime.fromisoformat(s)
        if not 2000 <= t.year <= 2200:
            raise ValueError("year out of range")
        return (t + (timedelta(days=1) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else timedelta(0))).timestamp()
    except (OverflowError, OSError) as e:                  # e.g. year 9999, or before 1970 on Windows
        raise ValueError(str(e)) from None


def public_url(u: str) -> str:
    """The link a campaign wants: a YouTube Studio edit link (a video YouTube kept private) becomes the public Shorts link."""
    m = re.match(r"https://studio\.youtube\.com/video/([\w-]{6,20})", u or "")
    return f"https://www.youtube.com/shorts/{m.group(1)}" if m else u


def safe_url(u) -> str:
    u = str(u or "").strip()
    if not re.fullmatch(r"https://[^\s\"'<>]{1,500}", u, re.I):
        raise CampaignError("must be a web link starting with https://")
    return u


def validate_one(raw, n: int = 1) -> dict:
    """A cleaned campaign, or CampaignError naming the campaign and the field that is wrong."""
    where = f"campaign {n}"
    try:
        if not isinstance(raw, dict):
            raise CampaignError("must be an object { ... }")
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 80:
            raise CampaignError("name is required (at most 80 characters)")
        where = f"campaign {n} ({name})"
        out: dict = {"id": _slug(str(raw.get("id") or name)), "name": name}
        try:
            out["url"] = safe_url(raw.get("url"))
        except CampaignError as e:
            raise CampaignError(f"url {e}") from None
        ch = raw.get("channels")
        if not isinstance(ch, list) or not ch or any(not isinstance(c, str) or not _CHAN.fullmatch(c.strip()) for c in ch):
            raise CampaignError('channels must be a list of channel names, e.g. ["kaicenat"]')
        out["channels"] = list(dict.fromkeys(c.strip().lower() for c in ch))
        pl = raw.get("platforms")
        if not isinstance(pl, list) or not pl or any(p not in PLATFORMS for p in pl):
            raise CampaignError("platforms must be a list using " + ", ".join(PLATFORMS))
        out["platforms"] = list(dict.fromkeys(pl))
        out["rate_per_1k"] = _number(raw.get("rate_per_1k"), "rate_per_1k", 0, 1000)
        out["min"] = _number(raw.get("min", 0), "min", 0)
        out["cap"] = _number(raw.get("cap", 0), "cap (0 = no cap)", 0)
        ends = raw.get("ends")
        if ends in (None, ""):
            out["ends"] = None
        else:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?", str(ends).strip()):
                    raise ValueError("format")
                ends_ts(str(ends))
            except ValueError:
                raise CampaignError('ends must be a date like "2026-12-31" (or null)') from None
            out["ends"] = str(ends).strip()
        out["active"] = _bool(raw.get("active", False), "active")
        out["budget_empty"] = _bool(raw.get("budget_empty", False), "budget_empty")
        r = raw.get("rules") or {}
        if not isinstance(r, dict):
            raise CampaignError("rules must be an object { ... }")
        rules = dict(RULE_DEFAULTS)
        if "disclosure" in r:
            d = r["disclosure"]
            if d is not None and (not isinstance(d, str) or len(d.strip()) > 30):
                raise CampaignError('rules.disclosure must be short text like "Ad", or null')
            rules["disclosure"] = (d or "").strip() or None
        tags = r.get("hashtags", [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or not _HASHTAG.fullmatch(t.strip()) for t in tags):
            raise CampaignError('rules.hashtags must be a list like ["#ad", "#gaming"]')
        rules["hashtags"] = list(dict.fromkeys("#" + t.strip().lstrip("#") for t in tags))[:30]
        ms = r.get("must_show")
        if ms is not None and ms not in MUST_SHOW:
            raise CampaignError("rules.must_show must be " + ", ".join(f'"{x}"' for x in MUST_SHOW) + " or null")
        rules["must_show"] = ms
        rules["min_len_s"] = _number(r.get("min_len_s", 0), "rules.min_len_s", 0, 600)
        rules["max_len_s"] = _number(r.get("max_len_s", 0), "rules.max_len_s (0 = no limit)", 0, 600)
        if rules["max_len_s"] and rules["min_len_s"] > rules["max_len_s"]:
            raise CampaignError("rules.min_len_s is longer than rules.max_len_s")
        rules["need_speech"] = _bool(r.get("need_speech", False), "rules.need_speech")
        rules["no_ai"] = _bool(r.get("no_ai", False), "rules.no_ai")
        rules["notes"] = str(r.get("notes") or "")[:2000]
        out["rules"] = rules
        return out
    except CampaignError as e:
        raise CampaignError(f"{where}: {e}") from None


def validate_all(data) -> list[dict]:
    rows = data.get("campaigns") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise CampaignError('the file must look like {"campaigns": [ ... ]}')
    out, seen = [], set()
    for i, raw in enumerate(rows, 1):
        c = validate_one(raw, i)
        if c["id"] in seen:
            raise CampaignError(f"campaign {i} ({c['name']}): two campaigns have the same name/id '{c['id']}'")
        seen.add(c["id"])
        out.append(c)
    return out


def load_all() -> tuple[list[dict], list[str]]:
    """(valid campaigns, problems). A damaged campaign is left out (never acted on) and named in problems."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except OSError:
        return [], []
    except ValueError as e:
        return [], [f"campaigns.json is not valid JSON: {e}"]
    rows = data.get("campaigns") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return [], ['campaigns.json must look like {"campaigns": [ ... ]}']
    out, problems, seen = [], [], set()
    for i, raw in enumerate(rows, 1):
        try:
            c = validate_one(raw, i)
        except CampaignError as e:
            problems.append(str(e))
            continue
        if c["id"] in seen:
            problems.append(f"campaign {i} ({c['name']}): duplicate name, left out")
            continue
        seen.add(c["id"])
        out.append(c)
    return out, problems


def load() -> list[dict]:
    return load_all()[0]


def raw_text() -> str:
    try:
        return path().read_text(encoding="utf-8")
    except OSError:
        return json.dumps(EXAMPLE, indent=2)


def save_text(text: str) -> list[dict]:
    """Validate the text from the Campaigns window and store it. Nothing is written when anything is wrong."""
    if len(text) > 500_000:
        raise CampaignError("campaigns.json is too big")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CampaignError(f"not valid JSON: {e}") from None
    camps = validate_all(data)
    _write_atomic(path(), {"campaigns": camps})
    return camps


def find(cid: str, camps: list[dict] | None = None) -> dict | None:
    return next((c for c in (load() if camps is None else camps) if c["id"] == cid), None)


# ---------------------------------------------------------------------------------------------------- posts + money
def posts(campaign_id: str | None = None, strict: bool = False) -> list[dict]:
    """The post log. strict (writers): a log that cannot be read right now raises, and a damaged one is moved aside to
    campaign_posts.damaged-<time>.json (kept for you) instead of being overwritten by the next post."""
    p = posts_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        rows = []
    except OSError as e:
        if strict:
            raise CampaignError(f"the post log cannot be read right now ({e}): try again") from None
        rows = []
    else:
        try:
            rows = json.loads(text).get("posts", [])
            if not isinstance(rows, list):
                raise ValueError("posts is not a list")
        except (ValueError, AttributeError):
            if strict:
                keep = p.with_name(f"campaign_posts.damaged-{time.strftime('%Y%m%d-%H%M%S')}.json")
                os.replace(p, keep)
                log.warning("campaign_posts.json was damaged: kept as %s, starting a new one", keep.name)
            rows = []
    rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
    return [r for r in rows if campaign_id is None or r.get("campaign") == campaign_id]


def _float(v, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def earnings(views: float, rate: float, cap: float, minimum: float) -> float:
    """min(views / 1000 * rate, cap) * 0.9; nothing at all while that is under the campaign minimum."""
    gross = max(float(views or 0), 0.0) / 1000.0 * float(rate or 0)
    if cap:
        gross = min(gross, float(cap))
    net = round(gross * FEE, 2)
    return net if net >= float(minimum or 0) else 0.0


def stats(c: dict, rows: list[dict] | None = None) -> dict:
    rows = posts(c["id"]) if rows is None else [r for r in rows if r.get("campaign") == c["id"]]
    views = lambda rs: sum(int(_float(r.get("views"), 0) or 0) for r in rs)       # noqa: E731
    approved = [r for r in rows if r.get("status") == "approved"]
    alive = [r for r in rows if r.get("status") != "rejected"]
    gross_all = views(alive) / 1000.0 * c["rate_per_1k"]
    return {"posted": len(rows), "to_submit": sum(1 for r in rows if r.get("status") == "to_submit"),
            "submitted": sum(1 for r in rows if r.get("status") in ("submitted", "approved", "rejected")),
            "approved": len(approved), "rejected": sum(1 for r in rows if r.get("status") == "rejected"),
            "views": views(rows), "approved_views": views(approved),
            "earnings": earnings(views(approved), c["rate_per_1k"], c["cap"], c["min"]),
            "earnings_if_approved": earnings(views(alive), c["rate_per_1k"], c["cap"], c["min"]),
            "capped": bool(c["cap"]) and gross_all >= c["cap"]}


def live_reason(c: dict, now: float | None = None, rows: list[dict] | None = None) -> str | None:
    """Why this campaign is not live right now, or None when it is."""
    now = time.time() if now is None else now
    if not c["active"]:
        return "switched off (active is false)"
    if c["budget_empty"]:
        return "you marked its budget empty"
    try:
        end = ends_ts(c["ends"])
    except ValueError:
        end = None
    if end and now >= end:
        return f"ended on {c['ends']}"
    if c["cap"] and stats(c, rows)["capped"]:
        return f"its cap (${c['cap']:g}) is reached"
    return None


def for_channel(channel: str | None, now: float | None = None, camps: list[dict] | None = None, live: bool = True,
                rows: list[dict] | None = None) -> dict | None:
    """The first campaign that covers this channel (only a live one, unless live=False)."""
    ch = str(channel or "").lower()
    if not ch:
        return None
    camps = load() if camps is None else camps
    rows = (posts() if camps else []) if rows is None else rows
    for c in camps:
        if ch in c["channels"] and (not live or live_reason(c, now, rows) is None):
            return c
    return None


def add_post(c: dict, clip_id: str, platform: str, url: str | None, title: str = "", now: float | None = None) -> dict:
    """Log a post made for a campaign (you submit its link next). Posting the same clip to the same platform again updates it."""
    now = time.time() if now is None else now
    pid = f"{clip_id}:{platform}"
    try:
        url = public_url(safe_url(url)) if url else None
    except CampaignError:
        url = None
    with _PostsLock():
        rows = posts(strict=True)
        cur = next((r for r in rows if r["id"] == pid), None)
        if cur:
            if url and not cur.get("url"):
                cur["url"] = url
            row, new = cur, False
        else:
            row = {"id": pid, "campaign": c["id"], "clip": clip_id, "platform": platform, "url": url, "title": str(title)[:200],
                   "posted_at": round(now, 1), "status": "to_submit", "reason": "", "views": None, "views_src": None}
            rows.append(row)
            new = True
        _write_atomic(posts_path(), {"posts": rows})
    if new:
        notify(f"Submit your clip to {c['name']}",
               f"{LABELS.get(platform, platform)} post is up. Submit the link within 30 minutes (Campaigns window).")
    return row


def update_post(pid: str, status: str | None = None, reason: str | None = None, url: str | None = None,
                views=None, views_src: str = "you", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _PostsLock():
        rows = posts(strict=True)
        row = next((r for r in rows if r["id"] == pid), None)
        if not row:
            raise CampaignError("post not found")
        if status is not None:
            if status not in STATUSES:
                raise CampaignError("status must be " + ", ".join(STATUSES))
            if status == "rejected" and not str(reason or row.get("reason") or "").strip():
                raise CampaignError("say why it was rejected (it teaches your style)")
            row["status"] = status
            row[{"submitted": "submitted_at", "to_submit": "reset_at"}.get(status, "decided_at")] = round(now, 1)
        if reason is not None:
            row["reason"] = str(reason).strip()[:500]
        if url is not None:
            row["url"] = safe_url(url) if url else None
        if views is not None and views != "":
            v = _number(views, "views", 0, 1e12)
            row["views"], row["views_src"] = int(v), views_src
        _write_atomic(posts_path(), {"posts": rows})
        return dict(row)


def to_submit(now: float | None = None) -> list[dict]:
    """Posts still waiting for you to submit them, oldest first, with how long is left before the 30-minute deadline."""
    now = time.time() if now is None else now
    names = {c["id"]: c for c in load()}
    out = []
    for r in posts():
        if r.get("status") != "to_submit":
            continue
        at = _float(r.get("posted_at"))
        if at is None or not isinstance(r.get("platform"), str):
            continue                                       # a hand-damaged row: never breaks the list
        r = {**r, "posted_at": at}
        age = now - at
        c = names.get(r.get("campaign")) or {}
        out.append({**r, "campaign_name": c.get("name", r.get("campaign")), "campaign_url": c.get("url", ""),
                    "age_s": round(age), "left_s": round(DEADLINE_S - age), "late": age >= WARN_S})
    out.sort(key=lambda r: r["posted_at"])
    return out


def to_submit_groups(now: float | None = None) -> list[dict]:
    """The To-submit list as one row per clip and campaign: its posts (one per platform), all links together, and the 30-minute
    timer counted from the EARLIEST of those posts. Oldest first."""
    now = time.time() if now is None else now
    groups: dict[tuple, dict] = {}
    for r in to_submit(now):
        g = groups.setdefault((r.get("campaign"), r.get("clip")), {
            "key": f"{r.get('campaign')}|{r.get('clip')}", "campaign": r.get("campaign"), "clip": r.get("clip"), "title": r.get("title", ""),
            "campaign_name": r["campaign_name"], "campaign_url": r["campaign_url"], "posted_at": r["posted_at"], "posts": []})
        g["posted_at"] = min(g["posted_at"], r["posted_at"])
        g["title"] = g["title"] or r.get("title", "")
        g["posts"].append({"id": r["id"], "platform": r["platform"], "url": r.get("url")})
    out = []
    for g in groups.values():
        age = now - float(g["posted_at"])
        g["posts"].sort(key=lambda x: PLATFORMS.index(x["platform"]) if x["platform"] in PLATFORMS else 9)
        out.append({**g, "links": "\n".join(x["url"] for x in g["posts"] if x["url"]), "age_s": round(age),
                    "left_s": round(DEADLINE_S - age), "late": age >= WARN_S})
    out.sort(key=lambda g: g["posted_at"])
    return out


def youtube_views(urls: list[str]) -> dict[str, int]:
    """View counts of your YouTube posts through your connected account, {url: views}. Raises with the reason when it cannot."""
    from . import accounts as acc
    ids = {}
    for u in urls:
        m = re.search(r"(?:shorts/|watch\?v=|youtu\.be/)([\w-]{6,20})", u or "")
        if m:
            ids[m.group(1)] = u
    if not ids:
        return {}
    token = acc.youtube_token()
    st, _, raw = acc.http("GET", "https://www.googleapis.com/youtube/v3/videos?part=statistics&id=" + ",".join(ids),
                          headers={"Authorization": f"Bearer {token}"}, retries=1)
    if st != 200:
        raise acc.ApiError(acc.api_message(st, raw))
    out = {}
    for it in acc.as_json(raw).get("items") or []:
        try:
            out[ids[it["id"]]] = int(it["statistics"]["viewCount"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def refresh_views() -> dict:
    """Fill in views from the platform APIs where that is possible (YouTube). Others you type in the Campaigns window."""
    yt = [r for r in posts() if r.get("platform") == "youtube" and r.get("url") and r.get("status") != "rejected"]
    if not yt:
        return {"updated": 0, "note": "No YouTube posts with a link. TikTok and Instagram views: type them in."}
    try:
        got = youtube_views([r["url"] for r in yt])
    except Exception as e:
        return {"updated": 0, "note": f"YouTube did not give the views ({e}). Type them in instead."}
    n = 0
    for r in yt:
        if r["url"] in got:
            try:
                update_post(r["id"], views=got[r["url"]], views_src="api")
                n += 1
            except (CampaignError, OSError) as e:
                return {"updated": n, "note": f"{n} updated, then the post log could not be saved ({e}). Try again."}
    return {"updated": n, "note": f"{n} YouTube post(s) updated. TikTok and Instagram views: type them in."}


# ---------------------------------------------------------------------------------------------------- layout + burned-in text
def fit_layout(layout: dict | None, must_show: str | None) -> tuple[dict | None, str | None, bool]:
    """(layout to use, warning for the card or None, ok). Never hides what the campaign says must be visible:
    a split or crop layout that could cut it off becomes the full frame over a blurred fill."""
    if not must_show:
        return layout, None, True
    mode = (layout or {}).get("mode", "blur")
    full = {"mode": "blur", **({"captions": layout["captions"]} if layout and layout.get("captions") else {})}
    if must_show == "facecam":
        if mode == "split":
            return layout, None, True
        if mode == "crop":
            return full, "Facecam: the centre crop could cut the webcam off, so the full frame is used. Check the webcam shows.", True
        return layout, "Facecam: the full frame is used. Check the stream really shows the webcam (no webcam layout is saved).", True
    if mode == "split" and layout.get("game_fit") == "blur" and not layout.get("game"):
        return layout, None, True                          # the whole stream frame is in the bottom panel
    if mode in ("split", "crop"):
        what = "stream overlay" if must_show == "overlay" else "streamer handle"
        return full, f"The {what} must show: the full frame (blurred fill) is used instead of your {mode} layout.", True
    return layout, None, True


def _ass_text(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[{}\\]", "", str(s or ""))).strip()[:120]


def _ts(t: float) -> str:
    t = max(t, 0.0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


_FORMAT = ("Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
           "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n")


def overlay_ass(base: str | None, vertical: bool, font: str, dur: float, disclosure: str | None = None,
                hook: str | None = None, handle: str | None = None, hook_style: str = "pop",
                protected: list | None = None) -> str | None:
    """Captions (base, may be None) plus the burned-in disclosure (first 3 s, top-left corner), hook text (first 2.5 s, top-centre,
    kept off the disclosure / handle corner band and off every `protected` (y0, y1) band such as the facecam) and @handle (whole clip)."""
    disclosure, hook, handle = _ass_text(disclosure), _ass_text(hook), _ass_text(handle)
    if not (disclosure or hook or handle):
        return base
    W, H = (1080, 1920) if vertical else (1920, 1080)
    if base:
        mx, my = re.search(r"PlayResX:\s*(\d+)", base), re.search(r"PlayResY:\s*(\d+)", base)
        W, H = (int(mx.group(1)) if mx else W), (int(my.group(1)) if my else H)
    font = re.sub(r"[,\r\n]", "", font or "Arial") or "Arial"
    s1, s3 = round(H * 0.032), round(H * 0.028)
    styles = (f"Style: Disc,{font},{s1},&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,-1,0,0,0,100,100,0,0,3,{max(4, s1 // 4)},0,7,"
              f"{round(W * 0.04)},{round(W * 0.04)},{round(H * 0.035)},1\n"
              f"Style: Handle,{font},{s3},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(3, s3 // 10)},1,9,"
              f"{round(W * 0.04)},{round(W * 0.04)},{round(H * 0.035)},1\n")
    end = max(dur, 0.5)
    ev = ""
    if disclosure:
        ev += f"Dialogue: 2,{_ts(0)},{_ts(min(DISCLOSURE_S, end))},Disc,,0,0,0,,{disclosure}\n"
    if hook:
        corner = (0.0, round(H * 0.035) + max(s1, s3) * 2.2) if (disclosure or handle) else None
        hs, hd = hooks.ass_event(hook, hook_style, W, H, list(protected or []) + ([corner] if corner else []), end, font)
        styles += hs
        ev += hd
    if handle:
        ev += f"Dialogue: 1,{_ts(0)},{_ts(end)},Handle,,0,0,0,,{handle}\n"
    if base and "\n[Events]" in base:
        out = base.replace("\n[Events]", styles + "\n[Events]", 1)
        return out + ("" if out.endswith("\n") else "\n") + ev
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n\n[V4+ Styles]\n{_FORMAT}{styles}\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    return head + ev


def plan(meta: dict, layout: dict | None, vertical: bool, cfg, now: float | None = None) -> dict:
    """What a campaign changes about an export of this clip: {'campaign', 'layout', 'record'} (campaign None = nothing)."""
    c = for_channel(meta.get("channel"), now)
    if not c:
        return {"campaign": None, "layout": layout, "record": None}
    ms = c["rules"]["must_show"]
    warn, ok = None, True
    if vertical:
        base = layout or {"mode": getattr(cfg, "vertical_mode", "blur")}
        new, warn, ok = fit_layout(base, ms)
        layout = new if new is not base else layout
    rec = {"id": c["id"], "disclosure": bool(c["rules"]["disclosure"]), "must_show": ms, "must_show_ok": ok, "warn": warn,
           "layout": (layout or {}).get("mode", getattr(cfg, "vertical_mode", "blur")) if vertical else "full"}
    return {"campaign": c, "layout": layout, "record": rec}


def protected_bands(layout: dict | None, vertical: bool, captions: bool) -> list[tuple[float, float]]:
    """Rows of the finished frame (y0, y1 in pixels) the hook must not cover: the facecam panel and the captions."""
    H = 1920 if vertical else 1080
    out: list[tuple[float, float]] = []
    mode = (layout or {}).get("mode")
    if vertical and mode == "split":
        out.append((0.0, H * float(layout.get("split", 0.5))))
    if captions:
        from .captions import caption_geometry
        g = caption_geometry(vertical, mode == "split", (layout or {}).get("captions") if vertical else None)
        if g["an"] == 5:                                   # centred on cy
            out.append((g["cy"] - g["size"] * 1.4, g["cy"] + g["size"] * 1.4))
        else:                                              # bottom-anchored at cy, up to 2 lines above it
            out.append((g["cy"] - g["size"] * 2.6, g["cy"] + g["size"] * 0.3))
    return out


def burn(p: dict, base_ass: str | None, vertical: bool, cfg, dur: float, meta: dict, hook: str | None = None,
         hook_style: str = "pop", protected: list | None = None) -> str | None:
    """The ASS text for an export: captions + campaign disclosure / handle + your hook text (never over the facecam / captions)."""
    c = p.get("campaign")
    handle = None
    if c and c["rules"]["must_show"] == "handle":
        from .describe import mention
        handle = mention(meta, cfg) or ("@" + str(meta.get("channel", "")))
    bands = protected_bands(p.get("layout"), vertical, bool(base_ass)) + list(protected or [])
    return overlay_ass(base_ass, vertical, getattr(cfg, "caption_font", "Arial"), dur,
                       c["rules"]["disclosure"] if c else None, hook, handle, hook_style, bands)


# ---------------------------------------------------------------------------------------------------- post text + pre-post check
def credit_line(meta: dict, cfg) -> str:
    from .describe import display_name, mention
    name = display_name(str(meta.get("channel", "")), cfg) if cfg is not None else str(meta.get("channel", ""))
    if meta.get("source_url"):
        return f"🎥 Clip from {name}: {meta['source_url']}"
    at = mention(meta, cfg) if cfg is not None else ""
    return f"🎥 Credit: {at or name}"


def has_credit(text: str, meta: dict, title_first: bool = True) -> bool:
    low = text.lower()
    if meta.get("source_url") and str(meta["source_url"]).lower() in low:
        return True
    lines = text.split("\n")[1:] if title_first else text.split("\n")     # never your title: "credit card" is not a credit
    lines = [re.sub(r"^\s*#ad\b\s*", "", ln, flags=re.I) for ln in lines]
    return any(ln.strip().startswith("🎥") or re.match(r"\s*(credit|clip from|via)\b\s*[:@]?", ln, re.I) and ("@" in ln or ":" in ln)
               for ln in lines)


def finalize_text(platform: str, title: str, text: str, c: dict, meta: dict, cfg) -> tuple[str, str]:
    """The post text a campaign needs: "#ad" first, every campaign hashtag, and a credit line (never added twice)."""
    r = c["rules"]
    body = text
    if r["disclosure"]:
        body = re.sub(r"(?<![\w#])#ad(?!\w)[ \t]?", "", body, flags=re.I)
        body = f"{AD_TAG} {body.strip()}".strip()
    lines = [ln.rstrip() for ln in body.strip().split("\n")]
    tag_i = next((i for i in range(len(lines) - 1, 0, -1) if lines[i].lstrip().startswith(("#", "@"))), None)   # never the "#ad" line
    missing = [t for t in r["hashtags"] if not re.search(re.escape(t) + r"(?!\w)", body, re.I)]
    if missing:
        if tag_i is None:
            lines += ["", ""]
            tag_i = len(lines) - 1
        lines[tag_i] = (lines[tag_i] + " " + " ".join(missing)).strip()
    if not has_credit(body, meta, title_first=platform != "youtube"):                         # the credit goes above the hashtag line, which stays last
        credit = credit_line(meta, cfg)
        lines[tag_i:tag_i] = [credit, ""] if tag_i is not None else []
        if tag_i is None:
            lines += ["", credit]
    return title, "\n".join(lines).strip()


def export_ready(e: dict | None, c: dict) -> bool:
    """Was this export made for campaign `c`, with everything it must burn in?"""
    cr = (e or {}).get("campaign") or {}
    return bool(e and e.get("vertical") and cr.get("id") == c["id"] and (cr.get("disclosure") or not c["rules"]["disclosure"])
                and cr.get("must_show_ok", True))


def check(c: dict, meta: dict, export: dict | None, platform: str, title: str, text: str, now: float | None = None,
          rows: list[dict] | None = None) -> list[str]:
    """Reasons this post would break the campaign's rules (empty = fine). Account problems come from the sharing method."""
    errs = []
    why = live_reason(c, now, rows)
    if why:
        errs.append(f"campaign '{c['name']}' is not live: {why}")
    if platform not in c["platforms"]:
        errs.append(f"{LABELS.get(platform, platform)} is not allowed by this campaign (allowed: "
                    + ", ".join(LABELS.get(p, p) for p in c["platforms"]) + ")")
    r = c["rules"]
    cr = (export or {}).get("campaign") or {}
    if r["disclosure"] and not (cr.get("id") == c["id"] and cr.get("disclosure")):
        errs.append(f'this export has no burned-in "{r["disclosure"]}" disclosure: export the clip again (campaign exports add it)')
    if r["disclosure"] and not re.match(re.escape(AD_TAG) + r"(?!\w)", text.lstrip(), re.I):
        errs.append(f'the post text must start with "{AD_TAG}"')
    missing = [t for t in r["hashtags"] if not re.search(re.escape(t) + r"(?!\w)", f"{title}\n{text}", re.I)]
    if missing:
        errs.append("missing hashtags: " + " ".join(missing))
    if export:
        try:
            ln = float(export["end"]) - float(export["start"])
        except (KeyError, TypeError, ValueError):
            ln = None
        if ln is not None and r["min_len_s"] and ln < r["min_len_s"] - 0.05:
            errs.append(f"clip is {ln:.1f} s, the campaign wants at least {r['min_len_s']:g} s")
        if ln is not None and r["max_len_s"] and ln > r["max_len_s"] + 0.05:
            errs.append(f"clip is {ln:.1f} s, the campaign allows at most {r['max_len_s']:g} s")
    if r["must_show"] and (cr.get("id") != c["id"] or not cr.get("must_show_ok", False)):
        errs.append(f"the {r['must_show']} must be visible: export the clip again so the layout keeps it in")
    return errs


def length_ok(e: dict, c: dict) -> bool:
    """Does this export's length meet the campaign's limits?"""
    try:
        n = float(e["end"]) - float(e["start"])
    except (KeyError, TypeError, ValueError):
        return False
    r = c["rules"]
    return not (r["min_len_s"] and n < r["min_len_s"] - 0.05) and not (r["max_len_s"] and n > r["max_len_s"] + 0.05)


def fit_trim(start: float, end: float, duration: float, c: dict) -> tuple[float, float] | None:
    """The trim stretched or shortened to the campaign's length rules, or None when the draft is too short."""
    r = c["rules"]
    n = end - start
    if r["max_len_s"] and n > r["max_len_s"]:
        end = start + r["max_len_s"]
    if r["min_len_s"] and n < r["min_len_s"]:
        need = r["min_len_s"] - n
        end = min(duration, end + need)
        start = max(0.0, end - r["min_len_s"])
        if end - start < r["min_len_s"] - 0.05:
            return None
    return round(start, 2), round(end, 2)


def card_view(meta: dict, provider_problem=None, now: float | None = None, camps: list[dict] | None = None,
              rows: list[dict] | None = None) -> dict | None:
    """What the clip card shows for a campaign clip: the campaign, warnings, and what would block Post (per platform)."""
    camps = load() if camps is None else camps
    rows = posts() if rows is None else rows
    c = for_channel(meta.get("channel"), now, camps, rows=rows) or for_channel(meta.get("channel"), now, camps, live=False, rows=rows)
    if not c:
        return None
    why = live_reason(c, now, rows)
    ex = [e for e in meta.get("exports") or [] if e.get("vertical")]
    best = next((e for e in reversed(ex) if export_ready(e, c)), ex[-1] if ex else None)
    d = meta.get("description") or {}
    warn = [w for w in [((best or {}).get("campaign") or {}).get("warn")] if w]
    problems = {}
    if not why:
        for pl in c["platforms"]:
            from .share import compose
            t, x = compose(pl, str(d.get("title", "")), str(d.get("body", "")))
            t, x = finalize_text(pl, t, x, c, meta, None)
            errs = check(c, meta, best, pl, t, x, now, rows) if best else ["export a 9:16 clip first"]
            pp = provider_problem(pl) if provider_problem else None
            problems[pl] = errs + ([pp] if pp else [])
    flags = {pl: sh.get("paid_flag") for pl, sh in (meta.get("shares") or {}).items()
             if sh.get("campaign") == c["id"] and sh.get("paid_flag") and sh.get("paid_flag") != "set"}
    if c["rules"]["need_speech"]:
        warn.append("This campaign wants speech in the clip: check the streamer talks.")
    return {"id": c["id"], "name": c["name"], "url": c["url"], "live": why is None, "why": why, "platforms": c["platforms"],
            "rules": c["rules"], "export": (best or {}).get("file"), "ready": bool(best and export_ready(best, c)),
            "problems": problems, "warn": warn, "flags_missing": flags,
            "posts": [r for r in rows if r.get("campaign") == c["id"] and r.get("clip") == meta.get("id")]}


# ---------------------------------------------------------------------------------------------------- desktop notification
_PS = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
       "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
       "$x = $t.GetElementsByTagName('text'); $x.Item(0).AppendChild($t.CreateTextNode($env:AUTOCLIP_NT)) > $null;"
       "$x.Item(1).AppendChild($t.CreateTextNode($env:AUTOCLIP_NM)) > $null;"
       "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
       "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show("
       "[Windows.UI.Notifications.ToastNotification]::new($t))")


def notify(title: str, msg: str) -> None:
    """Best effort desktop notification (the review page also shows one while it is open). Never raises."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AUTOCLIP_NO_NOTIFY"):
        return
    try:
        env = {**os.environ, "AUTOCLIP_NT": str(title)[:100], "AUTOCLIP_NM": str(msg)[:250]}      # text goes in through env, never the script
        if sys.platform == "win32":
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", 'display notification (system attribute "AUTOCLIP_NM") with title (system attribute "AUTOCLIP_NT")'],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["notify-send", env["AUTOCLIP_NT"], env["AUTOCLIP_NM"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                                 # no notifier on this PC: the page still shows the list
        log.debug("notification failed: %s", e)
