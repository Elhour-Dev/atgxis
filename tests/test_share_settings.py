import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from autoclip import settings, share
from autoclip.config import Config
from autoclip import config as cfgmod


# ------------------------------------------------------------------ settings & folders
@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_settings_roundtrip_and_apply(cfgdir, tmp_path):
    assert settings.load() == {}
    settings.update(export_dir=str(tmp_path / "final"), out_dir=str(tmp_path / "work"))
    assert (cfgdir / "settings.json").exists()                               # outside the project: git pull can't touch it
    cfg = Config(out_dir="clips")
    settings.apply(cfg)
    assert cfg.export_dir == str(tmp_path / "final") and cfg.out_dir == str(tmp_path / "work")
    (cfgdir / "settings.json").write_text("{ not json")
    assert settings.load() == {}                                             # corrupt file never crashes the program


def test_validate_dir(tmp_path):
    ok = settings.validate_dir(str(tmp_path / "new" / "deeper"))
    assert ok.is_dir()                                                       # created
    for bad, why in [("", "empty"), ("relative/path", "full path")]:
        with pytest.raises(ValueError, match=why):
            settings.validate_dir(bad)
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ValueError):
        settings.validate_dir(str(f / "sub"))                                # parent is a file: cannot be created
    assert settings.validate_dir(f'  "{tmp_path}"  ') == tmp_path.resolve()  # quotes/spaces from a pasted path are tolerated


def test_config_toml_is_loaded_automatically(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cfgmod.load(None).mult == 2.0                                     # no file: defaults
    (tmp_path / "config.toml").write_text("mult = 1.7\nmin_users = 100\n")
    c = cfgmod.load(None)                                                    # no --config flag needed any more
    assert (c.mult, c.min_users) == (1.7, 100)
    (tmp_path / "other.toml").write_text("mult = 3.1\n")
    assert cfgmod.load(tmp_path / "other.toml").mult == 3.1                  # an explicit path still wins
    (tmp_path / "config.toml").write_text("mult_typo = 1\n")
    with pytest.raises(ValueError, match="unknown setting"):
        cfgmod.load(None)                                                    # typos are reported, not ignored


# ------------------------------------------------------------------ server fixture with one exported clip
pytestmark_ff = pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0, reason="no ffmpeg")


def make_root(tmp_path, vertical=True, size_ok=True):
    d = tmp_path / "root" / "ch" / "s"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(b"draft")
    (d / "c_v.mp4").write_bytes(b"final clip bytes")
    meta = {"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 1, "duration_s": 30.0, "exports": [
        {"file": "c_v.mp4", "start": 0, "end": 30, "vertical": vertical, "captions": False}],
        "created": "2026-01-01T00:00:00", "source_url": "https://twitch.tv/ch", "spike": {}}
    (d / "c.clip.json").write_text(json.dumps(meta))
    return tmp_path / "root"


class Recorder:
    def __init__(self):
        self.opened, self.revealed = [], []


def assistant(rec):
    return share.AssistantProvider(open_url=rec.opened.append, reveal=rec.revealed.append)


def server(root, cfg=None):
    from autoclip import review
    srv = review.QuietServer(("127.0.0.1", 0), review.make_handler(review.Store(root, cfg or Config())))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    return srv, call


def meta_of(root):
    return json.loads((root / "ch" / "s" / "c.clip.json").read_text())


def test_folders_api(cfgdir, tmp_path, monkeypatch):
    root = make_root(tmp_path)
    srv, call = server(root)
    final = tmp_path / "MyFinal"
    code, out = call("/api/settings", {"export_dir": str(final)})
    assert code == 200 and final.is_dir() and out["restart_needed"] is False
    assert call("/api/settings")[1]["export_dir"] == str(final.resolve())
    assert json.loads((cfgdir / "settings.json").read_text())["export_dir"] == str(final.resolve())     # persisted for next run
    assert call("/api/settings", {"export_dir": "relative"})[0] == 400                                   # rejected with a reason
    assert call("/api/settings", {"out_dir": str(tmp_path / "elsewhere")})[1]["restart_needed"] is True
    assert call("/api/settings", {"export_dir": ""})[0] == 200 and call("/api/settings")[1]["export_dir"] == ""   # can be cleared
    monkeypatch.setattr(settings, "pick_folder", lambda initial="": str(tmp_path / "picked"))
    assert call("/api/browse", {"initial": ""})[1]["path"] == str(tmp_path / "picked")
    revealed = []
    monkeypatch.setattr(settings, "reveal", revealed.append)
    assert call("/api/reveal", {"id": "ch/s/c", "file": "c_v.mp4"})[0] == 200 and revealed[-1].name == "c_v.mp4"
    assert call("/api/reveal", {"id": "ch/s/c", "file": "../../../../etc/passwd"})[0] == 400            # traversal impossible
    assert call("/api/reveal", {"id": "ch/s/c", "file": "c.clip.json"})[0] == 400                        # only videos
    srv.shutdown()


