"""In-process event bus for SSE progress streaming.

Thread-safe pub/sub: each subscriber gets its own queue and `publish` fans out
non-blockingly (a slow consumer simply drops events). The bus keeps a small
ring buffer of recent events so a client connecting mid-job replays what it
missed — the same reconnect-replay trick Maintainerr uses.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import itertools
import logging
import queue
import threading
from collections import deque

log = logging.getLogger("doblarr.events")

BUFFER_SIZE = 50
SUB_QUEUE_SIZE = 200


class LoopQueue:
    """A subscriber queue read from an event loop; filled safely from any thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int = SUB_QUEUE_SIZE):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    def put_nowait(self, event: dict) -> None:
        self.loop.call_soon_threadsafe(self._put, event)

    put = put_nowait

    def _put(self, event: dict) -> None:
        if self.queue.full():
            log.debug("dropping event for a slow subscriber")
            return
        self.queue.put_nowait(event)

    async def get(self, timeout: float) -> dict:
        """The next event; raises TimeoutError after `timeout` seconds."""
        return await asyncio.wait_for(self.queue.get(), timeout)


class EventBus:
    def __init__(self, buffer_size: int = BUFFER_SIZE):
        self._subs: list = []
        # Log lines replay from their own buffer, so a burst of them cannot push
        # the job events a reconnecting page needs out of the replay.
        self._buffer: deque = deque(maxlen=buffer_size)
        self._log_buffer: deque = deque(maxlen=buffer_size)
        self._order = itertools.count()
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: dict) -> dict:
        """Fan an event out to all subscribers and keep it for replay."""
        event = {"topic": topic,
                 "at": _dt.datetime.now().isoformat(timespec="seconds"),
                 **payload}
        with self._lock:
            kept = self._log_buffer if topic == "log" else self._buffer
            kept.append((next(self._order), event))
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                log.debug("dropping event for a slow subscriber")
        return event

    def subscribe(self, replay: bool = True) -> queue.Queue:
        """Return a queue that receives live events, prefilled with the buffer."""
        q: queue.Queue = queue.Queue(maxsize=SUB_QUEUE_SIZE)
        self._attach(q, replay)
        return q

    def subscribe_async(self, replay: bool = True) -> LoopQueue:
        """Like subscribe, for a reader on the running event loop: waiting for an
        event holds no thread."""
        q = LoopQueue(asyncio.get_running_loop())
        self._attach(q, replay)
        return q

    def _attach(self, q, replay: bool) -> None:
        with self._lock:
            if replay:
                for _order, event in sorted([*self._buffer, *self._log_buffer],
                                            key=lambda kept: kept[0]):
                    q.put(event)
            self._subs.append(q)

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
