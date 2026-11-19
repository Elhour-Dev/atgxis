"""Chat readers. Both run in a background thread, reconnect forever, and push ChatEvent to a callback."""
from __future__ import annotations

import gzip
import json
import logging
import random
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
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


_YT = "https://www.youtube.com"
_YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9", "Cookie": "SOCS=CAI; CONSENT=YES+cb"}       # skips the EU cookie-consent page
BACKLOG_S = 30.0          # messages already in the chat when it opens are older than this: not heard live, never counted


def _json_after(text: str, marker: str):
    """The JSON value that follows `marker` in a page (None if it is not there or is not JSON)."""
    i = text.find(marker)
    if i < 0:
        return None
    i += len(marker)
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    try:
        return json.JSONDecoder().raw_decode(text, i)[0]
    except ValueError:
        return None


class NoLiveChat(ValueError):
    """The video has no live chat right now (not live, or chat off): retrying later is all that helps."""


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _path(v, *keys) -> dict:
    for k in keys:
        v = _d(v).get(k)
    return _d(v)


def parse_watch_page(html: str) -> tuple[str, dict, str]:
    """(api key, client context, first chat continuation) from a YouTube watch page. NoLiveChat / ValueError say why not."""
    data = _json_after(html, "ytInitialData =") or _json_after(html, 'window["ytInitialData"] =')
    key = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
    ctx = _json_after(html, '"INNERTUBE_CONTEXT":')
    if not isinstance(data, dict) or not key or not isinstance(ctx, dict):
        raise ValueError("could not read the YouTube page (YouTube may have changed it)")
    lcr = _path(data, "contents", "twoColumnWatchNextResults", "conversationBar", "liveChatRenderer")
    if not lcr:
        raise NoLiveChat("this video has no live chat (not live right now, or chat is turned off)")
    conts = lcr.get("continuations")
    plain = [_path(c, "reloadContinuationData").get("continuation") for c in (conts if isinstance(conts, list) else [])]
    plain = [c for c in plain if isinstance(c, str) and c]       # the page's own view keys are refused by the API: use this one
    if not plain:
        raise ValueError("could not find the live chat on this video")
    return key.group(1), ctx, plain[0]


def live_view(r) -> str | None:
    """The "Live chat" view (every message, not YouTube's filtered "Top chat") offered in a chat response, or None."""
    items = _path(r, "continuationContents", "liveChatContinuation", "header", "liveChatHeaderRenderer", "viewSelector",
                  "sortFilterSubMenuRenderer").get("subMenuItems")
    if not isinstance(items, list) or len(items) < 2 or not isinstance(items[-1], dict) or items[-1].get("selected"):
        return None                                               # [Top chat, Live chat]; already on Live chat: nothing to switch
    c = _path(items[-1], "continuation", "reloadContinuationData").get("continuation")
    return c if isinstance(c, str) and c else None


def _runs_text(msg) -> str:
    runs = _d(msg).get("runs")
    out = []
    for r in runs if isinstance(runs, list) else []:
        r = _d(r)
        if isinstance(r.get("text"), str):
            out.append(r["text"])
        elif r.get("emoji"):
            e = _d(r["emoji"])
            sc = e.get("shortcuts")
            out.append(str((sc[0] if isinstance(sc, list) and sc else None) or e.get("emojiId") or ""))
    return "".join(out)


def has_chat(r) -> bool:
    return bool(_path(r, "continuationContents", "liveChatContinuation"))


def parse_chat_response(r) -> tuple[list[tuple[float, str, str]], str | None, float]:
    """([(unix time, user id, text)], next continuation or None when the chat ended, seconds to wait before asking again).
    One odd item is skipped, never the whole batch."""
    lcc = _path(r, "continuationContents", "liveChatContinuation")
    if not lcc:
        return [], None, 0.0
    out = []
    acts = lcc.get("actions")
    for a in acts if isinstance(acts, list) else []:
        try:
            item = _path(a, "addChatItemAction", "item")
            m = _d(item.get("liveChatTextMessageRenderer")) or _d(item.get("liveChatPaidMessageRenderer"))
            if not m:
                continue
            ts = int(m["timestampUsec"]) / 1e6
            if isinstance(m["timestampUsec"], bool) or not 1e9 < ts < 1e11:
                continue
            user = m.get("authorExternalChannelId") or _d(m.get("authorName")).get("simpleText") or "?"
            out.append((ts, str(user), _runs_text(m.get("message"))))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    nxt, wait = None, 2.0
    conts = lcc.get("continuations")
    for c in conts if isinstance(conts, list) else []:
        for kind in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            k = _path(c, kind)
            if isinstance(k.get("continuation"), str) and k["continuation"]:
                nxt = k["continuation"]
                try:
                    ms = float(k.get("timeoutMs") or 2000)
                except (TypeError, ValueError):
                    ms = 2000.0
                wait = min(max(ms / 1000.0, 1.0), 5.0) if ms == ms else 2.0       # NaN -> the default
                break
        if nxt:
            break
    return out, nxt, wait


