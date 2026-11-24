"""Usage accounting in RAM with periodic atomic flush to a single JSON file.

This is the only routine disk traffic on the SD card (one small write per
flush interval). Counters are loaded back on startup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("llama_proxy.usage")

# Every per-day row carries exactly these counters. Newer versions may add
# fields (e.g. ``cached``); rows written by older versions lack them.
_ROW_FIELDS = ("prompt", "completion", "cached", "requests", "compute_seconds")


def _ensure_row_fields(row: dict) -> None:
    """Backfill any counter fields a per-day row is missing (legacy rows)."""
    for field in _ROW_FIELDS:
        row.setdefault(field, 0)


class UsageTracker:
    def __init__(self, data_dir: str, flush_interval_s: float) -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "usage.json"
        self._interval = max(5.0, flush_interval_s)
        self._data: dict[str, dict[str, dict[str, int]]] = {}
        self._last_flush = time.time()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                with self._path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = loaded
                    for per_day in self._data.values():
                        if isinstance(per_day, dict):
                            for row in per_day.values():
                                if isinstance(row, dict):
                                    _ensure_row_fields(row)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not load usage data: %s", exc)

    def record(
        self,
        user: str,
        prompt_tokens: int,
        completion_tokens: int,
        requests: int = 1,
        cached_tokens: int = 0,
        compute_seconds: float = 0.0,
    ) -> None:
        day = date.today().isoformat()
        per_user = self._data.setdefault(user, {})
        row = per_user.setdefault(day, {k: 0 for k in _ROW_FIELDS})
        _ensure_row_fields(row)
        row["prompt"] += int(prompt_tokens)
        row["completion"] += int(completion_tokens)
        row["cached"] += int(cached_tokens)
        row["requests"] += int(requests)
        row["compute_seconds"] += float(compute_seconds)

    def flush(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self._path)
            self._last_flush = time.time()
        except OSError as exc:
            log.warning("usage flush failed: %s", exc)

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await asyncio.to_thread(self.flush)

    def totals(self, days: int = 30) -> dict:
        """Aggregated totals for the last N days (for the web page / CLI)."""
        cutoff = datetime.combine(date.today(), datetime.min.time()).timestamp() - days * 86400
        out: dict[str, dict] = {}
        for user, per_day in self._data.items():
            agg = {"prompt": 0, "completion": 0, "cached": 0, "requests": 0, "compute_seconds": 0.0}
            for day, row in per_day.items():
                if self._in_window(day, cutoff):
                    agg["prompt"] += int(row.get("prompt", 0))
                    agg["completion"] += int(row.get("completion", 0))
                    agg["cached"] += int(row.get("cached", 0))
                    agg["requests"] += int(row.get("requests", 0))
                    agg["compute_seconds"] += float(row.get("compute_seconds", 0))
            agg["compute_seconds"] = round(agg["compute_seconds"], 2)
            out[user] = agg
        return out

    def daily(self, user: str | None = None, days: int = 30) -> dict:
        """Per-day rows for the last N days, oldest first.

        ``user`` selects one user; ``None`` returns every user. Each row has
        the date plus the counters (compute_seconds rounded to 2 decimals).
        """
        cutoff = datetime.combine(date.today(), datetime.min.time()).timestamp() - days * 86400
        users = [user] if user is not None else list(self._data.keys())
        out: dict[str, list[dict]] = {}
        for u in users:
            rows: list[dict] = []
            for day, row in self._data.get(u, {}).items():
                if not self._in_window(day, cutoff):
                    continue
                r = {"day": day}
                for k in _ROW_FIELDS:
                    r[k] = row.get(k, 0)
                r["compute_seconds"] = round(float(r.get("compute_seconds", 0)), 2)
                rows.append(r)
            rows.sort(key=lambda x: x["day"])
            out[u] = rows
        return out

    @staticmethod
    def _in_window(day: str, cutoff: float) -> bool:
        try:
            return datetime.fromisoformat(day).timestamp() >= cutoff
        except ValueError:
            return False

    @property
    def last_flush(self) -> float:
        return self._last_flush

    def today_total(self, user: str) -> int:
        """Prompt + completion tokens recorded for `user` today."""
        row = self._data.get(user, {}).get(date.today().isoformat())
        if row is None:
            return 0
        return int(row.get("prompt", 0)) + int(row.get("completion", 0))
