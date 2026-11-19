"""Twitch chat that survives a day: quick recovery after a healthy connection, dead-link detection, port fallback, honest status."""
import socket
import threading
import time

from autoclip.chat import TwitchChat
from autoclip.config import Config
from autoclip.detector import ChatEvent, SpikeDetector
from autoclip.worker import ChannelWorker

JOIN = b":x 366 y #chan :End of /NAMES list\r\n"
MSG = b":v!v@v.tmi.twitch.tv PRIVMSG #chan :hello\r\n"


def make(**kw):
    chat = TwitchChat("chan", lambda ev: None, threading.Event(), host="127.0.0.1", port=1, tls=False, **kw)
    chat.retry_min_s, chat.retry_max_s = 2.0, 60.0
    return chat


# ---- the retry delay ---------------------------------------------------------------------------------------
def test_retry_delay_grows_only_while_attempts_keep_failing():
    chat = make()
    assert [chat.plan_retry(0) for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    assert chat.plan_retry(300) == 2                          # it had been connected for 5 minutes: a fresh start, not failure no. 8
    assert chat.plan_retry(0) == 4                            # and the count starts over from there
    for _ in range(3):                                        # drop after drop, each after a long healthy connection: always quick
        assert chat.plan_retry(3600) == 2


def test_default_twitch_reader_also_knows_port_443_but_a_fake_server_does_not():
    assert TwitchChat("x", lambda e: None, threading.Event()).ports == (6697, 443)
    assert TwitchChat("x", lambda e: None, threading.Event(), host="127.0.0.1", port=5555, tls=False).ports == (5555,)


def test_other_port_is_tried_after_a_failure_but_the_port_that_worked_is_kept():
    chat = TwitchChat("x", lambda e: None, threading.Event())
    chat.plan_retry(0)                                        # 6697 failed
    assert chat.ports[chat._attempt % 2] == 443
    chat._attempt = 1
    chat.plan_retry(500)                                      # 443 worked for a long time, then dropped
    assert chat.ports[chat._attempt % 2] == 443               # try 443 again first


# ---- against a fake server -----------------------------------------------------------------------------------
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    return srv, srv.getsockname()[1]


def test_a_silent_dead_link_is_noticed_and_replaced_quickly():
    """Half-open Wi-Fi: the server just stops talking. Used to take ~6.7 minutes to notice; now ping, then reconnect."""
    srv, port = listener()
    seen, conns = {"ping": False}, []

    def serve():
        for n in range(2):
            c, _ = srv.accept()
            conns.append(c)
            c.recv(4096)
            c.sendall(JOIN)                                   # joined, then total silence (never answers a PING)
            if n == 0:
                c.settimeout(3)
                try:
                    seen["ping"] = b"PING" in c.recv(100)
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    stop = threading.Event()
    chat = TwitchChat("chan", lambda ev: None, stop, host="127.0.0.1", port=port, tls=False)
    chat.ping_after_s, chat.dead_after_s, chat.retry_min_s = 0.4, 1.0, 0.05
    chat.start()
    t0 = time.time()
    while len(conns) < 2 and time.time() - t0 < 8:
        time.sleep(0.05)
    stop.set()
    assert len(conns) == 2, "the dead link was never replaced"
    assert seen["ping"], "the client should ping a quiet server before giving up on it"
    assert time.time() - t0 < 6


def test_unreachable_port_falls_back_to_the_other_one_and_reports_the_outage(caplog):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    bad_port = closed.getsockname()[1]
    closed.close()                                            # nothing listens here: connection refused
    srv, good_port = listener()
    got = []

    def serve():
        c, _ = srv.accept()
        c.recv(4096)
        c.sendall(JOIN + MSG)
        time.sleep(1.5)
    threading.Thread(target=serve, daemon=True).start()
    stop, caplog_level = threading.Event(), caplog.set_level("INFO")
    chat = TwitchChat("chan", got.append, stop, host="127.0.0.1", port=bad_port, tls=False, fallback_ports=(good_port,))
    chat.retry_min_s = 0.05
    chat.start()
    t0 = time.time()
    while not got and time.time() - t0 < 8:
        time.sleep(0.05)
    assert chat.connected.wait(2)
    stop.set()
    assert got and got[0].text == "hello"
    assert "back after" in caplog.text and f"port {good_port}" in caplog.text     # the log says how long chat was down


# ---- what the rest of the program does with a dead chat connection -------------------------------------------
def test_detector_neither_learns_nor_fires_while_chat_is_down():
    cfg = Config(warmup_s=5, baseline_s=60, window_s=5, min_users=3, audio_assist=False)
    d = SpikeDetector(cfg)
    t = 1000
    for s in range(t, t + 20):                                # normal chat: 4 people a second
        for u in range(4):
            d.add(ChatEvent(t=s + .5, user=f"u{u}", text="hi"))
        d.advance(s + 2)
    normal = d.normal_level()
    n_samples = len(d.samples)
    d.chat_ok = False
    for s in range(t + 20, t + 60):                           # connection dead: nobody can be heard
        d.advance(s + 2)
    assert len(d.samples) == n_samples and d.normal_level() == normal          # silence was not learned as "normal"
    d.chat_ok = True
    d.advance(t + 62)
    assert len(d.samples) > n_samples                         # learning resumes


def test_status_says_chat_is_reconnecting(tmp_path):
    stop = threading.Event()
    down = threading.Thread()
    down.connected = threading.Event()                        # a reader that is not connected
    w = ChannelWorker(Config(buffer_dir=str(tmp_path), audio_assist=False), "solo", tmp_path / "c", stop, source_cmd=["x"], chat_factory=lambda s: down)
    assert w._chat_connected() is False
    w.detector.chat_ok = w._chat_connected()
    assert w.detector.chat_ok is False
    w.snapshot = {"label": "solo", "video": "live", "msgs_per_min": 0, "normal": 5.0, "warmup_left": 0, "chat_ok": False, "spikes": 0, "clips": 0, "last_clip": ""}
    assert "CHAT RECONNECTING" in w.status_line()
    down.connected.set()
    assert w._chat_connected() is True
    w.lanes.shutdown()
