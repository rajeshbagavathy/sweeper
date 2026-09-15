"""Combo "coverage" heatmap: entry-time x leg Stop-Loss% band grid.

Answers "what % of my swept combos landed where, and how good were they" at a
glance - each cell shows how many 'ok' combos fell into that time-slot/SL-band
combination, what share of the total that is, and the average of whatever quality
metric is selected. A results table with thousands of rows can't be scanned for
this; a grid of ~10x10 cells can.
"""
from __future__ import annotations

from typing import Any

from src.web import time_buckets

# Every metric offered is already a per-row numeric column in the results CSV -
# this exists purely to give the UI a labelled dropdown and a safe default/fallback.
METRICS: dict[str, str] = {
    "return_max_dd": "Return / Max DD",
    "reward_risk_ratio": "Reward : Risk",
    "win_rate": "Win %",
    "total_pnl": "Avg P&L",
}
DEFAULT_METRIC = "return_max_dd"

# The column for a cell with no recognizable leg Stop-Loss% (either the row predates
# the dual-basis feature and has neither column, or its SL is on the Underlying %
# basis - a different unit that isn't comparable on the same axis, see
# stoploss_pct_value below) - shown as its own band rather than silently dropped.
NO_SL_BAND = "n/a"


def stoploss_pct_value(row: dict) -> float | None:
    """Leg-level Stop Loss as a % of premium (the "Percent (%)" basis). Underlying %
    (a move in the underlying's own price, not the premium) is a different unit and
    is deliberately excluded here (returns None) rather than mixed onto the same
    band axis - see plan `enchanted-pondering-raccoon` / store._canonical_for_hash
    for the dual-basis leg risk feature this is reading. Falls back to the legacy
    flat column for rows from before that feature existed."""
    kind = row.get("leg_risk.stoploss_pct.kind")
    if kind:
        if kind != "percentage":
            return None
        raw = row.get("leg_risk.stoploss_pct.value")
    else:
        raw = row.get("leg_risk.stoploss_pct")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def sl_band_label(value: float | None, band_width: float) -> str:
    if value is None or band_width <= 0:
        return NO_SL_BAND
    lo = int(value // band_width) * band_width
    hi = lo + band_width
    return f"{lo:g}-{hi:g}%"


def _sl_band_sort_key(label: str) -> tuple[int, float]:
    # "n/a" always last, otherwise ascending by the band's lower bound.
    if label == NO_SL_BAND:
        return (1, 0.0)
    return (0, float(label.split("-")[0]))


def build_coverage_heatmap(
    rows: list[dict],
    *,
    interval_minutes: int = 15,
    sl_band_width: float = 10.0,
    metric: str = DEFAULT_METRIC,
) -> dict[str, Any]:
    """`rows` should already be scoped to status == "ok" and whatever instrument/
    DTE/entry-time-range the caller wants reflected - this only buckets and
    aggregates, it doesn't filter. Cells with zero combos are simply absent from
    `cells` (not zero-filled), so the frontend can tell "no data here" apart from
    "data here that happens to average to 0"."""
    if metric not in METRICS:
        metric = DEFAULT_METRIC

    time_labels = time_buckets.all_slot_labels(interval_minutes)
    grid: dict[str, dict[str, list[dict]]] = {}
    sl_bands_seen: set[str] = set()
    total = 0

    for row in rows:
        t_label = time_buckets.bucket_label(row.get("entry_time", ""), interval_minutes)
        if t_label is None:
            continue
        sl_label = sl_band_label(stoploss_pct_value(row), sl_band_width)
        sl_bands_seen.add(sl_label)
        grid.setdefault(t_label, {}).setdefault(sl_label, []).append(row)
        total += 1

    sl_bands = sorted(sl_bands_seen, key=_sl_band_sort_key)

    cells: dict[str, dict[str, dict[str, Any]]] = {}
    for t_label, by_band in grid.items():
        for sl_label, cell_rows in by_band.items():
            values: list[float] = []
            for r in cell_rows:
                raw = r.get(metric)
                if raw in (None, ""):
                    continue
                try:
                    values.append(float(raw))
                except ValueError:
                    continue
            avg = sum(values) / len(values) if values else None
            cells.setdefault(t_label, {})[sl_label] = {
                "count": len(cell_rows),
                "avg_metric": avg,
                "pct_of_total": (100.0 * len(cell_rows) / total) if total else 0.0,
            }

    return {
        "time_labels": time_labels,
        "sl_bands": sl_bands,
        "metric": metric,
        "metric_label": METRICS[metric],
        "total": total,
        "cells": cells,
    }
