"""The single authoritative row per combo_id - see the "reconcile scattered combo
data" plan this was built for. Before this existed, the same combo_id's data was
scattered across many independent results_web_*.csv sweep-run files (the same
combo could appear in a dozen of them with different metrics and different
recorded start_date/end_date, with nothing anywhere picking a winner - _read_rows
in app.py just concatenates every loaded file's rows), plus a Force re-download
only ever merged a refreshed row back into whichever specific files the caller
happened to pass in, leaving every OTHER file's copy of that combo_id stale with
no mechanism that ever went looking for it.

This module is deliberately NOT a replacement for results_web_*.csv itself - each
sweep run's own timestamped file stays exactly as it is today, a legitimate
"what happened in this specific run" log. This is the answer to a different
question: "what do we currently, authoritatively know about this one combo_id,
right now" - every sweep/refresh/CAS-analysis activity upserts its result here in
addition to (not instead of) its own run's own file, so that question always has
exactly one place to be answered from.

Reuses src/store.py's existing single-file read/write primitives directly rather
than reinventing CSV I/O - update_rows/append_row/migrate_header_if_needed all
already do exactly what's needed here, they just were never pointed at a shared,
permanent, cross-run file before."""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src import store
from src.correlate import parse_trade_report

ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_PATH = ROOT / "output" / "combo_registry.csv"
# A combo whose strategy is already known under a different combo_id (see
# store.strategy_key / SweepUIConfig.skip_known_duplicate_strategies) gets
# captured here instead of being replayed - the queue Force re-download acts
# on. Plain append-only CSV, same convention as correlate_state.py's own
# refresh_audit.log.
PENDING_DUPLICATE_REFRESH_PATH = ROOT / "output" / "pending_duplicate_refresh.csv"

# New fields this registry adds on top of whatever columns a results_web_*.csv
# row already has (see compute_freshness_fields) - kept as a named tuple of
# column names, not just inlined, so callers building a row for upsert_rows and
# tests asserting on it both have one place to spell them consistently.
FRESHNESS_FIELDS = ["last_replayed_at", "report_window_start", "report_window_end", "report_complete"]

# How many days a report's own actual first/last trade-date may fall short of its
# recorded start_date/end_date before being flagged incomplete - allows for the
# gap between a configured calendar boundary and the nearest ACTUAL trading day
# under this combo's own DTE/weekday cadence (e.g. a Wednesday start_date for a
# combo that only ever trades on Tuesdays lands its first real trade a few days
# later), WITHOUT being loose enough to also excuse a genuinely missing occurrence
# - a weekly-cadence combo short by one whole cycle is indistinguishable from "one
# trading day's worth of alignment slack" once the tolerance approaches 7 days, so
# this is deliberately tighter than a full week. Calibrated against the real
# partial-download example confirmed live this session (recorded 2026-08-05 to
# 2026-09-07, actual data only 2026-08-11 to 2026-09-01 - a 6-day gap at each end,
# genuinely a missing occurrence, not alignment slack) - that case must fail.
BOUNDARY_TOLERANCE_DAYS = 4


def upsert_rows(rows: dict[str, dict[str, Any]]) -> None:
    """Writes every combo_id -> row into the registry: an existing combo_id's row
    is overwritten in place (store.update_rows), a genuinely new one is appended
    - there is no "not found, try elsewhere" case here the way there is for
    update_rows against one of many results_web_*.csv files, because the
    registry IS the one place.

    Unlike store.update_rows' own single-call convention (which assumes every
    row in one batch shares the same columns - true within one sweep's own
    file), rows passed here can come from combo_ids discovered under
    DIFFERENT sweep configs over time (different leg counts/strike modes/etc),
    with genuinely different column sets. The header is expanded to the UNION
    of every row's own keys BEFORE calling store.update_rows - confirmed live
    this session as a real bug otherwise: store.update_rows' own internal
    header check (and this function's old one-row "sample_fields") only ever
    look at ONE representative row, so any other row's columns not in that
    sample were silently dropped by csv.DictWriter's extrasaction="ignore" (a
    live migration run lost 7 real columns - legs.0.strike.upper/lower,
    target.kind/value, target_pct, stoploss_pct - across combos from
    differently-shaped sweeps, before this fix). Doing the union-based
    migrate_header_if_needed call here first means store.update_rows' own
    internal header check then finds nothing missing and correctly reuses the
    now-already-full existing header instead of a narrow sample.

    New rows are appended in ONE file open, not one store.append_row call per
    row - a sweep can introduce thousands of genuinely-new combo_ids in a
    single upsert_rows call, and opening/writing/closing the file that many
    times is exactly the per-item file-I/O cost this session already found and
    fixed once (the O(N x M) redownload-gaps hang, src/web/app.py's
    redownload_correlate_gaps) - not repeating it here."""
    if not rows:
        return
    all_fields: list[str] = []
    seen: set[str] = set()
    for row in rows.values():
        for key in row:
            if key not in seen:
                seen.add(key)
                all_fields.append(key)
    # migrate_header_if_needed returns `all_fields` unchanged (no file touched)
    # when REGISTRY_PATH doesn't exist yet - exactly the fieldnames the batched
    # append below needs to create it fresh. When the file DOES exist, this
    # expands its on-disk header first if any row here introduces a new column.
    fieldnames = store.migrate_header_if_needed(REGISTRY_PATH, all_fields)
    # update_rows itself is a no-op returning an empty set when the file doesn't
    # exist yet, so every row correctly falls through to the append below.
    updated = store.update_rows(REGISTRY_PATH, rows)
    new_rows = [row for cid, row in rows.items() if cid not in updated]
    if not new_rows:
        return
    file_exists = REGISTRY_PATH.exists()
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in new_rows:
            writer.writerow({col: ("" if row.get(col) is None else row.get(col)) for col in fieldnames})
        f.flush()


