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


TWITCH_HOST = "irc.chat.twitch.tv"


class TwitchChat(threading.Thread):
    """Anonymous read-only Twitch IRC (the 'justinfan' login Twitch documents for read-only bots): no token, no account.

    Built to survive a day unattended: it notices a dead link within ~90 s (it pings the server when quiet), reconnects quickly after a
    drop that followed a healthy connection (the retry delay only grows while attempts keep failing), alternates between Twitch's TLS
    ports 6697 and 443 (a network that throttles one often lets the other through) and logs how long chat was down."""

    def __init__(self, channel: str, sink: Sink, stop: threading.Event,
                 host: str = TWITCH_HOST, port: int = 6697, tls: bool = True, fallback_ports: tuple[int, ...] | None = None):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel, self.sink, self.stop_evt = channel.lower().lstrip("#"), sink, stop
        self.host, self.port, self.tls = host, port, tls
        if fallback_ports is None:
            fallback_ports = (443,) if (host == TWITCH_HOST and port == 6697 and tls) else ()
        self.ports = (port, *fallback_ports)
        self.connected = threading.Event()
        self.messages = 0
        self.ping_after_s, self.dead_after_s = 45.0, 90.0        # silence before we ping / before we give up on the link
        self.retry_min_s, self.retry_max_s, self.healthy_s = 2.0, 60.0, 60.0
        self._backoff, self._attempt = self.retry_min_s, 0
        self._joined_at = 0.0
        self._down_since: float | None = None

    def plan_retry(self, lasted: float) -> float:
        """Seconds to wait before the next connection attempt, given how long the last connection was joined (0 = never joined).
        A connection that was healthy for a while resets the delay: it is a fresh start, not one more failure in a row."""
        wait = self._backoff
        if lasted >= self.healthy_s:
            wait = self._backoff = self.retry_min_s                # same port again: it just worked
        else:
            self._attempt += 1                                     # it never worked: try the other port next
        self._backoff = min(self._backoff * 2, self.retry_max_s)
        return wait

    def run(self):
        while not self.stop_evt.is_set():
            port = self.ports[self._attempt % len(self.ports)]
            self._joined_at, err = 0.0, ""
            try:
                self._session(port)
            except Exception as e:  # network drops are normal; just reconnect
                err = f"timed out connecting to or talking to {self.host}:{port}" if isinstance(e, (socket.timeout, TimeoutError)) else str(e)
            if self.stop_evt.is_set():
                break
            self.connected.clear()
            lasted = time.time() - self._joined_at if self._joined_at else 0.0
            if self._down_since is None:
                self._down_since = time.time()
            wait = self.plan_retry(lasted) * random.uniform(0.8, 1.2)      # jitter: several channels must not reconnect in lockstep
            log.warning("twitch chat %s: %s (retry in %.0fs)", self.channel, err or "connection closed", wait)
            self.stop_evt.wait(wait)

    def _session(self, port: int):
        raw = socket.create_connection((self.host, port), timeout=15)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.tls else raw
        sock.settimeout(max(0.2, min(30.0, self.ping_after_s / 2)))
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\n"
                     f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nJOIN #{self.channel}\r\n".encode())
        buf, last_data, pinged = b"", time.time(), False
        try:
            while not self.stop_evt.is_set():
                try:
                    data = sock.recv(8192)
                except socket.timeout:
                    idle = time.time() - last_data
                    if idle > self.dead_after_s:                  # a busy channel is never silent: the link is dead (half-open Wi-Fi etc.)
                        raise ConnectionError(f"no data and no reply to a ping for {idle:.0f}s")
                    if idle > self.ping_after_s and not pinged:
                        sock.sendall(b"PING :autoclip\r\n")
                        pinged = True
                    continue
                if not data:
                    raise ConnectionError("server closed connection")
                last_data, pinged = time.time(), False
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for ln in lines:
                    line = ln.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                        continue
                    if " 366 " in line or "ROOMSTATE" in line:
                        self._joined(port)
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

    def _joined(self, port: int):
        if not self.connected.is_set():
            self.connected.set()
            self._joined_at = time.time()
            if self._down_since is not None:
                log.info("twitch chat %s: back after %.0fs (port %d)", self.channel, time.time() - self._down_since, port)
                self._down_since = None


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
