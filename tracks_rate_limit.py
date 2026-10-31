"""Per-source-IP download rate limiting."""

import math
import threading
import time


class DownloadRateLimiter:
    """Allow at most ``limit`` distinct items per IP in a sliding window."""

    def __init__(self, limit=28, window_seconds=60):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 28
        self.limit = max(0, limit)
        self.window_seconds = max(1, int(window_seconds))
        self._items = {}
        self._blocked = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def _prune(self, events, now):
        cutoff = now - self.window_seconds
        for item, seen in list(events.items()):
            if seen <= cutoff:
                events.pop(item, None)

    def _cleanup(self, now):
        if now - self._last_cleanup < self.window_seconds:
            return
        self._last_cleanup = now
        for address, events in list(self._items.items()):
            self._prune(events, now)
            if not events:
                self._items.pop(address, None)
        cutoff = now - (self.window_seconds + 15)
        for address, blocked_at in list(self._blocked.items()):
            if blocked_at <= cutoff:
                self._blocked.pop(address, None)

    def check(self, address, item, now=None):
        """Return ``(allowed, retry_after_seconds)``."""

        if self.limit <= 0:
            return True, 0
        now = time.monotonic() if now is None else float(now)
        address = address or ""
        item = str(item or "").strip().upper()
        with self._lock:
            self._cleanup(now)
            events = self._items.get(address)
            if events:
                self._prune(events, now)
                if not events:
                    self._items.pop(address, None)
                    events = None
            if events and item in events:
                # Media elements can issue several Range requests for the same
                # track. Repeated requests for one item count as one download.
                return True, 0
            if events and len(events) >= self.limit:
                retry_after = max(
                    1,
                    math.ceil(
                        min(events.values()) + self.window_seconds - now
                    ),
                )
                self._blocked[address] = now
                return False, retry_after
            if events is None:
                events = {}
                self._items[address] = events
            events[item] = now
            return True, 0

    def is_limited(self, address, now=None, grace_seconds=15):
        """Return ``(limited, retry_after_seconds)`` without consuming quota."""

        if self.limit <= 0:
            return False, 0
        now = time.monotonic() if now is None else float(now)
        address = address or ""
        with self._lock:
            self._cleanup(now)
            blocked_at = self._blocked.get(address)
            if blocked_at is not None and now - blocked_at <= grace_seconds:
                events = self._items.get(address) or {}
                if events:
                    retry_after = max(
                        1,
                        math.ceil(
                            min(events.values())
                            + self.window_seconds
                            - now
                        ),
                    )
                else:
                    retry_after = max(1, math.ceil(grace_seconds))
                return True, retry_after
            events = self._items.get(address)
            if not events:
                return False, 0
            self._prune(events, now)
            if not events:
                self._items.pop(address, None)
                return False, 0
            if len(events) >= self.limit:
                retry_after = max(
                    1,
                    math.ceil(
                        min(events.values()) + self.window_seconds - now
                    ),
                )
                return True, retry_after
            return False, 0