def read_registry() -> dict[str, dict[str, Any]]:
    """Every row currently in the registry, keyed by combo_id - the one place
    "what do we currently know about this combo" gets answered from."""
    if not REGISTRY_PATH.exists():
        return {}
    with REGISTRY_PATH.open(newline="") as f:
        return {row["combo_id"]: row for row in csv.DictReader(f) if row.get("combo_id")}


def _is_single_dte(dte: str | None) -> bool:
    return bool(dte) and "," not in dte


def report_completeness(
    entry_dates: list[str], *, dte: str | None, recorded_start: str | None, recorded_end: str | None,
) -> bool:
    """Does this report look like a genuine, uncontaminated, complete replay of its
    own recorded [start_date, end_date] window? Two independent checks, both must
    pass:

    1. Weekday consistency - if `dte` is a single specific value (not a comma-
       separated multi-select), every entry date must land on the SAME weekday.
       Catches the exact contamination bug confirmed live this session: a bare
       combo_id's Force re-download fell back to the saved sweep config's full
       multi-DTE set instead of its own recorded single DTE, silently mixing in
       every weekday's trades instead of just the one true weekday a single-DTE
       combo can ever trade on (one real example found this session: a "dte=0"
       combo that should only ever trade Thursdays had 245 trading days spread
       across every weekday instead of ~52 Thursdays).
    2. Boundary coverage - the report's own actual first/last trade date must
       reach within BOUNDARY_TOLERANCE_DAYS of its recorded start_date/end_date.
       Catches a genuinely short/partial download (confirmed live this session:
       a combo recorded as covering 2026-08-05 to 2026-09-07 whose file only
       actually had trades from 2026-08-11 to 2026-09-01 - nearly a week short at
       both ends, silently).

    A report with no entry dates at all is never complete - nothing to check
    against, and nothing useful to slice from either."""
    if not entry_dates:
        return False

    if _is_single_dte(dte):
        weekdays = {date.fromisoformat(d).weekday() for d in entry_dates}
        if len(weekdays) > 1:
            return False

    actual_start, actual_end = min(entry_dates), max(entry_dates)
    if recorded_start:
        gap = (date.fromisoformat(actual_start) - date.fromisoformat(recorded_start)).days
        if gap > BOUNDARY_TOLERANCE_DAYS:
            return False
    if recorded_end:
        gap = (date.fromisoformat(recorded_end) - date.fromisoformat(actual_end)).days
        if gap > BOUNDARY_TOLERANCE_DAYS:
            return False
    return True


def compute_freshness_fields(
    report_path: Path, *, dte: str | None, recorded_start: str | None, recorded_end: str | None,
) -> dict[str, Any]:
    """The four FRESHNESS_FIELDS for one combo, computed by actually parsing its
    trade report (parse_trade_report - the same parser everything else in this
    app already uses, no new I/O convention introduced) rather than trusting
    whatever start_date/end_date a row happens to already claim."""
    if not report_path.exists():
        return {"last_replayed_at": None, "report_window_start": None,
                "report_window_end": None, "report_complete": False}

    series = parse_trade_report(report_path)
    last_replayed_at = datetime.fromtimestamp(report_path.stat().st_mtime).isoformat()
    if not series:
        return {"last_replayed_at": last_replayed_at, "report_window_start": None,
                "report_window_end": None, "report_complete": False}

    entry_dates = sorted(series)
    complete = report_completeness(
        entry_dates, dte=dte, recorded_start=recorded_start, recorded_end=recorded_end,
    )
    return {
        "last_replayed_at": last_replayed_at,
        "report_window_start": entry_dates[0],
        "report_window_end": entry_dates[-1],
        "report_complete": complete,
    }
