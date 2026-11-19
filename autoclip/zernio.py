"""Quick connect: post to Instagram and TikTok through Zernio, with ONE API key and no developer apps.

Zernio (zernio.com) holds the platform approvals; you only connect your accounts in its dashboard and paste a key here. Its free plan
covers the first 2 connected accounts with unlimited posts, which is why only Instagram + TikTok go this way and YouTube keeps using
the direct method (accounts.py / publish.py). Nothing here posts anything by itself: the review page asks you to confirm every share.

The `auto` sharing method picks, per platform, Zernio when that platform is connected there, else your direct account.
"""
from __future__ import annotations

from . import accounts as acc
from . import publish
from .accounts import ApiError, api_message, as_json, http
from .share import PLATFORMS, Job, Provider, Result

acc.ENDPOINTS.setdefault("zernio", "https://zernio.com/api/v1")      # tests point this at a fake server
KEY = "zernio"
FREE_ACCOUNTS = 2
TIKTOK_MODES = ("public", "draft")
ROUTED = ("instagram", "tiktok", "youtube")                         # what we look for among the Zernio accounts
DONE = {"published", "success", "succeeded", "sent", "completed", "live"}
FAILED = {"failed", "error", "partial", "rejected", "cancelled", "canceled"}


def config() -> dict:
    return acc.get(KEY)


def saved() -> bool:
    return bool(config().get("api_key"))


def covers(platform: str) -> bool:
    """Is this platform connected inside the saved Zernio account?"""
    return saved() and platform in (config().get("accounts") or {})


def _pid(v) -> str:
    """Zernio sends a profile as an object ({"_id": ..., "name": ...}) in some replies and as a plain id in others; posts want the id."""
    if isinstance(v, dict):
        v = v.get("_id") or v.get("id")
    return v if isinstance(v, str) else ""


def _auth(key: str | None = None) -> dict:
    return {"Authorization": f"Bearer {key or config().get('api_key', '')}"}


def summary() -> dict:
    """What the page may see. Never the key."""
    c = config()
    accounts = {pl: {"name": a.get("name", "")} for pl, a in (c.get("accounts") or {}).items()}
    total = int(c.get("total", len(accounts)) or 0)
    warn = (f"You have {total} accounts connected in Zernio. The free plan covers the first {FREE_ACCOUNTS}; each extra one costs $6 a month. "
            "Disconnect the ones you do not post to in the Zernio dashboard.") if total > FREE_ACCOUNTS else ""
    return {"saved": saved(), "accounts": accounts, "tiktok_mode": c.get("tiktok_mode", "public"), "warning": warn}


def _sync(key: str) -> dict:
    """Ask Zernio which accounts this key can post to, keep the first active one per platform."""
    st, _, raw = http("GET", acc.ENDPOINTS["zernio"] + "/accounts", headers=_auth(key), retries=2)
    if st in (401, 403):
        raise ApiError("Zernio did not accept this key. Copy it again from Zernio (the full text, nothing before or after) and paste it here.")
    d = as_json(raw)
    if st != 200 or not isinstance(d.get("accounts"), list):
        raise ApiError("Zernio gave an unexpected answer: " + api_message(st, raw))
    found: dict[str, dict] = {}
    active = [a for a in d["accounts"] if isinstance(a, dict) and a.get("isActive", True)]
    for a in active:
        pl = str(a.get("platform", "")).lower()
        if pl in ROUTED and pl not in found and a.get("_id"):
            found[pl] = {"id": a["_id"], "name": a.get("username") or a.get("displayName") or "", "profileId": _pid(a.get("profileId"))}
    return {"accounts": found, "total": len(active)}


def connect(key: str, tiktok_mode: str | None = None) -> dict:
    """Check the pasted key with Zernio, remember the Instagram / TikTok accounts it can post to. Posts nothing."""
    key = acc.tidy(key)
    if len(key) < 8:
        raise ApiError("paste the API key you created in Zernio")
    found = _sync(key)
    if not found["accounts"]:
        raise ApiError("The key works, but no Instagram, TikTok or YouTube account is connected in Zernio yet. "
                       "Connect them in the Zernio dashboard (step 2), then press the button again.")
    extra = {"tiktok_mode": tiktok_mode} if tiktok_mode in TIKTOK_MODES else {}
    acc.update(KEY, api_key=key, accounts=found["accounts"], total=found["total"], **extra)
    return summary()


def refresh() -> dict:
    """Same check with the saved key (after connecting more accounts in Zernio)."""
    if not saved():
        raise ApiError("not connected yet")
    return connect(config()["api_key"])


def set_options(tiktok_mode: str) -> dict:
    if tiktok_mode not in TIKTOK_MODES:
        raise ApiError("unknown TikTok mode")
    acc.update(KEY, tiktok_mode=tiktok_mode)
    return summary()


def disconnect() -> None:
    acc.clear(KEY)


