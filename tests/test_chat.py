import socket
import threading
import time

from autoclip.chat import TwitchChat, parse_irc_line
from autoclip.detector import ChatEvent

PRIV = "@badge-info=;display-name=CoolViewer;id=1 :coolviewer!coolviewer@coolviewer.tmi.twitch.tv PRIVMSG #chan :KEKW that was insane"


def test_parse_privmsg():
    ev = parse_irc_line(PRIV, now=5.0)
    assert (ev.user, ev.text, ev.t, ev.kind) == ("CoolViewer", "KEKW that was insane", 5.0, "msg")


def test_parse_action_and_colon_in_text():
    ev = parse_irc_line(":bob!bob@bob.tmi.twitch.tv PRIVMSG #c :\x01ACTION is: shocked\x01")
    assert ev.text == "is: shocked" and ev.user == "bob"


def test_parse_raid_and_ignored_lines():
    raid = "@msg-id=raid;login=x :tmi.twitch.tv USERNOTICE #chan"
    assert parse_irc_line(raid).kind == "raid"
    assert parse_irc_line("@msg-id=sub :tmi.twitch.tv USERNOTICE #chan :hi") is None
    assert parse_irc_line(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    assert parse_irc_line("PING :tmi.twitch.tv") is None
    assert parse_irc_line("") is None


def test_twitch_reader_against_fake_server_incl_reconnect():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port, got_login, conns = srv.getsockname()[1], [], []

    def serve():
        for n in range(2):                                   # first connection dies, client must reconnect
            c, _ = srv.accept()
            conns.append(c)
            got_login.append(c.recv(4096).decode())
            c.sendall(b"PING :tmi.twitch.tv\r\n")
            c.sendall((PRIV + "\r\n:x 366 y #chan :End\r\n").encode())
            if n == 0:
                time.sleep(0.3)
                c.close()
            else:
                time.sleep(3)

    threading.Thread(target=serve, daemon=True).start()
    events: list[ChatEvent] = []
    stop = threading.Event()
    chat = TwitchChat("Chan", events.append, stop, host="127.0.0.1", port=port, tls=False)
    chat.start()
    deadline = time.time() + 15
    while len(events) < 2 and time.time() < deadline:
        time.sleep(0.1)
    stop.set()
    assert len(events) >= 2, "did not reconnect and receive messages again"
    assert "NICK justinfan" in got_login[0] and "JOIN #chan" in got_login[0] and "PASS" in got_login[0]
    assert "oauth" not in got_login[0].lower()               # anonymous: never sends a real token
