"""The Export button must stay busy until the clip file really exists (state lives on the server, not in the page)."""
import json
import threading
import time

from autoclip import review
from autoclip.config import Config


def make_store(tmp_path):
    d = tmp_path / "ch" / "s"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(b"draft")
    (d / "c.clip.json").write_text(json.dumps({
        "id": "ch/s/c", "file": "c.mp4", "channel": "ch", "status": "new", "score": 1, "duration_s": 30.0, "exports": [],
        "created": "2026-01-01T00:00:00", "source_url": "x", "spike": {}}))
    return review.Store(tmp_path, Config())


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def job_state(store):
    return next(c for c in store.list() if c["id"] == "ch/s/c")["export_job"]


def test_job_stays_busy_until_the_file_is_recorded(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    gate, started = threading.Event(), threading.Event()

    def slow_export(cid, start, end, vertical, captions):
        started.set()
        assert gate.wait(5)
        return "c_v.mp4"

    monkeypatch.setattr(store, "export", slow_export)
    assert store.start_export("ch/s/c", 0, 10, True, False) == {"state": "queued"}
    assert started.wait(5)
    assert job_state(store)["state"] == "encoding"             # still busy however often the page redraws
    assert job_state(store)["state"] == "encoding"
    try:
        store.start_export("ch/s/c", 0, 10, True, False)       # a second click / tab cannot start a duplicate encode
        raise AssertionError("duplicate export was accepted")
    except ValueError as e:
        assert "already being exported" in str(e)
    gate.set()
    assert wait_for(lambda: job_state(store) is None)          # back to normal only once the export function returned


def test_failure_is_kept_until_dismissed_and_allows_retry(tmp_path, monkeypatch):
    store = make_store(tmp_path)

    def boom(*a):
        raise RuntimeError("ffmpeg failed: No such filter")

    monkeypatch.setattr(store, "export", boom)
    store.start_export("ch/s/c", 0, 10, True, False)
    assert wait_for(lambda: (job_state(store) or {}).get("state") == "error")
    assert "No such filter" in job_state(store)["message"]
    monkeypatch.setattr(store, "export", lambda *a: "ok.mp4")
    store.start_export("ch/s/c", 0, 10, True, False)           # retry is allowed after a failure
    assert wait_for(lambda: job_state(store) is None)
    monkeypatch.setattr(store, "export", boom)
    store.start_export("ch/s/c", 0, 10, True, False)
    assert wait_for(lambda: (job_state(store) or {}).get("state") == "error")
    store.dismiss_export("ch/s/c")
    assert job_state(store) is None


def test_bad_requests_fail_immediately(tmp_path):
    store = make_store(tmp_path)
    for args in (("nope", 0, 5), ("ch/s/c", 9, 2), ("ch/s/c", 0, 5000)):
        try:
            store.start_export(*args, True, False)
            raise AssertionError(f"accepted {args}")
        except ValueError:
            pass
    assert job_state(store) is None
