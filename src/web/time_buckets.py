"""Buckets results by entry_time into fixed-width slots anchored to market open
(09:15), not midnight - so a 30-min or 1-hour grid lands on 09:15, 09:45, 10:15... the
way a trader actually thinks about entry windows, not 09:00, 09:30, 10:00..."""
from __future__ import annotations

from datetime import datetime, timedelta

MARKET_OPEN = "09:15"
# Was "15:30" - since CAS (Contingent/Closing Auction Session) was enabled about a
# month ago, AlgoTest allows entry/exit times up to 15:40, not just the classic
# 09:15-15:30 cash-market session (confirmed live: 15:38 is a legitimate exit time
# now, not an AM/PM or 24-hour mixup - see TimeRange._within_market_hours below).
MARKET_CLOSE = "15:40"


def bucket_label(entry_time: str, interval_minutes: int) -> str | None:
    """Which slot (as "HH:MM") entry_time falls into, or None if unparseable."""
    try:
        t = datetime.strptime(entry_time, "%H:%M")
    except (ValueError, TypeError):
        return None
    anchor = datetime.strptime(MARKET_OPEN, "%H:%M")
    delta_minutes = int((t - anchor).total_seconds() // 60)
    bucket_offset = (delta_minutes // interval_minutes) * interval_minutes
    return (anchor + timedelta(minutes=bucket_offset)).strftime("%H:%M")


def all_slot_labels(interval_minutes: int, start: str = MARKET_OPEN, end: str = MARKET_CLOSE) -> list[str]:
    """Every slot label from market open to close, even ones no row falls into -
    lets the UI show a 0-count button so you know not to bother clicking it."""
    t = datetime.strptime(start, "%H:%M")
    end_t = datetime.strptime(end, "%H:%M")
    labels = []
    while t <= end_t:
        labels.append(t.strftime("%H:%M"))
        t += timedelta(minutes=interval_minutes)
    return labels


def bucket_counts(rows: list[dict], interval_minutes: int) -> list[dict]:
    """rows -> [{"label": "09:15", "count": N}, ...] for every slot in the trading
    day, in order, including zero-count slots."""
    counts: dict[str, int] = {}
    for row in rows:
        label = bucket_label(row.get("entry_time", ""), interval_minutes)
        if label is not None:
            counts[label] = counts.get(label, 0) + 1
    return [{"label": label, "count": counts.get(label, 0)} for label in all_slot_labels(interval_minutes)]


def filter_by_bucket(rows: list[dict], interval_minutes: int, bucket: str) -> list[dict]:
    return [r for r in rows if bucket_label(r.get("entry_time", ""), interval_minutes) == bucket]
