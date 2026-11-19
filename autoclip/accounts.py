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
import re
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
    "google_tokeninfo": "https://oauth2.googleapis.com/tokeninfo",
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
GOOGLE_ID_SUFFIX = ".apps.googleusercontent.com"


class ApiError(RuntimeError):
    """A platform said no. The message is written for a human."""


# ---------------------------------------------------------------------------------------------------- tidy what was pasted
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"), None)      # zero-width characters and no-break spaces


def tidy(v) -> str:
    """What you pasted, cleaned: no spaces / line breaks inside (a wrapped token still works), no surrounding quotes."""
    s = str(v or "").translate(_INVISIBLE).strip().strip("\"'`")
    return re.sub(r"\s+", "", s)


def parse_google_client_json(text: str) -> dict:
    """The file Google lets you download under Credentials > OAuth client ID > Download JSON  ->  {client_id, client_secret}."""
    try:
        d = json.loads(text)
    except ValueError:
        raise ApiError("that is not a JSON file. Use the file Google gives you with Download JSON on the OAuth client page.") from None
    if not isinstance(d, dict):
        raise ApiError("that JSON file is not a Google OAuth client file")
    if d.get("type") == "service_account":
        raise ApiError("that is a service-account key. YouTube uploads need an OAuth client ID of type Desktop app instead.")
    inner = d.get("installed")
    if not isinstance(inner, dict):
        if isinstance(d.get("web"), dict):
            raise ApiError("that file belongs to a 'Web application' client, which cannot sign in here. In Google Cloud create a new "
                           "OAuth client ID and choose the type Desktop app, then download that file.")
        raise ApiError("that is not a Google OAuth client file (it should start with {\"installed\": ...})")
    cid, sec = tidy(inner.get("client_id")), tidy(inner.get("client_secret"))
    if not cid or not sec:
        raise ApiError("that file has no client ID or secret in it")
    return {"client_id": cid, "client_secret": sec}


def clean_credentials(platform: str, values: dict) -> dict:
    """Normalise whatever the Accounts window sent and reject the usual mix-ups with a sentence that says what to do.
    YouTube also accepts the downloaded client JSON (key 'client_json', or pasted into either field)."""
    v = {k: x for k, x in (values or {}).items() if isinstance(x, (str, int, float))}
    if platform == "youtube":
        blob = str(v.pop("client_json", "") or "")
        for k in ("client_id", "client_secret"):
            if str(v.get(k, "")).lstrip().startswith("{"):          # pasted the whole file into a field
                blob = str(v.pop(k))
        if blob.strip():
            v.update(parse_google_client_json(blob))
        cid, sec = tidy(v.get("client_id")), tidy(v.get("client_secret"))
        if sec.endswith(GOOGLE_ID_SUFFIX) and not cid.endswith(GOOGLE_ID_SUFFIX):      # the two boxes were filled in the wrong way round
            cid, sec = sec, cid
        if cid and not cid.endswith(GOOGLE_ID_SUFFIX):
            raise ApiError("the client ID should end with " + GOOGLE_ID_SUFFIX + ". You may have pasted the project ID or an API key; "
                           "copy the Client ID of your OAuth client (type Desktop app), or just import the downloaded JSON file.")
        if sec.startswith("AIza"):
            raise ApiError("that is an API key, not the client secret. Open your OAuth client (Credentials page) and copy its Client secret.")
        return {k: x for k, x in (("client_id", cid), ("client_secret", sec)) if x}
    if platform == "instagram":
        t = re.sub(r"^\s*(?:bearer|access_token\s*[=:])\s*", "", str(v.get("access_token", "")), flags=re.I)
        t = tidy(t)
        return {"access_token": t} if t else {}
    if platform == "tiktok":
        return {k: x for k in ("client_key", "client_secret") if (x := tidy(v.get(k)))}
    raise ApiError("unknown platform")


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


