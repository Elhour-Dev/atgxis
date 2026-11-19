"""Sharing finished clips to TikTok / YouTube Shorts / Instagram Reels.

Safety rules (enforced here, not just in the page):
  * only a clip you EXPORTED (trimmed, laid out, captioned) can be shared, never a raw draft;
  * nothing is posted unless the page sends an explicit share request (the page asks you to confirm first);
  * a platform that is already marked done is not posted again unless you force it;
  * every result (done / opened / failed + the reason) is written to the clip's record.

Providers do the actual posting. `assistant` needs no accounts: it opens the platform's upload page, reveals the file and you press
Post. Automatic providers (platform APIs or a posting service) plug into PROVIDERS and get the same job description.
"""
from __future__ import annotations

import re
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import campaigns, settings, style, taste

PLATFORMS: dict[str, dict] = {
    "tiktok": {"label": "TikTok", "url": "https://www.tiktok.com/tiktokstudio/upload", "text_max": 2200},
    "youtube": {"label": "YouTube Shorts", "url": "https://studio.youtube.com/", "title_max": 100, "text_max": 5000},
    "instagram": {"label": "Instagram Reels", "url": "https://www.instagram.com/", "text_max": 2200, "hashtags_max": 30},
}


class ShareError(ValueError):
    pass


@dataclass
class Job:
    platform: str
    video: Path
    title: str
    text: str            # caption / description exactly as it should be posted
    privacy: str = "public"
    paid: bool = False   # a paid campaign clip: set the platform's paid-promotion flag where its API allows


@dataclass
class Result:
    ok: bool
    status: str          # "done" | "draft" (uploaded, you finish it in the app) | "opened" | "failed"
    message: str = ""
    url: str | None = None
    paid_flag: str | None = None     # paid jobs: "set", or why it could not be set (you switch it on in the app)


def compose(platform: str, title: str, body: str) -> tuple[str, str]:
    """(title, text) in the shape each platform wants."""
    title, body = title.strip(), body.strip()
    if platform == "youtube":
        text = body if re.search(r"#shorts\b", body, re.I) else (body + "\n\n#Shorts").strip()
        return title, text
    return title, f"{title}\n\n{body}".strip()


def check(platform: str, title: str, text: str, duration_s: float | None, vertical: bool) -> list[str]:
    """Problems that would make the platform reject the post. Empty list = fine."""
    spec, errs = PLATFORMS[platform], []
    if not title.strip():
        errs.append("title is empty")
    if spec.get("title_max") and len(title) > spec["title_max"]:
        errs.append(f"title is {len(title)} characters, {spec['label']} allows {spec['title_max']}")
    if len(text) > spec["text_max"]:
        errs.append(f"text is {len(text)} characters, {spec['label']} allows {spec['text_max']}")
    if spec.get("hashtags_max") and len(re.findall(r"#\w+", text)) > spec["hashtags_max"]:
        errs.append(f"too many hashtags (max {spec['hashtags_max']})")
    if not vertical:
        errs.append("this export is not vertical 9:16: tick 'Vertical 9:16' and export again")
    if duration_s and duration_s > 180:
        errs.append("clip is longer than 3 minutes")
    return errs


class Provider:
    name, label, automatic = "base", "base", False

    def configured(self) -> bool:
        return True

    def problems(self, platform: str) -> str | None:
        """Why this provider cannot post to `platform` right now (e.g. account not connected), or None."""
        return None

    def publish(self, job: Job) -> Result:
        raise NotImplementedError


class AssistantProvider(Provider):
    """No accounts, no API: opens the upload page and shows the file. You press Post (about 10 seconds per platform)."""
    name, label, automatic = "assistant", "Share assistant (opens the page, you press Post)", False

    def __init__(self, open_url: Callable[[str], object] = webbrowser.open, reveal: Callable[[Path], None] = settings.reveal):
        self.open_url, self.reveal = open_url, reveal

    def publish(self, job: Job) -> Result:
        label = PLATFORMS[job.platform]["label"]
        self.open_url(PLATFORMS[job.platform]["url"])
        self.reveal(job.video)
        return Result(True, "opened", f"{label} page opened and the video file is selected in your file manager. "
                                      "Caption is on your clipboard: paste it, attach the video, press Post.")


PROVIDERS: dict[str, Provider] = {"assistant": AssistantProvider()}     # publish.py adds "direct" (official APIs)


def providers_info() -> list[dict]:
    return [{"name": p.name, "label": p.label, "automatic": p.automatic, "configured": p.configured()} for p in PROVIDERS.values()]


@dataclass
class ShareRequest:
    clip_id: str
    file: str
    platforms: list[str]
    texts: dict[str, dict] = field(default_factory=dict)       # {platform: {"title":..., "body":...}}
    provider: str = "assistant"
    force: bool = False
    actor: str = "you"            # "autopilot" for posts made without you: those never count as your taste (see taste.py)


