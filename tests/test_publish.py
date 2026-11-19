"""Direct posting, tested against a FAKE local server that imitates the YouTube, Instagram and TikTok APIs.
Nothing here touches the real internet or posts anything."""
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autoclip import accounts, publish, share
from autoclip.config import Config
from test_share_settings import make_root, server


class Fake:
    """State + behaviour switches for the fake platforms."""
    yt_privacy = "public"
    ig_polls = 2
    ig_account_type = "BUSINESS"
    tt_state = "SEND_TO_USER_INBOX"
    yt_scope = "https://www.googleapis.com/auth/youtube.upload"
    calls: list


FAKE = Fake()
BASE = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        FAKE.calls.append(("GET", u.path))
        if u.path.endswith("/me"):
            if q.get("access_token") != "IGTOKEN" + "x" * 20:
                return self.reply(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})
            return self.reply(200, {"user_id": "1789", "username": "streamclips", "account_type": FAKE.ig_account_type})
        if u.path == "/google/tokeninfo":
            return self.reply(200, {"scope": FAKE.yt_scope, "expires_in": "3000"}) if q.get("access_token", "").startswith("ya29.") \
                else self.reply(400, {"error": "invalid_token"})
        if u.path == "/tt/user/info/":
            if not self.headers.get("Authorization", "").startswith("Bearer act."):
                return self.reply(401, {"error": {"code": "access_token_invalid"}})
            return self.reply(200, {"data": {"user": {"display_name": "Stream Clips"}}, "error": {"code": "ok"}})
        if u.path.endswith("/refresh_access_token"):
            return self.reply(200, {"access_token": "IGTOKEN" + "y" * 20, "expires_in": 5184000})
        if u.path.endswith("/CONTAINER1"):
            FAKE.ig_polls -= 1
            return self.reply(200, {"status_code": "FINISHED" if FAKE.ig_polls <= 0 else "IN_PROGRESS"})
        if u.path.endswith("/MEDIA9"):
            return self.reply(200, {"permalink": "https://instagram.com/reel/abc"})
        self.reply(404, {"error": {"message": "nope"}})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        raw = self.body()
        FAKE.calls.append(("POST", u.path))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("latin1")).items()} if "x-www-form" in (self.headers.get("Content-Type") or "") else {}
        # ---- Google / YouTube
        if u.path == "/google/token":
            if form.get("grant_type") == "authorization_code":
                assert form["code_verifier"] and form["code"] == "GOODCODE"
                return self.reply(200, {"access_token": "ya29.one", "refresh_token": "1//refresh", "expires_in": 3599})
            return self.reply(200, {"access_token": "ya29.two", "expires_in": 3599}) if form.get("refresh_token") == "1//refresh" \
                else self.reply(400, {"error": "invalid_grant"})
        if u.path == "/yt/upload":
            assert self.headers["Authorization"].startswith("Bearer ya29.")
            snippet = json.loads(raw)
            FAKE.yt_meta = snippet
            return self.reply(200, {}, {"Location": f"{BASE['url']}/yt/put/1"})
        # ---- Instagram
        if re.fullmatch(r"/ig/1789/media", u.path):
            assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
            FAKE.ig_caption = form["caption"]
            return self.reply(200, {"id": "CONTAINER1", "uri": f"{BASE['url']}/rupload/CONTAINER1"})
        if u.path == "/rupload/CONTAINER1":
            assert self.headers["Authorization"] == "OAuth IGTOKEN" + "x" * 20 and self.headers["offset"] == "0"
            assert int(self.headers["file_size"]) == len(raw) > 0
            return self.reply(200, {"success": True})
        if u.path == "/ig/1789/media_publish":
            return self.reply(200, {"id": "MEDIA9"})
        # ---- TikTok
        if u.path == "/tt/oauth/token/":
            if form.get("grant_type") == "authorization_code":
                return self.reply(200, {"access_token": "act.1", "refresh_token": "rft.1", "open_id": "o1", "expires_in": 86400})
            return self.reply(200, {"access_token": "act.2", "refresh_token": "rft.2", "expires_in": 86400})
        if u.path in ("/tt/post/publish/inbox/video/init/", "/tt/post/publish/video/init/"):
            body = json.loads(raw)
            FAKE.tt_init = (u.path, body)
            return self.reply(200, {"data": {"publish_id": "p1", "upload_url": f"{BASE['url']}/tt/put/1"}, "error": {"code": "ok"}})
        if u.path == "/tt/post/publish/status/fetch/":
            return self.reply(200, {"data": {"status": FAKE.tt_state, "fail_reason": "bad codec"}, "error": {"code": "ok"}})
        self.reply(404, {"error": {"message": "unknown " + u.path}})

    def do_PUT(self):
        raw = self.body()
        FAKE.calls.append(("PUT", self.path))
        if self.path == "/yt/put/1":
            assert int(self.headers["Content-Length"]) == len(raw) > 0
            return self.reply(200, {"id": "VID123", "status": {"privacyStatus": FAKE.yt_privacy}})
        if self.path == "/tt/put/1":
            assert self.headers["Content-Range"] == f"bytes 0-{len(raw) - 1}/{len(raw)}"
            return self.reply(201, {})
        self.reply(404)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    FAKE.yt_privacy, FAKE.ig_polls, FAKE.ig_account_type, FAKE.tt_state, FAKE.calls = "public", 2, "BUSINESS", "SEND_TO_USER_INBOX", []
    FAKE.yt_scope = "https://www.googleapis.com/auth/youtube.upload"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    for k, v in {"google_token": "/google/token", "yt_upload": "/yt/upload", "ig_graph": "/ig", "ig_refresh": "/ig/refresh_access_token",
                 "tt_api": "/tt", "google_auth": "/google/auth", "tt_auth": "/tt/auth", "google_tokeninfo": "/google/tokeninfo"}.items():
        monkeypatch.setitem(accounts.ENDPOINTS, k, BASE["url"] + v)
    monkeypatch.setattr(publish, "POLL_EVERY_S", 0.01)
    yield tmp_path
    srv.shutdown()


