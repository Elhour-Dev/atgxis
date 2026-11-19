"""YouTube live chat read straight from YouTube's chat feed (no chat-downloader): page parsing, message parsing, the reader loop."""
import json
import threading
import time

import pytest

from autoclip import chat as C


def _page(lcr=True, key=True):
    data = {"contents": {"twoColumnWatchNextResults": {"conversationBar": {"liveChatRenderer": {
        "continuations": [{"reloadContinuationData": {"continuation": "TOP0"}}],
        "header": {"liveChatHeaderRenderer": {"viewSelector": {"sortFilterSubMenuRenderer": {"subMenuItems": [
            {"title": "Top chat", "selected": True, "continuation": {"reloadContinuationData": {"continuation": "PAGE_TOP"}}},
            {"title": "Live chat", "selected": False, "continuation": {"reloadContinuationData": {"continuation": "PAGE_LIVE"}}}]}}}}}}
        if lcr else {}}}}
    return ("<html><script>var ytInitialData = " + json.dumps(data) + ";</script><script>ytcfg.set({"
            + ('"INNERTUBE_API_KEY":"KEY1",' if key else "") + '"INNERTUBE_CONTEXT":{"client":{"clientName":"WEB"}}});</script>')


def _msg(ts, user, *runs, paid=False):
    r = {"timestampUsec": str(int(ts * 1e6)), "authorExternalChannelId": user, "message": {"runs": list(runs)}}
    return {"addChatItemAction": {"item": {("liveChatPaidMessageRenderer" if paid else "liveChatTextMessageRenderer"): r}}}


def _resp(actions=(), cont="NEXT", menu=False, kind="invalidationContinuationData", timeout_ms=10000):
    lcc = {"actions": list(actions)}
    if cont:
        lcc["continuations"] = [{kind: {"continuation": cont, "timeoutMs": timeout_ms}}]
    if menu:
        lcc["header"] = {"liveChatHeaderRenderer": {"viewSelector": {"sortFilterSubMenuRenderer": {"subMenuItems": [
            {"title": "Top chat", "selected": True, "continuation": {"reloadContinuationData": {"continuation": "TOP1"}}},
            {"title": "Live chat", "selected": False, "continuation": {"reloadContinuationData": {"continuation": "LIVE1"}}}]}}}}
    return {"continuationContents": {"liveChatContinuation": lcc}}


def test_parse_watch_page_uses_the_key_the_api_accepts():
    key, ctx, cont = C.parse_watch_page(_page())
    assert (key, ctx, cont) == ("KEY1", {"client": {"clientName": "WEB"}}, "TOP0")     # the page's own view keys are refused (HTTP 400)


def test_parse_watch_page_says_why():
    with pytest.raises(ValueError, match="no live chat"):
        C.parse_watch_page(_page(lcr=False))
    with pytest.raises(ValueError, match="could not read"):
        C.parse_watch_page(_page(key=False))
    with pytest.raises(ValueError, match="could not read"):
        C.parse_watch_page("<html>consent page</html>")


def test_parse_chat_response_messages_emotes_and_next():
    r = _resp([_msg(1700000100.5, "UC1", {"text": "LOL "}, {"emoji": {"emojiId": "x", "shortcuts": [":joy:"]}}),
               _msg(1700000101.0, "UC2", {"text": "gg"}, paid=True),
               {"addChatItemAction": {"item": {"liveChatViewerEngagementMessageRenderer": {}}}},     # system notice: not a chatter
               {"markChatItemAsDeletedAction": {}}], timeout_ms=10000)
    ev, nxt, wait = C.parse_chat_response(r)
    assert ev == [(1700000100.5, "UC1", "LOL :joy:"), (1700000101.0, "UC2", "gg")]
    assert nxt == "NEXT" and wait == 5.0                                                  # never waits longer than 5 s
    assert C.parse_chat_response(_resp(cont=None))[1] is None                              # no continuation: the chat ended
    assert C.parse_chat_response(_resp(kind="timedContinuationData", timeout_ms=100))[2] == 1.0