def run(store, req: ShareRequest) -> dict[str, dict]:
    """Validate everything first, then post platform by platform. Returns {platform: {status, message, url}}."""
    path, meta = store.find(req.clip_id)
    if not path:
        raise ShareError("clip not found")
    ex = next((e for e in meta.get("exports", []) if e["file"] == req.file), None)
    if not ex:
        raise ShareError("share an exported clip: press 'Export clip' first, then share that file")
    video = path.parent / ex["file"]
    if not video.is_file():
        raise ShareError("the exported file is missing on disk")
    prov = PROVIDERS.get(req.provider)
    if not prov or not prov.configured():
        raise ShareError(f"sharing method '{req.provider}' is not set up")
    plats = list(dict.fromkeys(req.platforms))
    if not plats or any(p not in PLATFORMS for p in plats):
        raise ShareError("choose at least one platform: " + ", ".join(PLATFORMS))
    jobs, problems = [], []
    camp = campaigns.for_channel(meta.get("channel"))          # a live paid campaign covers this streamer: its rules must pass
    for pl in plats:
        t = req.texts.get(pl) or {}
        from .describe import with_mention
        title, text = compose(pl, str(t.get("title", "")), with_mention(str(t.get("body") or ""), meta, getattr(store, "cfg", None)))   # always @streamer
        if camp:
            title, text = campaigns.finalize_text(pl, title, text, camp, meta, getattr(store, "cfg", None))
            problems += [f"{PLATFORMS[pl]['label']}: {e}" for e in campaigns.check(camp, meta, ex, pl, title, text)]
        errs = check(pl, title, text, meta.get("duration_s"), bool(ex.get("vertical")))
        if errs:
            problems.append(f"{PLATFORMS[pl]['label']}: " + "; ".join(errs))
        done = (meta.get("shares") or {}).get(pl, {}).get("status") == "done"
        if done and not req.force:
            problems.append(f"{PLATFORMS[pl]['label']}: already posted (tick 'post again' to repeat)")
        why = prov.problems(pl)
        if why:
            problems.append(why)
        jobs.append(Job(pl, video, title, text, paid=bool(camp)))
    if problems:                                               # nothing is posted if ANY platform fails validation
        raise ShareError(" | ".join(problems))
    results: dict[str, dict] = {}
    learned = False
    for job in jobs:
        try:
            r = prov.publish(job)
        except Exception as e:                                 # one platform failing must not stop or hide the others
            r = Result(False, "failed", f"{type(e).__name__}: {e}")
        if camp and r.status in ("done", "draft", "opened") and not r.paid_flag:
            r.paid_flag = campaigns.FLAG_HELP[job.platform]
        results[job.platform] = {"status": r.status, "message": r.message, "url": r.url, "ok": r.ok, "title": job.title, "text": job.text,
                                 **({"paid_flag": r.paid_flag, "campaign": camp["id"]} if camp else {})}
        record(store, req.clip_id, job.platform, r, prov.name, ex["file"], req.actor, camp["id"] if camp else None)
        if camp and r.status == "done":                      # a draft goes on the list when you mark it posted
            try:
                campaigns.add_post(camp, req.clip_id, job.platform, r.url, job.title)
            except Exception as e:                             # the post went out: a busy log file must not hide that
                print(f"could not add the post to the campaign list (add it with Mark as posted): {e}")
        if req.actor == "you" and r.status in ("done", "draft") and not learned:
            learned = True                                     # once per share, however many platforms
            taste.record(meta, 1, "posted")                    # you chose to post it: a Keep (never overrides an explicit Trash)
    if any(r["status"] in ("done", "draft") for r in results.values()):
        p2, m2 = store.find(req.clip_id)                       # with the new shares recorded
        if p2:
            if req.actor == "you":
                d = dict(m2.get("description") or {})
                t = next((req.texts.get(pl) or {} for pl in req.platforms if (req.texts.get(pl) or {}).get("title")), {})
                if t.get("title") and t["title"] != d.get("title"):
                    d.update(title=t["title"], title_src="typed")
                if t.get("body") is not None and t.get("body") != d.get("body") and t.get("body"):
                    d.update(body=t["body"], body_edited=True)
                style.record({**m2, "description": d}, "post", "good", "you", p2.parent)
            else:
                style.record(m2, "post", "none", "autopilot", p2.parent)       # its own output: never learned as yours
    return results


def record(store, clip_id: str, platform: str, r: Result, provider: str, file: str, actor: str = "you", campaign: str | None = None) -> None:
    with store.lock:
        p, m = store.find(clip_id)
        if not p:
            return
        m.setdefault("shares", {})[platform] = {"status": r.status, "message": r.message, "url": r.url, "provider": provider,
                                                "file": file, "actor": actor, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                **({"campaign": campaign, "paid_flag": r.paid_flag} if campaign else {})}
        from .review import _write_json
        _write_json(p, m)


def mark(store, clip_id: str, platform: str, status: str) -> bool:
    """You press Post yourself with the assistant, then tell the app: 'done' (or 'reset' to clear it)."""
    if platform not in PLATFORMS or status not in ("done", "reset"):
        return False
    with store.lock:
        p, m = store.find(clip_id)
        if not p:
            return False
        shares = m.setdefault("shares", {})
        if status == "reset":
            shares.pop(platform, None)
        else:
            cur = shares.get(platform, {})
            shares[platform] = {**cur, "status": "done", "provider": cur.get("provider", "manual"),
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "message": "marked as posted"}
        from .review import _write_json
        _write_json(p, m)
    if status == "done":
        taste.record(m, 1, "posted")                           # you posted it yourself: counts as a Keep
        sh = (m.get("shares") or {}).get(platform) or {}
        camp = campaigns.find(sh["campaign"]) if sh.get("campaign") else campaigns.for_channel(m.get("channel"))
        if camp and platform in camp["platforms"]:             # posted for a campaign: it goes on your To-submit list
            try:
                campaigns.add_post(camp, clip_id, platform, sh.get("url"), str((m.get("description") or {}).get("title", "")))
            except Exception as e:
                print(f"could not add the post to the campaign list: {e}")
    style.record(m, "mark" if status == "done" else "mark_reset", "good" if status == "done" else "none", "you", p.parent)
    return True
