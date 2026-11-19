"""Work shared by ALL streams that run at the same time.

Two lanes, because the two kinds of job are completely different:

* drafts   - stream copy of a few seconds of buffer: milliseconds, no CPU. They must NEVER wait: the buffer only holds
             `buffer_s` seconds, so a draft stuck behind a slow encode can find its video already deleted (clip lost).
* encodes  - the ready-to-post 9:16 render: tens of seconds of CPU/GPU each. Bounded (`cut_workers` in total, however many
             streams there are) and best-score-first, so when several streams spike at once the best moments are rendered first.
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .config import Config

log = logging.getLogger("autoclip.lanes")

_STOP = float("inf")          # sorts after every real job, so queued work runs before a worker sees its stop signal


class PriorityPool:
    """`workers` threads that always take the highest-priority waiting job (first come, first served among equals)."""

    def __init__(self, workers: int, name: str = "pool"):
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._running = 0
        self._waiting = 0
        self._closed = False
        self._threads = [threading.Thread(target=self._loop, daemon=True, name=f"{name}-{i}") for i in range(max(1, workers))]
        for t in self._threads:
            t.start()

    @property
    def running(self) -> int:
        return self._running

    @property
    def waiting(self) -> int:
        return self._waiting

    def submit(self, priority: float, fn: Callable, *args) -> bool:
        """Queue fn(*args). Higher `priority` runs first. False if the pool is already shut down."""
        with self._lock:
            if self._closed:
                return False
            self._waiting += 1
            self._q.put((-float(priority), next(self._seq), fn, args))
        return True

    def _loop(self) -> None:
        while True:
            key, _, fn, args = self._q.get()
            if key == _STOP:
                return
            with self._lock:
                self._waiting -= 1
                self._running += 1
            try:
                fn(*args)
            except Exception:
                log.exception("background job failed")
            finally:
                with self._lock:
                    self._running -= 1

    def shutdown(self, wait: bool = True, cancel_queued: bool = False) -> int:
        """Stop accepting work. Jobs already running always finish. Queued ones run too, unless cancel_queued
        (then they are dropped and counted). Returns how many queued jobs were dropped."""
        dropped = 0
        with self._lock:
            if self._closed:
                return 0
            self._closed = True
            if cancel_queued:
                try:
                    while True:
                        self._q.get_nowait()
                        dropped += 1
                except queue.Empty:
                    pass
                self._waiting -= dropped
            for _ in self._threads:
                self._q.put((_STOP, next(self._seq), None, ()))
        if wait:
            for t in self._threads:
                t.join()
        return dropped


class Lanes:
    def __init__(self, cfg: Config):
        self.drafts = ThreadPoolExecutor(max_workers=max(1, cfg.draft_workers), thread_name_prefix="draft")
        self.encodes = PriorityPool(cfg.cut_workers, "encode")

    def shutdown(self, cancel_queued_encodes: bool = False) -> int:
        """Drafts first (they may still queue an encode), then the encodes. Returns the number of queued encodes dropped."""
        self.drafts.shutdown(wait=True)
        return self.encodes.shutdown(wait=True, cancel_queued=cancel_queued_encodes)