# ---------------------------------------------------------------------------------------------------- guided setup
# What the Accounts window shows. A step may carry a link to the exact console page ("link") and a value to copy ("copy": key into
# the dict setup_guide() resolves). The platforms require you to own a (free) developer app: that cannot be skipped, so the aim is that
# every step is one click away and every wrong turn has a plain-language fix.
_SETUP = {
    "youtube": {
        "minutes": 5,
        "steps": [
            {"text": "Create a Google Cloud project (any name; an existing one is fine).",
             "link": ("https://console.cloud.google.com/projectcreate", "Open Google Cloud")},
            {"text": "Turn on YouTube Data API v3 for it: open the page and press Enable.",
             "link": ("https://console.cloud.google.com/apis/library/youtube.googleapis.com", "Open the API page")},
            {"text": "Set up the consent screen: type External. Under Audience add your own Google account as a test user, then press "
                     "Publish app, so the login does not expire after 7 days. (Google says 'not verified' for your own app: that is expected.)",
             "link": ("https://console.cloud.google.com/apis/credentials/consent", "Open the consent screen")},
            {"text": "Credentials > Create credentials > OAuth client ID > Application type Desktop app. Press Download JSON on the "
                     "new client and drop that file into the box below (or copy the client ID and secret by hand).",
             "link": ("https://console.cloud.google.com/apis/credentials", "Open Credentials")},
            {"text": "Press Save and connect, choose your YouTube channel in the tab that opens and allow the upload permission."},
        ],
        "troubleshooting": [
            ("Google says 'Error 400: redirect_uri_mismatch'", "The OAuth client is the wrong type. Create a new one with type Desktop app (not Web application)."),
            ("Google says 'Access blocked' or 'access_denied'", "Your Google account is not allowed to use the app yet: add it under Audience > Test users, or press Publish app."),
            ("'Google hasn't verified this app'", "Normal for your own app. Press Advanced, then Go to (app name), then Allow."),
            ("It worked, then stopped after about a week", "The consent screen is still in Testing. Press Publish app (Audience page), then connect again."),
            ("'Google did not give a permanent token'", "Remove the app at myaccount.google.com/permissions and press Save and connect again."),
        ],
    },
    "instagram": {
        "minutes": 8,
        "steps": [
            {"text": "In the Instagram app switch the account to a Professional account (Settings > Account type and tools). "
                     "A Business account is the safest choice."},
            {"text": "Create a Meta developer app (type Business) and add the product Instagram with Instagram login.",
             "link": ("https://developers.facebook.com/apps/", "Open Meta for Developers")},
            {"text": "Add your Instagram account as an Instagram tester (App roles) and accept the invite in the Instagram app "
                     "(Settings > Website permissions > Tester invites)."},
            {"text": "In the app's Instagram settings press Generate token for your account, copy it and paste it below. "
                     "It is checked the moment you press Save and connect."},
        ],
        "troubleshooting": [
            ("'Instagram did not accept this token'", "Generate a new token and paste all of it. It must be the Instagram user token, not a Facebook one."),
            ("'personal Instagram account'", "Switch the account to Professional in the Instagram app first."),
            ("It worked, then stopped after two months", "Tokens last 60 days. The program renews them while you post; if you did not post for a long time, generate a new one."),
        ],
    },
    "tiktok": {
        "minutes": 8,
        "steps": [
            {"text": "Create an app and add the products Login Kit and Content Posting API.",
             "link": ("https://developers.tiktok.com/apps/", "Open TikTok for Developers")},
            {"text": "In Login Kit add this redirect address EXACTLY (it contains this page's port number):",
             "copy": ("redirect_uri", "Copy redirect address")},
            {"text": "Enable these scopes, and add your own TikTok account as a test user (while TikTok has not reviewed your app only "
                     "test users can sign in):",
             "copy": ("scopes", "Copy scopes")},
            {"text": "Paste the client key and secret below and press Save and connect. TikTok only allows private direct posts for apps "
                     "it has not audited, so Draft mode (the video waits in your TikTok inbox, you press Post) is the default."},
        ],
        "troubleshooting": [
            ("TikTok complains about the redirect_uri", "The address in Login Kit must match the one above exactly, including the port. "
                                                      "Start the review page with the same --port every time."),
            ("'scope not authorized' or an invalid_scope error", "Add both products (Login Kit and Content Posting API) and enable the scopes video.upload and video.publish."),
            ("Posts are only visible to me", "Normal until TikTok audits your app. Use Draft mode, or ask TikTok for the audit."),
        ],
    },
}


def setup_guide(port: int) -> dict:
    """The steps for every platform, with the values to copy filled in for this review page's port."""
    values = {"redirect_uri": redirect_uri(port), "scopes": TT_SCOPES}
    out = {}
    for pl, g in _SETUP.items():
        steps = []
        for s in g["steps"]:
            item = {"text": s["text"]}
            if s.get("link"):
                item["link"] = {"url": s["link"][0], "label": s["link"][1]}
            if s.get("copy"):
                item["copy"] = {"value": values[s["copy"][0]], "label": s["copy"][1]}
            steps.append(item)
        out[pl] = {"minutes": g["minutes"], "steps": steps,
                   "troubleshooting": [{"problem": p, "fix": f} for p, f in g["troubleshooting"]]}
    return out


_OAUTH_HELP = {
    "youtube": {
        "access_denied": "Google refused (or you pressed Cancel). If you did not cancel: add your Google account under Audience > Test users, "
                         "or press Publish app, then connect again.",
        "redirect_uri_mismatch": "The OAuth client must be of type Desktop app. Create a new one (not Web application) and import its JSON file.",
        "invalid_client": "Google does not recognise the client ID/secret. Import the JSON of your Desktop-app client again.",
        "invalid_grant": "The sign-in expired. Press Save and connect again.",
        "invalid_scope": "Turn on YouTube Data API v3 for the project, then connect again.",
    },
    "tiktok": {
        "access_denied": "TikTok refused (or you pressed Cancel). Add your TikTok account as a test user in the app settings, then connect again.",
        "invalid_scope": "Enable the scopes video.upload and video.publish (Login Kit and Content Posting API products) in the TikTok app.",
        "scope_not_authorized": "Enable the scopes video.upload and video.publish (Login Kit and Content Posting API products) in the TikTok app.",
        "redirect_uri": "The redirect address in the TikTok app does not match this page's address. Copy it again from the Accounts window.",
        "invalid_client": "TikTok does not recognise the client key/secret. Copy them again from the app page.",
    },
}