def connect_all():
    accounts.update("youtube", client_id="cid", client_secret="sec", refresh_token="1//refresh", account="YouTube")
    accounts.connect_instagram("IGTOKEN" + "x" * 20)
    accounts.update("tiktok", client_key="ck", client_secret="cs", refresh_token="rft.1", account="TikTok")


def job(platform, tmp_path, text="hello #fyp"):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"fakevideo" * 1000)
    return share.Job(platform, f, "Great title", text)


# ---------------------------------------------------------------------------------------------- tests
def test_youtube_public_short(world):
    connect_all()
    r = publish.youtube_upload(job("youtube", world))
    assert r.status == "done" and r.url == "https://www.youtube.com/shorts/VID123"
    assert FAKE.yt_meta["snippet"]["title"] == "Great title" and FAKE.yt_meta["status"]["privacyStatus"] == "public"
    assert FAKE.yt_meta["status"]["selfDeclaredMadeForKids"] is False


def test_youtube_locked_private_is_reported_honestly(world):
    connect_all()
    FAKE.yt_privacy = "private"
    r = publish.youtube_upload(job("youtube", world))
    assert r.status == "draft" and "PRIVATE" in r.message and "studio.youtube.com/video/VID123" in r.url


def test_youtube_token_is_refreshed_and_cached(world):
    connect_all()
    assert accounts.youtube_token() == "ya29.two"
    n = len(FAKE.calls)
    assert accounts.youtube_token() == "ya29.two" and len(FAKE.calls) == n          # cached: no second request
    accounts.update("youtube", refresh_token="revoked", access_token="", expires_at=0)
    with pytest.raises(accounts.ApiError, match="In production"):
        accounts.youtube_token()


def test_youtube_oauth_roundtrip(world):
    accounts.update("youtube", client_id="cid", client_secret="sec")
    url = accounts.begin_oauth("youtube", 8765)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/oauth/callback"] and q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"] and "youtube.upload" in q["scope"][0]
    with pytest.raises(accounts.ApiError, match="expired or was not started"):
        accounts.finish_oauth("forged-state", "GOODCODE")                            # a made-up state is rejected
    assert "connected" in accounts.finish_oauth(q["state"][0], "GOODCODE")
    assert accounts.connected("youtube") and accounts.get("youtube")["refresh_token"] == "1//refresh"
    with pytest.raises(accounts.ApiError):
        accounts.finish_oauth(q["state"][0], "GOODCODE")                             # a state works once only


def test_instagram_reel_flow(world):
    connect_all()
    assert accounts.get("instagram")["username"] == "streamclips"
    r = publish.instagram_upload(job("instagram", world, "caption text #reels"))
    assert r.status == "done" and r.url == "https://instagram.com/reel/abc"
    assert FAKE.ig_caption == "caption text #reels"
    assert [c for c in FAKE.calls if c == ("GET", "/ig/CONTAINER1")].__len__() == 2      # waited while IN_PROGRESS, then FINISHED


def test_instagram_rejects_bad_and_personal_accounts(world):
    with pytest.raises(accounts.ApiError, match="did not accept"):
        accounts.connect_instagram("wrongtoken" + "z" * 20)
    FAKE.ig_account_type = "PERSONAL"
    with pytest.raises(accounts.ApiError, match="Professional"):
        accounts.connect_instagram("IGTOKEN" + "x" * 20)
    assert not accounts.connected("instagram")


