"""Automatic posting through the platforms' OFFICIAL APIs, using accounts you connected yourself (see accounts.py).

  YouTube    videos.insert, resumable upload   -> a Short on your channel
  Instagram  Reels container + resumable upload -> a Reel on your professional account
  TikTok     Content Posting API                -> a draft in your TikTok inbox (default) or a direct post

Each uploader gets exactly the file and text you confirmed in the review page. Platform quirks are reported honestly in the result
message (for example: YouTube keeps uploads from brand-new API projects private until Google audits the project).
"""
from __future__ import annotations

import time
import urllib.parse

from . import accounts as acc
from .accounts import ApiError, api_message, as_json, http
from .campaigns import FLAG_HELP
from .share import PLATFORMS, Job, Provider, Result

UPLOAD_TIMEOUT = 900
POLL_EVERY_S = 3.0           # the tests shrink this
POLL_MAX_S = 300.0


def _sleep(s: float) -> None:
    time.sleep(s)


# ---------------------------------------------------------------------------------------------------- YouTube
def youtube_upload(job: Job) -> Result:
    token = acc.youtube_token()
    a = acc.get("youtube")
    privacy = a.get("privacy", "public")
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    size = job.video.stat().st_size
    meta = {"snippet": {"title": job.title[:100], "description": job.text[:5000], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    auth = {"Authorization": f"Bearer {token}"}
    parts = "snippet,status"
    if job.paid:                                           # a paid campaign clip: declare the paid promotion
        parts += ",paidProductPlacementDetails"
        meta["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
    up_hdr = {**auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}
    st, hdr, raw = http("POST", acc.ENDPOINTS["yt_upload"] + "?uploadType=resumable&part=" + parts, headers=up_hdr, body=meta, retries=2)
    if job.paid and st == 400:                             # the paid field may not be accepted: never let it block the upload
        meta.pop("paidProductPlacementDetails")            # YouTube would not take the flag: upload anyway, you tick it in Studio
        st, hdr, raw = http("POST", acc.ENDPOINTS["yt_upload"] + "?uploadType=resumable&part=snippet,status", headers=up_hdr, body=meta, retries=2)
    if st != 200 or not hdr.get("location"):
        msg = api_message(st, raw)
        if "quota" in msg.lower():
            msg += ". YouTube allows only a handful of uploads per day for a new project; try again tomorrow."
        raise ApiError(msg)
    with open(job.video, "rb") as f:
        st, _, raw = http("PUT", hdr["location"], headers={**auth, "Content-Type": "video/mp4", "Content-Length": str(size)},
                          data=f, timeout=UPLOAD_TIMEOUT, retries=1)
    d = as_json(raw)
    if st not in (200, 201) or not d.get("id"):
        raise ApiError("YouTube upload failed: " + api_message(st, raw))
    vid = d["id"]
    final = (d.get("status") or {}).get("privacyStatus", privacy)
    flag = None
    if job.paid:                                           # "set" only when YouTube confirms it
        flag = "set" if (d.get("paidProductPlacementDetails") or {}).get("hasPaidProductPlacement") is True else FLAG_HELP["youtube"]
    if privacy == "public" and final != "public":
        return Result(True, "draft", "Uploaded, but YouTube kept it PRIVATE: videos from new API projects are locked private until Google audits "
                      "the project. Open YouTube Studio, edit this video and set it to Public (title and description are already filled in).",
                      f"https://studio.youtube.com/video/{vid}/edit", flag)
    return Result(True, "done", f"Published as a {final} Short.", f"https://www.youtube.com/shorts/{vid}", flag)


# ---------------------------------------------------------------------------------------------------- Instagram
def instagram_upload(job: Job) -> Result:
    token = acc.instagram_token()
    a = acc.get("instagram")
    g = acc.ENDPOINTS["ig_graph"]
    size = job.video.stat().st_size
    st, _, raw = http("POST", f"{g}/{a['user_id']}/media", form={
        "media_type": "REELS", "upload_type": "resumable", "caption": job.text[:2200], "share_to_feed": "true", "access_token": token}, retries=2)
    d = as_json(raw)
    if st != 200 or not d.get("id"):
        raise ApiError("Instagram refused to start the Reel: " + api_message(st, raw))
    container, uri = d["id"], d.get("uri")
    if not uri:
        raise ApiError("Instagram did not return an upload address (resumable upload may not be available for this app type)")
    with open(job.video, "rb") as f:
        st, _, raw = http("POST", uri, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                                                "Content-Length": str(size)}, data=f, timeout=UPLOAD_TIMEOUT, retries=1)
    if st != 200:
        raise ApiError("Instagram upload failed: " + api_message(st, raw))
    waited = 0.0
    while True:                                            # Instagram processes the video before it can be published
        st, _, raw = http("GET", f"{g}/{container}?" + urllib.parse.urlencode({"fields": "status_code,status", "access_token": token}), retries=2)
        d = as_json(raw)
        code = d.get("status_code", "")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED") or st >= 400:
            raise ApiError(f"Instagram could not process the video ({code or st}): {d.get('status') or api_message(st, raw)}")
        if waited > POLL_MAX_S:
            raise ApiError("Instagram is still processing the video after 5 minutes; check the Instagram app in a few minutes")
        _sleep(POLL_EVERY_S)
        waited += POLL_EVERY_S
    st, _, raw = http("POST", f"{g}/{a['user_id']}/media_publish", form={"creation_id": container, "access_token": token}, retries=1)
    d = as_json(raw)
    if st != 200 or not d.get("id"):
        raise ApiError("Instagram refused to publish: " + api_message(st, raw))
    link = None
    st, _, raw = http("GET", f"{g}/{d['id']}?" + urllib.parse.urlencode({"fields": "permalink", "access_token": token}), retries=1)
    if st == 200:
        link = as_json(raw).get("permalink")
    return Result(True, "done", "Reel published.", link)


# ---------------------------------------------------------------------------------------------------- TikTok
def _chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) as TikTok defines them: whole file in one chunk up to 64 MB, else floor(size / chunk)."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    chunk = 32 * 1024 * 1024
    return chunk, size // chunk


def tiktok_upload(job: Job) -> Result:
    token = acc.tiktok_token()
    a = acc.get("tiktok")
    mode = a.get("mode", "draft")
    api = acc.ENDPOINTS["tt_api"]
    size = job.video.stat().st_size
    chunk, total = _chunks(size)
    src = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    if mode == "direct":
        init_url = api + "/post/publish/video/init/"
        payload = {"post_info": {"title": job.text[:2200], "privacy_level": a.get("privacy", "SELF_ONLY"),
                                 "disable_duet": False, "disable_comment": False, "disable_stitch": False}, "source_info": src}
    else:
        init_url, payload = api + "/post/publish/inbox/video/init/", {"source_info": src}
    flag = None
    if job.paid:                                           # Content disclosure > Paid partnership: only a direct, non-private post can carry it
        if mode == "direct" and a.get("privacy", "SELF_ONLY") != "SELF_ONLY":
            payload["post_info"].update(brand_content_toggle=True, brand_organic_toggle=False)
            flag = "set"
        else:
            flag = FLAG_HELP["tiktok"] + (" (draft mode cannot carry it)" if mode != "direct" else " (not sent on private posts)")
    st, _, raw = http("POST", init_url, headers=auth, body=payload, retries=2)
    d = as_json(raw)
    err = d.get("error") or {}
    if st != 200 or err.get("code") not in (None, "ok") or not (d.get("data") or {}).get("upload_url"):
        msg = err.get("message") or api_message(st, raw)
        if err.get("code") == "unaudited_client_can_only_post_to_private_accounts":
            msg += " (TikTok only lets unaudited apps post privately; use draft mode, or ask TikTok to audit your app)"
        raise ApiError("TikTok refused the upload: " + msg)
    upload_url, pid = d["data"]["upload_url"], d["data"]["publish_id"]
    with open(job.video, "rb") as f:
        for i in range(total):
            lo = i * chunk
            hi = size - 1 if i == total - 1 else lo + chunk - 1
            part = f.read(hi - lo + 1)
            st, _, raw = http("PUT", upload_url, headers={"Content-Type": "video/mp4", "Content-Length": str(len(part)),
                                                          "Content-Range": f"bytes {lo}-{hi}/{size}"}, data=part, timeout=UPLOAD_TIMEOUT, retries=2)
            if st not in (200, 201, 206):
                raise ApiError("TikTok upload failed: " + api_message(st, raw))
    waited = 0.0
    while True:
        st, _, raw = http("POST", api + "/post/publish/status/fetch/", headers=auth, body={"publish_id": pid}, retries=2)
        data = as_json(raw).get("data") or {}
        state = data.get("status", "")
        if state == "SEND_TO_USER_INBOX":
            return Result(True, "draft", "Uploaded to your TikTok inbox. Open the TikTok app: the video is waiting in your notifications. "
                          "Write the caption (it is on your clipboard) and press Post.", paid_flag=flag)
        if state == "PUBLISH_COMPLETE":
            vis = a.get("privacy", "SELF_ONLY")
            return Result(True, "done" if vis != "SELF_ONLY" else "draft",
                          "Posted on TikTok." if vis != "SELF_ONLY" else "Posted, but only visible to you (TikTok restricts unaudited apps). "
                          "Open it in TikTok and change who can watch.", paid_flag=flag)
        if state == "FAILED":
            raise ApiError("TikTok could not process the video: " + data.get("fail_reason", "unknown reason"))
        if waited > POLL_MAX_S:
            raise ApiError("TikTok is still processing the video after 5 minutes; check the TikTok app in a few minutes")
        _sleep(POLL_EVERY_S)
        waited += POLL_EVERY_S


UPLOADERS = {"youtube": youtube_upload, "instagram": instagram_upload, "tiktok": tiktok_upload}


class DirectProvider(Provider):
    name, label, automatic = "direct", "Post directly to my connected accounts", True

    def configured(self) -> bool:
        return any(acc.connected(p) for p in PLATFORMS)

    def problems(self, platform: str) -> str | None:
        if not acc.connected(platform):
            return f"{PLATFORMS[platform]['label']} is not connected (open Setup, Connect accounts at the top of the page)"
        return None

    def publish(self, job: Job) -> Result:
        try:
            return UPLOADERS[job.platform](job)
        except ApiError as e:
            return Result(False, "failed", str(e))


from . import share as _share          # noqa: E402  (registered last: share.py must not import this module)
_share.PROVIDERS["direct"] = DirectProvider()