def explain_oauth_error(platform: str, code: str, description: str = "") -> str:
    """A sign-in problem as a sentence that says what to do. Falls back to the platform's own words."""
    hint = _OAUTH_HELP.get(platform, {}).get(code)
    if not hint:                                            # TikTok / Google describe some of these only in the free text
        for key, text in _OAUTH_HELP.get(platform, {}).items():
            if key in (description or "").lower():
                hint = text
                break
    label = {"youtube": "Google", "tiktok": "TikTok"}.get(platform, "The platform")
    detail = ": ".join(x for x in (code, description) if x)
    base = f"{label} did not complete the sign-in" + (f" ({detail})" if detail else "")
    return base + ". " + hint if hint else base + "."


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


def cancel_oauth(state: str) -> str:
    """The browser came back with an error instead of a code: forget that sign-in and say which platform it was for ('' if unknown)."""
    return (_PENDING.pop(state, None) or {}).get("platform", "")


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
        if st != 200:
            raise ApiError(explain_oauth_error("youtube", str(d.get("error", "")), str(d.get("error_description", ""))))
        if not d.get("refresh_token"):
            raise ApiError("Google did not give a permanent token: remove this app at myaccount.google.com/permissions and connect again")
        update("youtube", refresh_token=d["refresh_token"], access_token=d.get("access_token", ""),
               expires_at=time.time() + int(d.get("expires_in", 3000)) - 60, account="YouTube (connected)")
        return "YouTube is connected."
    if pl == "tiktok":
        st, _, raw = http("POST", ENDPOINTS["tt_api"] + "/oauth/token/", form={
            "client_key": a["client_key"], "client_secret": a["client_secret"], "code": code, "grant_type": "authorization_code",
            "redirect_uri": flow["redirect"]}, retries=2)
        d = as_json(raw)
        if st != 200 or not d.get("access_token"):
            raise ApiError(explain_oauth_error("tiktok", str(d.get("error", "")), str(d.get("error_description") or api_message(st, raw))))
        update("tiktok", access_token=d["access_token"], refresh_token=d.get("refresh_token", ""), open_id=d.get("open_id", ""),
               expires_at=time.time() + int(d.get("expires_in", 80000)) - 60, account="TikTok (connected)")
        try:
            name = _tiktok_name()                                    # show who is connected; never a reason to fail the sign-in
        except ApiError:
            name = ""
        return f"TikTok is connected{' as ' + name if name else ''}."
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


# ---------------------------------------------------------------------------------------------------- "does it still work?"
def _tiktok_name() -> str:
    """Display name of the connected TikTok account (needs only the user.info.basic scope); remembered as the account label."""
    st, _, raw = http("GET", ENDPOINTS["tt_api"] + "/user/info/?fields=display_name",
                      headers={"Authorization": f"Bearer {tiktok_token()}"}, retries=1)
    name = ((as_json(raw).get("data") or {}).get("user") or {}).get("display_name", "") if st == 200 else ""
    if name:
        update("tiktok", account=f"TikTok ({name})")
    return name


def verify(platform: str) -> dict:
    """Check a connected account for real, without posting anything. Raises ApiError with the fix if it no longer works."""
    if platform not in FIELDS:
        raise ApiError("unknown platform")
    if not connected(platform):
        raise ApiError("not connected yet")
    if platform == "youtube":
        token = youtube_token()                                       # refreshes the login: fails here if Google revoked it
        st, _, raw = http("GET", ENDPOINTS["google_tokeninfo"] + "?" + urllib.parse.urlencode({"access_token": token}), retries=1)
        scope = str(as_json(raw).get("scope", ""))
        if st == 200 and "youtube.upload" not in scope and "youtube" not in scope:
            raise ApiError("signed in, but the upload permission was not granted. Press Save and connect again and tick every box Google shows.")
        msg = "YouTube sign-in works and the upload permission is granted."
    elif platform == "tiktok":
        name = _tiktok_name()
        msg = "TikTok sign-in works" + (f" (account: {name})." if name else ".")
    else:
        st, _, raw = http("GET", ENDPOINTS["ig_graph"] + "/me?" + urllib.parse.urlencode(
            {"fields": "user_id,username", "access_token": instagram_token()}), retries=1)
        d = as_json(raw)
        if st != 200 or not (d.get("user_id") or d.get("id")):
            raise ApiError("Instagram no longer accepts the saved token (" + api_message(st, raw) + "). Generate a new token and paste it again.")
        if d.get("username"):
            update("instagram", username=d["username"], account="@" + d["username"])
        msg = "Instagram token works" + (f" (account: @{d['username']})." if d.get("username") else ".")
    return {"ok": True, "message": msg, "account": get(platform).get("account", "")}
