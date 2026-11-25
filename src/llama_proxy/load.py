"""In-memory rolling window of recent job load (per user, per job).

Feeds the "Auslastung (letzte 20 Min.)" card on the web pages. Not
persisted: after a proxy restart the window starts empty, which is the
honest state for a live indicator.
"""
from __future__ import annotations

import time
from collections import deque

# Entries older than this can never be part of a valid window again (the
# endpoint clamps to 3600 s), so they are dropped; MAX_ENTRIES bounds
# memory under pathological load.
PRUNE_AGE_S = 3600.0
MAX_ENTRIES = 20000


class RecentLoad:
    """Rolling record of finished jobs: (finished_at, user, compute_seconds).

    ``record`` is called by the scheduler when a job finishes (with the
    same compute_seconds that go into the usage tracker), ``snapshot``
    aggregates whatever falls inside a window for display.
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

    def snapshot(self, window_s: float, now: float | None = None) -> dict:
        """Aggregate all jobs finished within the last `window_s` seconds.

        ``busy_seconds`` is the sum of GPU compute time in the window;
        ``utilization_pct`` is that as a share of the window wall-clock
        (capped at 100); ``users`` lists every user's busy time, where
        ``pct`` is the user's GPU utilization within the window
        (busy_seconds ÷ window, capped at 100 — a single busy user is
        not "100% of the busy time" but "their share of the window"),
        sorted by busy_seconds descending.
        """
        now = time.time() if now is None else now
        self._prune(now)
        cutoff = now - window_s
        busy = 0.0
        jobs = 0
        per_user: dict[str, dict] = {}
        for ts, user, secs in self._items:
            if ts < cutoff:
                continue
            busy += secs
            jobs += 1
            u = per_user.setdefault(user, {"busy_seconds": 0.0, "jobs": 0})
            u["busy_seconds"] += secs
            u["jobs"] += 1
        users = sorted(
            (
                {
                    "name": name,
                    "busy_seconds": round(u["busy_seconds"], 3),
                    "pct": round(min(100.0, 100.0 * u["busy_seconds"] / window_s), 1)
                    if window_s > 0
                    else 0.0,
                    "jobs": u["jobs"],
                }
                for name, u in per_user.items()
            ),
            key=lambda u: u["busy_seconds"],
            reverse=True,
        )
        return {
            "window_s": int(window_s),
            "busy_seconds": round(busy, 3),
            "utilization_pct": round(min(100.0, 100.0 * busy / window_s), 1)
            if window_s > 0
            else 0.0,
            "jobs": jobs,
            "users": users,
        }
