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
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not load usage data: %s", exc)

    def record(
        self, user: str, prompt_tokens: int, completion_tokens: int, requests: int = 1
    ) -> None:
        day = date.today().isoformat()
        per_user = self._data.setdefault(user, {})
        row = per_user.setdefault(day, {"prompt": 0, "completion": 0, "requests": 0})
        row["prompt"] += int(prompt_tokens)
        row["completion"] += int(completion_tokens)
        row["requests"] += int(requests)

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
        """Aggregated totals for the last N days (for /ui and future CLI)."""
        cutoff = datetime.combine(date.today(), datetime.min.time()).timestamp() - days * 86400
        out: dict[str, dict[str, int]] = {}
        for user, per_day in self._data.items():
            agg = {"prompt": 0, "completion": 0, "requests": 0}
            for day, row in per_day.items():
                try:
                    ts = datetime.fromisoformat(day).timestamp()
                except ValueError:
                    continue
                if ts >= cutoff:
                    for k in agg:
                        agg[k] += int(row.get(k, 0))
            out[user] = agg
        return out

    @property
    def last_flush(self) -> float:
        return self._last_flush

    def today_total(self, user: str) -> int:
        """Prompt + completion tokens recorded for `user` today."""
        row = self._data.get(user, {}).get(date.today().isoformat())
        if row is None:
            return 0
        return int(row.get("prompt", 0)) + int(row.get("completion", 0))
