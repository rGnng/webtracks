"""In-memory brute-force protection for the Setup admin password."""

import collections
import math
import threading
import time


class AdminLoginGuard:
    """Track failed admin logins per source IP and lock repeated offenders."""

    def __init__(self, limit=5, window_seconds=60, lock_seconds=86400,
                 clock=None):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        try:
            window_seconds = int(window_seconds)
        except (TypeError, ValueError):
            window_seconds = 60
        try:
            lock_seconds = int(lock_seconds)
        except (TypeError, ValueError):
            lock_seconds = 86400
        self.limit = max(0, limit)
        self.window_seconds = max(1, window_seconds)
        self.lock_seconds = max(1, lock_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures = {}
        self._locked = {}

    def _prune(self, address, now):
        failures = self._failures.get(address)
        if failures is None:
            return None
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(address, None)
            return None
        return failures

    def is_locked(self, address, now=None):
        """Return ``(locked, retry_after_seconds)``."""

        if self.limit <= 0:
            return False, 0
        now = self._clock() if now is None else float(now)
        address = address or ""
        with self._lock:
            until = self._locked.get(address)
            if until is None:
                return False, 0
            if until <= now:
                self._locked.pop(address, None)
                return False, 0
            return True, max(1, math.ceil(until - now))

    def record_failure(self, address, now=None):
        """Record a failed login and return whether the IP became locked."""

        if self.limit <= 0:
            return False
        now = self._clock() if now is None else float(now)
        address = address or ""
        with self._lock:
            until = self._locked.get(address)
            if until is not None:
                if until > now:
                    return True
                self._locked.pop(address, None)
            failures = self._prune(address, now)
            if failures is None:
                failures = collections.deque()
                self._failures[address] = failures
            failures.append(now)
            if len(failures) > self.limit:
                self._failures.pop(address, None)
                self._locked[address] = now + self.lock_seconds
                return True
            return False

    def record_success(self, address):
        address = address or ""
        with self._lock:
            self._failures.pop(address, None)
            self._locked.pop(address, None)
