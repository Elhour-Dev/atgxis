"""Connecting accounts made easy: tidy pasted values, Google JSON import, guided steps, plain-language errors, 'does it still work?'.
Uses the fake platform server from test_publish: nothing touches the real internet."""
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from autoclip import accounts, cli
from autoclip.accounts import ApiError
from test_publish import BASE, FAKE, connect_all, world  # noqa: F401  (world is a fixture)
from test_share_settings import make_root, server

GID = "123456-abc.apps.googleusercontent.com"
INSTALLED = {"installed": {"client_id": GID, "client_secret": "GOCSPX-secret", "auth_uri": "x", "token_uri": "y"}}


# ---- tidy what was pasted ----------------------------------------------------------------------------
def test_pasted_values_are_tidied():
    assert accounts.clean_credentials("youtube", {"client_id": f'  "{GID}" ', "client_secret": "GOCSPX-ab\n cd​"}) == \
        {"client_id": GID, "client_secret": "GOCSPX-abcd"}
    assert accounts.clean_credentials("instagram", {"access_token": "Bearer  IGAA" + "x" * 30 + "\n"}) == {"access_token": "IGAA" + "x" * 30}
    assert accounts.clean_credentials("instagram", {"access_token": "access_token=IGAAx"}) == {"access_token": "IGAAx"}
    assert accounts.clean_credentials("tiktok", {"client_key": " aw123 ", "client_secret": "", "other": "ignored"}) == {"client_key": "aw123"}
    assert accounts.clean_credentials("youtube", {}) == {}                      # nothing typed = keep what is saved


def test_swapped_boxes_are_fixed_and_wrong_values_are_explained():
    assert accounts.clean_credentials("youtube", {"client_id": "GOCSPX-s", "client_secret": GID}) == {"client_id": GID, "client_secret": "GOCSPX-s"}
    with pytest.raises(ApiError, match="should end with .apps.googleusercontent.com"):
        accounts.clean_credentials("youtube", {"client_id": "my-project-123"})
    with pytest.raises(ApiError, match="API key"):
        accounts.clean_credentials("youtube", {"client_id": GID, "client_secret": "AIzaSyABC"})


def test_google_client_json_import():
    want = {"client_id": GID, "client_secret": "GOCSPX-secret"}
    assert accounts.clean_credentials("youtube", {"client_json": json.dumps(INSTALLED)}) == want
    assert accounts.clean_credentials("youtube", {"client_id": json.dumps(INSTALLED, indent=2)}) == want      # whole file pasted into a box
    with pytest.raises(ApiError, match="Desktop app"):                                                         # the most common mistake
        accounts.clean_credentials("youtube", {"client_json": json.dumps({"web": INSTALLED["installed"]})})
    with pytest.raises(ApiError, match="service-account"):
        accounts.clean_credentials("youtube", {"client_json": '{"type": "service_account"}'})
    with pytest.raises(ApiError, match="not a JSON file"):
        accounts.clean_credentials("youtube", {"client_json": "hello"})


# ---- guided steps ------------------------------------------------------------------------------------
def test_setup_guide_has_links_copy_values_and_troubleshooting():
    g = accounts.setup_guide(9123)
    assert set(g) == {"youtube", "instagram", "tiktok"}
    for pl, guide in g.items():
        assert guide["minutes"] > 0 and len(guide["steps"]) >= 4 and guide["troubleshooting"], pl
        for step in guide["steps"]:
            if "link" in step:
                assert step["link"]["url"].startswith("https://")             # the page only ever opens https links
    tt = {s["copy"]["label"]: s["copy"]["value"] for s in g["tiktok"]["steps"] if "copy" in s}
    assert tt["Copy redirect address"] == "http://127.0.0.1:9123/oauth/callback"   # this page's real port, ready to paste
    assert tt["Copy scopes"] == accounts.TT_SCOPES
    assert any("redirect_uri_mismatch" in t["problem"] and "Desktop app" in t["fix"] for t in g["youtube"]["troubleshooting"])


def test_tiktok_asks_only_for_the_scopes_the_mode_needs(world):
    """Draft mode must not request video.publish: an app that lacks that scope would make TikTok refuse the whole sign-in."""
    def scope(mode):
        accounts.update("tiktok", client_key="ck", client_secret="cs", mode=mode)
        return urllib.parse.parse_qs(urllib.parse.urlparse(accounts.begin_oauth("tiktok", 8765)).query)["scope"][0]
    assert scope("draft") == "user.info.basic,video.upload"
    assert scope("direct") == "user.info.basic,video.upload,video.publish"
    accounts.update("tiktok", mode=None)
    assert "video.publish" not in scope("draft")