def test_instagram_old_token_is_refreshed(world):
    connect_all()
    accounts.update("instagram", saved_at=1.0)                                       # saved in 1970: very old
    assert accounts.instagram_token().startswith("IGTOKENy")


def test_tiktok_draft_and_direct(world):
    connect_all()
    r = publish.tiktok_upload(job("tiktok", world))
    assert r.status == "draft" and "inbox" in r.message
    path, body = FAKE.tt_init
    assert path.endswith("/inbox/video/init/") and body["source_info"]["total_chunk_count"] == 1
    accounts.update("tiktok", mode="direct")                                         # direct post: private by default (unaudited apps)
    FAKE.tt_state = "PUBLISH_COMPLETE"
    r = publish.tiktok_upload(job("tiktok", world, "my caption"))
    assert r.status == "draft" and "only visible to you" in r.message
    path, body = FAKE.tt_init
    assert path.endswith("/post/publish/video/init/") and body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert body["post_info"]["title"] == "my caption"
    accounts.update("tiktok", privacy="PUBLIC_TO_EVERYONE")
    assert publish.tiktok_upload(job("tiktok", world)).status == "done"


def test_tiktok_failure_is_explained(world):
    connect_all()
    FAKE.tt_state = "FAILED"
    with pytest.raises(accounts.ApiError, match="bad codec"):
        publish.tiktok_upload(job("tiktok", world))


def test_tiktok_chunk_rule():
    assert publish._chunks(10_000_000) == (10_000_000, 1)
    size = 100 * 1024 * 1024
    chunk, n = publish._chunks(size)
    assert chunk * n <= size < chunk * (n + 1)                                       # the last chunk absorbs the remainder


def test_secrets_never_reach_the_page(world):
    connect_all()
    blob = json.dumps(accounts.status())
    for secret in ("sec", "1//refresh", "IGTOKEN", "rft.1", "cs"):
        assert secret not in blob.replace("secret", "").replace("Client secret", "")
    assert accounts.status()["youtube"]["connected"] is True


# ---- end to end through the review server: confirmation rules still hold, results are recorded
def test_share_directly_through_the_server(world):
    connect_all()
    root = make_root(world)
    srv, call = server(root)
    code, info = call("/api/share/providers")
    assert code == 200
    body = {"id": "ch/s/c", "file": "c_v.mp4", "platforms": ["youtube", "instagram", "tiktok"], "provider": "direct",
            "texts": {p: {"title": "T", "body": "B #x"} for p in ("youtube", "instagram", "tiktok")}}
    code, out = call("/api/share", body)
    assert code == 200, out
    res = out["results"]
    assert res["youtube"]["status"] == "done" and res["instagram"]["status"] == "done" and res["tiktok"]["status"] == "draft"
    saved = json.loads((root / "ch" / "s" / "c.clip.json").read_text())["shares"]
    assert saved["youtube"]["url"].endswith("VID123") and saved["tiktok"]["provider"] == "direct"
    code, out = call("/api/share", body)                                             # posting again is refused (youtube/instagram done)
    assert code == 400 and "already posted" in out
    srv.shutdown()


def test_disconnected_platform_blocks_everything_before_any_upload(world):
    accounts.update("youtube", client_id="cid", client_secret="sec", refresh_token="1//refresh")
    root = make_root(world)
    srv, call = server(root)
    code, out = call("/api/share", {"id": "ch/s/c", "file": "c_v.mp4", "platforms": ["youtube", "tiktok"], "provider": "direct",
                                    "texts": {p: {"title": "T", "body": "B"} for p in ("youtube", "tiktok")}})
    assert code == 400 and "TikTok is not connected" in out
    assert not any(c[0] in ("PUT",) or "yt/upload" in c[1] for c in FAKE.calls), "nothing may be uploaded when any platform is not ready"
    srv.shutdown()


def test_accounts_api(world):
    root = make_root(world)
    srv, call = server(root)
    code, out = call("/api/accounts/save", {"platform": "instagram", "values": {"access_token": "IGTOKEN" + "x" * 20}})
    assert code == 200 and out == {"connected": True, "account": "@streamclips"}
    code, out = call("/api/accounts/save", {"platform": "youtube", "values": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "sec"}, "options": {"privacy": "unlisted"}})
    assert code == 200 and out["auth_url"].startswith(BASE["url"] + "/google/auth") and accounts.get("youtube")["privacy"] == "unlisted"
    code, out = call("/api/accounts")
    assert code == 200 and out["platforms"]["instagram"]["connected"] and "IGTOKEN" not in json.dumps(out)
    code, _ = call("/api/accounts/disconnect", {"platform": "instagram"})
    assert code == 200 and not accounts.connected("instagram")
    assert call("/api/accounts/save", {"platform": "myspace", "values": {}})[0] == 400
    srv.shutdown()