def test_live_view_switch_only_once():
    assert C.live_view(_resp(menu=True)) == "LIVE1"
    assert C.live_view(_resp()) is None


def test_reader_switches_to_live_chat_skips_backlog_and_survives_empty_answers(monkeypatch):
    now = time.time()
    script = [_resp([_msg(now - 1, "TOPCHAT_ONLY", {"text": "x"})], cont="T2", menu=True),     # Top chat: only used to find Live chat
              _resp([_msg(now - 300, "OLD", {"text": "backlog"}), _msg(now - 2, "A", {"text": "hi"})], cont="L2", timeout_ms=1),
              {"responseContext": {}},                                                      # an answer without chat: asked again
              _resp([_msg(now, "B", {"text": "KEKW"})], cont=None)]                          # stream over
    sent = []
    monkeypatch.setattr(C, "_yt_get", lambda url: _page())
    monkeypatch.setattr(C, "_yt_post", lambda url, body: sent.append(body["continuation"]) or script[len(sent) - 1])
    got, stop = [], threading.Event()
    yc = C.YouTubeChat("https://www.youtube.com/watch?v=abc", got.append, stop)
    monkeypatch.setattr(yc.stop_evt, "wait", lambda s=None: stop.is_set())                  # no real sleeping in the test
    yc._read_direct("https://www.youtube.com/watch?v=abc")
    assert sent == ["TOP0", "LIVE1", "L2", "L2"]
    assert [(e.user, e.text) for e in got] == [("A", "hi"), ("B", "KEKW")]                   # old backlog and the Top-chat copy never count
    assert yc.connected.is_set() and yc.messages == 2


def test_reader_gives_up_after_repeated_empty_answers(monkeypatch):
    monkeypatch.setattr(C, "_yt_get", lambda url: _page())
    monkeypatch.setattr(C, "_yt_post", lambda url, body: {"responseContext": {}})
    yc = C.YouTubeChat("u", lambda e: None, threading.Event())
    monkeypatch.setattr(yc.stop_evt, "wait", lambda s=None: False)
    with pytest.raises(ValueError, match="stopped sending"):
        yc._read_direct("https://www.youtube.com/watch?v=abc")


def test_odd_items_are_skipped_not_the_batch():
    r = _resp([_msg(1700000100.0, "UC1", {"text": "ok"}), "junk", {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "timestampUsec": "9" * 40, "authorName": "str", "message": {"runs": ["x"]}}}}}, _msg(1700000101.0, "UC2", {"text": "fine"})],
        timeout_ms="NaN")
    ev, nxt, wait = C.parse_chat_response(r)
    assert [u for _, u, _ in ev] == ["UC1", "UC2"] and nxt == "NEXT" and wait == 2.0
    for bad in ([], "x", {"continuationContents": []}, {"continuationContents": {"liveChatContinuation": {"actions": {}, "continuations": "x"}}}):
        assert C.parse_chat_response(bad)[0] == []
        assert C.live_view(bad) is None
    with pytest.raises(ValueError):
        C.parse_watch_page('var ytInitialData = {"contents": "x"}; "INNERTUBE_API_KEY":"K" "INNERTUBE_CONTEXT":{}')


def _run_reader(monkeypatch, post, has_cd, rounds=4):
    """Run YouTubeChat.run() for a few rounds without real waiting; returns (waits between tries, chat-downloader calls)."""
    import importlib.util
    monkeypatch.setattr(C, "_yt_get", lambda url: _page())
    monkeypatch.setattr(C, "_yt_post", post)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: object() if n == "chat_downloader" and has_cd else
                        (None if n == "chat_downloader" else real(n, *a)))
    stop, waits, cd = threading.Event(), [], []
    yc = C.YouTubeChat("https://www.youtube.com/watch?v=abc", lambda e: None, stop)
    monkeypatch.setattr(yc, "_read_chat_downloader", lambda v: cd.append(v) or (_ for _ in ()).throw(RuntimeError("cd broken")))

    def wait(s=None):
        if s and s >= 5:
            waits.append(s)
            if len(waits) >= rounds:
                stop.set()
        return stop.is_set()
    monkeypatch.setattr(stop, "wait", wait)
    yc.run()
    return waits, cd