def test_explain_oauth_error():
    assert "Test users" in accounts.explain_oauth_error("youtube", "access_denied")
    assert "Desktop app" in accounts.explain_oauth_error("youtube", "redirect_uri_mismatch")
    assert "exactly" not in accounts.explain_oauth_error("tiktok", "mystery")           # unknown codes: just the platform's own words
    assert "redirect address" in accounts.explain_oauth_error("tiktok", "", "The redirect_uri does not match")


# ---- through the review server ---------------------------------------------------------------------------
def test_api_import_json_and_helpful_errors(world):
    srv, call = server(make_root(world))
    code, out = call("/api/accounts/save", {"platform": "youtube", "values": {"client_json": json.dumps(INSTALLED)}})
    assert code == 200 and out["auth_url"].startswith(BASE["url"] + "/google/auth")
    assert accounts.get("youtube")["client_id"] == GID and accounts.get("youtube")["client_secret"] == "GOCSPX-secret"
    code, out = call("/api/accounts/save", {"platform": "youtube", "values": {"client_json": json.dumps({"web": {}})}})
    assert code == 400 and "Desktop app" in out
    code, out = call("/api/accounts/save", {"platform": "youtube", "values": {"client_id": "nonsense"}})
    assert code == 400 and "apps.googleusercontent.com" in out
    code, out = call("/api/accounts")
    assert code == 200 and out["setup"]["tiktok"]["steps"] and "GOCSPX" not in json.dumps(out)
    srv.shutdown()


def test_share_info_says_which_platforms_are_connected(world):
    accounts.connect_instagram("IGTOKEN" + "x" * 20)
    srv, call = server(make_root(world))
    code, out = call("/api/share/providers")
    assert code == 200 and out["connected"] == {"tiktok": False, "youtube": False, "instagram": True}
    srv.shutdown()


def test_refused_sign_in_comes_back_with_the_fix(world):
    srv, call = server(make_root(world))
    accounts.update("youtube", client_id=GID, client_secret="s")
    url = accounts.begin_oauth("youtube", srv.server_address[1])
    state = url.split("state=")[1].split("&")[0]
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{base}/oauth/callback?error=access_denied&state={state}")
    page = e.value.read().decode()
    assert e.value.code == 400 and "Test users" in page and "Not connected" in page
    srv.shutdown()


# ---- "does it still work?" -------------------------------------------------------------------------------
def test_verify_each_platform(world):
    connect_all()
    assert "upload permission is granted" in accounts.verify("youtube")["message"]
    out = accounts.verify("tiktok")
    assert out["account"] == "TikTok (Stream Clips)" and "Stream Clips" in out["message"]
    assert accounts.verify("instagram")["account"] == "@streamclips"


def test_verify_reports_what_is_wrong_and_what_to_do(world):
    with pytest.raises(ApiError, match="not connected"):
        accounts.verify("youtube")
    connect_all()
    FAKE.yt_scope = "https://www.googleapis.com/auth/userinfo.email"
    with pytest.raises(ApiError, match="upload permission was not granted"):
        accounts.verify("youtube")
    accounts.update("youtube", refresh_token="revoked", access_token="", expires_at=0)
    with pytest.raises(ApiError, match="In production"):                              # Google revoked it: says why and how to fix
        accounts.verify("youtube")
    accounts.update("instagram", access_token="bad" * 10)
    with pytest.raises(ApiError, match="Generate a new token"):
        accounts.verify("instagram")


def test_tiktok_sign_in_shows_who_is_connected(world):
    accounts.update("tiktok", client_key="ck", client_secret="cs")
    url = accounts.begin_oauth("tiktok", 8765)
    state = url.split("state=")[1].split("&")[0]
    assert "connected as Stream Clips" in accounts.finish_oauth(state, "anything")
    assert accounts.get("tiktok")["account"] == "TikTok (Stream Clips)"


def test_test_connection_endpoint(world):
    srv, call = server(make_root(world))
    assert call("/api/accounts/test", {"platform": "youtube"})[0] == 400                # not connected: says so
    connect_all()
    code, out = call("/api/accounts/test", {"platform": "youtube"})
    assert code == 200 and out["ok"] is True and "ya29" not in json.dumps(out)
    assert call("/api/accounts/test", {"platform": "myspace"})[0] == 400
    srv.shutdown()


# ---- command line ----------------------------------------------------------------------------------------
def test_connect_command_opens_the_accounts_window(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("autoclip.review.serve", lambda root, cfg, port, browser, open_path="": seen.update(port=port, path=open_path, browser=browser))
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path))
    assert cli.main(["connect", "tiktok", "--out", str(tmp_path / "c"), "--port", "9001"]) == 0
    assert seen == {"port": 9001, "path": "#connect=tiktok", "browser": True}
    cli.main(["connect", "--out", str(tmp_path / "c"), "--no-browser"])
    assert seen["path"] == "#connect=" and seen["browser"] is False
