"""Your own social accounts: where the login tokens live, the browser sign-in (OAuth) flow, and a small HTTP helper.

Everything stays on your PC. Tokens are kept in `accounts.json` in the settings folder (next to settings.json), readable only by
you where the OS supports it, and are NEVER sent to the review page or written to the log. Only the platforms' official APIs are used,
with credentials from developer apps that YOU create on your own accounts (free). Nothing here posts anything: see publish.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import settings

# Overridable so the tests can talk to a fake local server instead of the real internet.
ENDPOINTS = {
    "google_auth": "https://accounts.google.com/o/oauth2/v2/auth",
    "google_token": "https://oauth2.googleapis.com/token",
    "yt_upload": "https://www.googleapis.com/upload/youtube/v3/videos",
    "ig_graph": "https://graph.instagram.com/v21.0",
    "ig_refresh": "https://graph.instagram.com/refresh_access_token",
    "tt_auth": "https://www.tiktok.com/v2/auth/authorize/",
    "tt_api": "https://open.tiktokapis.com/v2",
}
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TT_SCOPES = "user.info.basic,video.upload,video.publish"
FIELDS = {                      # what the user must give us, per platform (secret ones are never shown again)
    "youtube": [("client_id", "OAuth client ID", False), ("client_secret", "OAuth client secret", True)],
    "instagram": [("access_token", "Instagram access token", True)],
    "tiktok": [("client_key", "Client key", False), ("client_secret", "Client secret", True)],
}
_LOCK = threading.Lock()


class ApiError(RuntimeError):
    """A platform said no. The message is written for a human."""


def path() -> Path:
    return settings.config_dir() / "accounts.json"


def load() -> dict:
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)                       # no effect on Windows: the file lives in your own %APPDATA% profile
    except OSError:
        pass
    os.replace(tmp, p)


def get(platform: str) -> dict:
    return dict(load().get(platform) or {})


def update(platform: str, **values) -> dict:
    with _LOCK:
        data = load()
        cur = dict(data.get(platform) or {})
        cur.update({k: v for k, v in values.items()})
        data[platform] = cur
        _write(data)
        return cur


def clear(platform: str) -> None:
    with _LOCK:
        data = load()
        data.pop(platform, None)
        _write(data)


def connected(platform: str) -> bool:
    a = get(platform)
    if platform == "youtube":
        return bool(a.get("refresh_token") and a.get("client_id") and a.get("client_secret"))
    if platform == "instagram":
        return bool(a.get("access_token") and a.get("user_id"))
    if platform == "tiktok":
        return bool(a.get("refresh_token") and a.get("client_key"))
    return False


def status() -> dict:
    """What the page may see: who is connected and which fields are filled in. Never a secret value."""
    out = {}
    for pl, fields in FIELDS.items():
        a = get(pl)
        options = {}
        if pl == "youtube":
            options["privacy"] = a.get("privacy", "public")
        if pl == "tiktok":
            options.update(mode=a.get("mode", "draft"), privacy=a.get("privacy", "SELF_ONLY"))
        out[pl] = {"connected": connected(pl), "account": a.get("account", ""), "have": [k for k, _, _ in fields if a.get(k)],
                   "fields": [{"key": k, "label": lab, "secret": sec} for k, lab, sec in fields], "options": options}
    return out


# ---------------------------------------------------------------------------------------------------- http
def http(method: str, url: str, *, headers: dict | None = None, data=None, form: dict | None = None, body: dict | None = None,
         timeout: float = 60, retries: int = 0) -> tuple[int, dict, bytes]:
    """(status, lower-cased headers, body). HTTP error codes are returned, not raised; network failures retry then raise ApiError."""
    h = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json; charset=UTF-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            if hasattr(data, "seek"):
                data.seek(0)
            time.sleep(min(2 ** attempt, 8))
    raise ApiError(f"could not reach {urllib.parse.urlparse(url).hostname}: {last}")


def as_json(raw: bytes) -> dict:
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def api_message(status: int, raw: bytes) -> str:
    """The most useful sentence out of a Google / Meta / TikTok error body."""
    d = as_json(raw)
    err = d.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("error_user_msg") or err.get("code") or ""
        reason = (err.get("errors") or [{}])[0].get("reason", "") if isinstance(err.get("errors"), list) else ""
        return f"HTTP {status}: {msg} {('(' + reason + ')') if reason else ''}".strip()
    if isinstance(err, str):
        return f"HTTP {status}: {err}: {d.get('error_description', '')}".strip(": ")
    return f"HTTP {status}: {raw[:200].decode('utf-8', 'replace')}"


# ---------------------------------------------------------------------------------------------------- OAuth
_PENDING: dict[str, dict] = {}             # state -> {platform, verifier, redirect, created}


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}/oauth/callback"


def begin_oauth(platform: str, port: int) -> str:
    """The sign-in URL to open in the browser. Needs the client id/secret to be saved already."""
    a = get(platform)
    redirect = redirect_uri(port)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)[:96]
    for k in [k for k, v in _PENDING.items() if time.time() - v["created"] > 900]:
        _PENDING.pop(k, None)
    _PENDING[state] = {"platform": platform, "verifier": verifier, "redirect": redirect, "created": time.time()}
    if platform == "youtube":
        if not (a.get("client_id") and a.get("client_secret")):
            raise ApiError("save the OAuth client ID and secret first")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"client_id": a["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": YT_SCOPE, "state": state,
             "access_type": "offline", "prompt": "consent", "code_challenge": challenge, "code_challenge_method": "S256"}
        return ENDPOINTS["google_auth"] + "?" + urllib.parse.urlencode(q)
    if platform == "tiktok":
        if not (a.get("client_key") and a.get("client_secret")):
            raise ApiError("save the client key and secret first")
        q = {"client_key": a["client_key"], "scope": TT_SCOPES, "response_type": "code", "redirect_uri": redirect, "state": state}
        return ENDPOINTS["tt_auth"] + "?" + urllib.parse.urlencode(q)
    raise ApiError(f"{platform} does not use a browser sign-in")


def finish_oauth(state: str, code: str) -> str:
    """Called by the review server when the browser comes back. Returns a human message; raises ApiError on failure."""
    flow = _PENDING.pop(state, None)
    if not flow or time.time() - flow["created"] > 900:
        raise ApiError("this sign-in link expired or was not started here. Press Connect again.")
    pl, a = flow["platform"], get(flow["platform"])
    if pl == "youtube":
        st, _, raw = http("POST", ENDPOINTS["google_token"], form={
            "code": code, "client_id": a["client_id"], "client_secret": a["client_secret"], "redirect_uri": flow["redirect"],
            "grant_type": "authorization_code", "code_verifier": flow["verifier"]}, retries=2)
        d = as_json(raw)
        if st != 200 or not d.get("refresh_token"):
            raise ApiError("Google did not give a permanent token: " + (api_message(st, raw) if st != 200 else
                           "remove this app at myaccount.google.com/permissions and connect again"))
        update("youtube", refresh_token=d["refresh_token"], access_token=d.get("access_token", ""),
               expires_at=time.time() + int(d.get("expires_in", 3000)) - 60, account="YouTube (connected)")
        return "YouTube is connected."
    if pl == "tiktok":
        st, _, raw = http("POST", ENDPOINTS["tt_api"] + "/oauth/token/", form={
            "client_key": a["client_key"], "client_secret": a["client_secret"], "code": code, "grant_type": "authorization_code",
            "redirect_uri": flow["redirect"]}, retries=2)
        d = as_json(raw)
        if st != 200 or not d.get("access_token"):
            raise ApiError("TikTok refused the sign-in: " + (d.get("error_description") or api_message(st, raw)))
        update("tiktok", access_token=d["access_token"], refresh_token=d.get("refresh_token", ""), open_id=d.get("open_id", ""),
               expires_at=time.time() + int(d.get("expires_in", 80000)) - 60, account="TikTok (connected)")
        return "TikTok is connected."
    raise ApiError("unknown platform")


# ---------------------------------------------------------------------------------------------------- access tokens
def youtube_token() -> str:
    a = get("youtube")
    if a.get("access_token") and a.get("expires_at", 0) > time.time() + 30:
        return a["access_token"]
    st, _, raw = http("POST", ENDPOINTS["google_token"], form={
        "client_id": a.get("client_id", ""), "client_secret": a.get("client_secret", ""),
        "refresh_token": a.get("refresh_token", ""), "grant_type": "refresh_token"}, retries=2)
    d = as_json(raw)
    if st != 200 or not d.get("access_token"):
        hint = " Google test-mode apps lose access after 7 days: set the consent screen to 'In production' and connect again." \
            if d.get("error") == "invalid_grant" else ""
        raise ApiError("YouTube sign-in no longer works (" + api_message(st, raw) + ")." + hint)
    update("youtube", access_token=d["access_token"], expires_at=time.time() + int(d.get("expires_in", 3000)) - 60)
    return d["access_token"]


def tiktok_token() -> str:
    a = get("tiktok")
    if a.get("access_token") and a.get("expires_at", 0) > time.time() + 30:
        return a["access_token"]
    st, _, raw = http("POST", ENDPOINTS["tt_api"] + "/oauth/token/", form={
        "client_key": a.get("client_key", ""), "client_secret": a.get("client_secret", ""),
        "grant_type": "refresh_token", "refresh_token": a.get("refresh_token", "")}, retries=2)
    d = as_json(raw)
    if st != 200 or not d.get("access_token"):
        raise ApiError("TikTok sign-in no longer works (" + (d.get("error_description") or api_message(st, raw)) + "). Connect TikTok again.")
    update("tiktok", access_token=d["access_token"], refresh_token=d.get("refresh_token", a.get("refresh_token", "")),
           expires_at=time.time() + int(d.get("expires_in", 80000)) - 60)
    return d["access_token"]


def connect_instagram(token: str) -> str:
    """Check a pasted Instagram token, remember it with the account's id and username. Returns the username."""
    token = token.strip()
    if len(token) < 20:
        raise ApiError("that does not look like an access token")
    st, _, raw = http("GET", ENDPOINTS["ig_graph"] + "/me?" + urllib.parse.urlencode({"fields": "user_id,username,account_type", "access_token": token}), retries=2)
    d = as_json(raw)
    if st != 200 or not (d.get("user_id") or d.get("id")):
        raise ApiError("Instagram did not accept this token: " + api_message(st, raw))
    if d.get("account_type") == "PERSONAL":
        raise ApiError("this is a personal Instagram account. Switch it to a Professional (Creator or Business) account in the Instagram app first.")
    update("instagram", access_token=token, user_id=str(d.get("user_id") or d.get("id")), username=d.get("username", ""),
           account="@" + d.get("username", "instagram"), saved_at=time.time())
    return d.get("username", "")


def instagram_token() -> str:
    """The saved token, refreshed first if it is getting old (long-lived tokens last 60 days; refreshing resets the clock)."""
    a = get("instagram")
    if time.time() - float(a.get("saved_at", time.time())) > 30 * 86400:
        st, _, raw = http("GET", ENDPOINTS["ig_refresh"] + "?" + urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": a["access_token"]}), retries=1)
        d = as_json(raw)
        if st == 200 and d.get("access_token"):
            update("instagram", access_token=d["access_token"], saved_at=time.time())
            return d["access_token"]
    return a["access_token"]
