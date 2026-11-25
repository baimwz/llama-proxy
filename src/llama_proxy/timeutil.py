"""Local (Europe/Berlin) time helpers — DST-aware day boundaries.

The proxy LXC runs on UTC, but everything user-visible must follow the
local wall clock: day boundaries ("today" in the usage tables, per-day
token limits, the retry-after midnight) and displayed times. ``zoneinfo``
handles the CET/CEST (Sommer-/Winterzeit) switch automatically.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Single source of truth for the local timezone of this deployment.
TZ: ZoneInfo = ZoneInfo("Europe/Berlin")


def now_local() -> datetime:
    """Current time in the local timezone (timezone-aware)."""
    return datetime.now(TZ)


def today_local() -> date:
    """Today's date in the local timezone (DST-aware)."""
    return now_local().date()


def local_midnight_ts(day_offset: int = 0) -> float:
    """Unix timestamp of local midnight, shifted by ``day_offset`` days.

    ``day_offset=1`` is the next local midnight — the point at which
    per-day limits reset.
    """
    d = today_local() + timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()