def test_backoff_grows_when_the_chat_api_keeps_failing(monkeypatch):
    def post(url, body):
        raise OSError("HTTP Error 400")
    waits, cd = _run_reader(monkeypatch, post, has_cd=False)
    assert waits == [5.0, 10.0, 20.0, 40.0]                       # never "connected": no 1.5 MB page every 5 s


def test_no_live_chat_does_not_try_the_fallback(monkeypatch):
    import importlib.util
    monkeypatch.setattr(C, "parse_watch_page", lambda html: (_ for _ in ()).throw(C.NoLiveChat("not live")))
    waits, cd = _run_reader(monkeypatch, lambda u, b: {}, has_cd=True, rounds=2)
    assert cd == []


def test_fallback_only_when_the_direct_reader_cannot_start(monkeypatch):
    def post(url, body):
        raise OSError("HTTP Error 400")
    waits, cd = _run_reader(monkeypatch, post, has_cd=True, rounds=2)
    assert len(cd) == 2                                           # tried once per round, and it did not reset the backoff
    assert waits == [5.0, 10.0]


def test_reconnect_does_not_repeat_messages(monkeypatch):
    now = time.time()
    batch = [_msg(now - 5, "A", {"text": "one"}), _msg(now - 1, "B", {"text": "two"})]
    monkeypatch.setattr(C, "_yt_get", lambda url: _page())
    monkeypatch.setattr(C, "_yt_post", lambda url, body: _resp(batch, cont=None))
    got = []
    yc = C.YouTubeChat("u", got.append, threading.Event())
    monkeypatch.setattr(yc.stop_evt, "wait", lambda s=None: False)
    yc._read_direct("https://www.youtube.com/watch?v=abc")
    yc._read_direct("https://www.youtube.com/watch?v=abc")        # a reconnect gets the same recent backlog again
    assert [e.user for e in got] == ["A", "B"]


def test_a_message_with_a_bad_time_never_hides_later_ones(monkeypatch):
    now = time.time()
    batches = iter([_resp([_msg(9e10, "FUTURE", {"text": "x"})], cont=None), _resp([_msg(now - 1, "A", {"text": "hi"})], cont=None)])
    monkeypatch.setattr(C, "_yt_get", lambda url: _page())
    monkeypatch.setattr(C, "_yt_post", lambda url, body: next(batches))
    got = []
    yc = C.YouTubeChat("u", got.append, threading.Event())
    monkeypatch.setattr(yc.stop_evt, "wait", lambda s=None: False)
    yc._read_direct("v")
    yc._read_direct("v")                                          # reconnect
    assert [e.user for e in got] == ["FUTURE", "A"] and abs(got[0].t - now) < 5      # stamped "now", and A still counts


def test_fallback_counts_as_connected_only_once_messages_arrive(monkeypatch):
    import sys, types
    class Chat:
        def __iter__(self):
            raise RuntimeError("cd iteration failed")
            yield
    fake = types.ModuleType("chat_downloader")
    fake.ChatDownloader = lambda: types.SimpleNamespace(get_chat=lambda v, message_groups=None: Chat())
    monkeypatch.setitem(sys.modules, "chat_downloader", fake)
    yc = C.YouTubeChat("u", lambda e: None, threading.Event())
    with pytest.raises(RuntimeError, match="cd iteration failed"):
        yc._read_chat_downloader("v")
    assert not yc.connected.is_set()
