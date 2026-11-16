"""In-RAM per-user rate limiting (sliding window).

RAM-only by design: a proxy restart resets the windows. That is acceptable -
limits are a fairness device, not a billing boundary (consistent with the
no-database policy).
"""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self) -> None:
        self._stamps: dict[str, deque[float]] = {}

    def allow(self, user: str, limit_per_hour: int) -> tuple[bool, int]:
        """Check the per-hour request window.

        Returns (allowed, retry_after_s). A successful check records the
        request timestamp (attempts are counted, not successes).
        """
        if limit_per_hour <= 0:
            return True, 0
        now = time.time()
        stamps = self._stamps.setdefault(user, deque())
        while stamps and now - stamps[0] >= 3600:
            stamps.popleft()
        if len(stamps) >= limit_per_hour:
            retry_after = int(3600 - (now - stamps[0])) + 1
            return False, max(1, retry_after)
        stamps.append(now)
        return True, 0
