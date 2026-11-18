"""Chat readers. Both run in a background thread, reconnect forever, and push ChatEvent to a callback."""
from __future__ import annotations

import logging
import random
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from typing import Callable

from .detector import ChatEvent

log = logging.getLogger("autoclip.chat")
Sink = Callable[[ChatEvent], None]

_IRC = re.compile(r"^(?:@(?P<tags>\S+) )?(?::(?P<prefix>\S+) )?(?P<cmd>[A-Z0-9]+)(?: (?P<rest>.*))?$")


def parse_irc_line(line: str, now: float | None = None) -> ChatEvent | None:
    """Turn one raw Twitch IRC line into a ChatEvent (or None for anything we do not care about)."""
    m = _IRC.match(line.rstrip("\r\n"))
    if not m:
        return None
    now = time.time() if now is None else now
    cmd, rest = m["cmd"], m["rest"] or ""
    tags = dict(p.split("=", 1) for p in (m["tags"] or "").split(";") if "=" in p)
    if cmd == "PRIVMSG":
        _, _, text = rest.partition(" :")
        nick = (m["prefix"] or "").split("!", 1)[0]
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            text = text[8:-1]
        return ChatEvent(t=now, user=tags.get("display-name") or nick, text=text)
    if cmd == "USERNOTICE" and tags.get("msg-id") == "raid":
        return ChatEvent(t=now, kind="raid")
    return None


class TwitchChat(threading.Thread):
    """Anonymous read-only Twitch IRC (the 'justinfan' login Twitch documents for read-only bots): no token, no account."""

    def __init__(self, channel: str, sink: Sink, stop: threading.Event,
                 host: str = "irc.chat.twitch.tv", port: int = 6697, tls: bool = True):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel, self.sink, self.stop_evt = channel.lower().lstrip("#"), sink, stop
        self.host, self.port, self.tls = host, port, tls
        self.connected = threading.Event()
        self.messages = 0

    def run(self):
        backoff = 2.0
        while not self.stop_evt.is_set():
            try:
                self._session()
                backoff = 2.0
            except Exception as e:  # network drops are normal; just reconnect
                log.warning("twitch chat %s: %s (retry in %.0fs)", self.channel, e, backoff)
            self.connected.clear()
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _session(self):
        raw = socket.create_connection((self.host, self.port), timeout=15)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.tls else raw
        sock.settimeout(30)
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\n"
                     f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nJOIN #{self.channel}\r\n".encode())
        buf, last_data = b"", time.time()
        try:
            while not self.stop_evt.is_set():
                try:
                    data = sock.recv(8192)
                except socket.timeout:
                    if time.time() - last_data > 400:     # Twitch pings every ~5 min; silence = dead link
                        raise ConnectionError("no data for 400s")
                    continue
                if not data:
                    raise ConnectionError("server closed connection")
                last_data = time.time()
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for ln in lines:
                    line = ln.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                        continue
                    if " 366 " in line or "ROOMSTATE" in line:
                        self.connected.set()
                    if line.startswith("RECONNECT") or " RECONNECT" in line[:40]:
                        raise ConnectionError("server asked to reconnect")
                    ev = parse_irc_line(line)
                    if ev:
                        self.messages += 1
                        self.sink(ev)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def resolve_youtube_video_url(url: str) -> str:
    """A channel '/live' or '@handle' link -> the concrete watch URL of the current livestream (needs yt-dlp)."""
    if "watch?v=" in url or "youtu.be/" in url:
        return url
    exe = shutil.which("yt-dlp")
    if not exe:
        return url
    try:
        out = subprocess.run([exe, "--no-warnings", "--skip-download", "--print", "id", url],
                             capture_output=True, text=True, timeout=60)
        vid = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""
        return f"https://www.youtube.com/watch?v={vid}" if vid else url
    except Exception:
        return url


class YouTubeChat(threading.Thread):
    """YouTube live chat via the `chat-downloader` package (no login). NOTE: relies on YouTube's
    unofficial chat feed, so it can break when YouTube changes it; `autoclip doctor` shows the status."""

    def __init__(self, url: str, sink: Sink, stop: threading.Event):
        super().__init__(daemon=True, name="chat-youtube")
        self.url, self.sink, self.stop_evt = url, sink, stop
        self.connected = threading.Event()
        self.messages = 0

    def run(self):
        try:
            from chat_downloader import ChatDownloader
        except ImportError:
            log.error("YouTube chat needs:  pip install chat-downloader")
            return
        backoff = 5.0
        while not self.stop_evt.is_set():
            try:
                video = resolve_youtube_video_url(self.url)
                chat = ChatDownloader().get_chat(video, message_groups=["messages"])
                self.connected.set()
                backoff = 5.0
                for m in chat:
                    if self.stop_evt.is_set():
                        break
                    text = m.get("message") or ""
                    author = m.get("author") or {}
                    ts = m.get("timestamp")                      # microseconds since epoch
                    now = time.time()
                    t = ts / 1e6 if ts and abs(now - ts / 1e6) < 60 else now
                    self.messages += 1
                    self.sink(ChatEvent(t=t, user=str(author.get("id") or author.get("name") or "?"), text=text))
                log.info("youtube chat ended (stream over or offline); retrying")
            except Exception as e:
                log.warning("youtube chat: %s (retry in %.0fs)", e, backoff)
            self.connected.clear()
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 120.0)
