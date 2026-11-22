"""Daily slots in Kyiv time."""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # very old tzdata
    TZ = ZoneInfo("Europe/Kiev")

# (start, end) in minutes from midnight, from data/daily_plan.json; the selling slot is in the evening (17-20 peak)
WINDOWS = [(8 * 60 + 30, 9 * 60 + 30), (11 * 60 + 30, 13 * 60), (15 * 60, 16 * 60 + 30), (19 * 60, 20 * 60 + 30)]
MIN_GAP = 110
LAST_PUBLISH_MIN = 22 * 60 + 45  # do not publish after 22:45
SPACING_MIN = 75                 # at least 75 minutes between our posts


def now_kyiv() -> datetime:
    return datetime.now(TZ)


def make_slots(rnd: random.Random) -> list[int]:
    for _ in range(200):
        mins = [rnd.randint(a, b) for a, b in WINDOWS]
        if all(b - a >= MIN_GAP for a, b in zip(mins, mins[1:])):
            return mins
    return [a for a, _ in WINDOWS]


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes_of(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def since_minutes(iso: str | None, now: datetime) -> float:
    if not iso:
        return 1e9
    return (now - parse_iso(iso)).total_seconds() / 60


def ago(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
