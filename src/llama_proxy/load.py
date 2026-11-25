"""In-memory rolling window of recent job load (total GPU busy time).

Feeds the "Auslastung (letzte 12 Std.)" graph on the web pages. Not
persisted: after a proxy restart the window starts empty, which is the
honest state for a live indicator.

The graph is the same for everyone and covers total utilization only
(Anton: "bei allen gleich aussehen und nur die Gesamtauslastung abbilden"):
per-user data is deliberately not exposed.
"""
from __future__ import annotations

import time
from collections import deque

# Entries older than this can never be part of a valid window again (the
# endpoint clamps the window to 43200 s = 12 h), so they are dropped;
# MAX_ENTRIES bounds memory under pathological load.
PRUNE_AGE_S = 43200.0
MAX_ENTRIES = 20000


class RecentLoad:
    """Rolling record of finished jobs: (finished_at, user, compute_seconds).

    ``record`` is called by the scheduler when a job finishes (with the
    same compute_seconds that go into the usage tracker); ``graph``
    aggregates whatever falls inside a window into fixed-size buckets for
    display. The user field is kept only so ``record``'s call sites stay
    unchanged - the public graph never breaks down per user.
    """

    def __init__(self) -> None:
        self._items: deque[tuple[float, str, float]] = deque()

    def record(self, user: str, compute_seconds: float, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        self._items.append((ts, user, max(0.0, float(compute_seconds))))
        self._prune(ts)

    def _prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - PRUNE_AGE_S
        while len(self._items) > MAX_ENTRIES:
            self._items.popleft()
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()

    def graph(self, window_s: float, bucket_s: float, now: float | None = None) -> dict:
        """Total GPU load over a rolling window as fixed-size buckets.

        Buckets are aligned to the epoch, so their boundaries are stable
        between polls (a job lands in the same bucket on every refresh).
        The current, still-open bucket is reported as-is: that is the
        honest "rounded" behaviour for a live indicator. ``utilization_pct``
        is busy time as a share of the period, capped at 100 %.
        """
        now = time.time() if now is None else now
        self._prune(now)
        n = max(1, int(round(window_s / bucket_s)))
        end = (int(now) // bucket_s + 1) * bucket_s
        start = end - n * bucket_s
        busy = [0.0] * n
        jobs = 0
        for ts, _user, secs in self._items:
            if ts < start:
                continue
            busy[min(n - 1, int((ts - start) // bucket_s))] += secs
            jobs += 1
        total = sum(busy)
        period = n * bucket_s
        return {
            "window_s": period,
            "bucket_s": bucket_s,
            "busy_seconds": round(total, 3),
            "utilization_pct": round(min(100.0, 100.0 * total / period), 1),
            "jobs": jobs,
            "buckets": [
                {
                    "start": start + i * bucket_s,
                    "busy_seconds": round(v, 3),
                    "utilization_pct": round(min(100.0, 100.0 * v / bucket_s), 1),
                }
                for i, v in enumerate(busy)
            ],
        }