@pytestmark_ff
def test_export_is_copied_to_the_final_folder(cfgdir, tmp_path):
    from autoclip.cutter import probe_duration
    root = tmp_path / "root"
    d = root / "ch" / "s"
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=4", "-f", "lavfi", "-i",
                    "sine=d=4", "-c:v", "libx264", "-c:a", "aac", str(d / "c.mp4")], check=True)
    (d / "c.clip.json").write_text(json.dumps({"id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 1,
                                               "duration_s": probe_duration(d / "c.mp4"), "exports": [], "created": "x", "source_url": "x", "spike": {}}))
    final = tmp_path / "Final Clips (mine)"
    from autoclip import review
    store = review.Store(root, Config(preset="ultrafast", export_dir=str(final)))
    name = store.export("ch/s/c", 0.0, 3.0, True, False)
    assert (final / "ch" / name).is_file() and (d / name).is_file()                 # kept with the drafts AND copied
    ex = json.loads((d / "c.clip.json").read_text())["exports"][0]
    assert ex["copy"] == str(final.resolve() / "ch" / name)
    store.cfg.export_dir = ""
    name2 = store.export("ch/s/c", 0.0, 2.0, True, False)
    assert json.loads((d / "c.clip.json").read_text())["exports"][-1]["copy"] is None   # no folder chosen: no copy


# ------------------------------------------------------------------ sharing rules
def test_compose_and_platform_limits():
    t, x = share.compose("youtube", " Title ", "credit\n\n#fyp")
    assert t == "Title" and x.endswith("#Shorts")                                      # Shorts tag added once
    assert share.compose("youtube", "T", "#shorts")[1] == "#shorts"
    assert share.compose("tiktok", "T", "body") == ("T", "T\n\nbody")
    assert share.check("youtube", "T" * 101, "x", 30, True)[0].startswith("title is 101")
    assert any("not vertical" in e for e in share.check("tiktok", "T", "x", 30, False))
    assert any("hashtags" in e for e in share.check("instagram", "T", " ".join(f"#t{i}" for i in range(31)), 30, True))
    assert share.check("instagram", "T", "x", 30, True) == []
    assert any("empty" in e for e in share.check("tiktok", " ", "x", 30, True))


def test_assistant_opens_fixed_pages_only_and_never_posts_without_a_request(tmp_path):
    root = make_root(tmp_path)
    rec = Recorder()
    prov = assistant(rec)
    share.PROVIDERS["assistant"] = prov
    from autoclip import review
    store = review.Store(root, Config())
    assert rec.opened == [] and rec.revealed == []                                    # nothing happens on its own
    res = share.run(store, share.ShareRequest("ch/s/c", "c_v.mp4", ["tiktok", "instagram"],
                                              {"tiktok": {"title": "T", "body": "b"}, "instagram": {"title": "T", "body": "b"}}))
    assert rec.opened == [share.PLATFORMS["tiktok"]["url"], share.PLATFORMS["instagram"]["url"]]     # fixed, trusted URLs only
    assert [p.name for p in rec.revealed] == ["c_v.mp4", "c_v.mp4"]
    assert res["tiktok"]["status"] == "opened" and res["tiktok"]["text"] == "T\n\nb"
    m = meta_of(root)["shares"]
    assert m["tiktok"]["status"] == "opened" and m["instagram"]["status"] == "opened" and "youtube" not in m


