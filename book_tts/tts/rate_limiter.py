"""Sliding-window rate limiter for TTS API calls."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Thread-safe sliding-window rate limiter.

    Parameters
    ----------
    max_calls:
        Maximum number of calls allowed within the sliding window.
    period:
        Window length in seconds.
    """

    def __init__(self, max_calls: int, period: float) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        if period <= 0:
            raise ValueError("period must be positive")

        self.max_calls = max_calls
        self.period = period
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request is allowed under the rate limit."""
        while True:
            with self._lock:
                now = time.monotonic()
                # Evict timestamps older than the sliding window
                while self._timestamps and self._timestamps[0] <= now - self.period:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return

                # Sleep until the oldest entry expires from the window
                sleep_for = self._timestamps[0] + self.period - now

            time.sleep(sleep_for)
