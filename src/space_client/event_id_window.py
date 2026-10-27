"""A bounded memory of the ``eventId``s seen recently, to drop repeated events."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class EventIdWindow:
    """Remembers recent ``eventId``s so that a second copy of an event is dropped.

    Memory is bounded: an id is forgotten ``window_seconds`` after it was first
    seen, or earlier when more than ``max_entries`` ids are held (oldest first).
    Space sends one copy of an event per connection and the copies of one event
    are emitted together, so a short window is enough. Ids are compared for
    equality only; they do not order events.

    Safe to use from several threads (the synchronous client receives events on
    the socket's thread).
    """

    DEFAULT_MAX_ENTRIES = 10_000
    DEFAULT_WINDOW_SECONDS = 600

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError(f"max_entries must be a positive integer, got {max_entries!r}")
        if not window_seconds > 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds!r}")
        self.max_entries = max_entries
        self.window_seconds = window_seconds
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._seen)

    def accept(self, event_id: str) -> bool:
        """Record ``event_id``; ``True`` the first time, ``False`` for a repeat.

        Args:
            event_id (str): The ``eventId`` of a received event.

        Returns:
            bool: Whether the event should be delivered.
        """
        with self._lock:
            now = self._clock()
            while self._seen:
                oldest, seen_at = next(iter(self._seen.items()))
                if now - seen_at <= self.window_seconds:
                    break
                del self._seen[oldest]
            if event_id in self._seen:
                return False
            self._seen[event_id] = now
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)
            return True

    def clear(self) -> None:
        """Forget every id."""
        with self._lock:
            self._seen.clear()