def test_validation_is_all_or_nothing_and_blocks_bad_requests(tmp_path):
    root = make_root(tmp_path)
    rec = Recorder()
    share.PROVIDERS["assistant"] = assistant(rec)
    from autoclip import review
    store = review.Store(root, Config())
    ok = {"tiktok": {"title": "T", "body": "b"}, "youtube": {"title": "Y" * 150, "body": "b"}}
    with pytest.raises(share.ShareError, match="YouTube Shorts: title is 150"):
        share.run(store, share.ShareRequest("ch/s/c", "c_v.mp4", ["tiktok", "youtube"], ok))
    assert rec.opened == [] and "shares" not in meta_of(root)                          # TikTok was valid but NOTHING happened
    for req, why in [(share.ShareRequest("nope", "c_v.mp4", ["tiktok"]), "clip not found"),
                     (share.ShareRequest("ch/s/c", "c.mp4", ["tiktok"], ok), "exported clip"),                    # a raw draft cannot be shared
                     (share.ShareRequest("ch/s/c", "../../x.mp4", ["tiktok"], ok), "exported clip"),
                     (share.ShareRequest("ch/s/c", "c_v.mp4", [], ok), "at least one platform"),
                     (share.ShareRequest("ch/s/c", "c_v.mp4", ["myspace"], ok), "at least one platform"),
                     (share.ShareRequest("ch/s/c", "c_v.mp4", ["tiktok"], ok, provider="evil"), "not set up")]:
        with pytest.raises(share.ShareError, match=why):
            share.run(store, req)
    assert rec.opened == []
    root2 = make_root(tmp_path / "b", vertical=False)
    with pytest.raises(share.ShareError, match="not vertical"):
        share.run(review.Store(root2, Config()), share.ShareRequest("ch/s/c", "c_v.mp4", ["tiktok"], ok))


class FakeAuto(share.Provider):
    name, label, automatic = "fake", "Fake auto", True

    def __init__(self, fail=()):
        self.fail, self.jobs = set(fail), []

    def publish(self, job):
        self.jobs.append(job)
        if job.platform in self.fail:
            raise RuntimeError("rate limited")
        return share.Result(True, "done", "posted", url=f"https://example.test/{job.platform}/1")


def test_no_double_posting_partial_failure_retry_and_marking(tmp_path):
    root = make_root(tmp_path)
    from autoclip import review
    store = review.Store(root, Config())
    fake = FakeAuto(fail={"youtube"})
    share.PROVIDERS["fake"] = fake
    texts = {p: {"title": "T", "body": "b"} for p in share.PLATFORMS}
    res = share.run(store, share.ShareRequest("ch/s/c", "c_v.mp4", list(share.PLATFORMS), texts, provider="fake"))
    assert res["tiktok"]["status"] == "done" and res["instagram"]["status"] == "done"
    assert res["youtube"]["status"] == "failed" and "rate limited" in res["youtube"]["message"]   # one failure doesn't hide the others
    assert len(fake.jobs) == 3
    sh = meta_of(root)["shares"]
    assert sh["tiktok"]["url"] == "https://example.test/tiktok/1" and sh["youtube"]["status"] == "failed"
    with pytest.raises(share.ShareError, match="TikTok: already posted"):                       # pressing share twice must not double-post
        share.run(store, share.ShareRequest("ch/s/c", "c_v.mp4", ["tiktok"], texts, provider="fake"))
    assert len(fake.jobs) == 3
    fake.fail.clear()
    res = share.run(store, share.ShareRequest("ch/s/c", "c_v.mp4", ["youtube"], texts, provider="fake"))   # retry only the failed one
    assert res["youtube"]["status"] == "done" and len(fake.jobs) == 4
    share.run(store, share.ShareRequest("ch/s/c", "c_v.mp4", ["tiktok"], texts, provider="fake", force=True))   # deliberate repeat
    assert len(fake.jobs) == 5
    assert share.mark(store, "ch/s/c", "tiktok", "reset") and "tiktok" not in meta_of(root)["shares"]
    assert share.mark(store, "ch/s/c", "tiktok", "done") and meta_of(root)["shares"]["tiktok"]["status"] == "done"
    assert not share.mark(store, "ch/s/c", "myspace", "done") and not share.mark(store, "ch/s/c", "tiktok", "hacked")


def test_share_http_api(tmp_path):
    root = make_root(tmp_path)
    rec = Recorder()
    share.PROVIDERS["assistant"] = assistant(rec)
    srv, call = server(root)
    info = call("/api/share/providers")[1]
    assert info["providers"][0]["name"] == "assistant" and set(info["platforms"]) == {"tiktok", "youtube", "instagram"}
    body = {"id": "ch/s/c", "file": "c_v.mp4", "platforms": ["tiktok"], "texts": {"tiktok": {"title": "T", "body": "b"}}}
    code, out = call("/api/share", body)
    assert code == 200 and out["results"]["tiktok"]["status"] == "opened" and rec.opened == [share.PLATFORMS["tiktok"]["url"]]
    assert call("/api/share", {**body, "file": "c.mp4"})[0] == 400
    assert call("/api/share", {"id": "ch/s/c"})[0] == 400                                          # malformed request: clean error
    assert call("/api/share/mark", {"id": "ch/s/c", "platform": "tiktok", "status": "done"})[0] == 200
    assert call("/api/clips")[1][0]["shares"]["tiktok"]["status"] == "done"
    srv.shutdown()