def _yt_fetch(req: urllib.request.Request, timeout: float) -> str:
    req.add_header("Accept-Encoding", "gzip")              # the watch page is ~1.5 MB: 4x smaller compressed (slow lines time out otherwise)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def _yt_get(url: str) -> str:
    return _yt_fetch(urllib.request.Request(url, headers=_YT_HEADERS), 120)


def _yt_post(url: str, body: dict) -> dict:
    return json.loads(_yt_fetch(urllib.request.Request(url, data=json.dumps(body).encode(),
                                                       headers={**_YT_HEADERS, "Content-Type": "application/json"}), 60))


class YouTubeChat(threading.Thread):
    """YouTube live chat, read the way the YouTube web page reads it (no login, standard library only). If that cannot even start
    and the `chat-downloader` package is installed, that is tried as a second way. NOTE: both rely on YouTube's unofficial chat
    feed, so they can break when YouTube changes it."""

    def __init__(self, url: str, sink: Sink, stop: threading.Event):
        super().__init__(daemon=True, name="chat-youtube")
        self.url, self.sink, self.stop_evt = url, sink, stop
        self.connected = threading.Event()
        self.messages = 0
        self._last_ts = 0.0                                      # newest message passed on: a reconnect never repeats older ones

    def _emit(self, ts: float | None, user: str, text: str) -> None:
        now = time.time()
        t = ts if ts and abs(now - ts) < 60 else now
        if t == ts:                                              # only a believable time: a bad one must never hide later messages
            self._last_ts = max(self._last_ts, ts)
        self.messages += 1
        self.sink(ChatEvent(t=t, user=user, text=text))

    def _read_direct(self, video: str) -> None:
        key, ctx, cont = parse_watch_page(_yt_get(video))
        first, switched, misses = True, False, 0
        while not self.stop_evt.is_set():
            r = _yt_post(f"{_YT}/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false", {"context": ctx, "continuation": cont})
            if not has_chat(r):
                misses += 1                                      # an answer without chat now and then is normal: ask again
                if misses >= 5:
                    raise ValueError("YouTube stopped sending the chat")
                self.stop_evt.wait(3.0)
                continue
            misses = 0
            lv = None if switched else live_view(r)
            switched = True
            if lv:                                               # move to "Live chat"; its first answer repeats the recent backlog
                cont = lv
                continue
            events, nxt, wait = parse_chat_response(r)
            self.connected.set()                                 # only now: real chat is coming in (warm-up must not learn silence)
            now = time.time()
            for ts, user, text in events:
                if first and (now - ts > BACKLOG_S or ts <= self._last_ts):
                    continue
                self._emit(ts, user, text)
            first = False
            if not nxt:
                return                                           # the stream (or its chat) ended
            cont = nxt
            self.stop_evt.wait(wait)

    def _read_chat_downloader(self, video: str) -> None:
        from chat_downloader import ChatDownloader
        chat = ChatDownloader().get_chat(video, message_groups=["messages"])
        for m in chat:
            if self.stop_evt.is_set():
                break
            self.connected.set()                                 # only once a message really arrived
            author = m.get("author") or {}
            ts = m.get("timestamp")                              # microseconds since epoch
            self._emit(ts / 1e6 if ts else None, str(author.get("id") or author.get("name") or "?"), m.get("message") or "")

    def run(self):
        import importlib.util
        has_cd = importlib.util.find_spec("chat_downloader") is not None
        backoff = 5.0
        while not self.stop_evt.is_set():
            try:
                video = resolve_youtube_video_url(self.url)
                try:
                    self._read_direct(video)
                except NoLiveChat:
                    raise                                        # nothing else can read a chat that is not there
                except Exception as e:
                    if not has_cd or self.stop_evt.is_set() or self.connected.is_set():
                        raise                                    # it was working: just reconnect (also never hides a bug of ours)
                    log.warning("youtube chat: %s; trying chat-downloader instead", e)
                    try:
                        self._read_chat_downloader(video)
                    except Exception as e2:
                        if self.connected.is_set():
                            raise                                # chat-downloader worked for a while: its own error is the news
                        raise RuntimeError(f"{e}; chat-downloader: {e2}") from None
                if not self.stop_evt.is_set():
                    log.info("youtube chat ended (stream over or offline); retrying")
            except Exception as e:
                log.warning("youtube chat: %s (retry in %.0fs)", e, 5.0 if self.connected.is_set() else backoff)
            if self.connected.is_set():                          # chat really came in for a while: the next try is quick again
                backoff = 5.0
            self.connected.clear()
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 120.0)
