"""Quick connect (Zernio), tested against a FAKE local server. Nothing here touches the real internet or posts anything."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autoclip import accounts, publish, share, zernio
from autoclip.accounts import ApiError
from test_share_settings import make_root, server

KEY = "zk_live_" + "a" * 24


class Fake:
    accounts = []
    post_state = "published"
    polls_until_done = 1
    calls = []
    last_post = None
    uploaded = 0


F = Fake()
BASE = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def authed(self):
        return self.headers.get("Authorization") == f"Bearer {KEY}"

    def do_GET(self):
        F.calls.append(("GET", self.path))
        if not self.authed():
            return self.reply(401, {"error": "Unauthorized"})
        if self.path == "/z/accounts":
            return self.reply(200, {"accounts": F.accounts})
        if self.path == "/z/posts/POST1":
            F.polls_until_done -= 1
            state = F.post_state if F.polls_until_done <= 0 else "publishing"
            return self.reply(200, {"post": {"_id": "POST1", "status": state, "platforms": [
                {"platform": F.last_post["platforms"][0]["platform"], "status": state, "platformPostUrl": "https://example.com/p/1",
                 "error": "Instagram rejected the video" if state == "failed" else ""}]}})
        self.reply(404, {"error": "nope"})

    def do_POST(self):
        raw = self.body()
        F.calls.append(("POST", self.path))
        if not self.authed():
            return self.reply(401, {"error": "Unauthorized"})
        if self.path == "/z/media/presign":
            assert json.loads(raw)["contentType"] == "video/mp4"
            return self.reply(200, {"uploadUrl": BASE["url"] + "/put/1", "publicUrl": "https://media.example/temp/v.mp4"})
        if self.path == "/z/posts":
            F.last_post = json.loads(raw)
            return self.reply(201, {"post": {"_id": "POST1", "status": "publishing"}})
        self.reply(404, {"error": "nope"})

    def do_PUT(self):
        raw = self.body()
        F.calls.append(("PUT", self.path))
        assert int(self.headers["Content-Length"]) == len(raw) > 0
        F.uploaded = len(raw)
        self.reply(200)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    F.accounts = [{"_id": "A-IG", "platform": "instagram", "profileId": "P1", "username": "@streamclips", "isActive": True},
                  {"_id": "A-TT", "platform": "tiktok", "profileId": "P1", "username": "@streamclips", "isActive": True}]
    F.post_state, F.polls_until_done, F.calls, F.last_post, F.uploaded = "published", 1, [], None, 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setitem(accounts.ENDPOINTS, "zernio", BASE["url"] + "/z")
    monkeypatch.setattr(publish, "POLL_EVERY_S", 0.01)
    yield tmp_path
    srv.shutdown()


def job(platform, tmp_path, text="hello #fyp"):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"fakevideo" * 1000)
    return share.Job(platform, f, "Great title", text)


def test_connect_finds_accounts_and_never_exposes_the_key(world):
    s = zernio.connect("  " + KEY + "\n")                          # pasted with spaces / a line break
    assert s["saved"] and set(s["accounts"]) == {"instagram", "tiktok"} and s["accounts"]["tiktok"]["name"] == "@streamclips"
    assert s["warning"] == "" and "api_key" not in json.dumps(s)
    assert zernio.covers("instagram") and zernio.covers("tiktok") and not zernio.covers("youtube")


def test_bad_key_and_empty_account_give_plain_messages(world):
    with pytest.raises(ApiError, match="did not accept this key"):
        zernio.connect("zk_live_" + "b" * 24)
    assert not zernio.saved()
    F.accounts = []
    with pytest.raises(ApiError, match="no Instagram, TikTok or YouTube account is connected in Zernio"):
        zernio.connect(KEY)
    assert not zernio.saved()


def test_more_than_two_accounts_warns_about_the_free_limit(world):
    F.accounts.append({"_id": "A-YT", "platform": "youtube", "username": "chan", "isActive": True})
    s = zernio.connect(KEY)
    assert "free plan covers the first 2" in s["warning"] and "youtube" in s["accounts"]


def test_inactive_accounts_are_ignored(world):
    F.accounts[1]["isActive"] = False
    assert set(zernio.connect(KEY)["accounts"]) == {"instagram"}


def test_instagram_post_uploads_the_file_then_publishes(world):
    zernio.connect(KEY)
    r = zernio.zernio_upload(job("instagram", world))
    assert r.status == "done" and r.url == "https://example.com/p/1"
    assert F.uploaded == len(b"fakevideo" * 1000)                              # the real file reached the presigned URL
    p = F.last_post
    assert p["publishNow"] is True and p["content"] == "hello #fyp" and p["profileId"] == "P1"
    assert p["platforms"] == [{"platform": "instagram", "accountId": "A-IG"}]
    assert p["mediaItems"][0]["url"] == "https://media.example/temp/v.mp4" and "tiktokSettings" not in p
    assert [c for c in F.calls if c[0] == "POST"].count(("POST", "/z/posts")) == 1       # exactly one post call


def test_tiktok_public_and_draft(world):
    zernio.connect(KEY, "public")
    assert zernio.zernio_upload(job("tiktok", world)).status == "done"
    ts = F.last_post["tiktokSettings"]
    assert ts["privacy_level"] == "PUBLIC_TO_EVERYONE" and ts["draft"] is False and ts["content_preview_confirmed"] is True
    zernio.set_options("draft")
    F.polls_until_done = 99                                                    # TikTok drafts never report "published"
    r = zernio.zernio_upload(job("tiktok", world))
    assert r.status == "draft" and F.last_post["tiktokSettings"]["draft"] is True and "drafts" in r.message


def test_platform_failure_is_reported_with_zernios_reason(world):
    zernio.connect(KEY)
    F.post_state = "failed"
    with pytest.raises(ApiError, match="Instagram rejected the video"):
        zernio.zernio_upload(job("instagram", world))


def test_slow_publishing_is_not_called_done(world, monkeypatch):
    zernio.connect(KEY)
    monkeypatch.setattr(publish, "POLL_MAX_S", 0.05)
    F.polls_until_done = 10 ** 6
    r = zernio.zernio_upload(job("instagram", world))
    assert r.status == "draft" and "still publishing" in r.message


def test_auto_provider_routes_per_platform(world, monkeypatch):
    zernio.connect(KEY)
    accounts.update("youtube", client_id="cid", client_secret="sec", refresh_token="1//refresh")
    sent = []
    monkeypatch.setitem(publish.UPLOADERS, "youtube", lambda j: sent.append(j.platform) or share.Result(True, "done", "direct"))
    prov = zernio.AutoProvider()
    assert prov.configured()
    assert prov.publish(job("instagram", world)).status == "done" and sent == []              # Zernio
    r = prov.publish(job("youtube", world))
    assert r.message == "direct" and sent == ["youtube"]                                      # the direct account
    accounts.clear("youtube")
    assert "not connected" in prov.problems("youtube") and prov.problems("tiktok") is None


def test_review_server_endpoints(world):
    root = make_root(world)
    srv, call = server(root)
    assert call("/api/quick")[1]["saved"] is False
    code, msg = call("/api/quick/save", {"api_key": "nope-nope-nope"})
    assert code == 400 and "did not accept" in msg
    code, out = call("/api/quick/save", {"api_key": KEY, "tiktok_mode": "draft"})
    assert code == 200 and out["tiktok_mode"] == "draft" and set(out["accounts"]) == {"instagram", "tiktok"}
    info = call("/api/share/providers")[1]
    assert info["quick"] == {"tiktok": True, "youtube": False, "instagram": True}
    assert next(p for p in info["providers"] if p["name"] == "auto")["configured"] is True
    assert call("/api/quick/options", {"tiktok_mode": "bogus"})[0] == 400
    assert call("/api/quick/refresh", {})[0] == 200
    assert KEY not in json.dumps(call("/api/quick")[1])
    assert call("/api/quick/disconnect", {})[0] == 200 and call("/api/quick")[1]["saved"] is False
    srv.shutdown()


def test_profile_given_as_an_object_is_sent_as_a_plain_id(world):
    F.accounts[0]["profileId"] = {"_id": "P1", "name": "Main"}          # what Zernio really returns: "expected string, received object"
    zernio.connect(KEY)
    assert accounts.get("zernio")["accounts"]["instagram"]["profileId"] == "P1"
    accounts.update("zernio", accounts={**accounts.get("zernio")["accounts"], "tiktok": {"id": "A-TT", "name": "x", "profileId": {"_id": "P1"}}})
    zernio.zernio_upload(job("tiktok", world))                          # an account saved by the earlier version still works
    assert F.last_post["profileId"] == "P1"
    F.accounts[0]["profileId"] = None
    zernio.connect(KEY)
    zernio.zernio_upload(job("instagram", world))
    assert "profileId" not in F.last_post                               # no profile known: omitted, never an object
