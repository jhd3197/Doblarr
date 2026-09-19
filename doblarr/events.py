"""In-process event bus for SSE progress streaming.

Thread-safe pub/sub: each subscriber gets its own queue and `publish` fans out
non-blockingly (a slow consumer simply drops events). The bus keeps a small
ring buffer of recent events so a client connecting mid-job replays what it
missed — the same reconnect-replay trick Maintainerr uses.
"""

from __future__ import annotations

import datetime as _dt
import logging
import queue
import threading
from collections import deque

log = logging.getLogger("doblarr.events")

BUFFER_SIZE = 50
SUB_QUEUE_SIZE = 200


class EventBus:
    def __init__(self, buffer_size: int = BUFFER_SIZE):
        self._subs: list[queue.Queue] = []
        self._buffer: deque = deque(maxlen=buffer_size)
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: dict) -> dict:
        """Fan an event out to all subscribers and keep it for replay."""
        event = {"topic": topic,
                 "at": _dt.datetime.now().isoformat(timespec="seconds"),
                 **payload}
        with self._lock:
            self._buffer.append(event)
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
        with self._lock:
            if replay:
                for event in self._buffer:
                    q.put(event)
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