# ---------------------------------------------------------------------------------------------------- posting
def _outcome(d: dict, platform: str) -> tuple[str, str | None, str]:
    """('done' | 'failed' | 'pending', link, message) from a Zernio post record, however it is wrapped."""
    post = d.get("post") if isinstance(d.get("post"), dict) else d
    mine = next((p for p in (post.get("platforms") or []) if isinstance(p, dict) and str(p.get("platform", "")).lower() == platform), {})
    state = str(mine.get("status") or post.get("status") or "").lower()
    link = mine.get("platformPostUrl") or mine.get("postUrl") or mine.get("url")
    err = mine.get("error") or mine.get("errorMessage") or post.get("error") or ""
    if isinstance(err, dict):
        err = err.get("message") or ""
    if state in DONE:
        return "done", link, ""
    if state in FAILED:
        return "failed", link, str(err) or f"Zernio reports the post as {state}"
    return "pending", link, str(err).strip()                                  # e.g. "queued until tomorrow: daily limit"


def zernio_upload(job: Job) -> Result:
    c = config()
    acct = (c.get("accounts") or {}).get(job.platform)
    if not acct:
        raise ApiError(f"{PLATFORMS[job.platform]['label']} is not connected in Zernio")
    base, auth = acc.ENDPOINTS["zernio"], _auth()
    size = job.video.stat().st_size
    st, _, raw = http("POST", base + "/media/presign", headers=auth, body={"filename": job.video.name, "contentType": "video/mp4"}, retries=2)
    up = as_json(raw)
    if st >= 300 or not up.get("uploadUrl") or not up.get("publicUrl"):
        raise ApiError("Zernio would not take the video: " + api_message(st, raw))
    with open(job.video, "rb") as f:
        st, _, raw = http("PUT", up["uploadUrl"], headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                          data=f, timeout=publish.UPLOAD_TIMEOUT, retries=1)
    if st not in (200, 201, 204):
        raise ApiError("Zernio upload failed: " + api_message(st, raw))
    body: dict = {"content": job.text, "title": job.title[:100],
                  "mediaItems": [{"type": "video", "url": up["publicUrl"], "filename": job.video.name, "size": size, "mimeType": "video/mp4"}],
                  "platforms": [{"platform": job.platform, "accountId": acct["id"]}], "publishNow": True}
    if _pid(acct.get("profileId")):                                    # also repairs accounts saved by an earlier version
        body["profileId"] = _pid(acct["profileId"])
    draft = job.platform == "tiktok" and c.get("tiktok_mode") == "draft"
    if job.platform == "tiktok":
        body["tiktokSettings"] = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True, "allow_duet": True, "allow_stitch": True,
                                  "commercial_content_type": "none", "content_preview_confirmed": True, "express_consent_given": True,
                                  "media_type": "video", "auto_add_music": False, "video_made_with_ai": False, "draft": draft}
    st, _, raw = http("POST", base + "/posts", headers=auth, body=body, retries=0)    # never retried: a repeat could post twice
    d = as_json(raw)
    if st >= 300:
        raise ApiError("Zernio refused the post: " + api_message(st, raw))
    post = d.get("post") if isinstance(d.get("post"), dict) else d
    pid = post.get("_id") or post.get("id")
    waited = 0.0
    while True:
        state, link, msg = _outcome(d, job.platform)
        if state == "failed":
            raise ApiError("Zernio could not publish it: " + msg)
        if state == "done" or (draft and state == "pending" and waited > 0):      # a draft has no "published" state to wait for
            if draft:
                return Result(True, "draft", "Sent to your TikTok drafts through Zernio. Open TikTok, check it and press Post.", link)
            return Result(True, "done", "Published through Zernio.", link)
        if not pid or waited > publish.POLL_MAX_S:
            if msg:
                return Result(True, "draft", "Sent to Zernio, which is still publishing it: " + msg, link)
            return Result(True, "draft", "Sent to Zernio, which is still publishing it. Check the Zernio dashboard in a few minutes.", link)
        publish._sleep(publish.POLL_EVERY_S)
        waited += publish.POLL_EVERY_S
        st, _, raw = http("GET", f"{base}/posts/{pid}", headers=auth, retries=2)
        if st == 200:
            d = as_json(raw)


class AutoProvider(Provider):
    """Per platform: Zernio if the platform is connected there, otherwise the direct account (YouTube, normally)."""
    name, label, automatic = "auto", "Post automatically (Quick connect + my own accounts)", True

    def configured(self) -> bool:
        return any(covers(p) or acc.connected(p) for p in PLATFORMS)

    def problems(self, platform: str) -> str | None:
        if covers(platform) or acc.connected(platform):
            return None
        how = "⚡ Quick connect" if platform != "youtube" else "🔗 Accounts"
        return f"{PLATFORMS[platform]['label']} is not connected (open {how} at the top of the page)"

    def publish(self, job: Job) -> Result:
        try:
            if covers(job.platform):
                r = zernio_upload(job)
                return Result(r.ok, r.status, r.message, r.url)
            return publish.UPLOADERS[job.platform](job)
        except ApiError as e:
            return Result(False, "failed", str(e))


from . import share as _share          # noqa: E402  (registered last: share.py must not import this module)
_share.PROVIDERS["auto"] = AutoProvider()
