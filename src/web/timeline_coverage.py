"""Two coverage charts for the Analyze page:

- strategy_timing_coverage: one entry per DISTINCT (entry_time, exit_time) pair
  actually present among the currently-filtered candidates, with a count and a
  handful of averaged performance metrics for that exact pair. Deliberately NOT a
  continuous time axis and NOT overlap-aware - two groups whose spans happen to
  overlap (e.g. 10 strategies at 09:30-12:00, 25 different strategies at
  10:00-12:00) are entirely independent counts, never merged or blended, since
  each (entry_time, exit_time) combination is a genuinely distinct strategy
  timing in its own right (confirmed against a real misunderstanding this was
  built to correct: an earlier fixed-width-slot version conflated overlapping
  spans together, which is exactly what the user does NOT want). Answers "how
  many strategies use this exact timing, and how do they perform."
- backtest_period_coverage: for each calendar month, how many candidates' own
  backtest window (start_date to end_date) actually covers that month. Answers
  "which historical period is my candidate pool actually backed by."

Both are pure aggregation - `rows` should already be scoped to status == "ok" and
whatever instrument/DTE/entry-time/exit-time filters the caller wants reflected,
same convention as heatmap.build_coverage_heatmap.
"""
from __future__ import annotations

from typing import Any

# Averaged per (entry_time, exit_time) group - same metric names used everywhere
# else in this app (see analyze.html's own PRIORITY_METRICS), not new jargon.
_METRIC_FIELDS = ["return_max_dd", "reward_risk_ratio", "win_rate", "expectancy", "max_profit", "max_loss"]


def _avg(group_rows: list[dict[str, Any]], field: str) -> float | None:
    values = []
    for row in group_rows:
        raw = row.get(field)
        if raw in (None, ""):
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return sum(values) / len(values) if values else None


def strategy_timing_coverage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{"label": "09:30 to 12:00", "entry_time": "09:30", "exit_time": "12:00",
    "count": N, "avg_return_max_dd": .., "avg_reward_risk_ratio": .., "avg_win_rate":
    .., "avg_expectancy": .., "avg_max_profit": .., "avg_max_loss": ..}, ...],
    sorted by count descending (the most common strategy timing first). A row
    missing entry_time or exit_time is excluded; an average is None when no row in
    the group has a parseable value for that metric."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        entry = row.get("entry_time") or ""
        exit_ = row.get("exit_time") or ""
        if not entry or not exit_:
            continue
        groups.setdefault((entry, exit_), []).append(row)

    out = []
    for (entry, exit_), group_rows in groups.items():
        entry_dict = {
            "label": f"{entry} to {exit_}",
            "entry_time": entry,
            "exit_time": exit_,
            "count": len(group_rows),
        }
        for field in _METRIC_FIELDS:
            entry_dict[f"avg_{field}"] = _avg(group_rows, field)
        out.append(entry_dict)

    out.sort(key=lambda o: -o["count"])
    return out


def _month_label(iso_date: str) -> str | None:
    if not iso_date or len(iso_date) < 7:
        return None
    return iso_date[:7]  # "YYYY-MM-DD" -> "YYYY-MM", cheap and exact for this format


def _month_range(start_label: str, end_label: str) -> list[str]:
    """Every "YYYY-MM" label from start_label to end_label inclusive, contiguous -
    so a gap between two covered periods still shows as a real (zero-count) bar."""
    start_y, start_m = (int(x) for x in start_label.split("-"))
    end_y, end_m = (int(x) for x in end_label.split("-"))
    labels = []
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        labels.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return labels


def backtest_period_coverage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{"label": "2026-08", "count": N}, ...] for every month between the
    earliest start_date and latest end_date seen across `rows` (inclusive,
    contiguous - zero-count months included same as session_coverage above). A
    row counts toward EVERY month its own backtest window actually spans, not
    just the month it starts in - a year-long combo should show up in all ~12
    months it covers, not just the first. A row with a missing/unparseable
    start_date or end_date is simply excluded."""
    row_months: list[tuple[str, str]] = []
    all_labels: set[str] = set()
    for row in rows:
        start_label = _month_label(row.get("start_date") or "")
        end_label = _month_label(row.get("end_date") or "")
        if not start_label or not end_label or start_label > end_label:
            continue
        row_months.append((start_label, end_label))
        all_labels.add(start_label)
        all_labels.add(end_label)

    if not all_labels:
        return []

    full_range = _month_range(min(all_labels), max(all_labels))
    counts = {label: 0 for label in full_range}
    for start_label, end_label in row_months:
        for label in _month_range(start_label, end_label):
            counts[label] += 1
    return [{"label": label, "count": counts[label]} for label in full_range]
