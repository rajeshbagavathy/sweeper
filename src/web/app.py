from __future__ import annotations

import csv
import functools
import os
import platform
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scripts.build_cas_subset import DEFAULT_CAS_START, run_cas_subset
from src.store import strategy_key
from src.sweep import TooManyCombinations
from src.web import executions as executions_mod
from src.web import heatmap
from src.web import param_breakdown
from src.web import portfolio
from src.web import time_buckets
from src.web import timeline_coverage
from src.web.combo_launcher import basket_save_state, combo_launcher_state
from src.web.correlate_state import REPORTS_DIR, compute_correlation, correlate_state
from src.web.expand import estimate_ui_config, expand_ui_config, load_ui_config, save_ui_config
from src.web.login_helper import login_helper_state
from src.web.models import SweepUIConfig
from src.web.narrow import combined_sort_key, narrow_config, numeric_sort_key, profitability_gated_key
from src.web import portfolio_sweep_favorites
from src.web import registry
from src.web.portfolio_sweep_state import build_grid, frange_inclusive, portfolio_sweep_state
from src.web.regime_state import (
    cas_slot_buckets,
    list_downloaded_regime_windows,
    regime_download_state,
    regime_sweep_state,
    regime_window_dir,
    shortlist_for_download,
)
from src.web.state import OUTPUT_DIR, _known_strategy_keys, run_state

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Each grid point is a fast, local re-computation (no browser, no new backtests) -
# this just guards against a UI typo (e.g. a step of 0.001) producing an
# accidental six-figure grid, not against genuine legitimate use.
MAX_SWEEP_GRID_SIZE = 3000

app = FastAPI(title="AlgoTest Sweeper")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/analyze")
def analyze_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "analyze.html")


@app.get("/api/config")
def get_config() -> SweepUIConfig:
    return load_ui_config()


@app.post("/api/config")
def post_config(cfg: SweepUIConfig) -> dict:
    save_ui_config(cfg)
    return {"ok": True}


@app.post("/api/dry-run")
def dry_run(cfg: SweepUIConfig) -> dict:
    # Exact for a config small enough that it's already fast; a fast sampled estimate
    # otherwise (see estimate_ui_config) - a huge sweep's exact post-exclude count used
    # to mean tens of seconds of blocking eval() work on every Preview click.
    try:
        result = estimate_ui_config(cfg)
    except TooManyCombinations as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # How many of these would actually be captured as known-duplicate strategies
    # (see SweepUIConfig.skip_known_duplicate_strategies) instead of replayed -
    # shown in Preview so this is visible BEFORE committing to a run, not only
    # discovered afterward in the completion summary. Exact for the exact-count
    # case (expand_ui_config is already fast at this size, by definition);
    # extrapolated from the same sampled survival estimate `count` itself uses
    # for the large-sweep case, labeled with the same "estimated" flag.
    result["duplicate_count"] = 0
    if cfg.skip_known_duplicate_strategies:
        known = _known_strategy_keys()
        if known:
            if not result["estimated"]:
                exact_combos = expand_ui_config(cfg)
                result["duplicate_count"] = sum(
                    1 for c in exact_combos if strategy_key(c) in known
                )
            else:
                sample = result["sample"]
                if sample:
                    sample_dupes = sum(1 for c in sample if strategy_key(c) in known)
                    result["duplicate_count"] = round(result["count"] * sample_dupes / len(sample))
    return result


@app.post("/api/run")
def start_run(cfg: SweepUIConfig, resume: bool = False, resume_csv: str | None = None) -> dict:
    if correlate_state.is_running():
        # Both would fight over the same account browser profiles - a Correlate job
        # already holds them.
        raise HTTPException(status_code=409, detail="A Correlate top N job is running - stop it first.")
    save_ui_config(cfg)
    try:
        run_state.start(cfg, resume=resume, resume_csv=resume_csv)
    except TooManyCombinations as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/accounts")
def get_accounts() -> dict:
    """Which AlgoTest login each parallel-tabs account slot in the UI actually
    drives - the .env vars themselves are only loaded lazily, at run/launch time
    (see RunState._run, combo_launcher, correlate_state), so this loads them too
    rather than assuming they're already in os.environ. Emails only, never the
    passwords - this just labels "Account 2" as the login it really is instead of
    a number that means nothing without opening .env."""
    load_dotenv()
    return {
        "account1": os.environ.get("ALGOTEST_EMAIL"),
        "account2": os.environ.get("ALGOTEST_EMAIL_2"),
        "account3": os.environ.get("ALGOTEST_EMAIL_3"),
    }


@app.post("/api/stop")
def stop_run() -> dict:
    run_state.stop()
    return {"ok": True}


@app.get("/api/status")
def get_status() -> dict:
    return run_state.snapshot()


@functools.lru_cache(maxsize=8)
def _read_rows_cached(cache_key: tuple[tuple[str, int], ...]) -> tuple[list[dict], list[str]]:
    """The actual parse, keyed on (path, mtime_ns) for every file in the set -
    cache_key changes the instant any file is written to (a running sweep still
    appending, a fresh Force re-download, this script's own new *_cas_all.csv),
    so a hit here always reflects what's really on disk right now, never stale
    data. maxsize=8 is a small LRU, not "cache everything ever queried" - this
    is a single-user local tool (see executions.py's own same assumption); a
    handful of recently-used file-set combinations is what one person switching
    between a few loaded selections actually needs, without growing unbounded
    if a very large number of distinct combinations were ever tried in one
    server lifetime."""
    rows: list[dict] = []
    columns: list[str] = []
    for path_str, _mtime_ns in cache_key:
        with open(path_str, newline="") as f:
            reader = csv.DictReader(f)
            for col in reader.fieldnames or []:
                if col not in columns:
                    columns.append(col)
            rows.extend(reader)
    return rows, columns


def _read_rows(csv_paths: list[Path]) -> tuple[list[dict], list[str]]:
    """Read and concatenate rows from one or more CSVs - shared by the single-file
    run_state-backed endpoints below and the multi-file /api/analyze/* endpoints, so
    filtering/sorting logic never has to be written twice.

    Cached (see _read_rows_cached) - confirmed live this was the actual cause of
    the Analyze page feeling slow once the loaded CSVs grew into the tens of
    thousands of rows: a SINGLE filter click fans out to several endpoints
    (results, heatmap, timeline-coverage, param breakdown, exit-time options,
    ...), each independently calling this function with the exact same
    csv_paths - meaning one click meant re-opening and re-parsing the same
    files from disk 4-5+ times over, sequentially, even though the actual
    filtering logic itself was never the slow part (measured: ~0.35s to parse
    37,000 rows across 3 files, vs ~0.1s to filter/sort them - the redundant
    re-parsing was the multiplier, not a fundamentally slow algorithm).

    Always returns a FRESH top-level list (a shallow copy of whatever's
    cached), never the cached list object itself - _filtered_results (and
    others) call .sort() directly on what they're handed, in place, whenever
    every optional filter happens to be None (the common "nothing filtered
    yet" case skips every list-comprehension reassignment that would
    otherwise have produced a fresh list on its own). Handing back the SAME
    cached list there would let one caller's sort silently reorder it for
    every other caller sharing that cache entry - confirmed by checking every
    existing call site before adding this cache, not assumed."""
    existing = [p for p in csv_paths if p.exists()]
    cache_key = tuple((str(p), p.stat().st_mtime_ns) for p in existing)
    rows, columns = _read_rows_cached(cache_key)
    return list(rows), list(columns)


def _filtered_results(
    rows: list[dict],
    columns: list[str],
    *,
    limit: int,
    interval: int | None,
    bucket: str | None,
    sort_by: str,
    dte: str | None,
    instrument: str | None,
    rmdd_weight: float = 0.65,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    combo_search: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    # A combo_id search is a direct lookup, not one more filter to combine with the
    # rest - the whole point is finding a specific combo regardless of whatever
    # instrument/DTE/time filter happens to be active, so it short-circuits
    # everything else below rather than being AND-ed with it.
    if combo_search:
        needle = combo_search.strip().lower()
        matches = [r for r in rows if needle in r.get("combo_id", "").lower()]
        matches.sort(key=lambda r: r.get("combo_id", ""))
        return {"rows": matches[:limit], "columns": columns}

    # Instrument is applied first, per the user's request - multiple indices can
    # share one CSV (or, via /api/analyze, several CSVs at once), so narrowing to a
    # single instrument before any other filter avoids e.g. NIFTY and BANKNIFTY rows
    # blending together in the same time-bucket/DTE view.
    if instrument is not None:
        rows = [r for r in rows if r.get("instrument", "") == instrument]

    if interval is not None and bucket is not None:
        rows = time_buckets.filter_by_bucket(rows, interval, bucket)

    # Manual entry-time range - independent of (and typically used instead of) the
    # fixed-size bucket picker above, for picking an arbitrary window (e.g. "09:20 to
    # 09:52") a 15/30/60-min grid can't express. "HH:MM" strings compare correctly as
    # plain strings since they're zero-padded and same-day. A row with no entry_time
    # recorded can't be confirmed in-range, so it's excluded rather than guessed at.
    if entry_time_from is not None:
        rows = [r for r in rows if r.get("entry_time") and r["entry_time"] >= entry_time_from]
    if entry_time_to is not None:
        rows = [r for r in rows if r.get("entry_time") and r["entry_time"] <= entry_time_to]

    if dte is not None:
        rows = [r for r in rows if r.get("dte", "") == dte]

    # Backtest period - an exact-match pill filter (see _date_range_options), the
    # concrete tool for "make sure all reports have the same period": one click
    # isolates the loaded pool to a single genuine (start_date, end_date) window
    # before comparing reward:risk/etc. across rows, instead of silently mixing a
    # majority window with whatever a force-redownload rolled a minority forward to.
    if date_range is not None:
        rows = [r for r in rows if _row_date_range_label(r) == date_range]

    # A genuine RANGE over each row's own start_date - NOT the same thing as
    # date_range above (an exact single-window pill match). See _scope_ok_rows'
    # own docstring for why this exists: different sweeps of the same CAS-era
    # period end up with slightly different end_dates, so no single exact pill
    # can ever pool "everything from Aug 1 onward" - this can, in one filter,
    # with no need to know in advance which files/pills those rows live in.
    if date_from is not None:
        rows = [r for r in rows if r.get("start_date") and r["start_date"] >= date_from]
    if date_to is not None:
        rows = [r for r in rows if r.get("start_date") and r["start_date"] <= date_to]

    # Exit time - an exact-match pill filter (see _exit_time_options) for picking one
    # of the handful of specific values actually present, PLUS an optional manual
    # range (same "HH:MM" string-comparison convention as entry_time_from/to) for
    # catching a whole window of them at once (e.g. every morning-session exit
    # between 12:45 and 13:50) - the two combine rather than one replacing the other.
    if exit_time is not None:
        rows = [r for r in rows if r.get("exit_time", "") == exit_time]
    if exit_time_from is not None:
        rows = [r for r in rows if r.get("exit_time") and r["exit_time"] >= exit_time_from]
    if exit_time_to is not None:
        rows = [r for r in rows if r.get("exit_time") and r["exit_time"] <= exit_time_to]

    # "combined" isn't a real CSV column - it's a blended Return/MaxDD + Reward:Risk
    # ranking (see narrow.combined_sort_key), computed here since it needs the whole
    # filtered rows list to normalize against, not just one row at a time.
    key_fn = combined_sort_key(rows, rmdd_weight=rmdd_weight) if sort_by == "combined" else numeric_sort_key(sort_by)
    # Profitability is a hard prerequisite for "best," not one more weighted
    # ingredient - see narrow.profitability_gated_key for why RMDD/Reward:Risk alone
    # can't guarantee a money-losing strategy never outranks a profitable one.
    rows.sort(key=profitability_gated_key(key_fn), reverse=True)
    return {"rows": rows[:limit], "columns": columns}


def _instrument_options(rows: list[dict]) -> dict:
    """Every distinct instrument actually present, with counts - lets the UI show one
    filter pill per index instead of guessing what's there."""
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    counts: dict[str, int] = {}
    for row in ok_rows:
        label = row.get("instrument", "")
        counts[label] = counts.get(label, 0) + 1
    # "unknown" (rows with no instrument recorded) last, otherwise most-populous first.
    options = sorted(
        ({"label": label, "count": count} for label, count in counts.items()),
        key=lambda o: (o["label"] == "", -o["count"]),
    )
    return {"options": options}


def _row_date_range_label(row: dict) -> str:
    """"{start_date} to {end_date}" - reconstructed from the row's own two columns
    rather than stored as a new field, since both already exist on every row."""
    return f"{row.get('start_date', '')} to {row.get('end_date', '')}"


def _date_range_options(rows: list[dict], instrument: str | None = None, dte: str | None = None) -> dict:
    """Every distinct (start_date, end_date) backtest period actually present among
    the currently loaded rows, with counts - most-populous first, same convention
    as _instrument_options/_dte_options. This is the whole point of the "backtest
    period visibility" feature: a force-redownload's roll_date_window
    (correlate_state.py) can silently advance a MINORITY of rows' own dates forward
    while the rest keep their original window (confirmed live against a real
    results_web_*.csv: 8418 rows at one period, 914 at another) - Reward:Risk isn't
    directly comparable across rows that don't share the same period, so the UI
    needs to be able to show "these N rows use a different period than the rest,"
    not just the numbers themselves."""
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if instrument is not None:
        ok_rows = [r for r in ok_rows if r.get("instrument", "") == instrument]
    if dte is not None:
        ok_rows = [r for r in ok_rows if r.get("dte", "") == dte]

    counts: dict[str, int] = {}
    for row in ok_rows:
        label = _row_date_range_label(row)
        counts[label] = counts.get(label, 0) + 1

    options = sorted(
        ({"label": label, "count": count} for label, count in counts.items()),
        key=lambda o: -o["count"],
    )
    return {"options": options}


def _dte_options(rows: list[dict], instrument: str | None, date_range: str | None = None) -> dict:
    """Every distinct DTE combination actually present (e.g. "0", "0,1,2"), with
    counts. Rows predating DTE tracking show up as "" (unknown). Scoped to
    `instrument`/`date_range` when given, so counts reflect whichever filters
    already narrow the results below (instrument is the first filter applied,
    backtest period is the second)."""
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if instrument is not None:
        ok_rows = [r for r in ok_rows if r.get("instrument", "") == instrument]
    if date_range is not None:
        ok_rows = [r for r in ok_rows if _row_date_range_label(r) == date_range]

    counts: dict[str, int] = {}
    for row in ok_rows:
        label = row.get("dte", "")
        counts[label] = counts.get(label, 0) + 1

    # "unknown" (pre-DTE-tracking rows) last, otherwise most-populous combination first.
    options = sorted(
        ({"label": label, "count": count} for label, count in counts.items()),
        key=lambda o: (o["label"] == "", -o["count"]),
    )
    return {"options": options}


def _exit_time_options(
    rows: list[dict], instrument: str | None, dte: str | None,
    entry_time_from: str | None = None, entry_time_to: str | None = None,
    date_range: str | None = None,
) -> dict:
    """Every distinct exit_time actually present, with counts - a real backtest's
    exit times cluster into a handful of specific values (often just one or two per
    sweep, since exit_time is commonly fixed or a narrow interval), not a smooth
    continuum the way entry_time can be - so a pill-per-value picker (like DTE/
    Instrument) is a better fit here than a generic 15/30/60-min bucket grid. Sorted
    chronologically (a time genuinely has an order, unlike DTE's "0,1,2" combination
    strings) rather than by count, so the picker reads like a timeline. Scoped to
    `instrument`/`dte`/`date_range`/`entry_time_from`/`entry_time_to` when given,
    matching the "each option list reflects whichever filters already sit above it"
    convention DTE follows for Instrument - without the entry-time scoping, loading
    several different sweeps together (e.g. a CAS-window sweep entering 15:14-15:35
    plus an unrelated midday sweep entering 11:40-12:00) would show every exit time
    from EVERY sweep regardless of which entry-time range is actually selected, even
    though exit_time is typically fixed per sweep alongside its own entry_time -
    confirmed live: filtering to entry_time 15:14-15:35 still showed 11:15/11:30/
    11:45/13:25 as exit-time options, none of which belong to that entry window."""
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if instrument is not None:
        ok_rows = [r for r in ok_rows if r.get("instrument", "") == instrument]
    if dte is not None:
        ok_rows = [r for r in ok_rows if r.get("dte", "") == dte]
    if date_range is not None:
        ok_rows = [r for r in ok_rows if _row_date_range_label(r) == date_range]
    if entry_time_from is not None:
        ok_rows = [r for r in ok_rows if r.get("entry_time") and r["entry_time"] >= entry_time_from]
    if entry_time_to is not None:
        ok_rows = [r for r in ok_rows if r.get("entry_time") and r["entry_time"] <= entry_time_to]

    counts: dict[str, int] = {}
    for row in ok_rows:
        label = row.get("exit_time", "")
        counts[label] = counts.get(label, 0) + 1

    # "unknown" (blank exit_time) last, otherwise chronological ("HH:MM" strings sort
    # correctly as plain strings - zero-padded, same day, same convention already
    # used for entry_time_from/to comparisons above).
    options = sorted(
        ({"label": label, "count": count} for label, count in counts.items()),
        key=lambda o: (o["label"] == "", o["label"]),
    )
    return {"options": options}


def _time_bucket_counts(rows: list[dict], interval: int, instrument: str | None) -> dict:
    """Counts of 'ok' results per entry-time slot (see time_buckets.py for why the
    grid is anchored to market open, not midnight) - so the UI can show a count on
    every slot button, including ones with zero results. Scoped to `instrument` when
    given (instrument is the first filter applied - see _filtered_results)."""
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if instrument is not None:
        ok_rows = [r for r in ok_rows if r.get("instrument", "") == instrument]
    return {"buckets": time_buckets.bucket_counts(ok_rows, interval)}


def _scope_ok_rows(
    rows: list[dict],
    *,
    instrument: str | None,
    dte: str | None,
    entry_time_from: str | None,
    entry_time_to: str | None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Same instrument/DTE/backtest-period/entry-time-range/exit-time scoping as
    _filtered_results, minus the sort+limit - the coverage heatmap aggregates over
    its *entire* scoped set (every band needs its true count), so truncating to a
    "top N" first would silently undercount every cell instead of just the ones
    near the cutoff.

    date_from/date_to are a genuine RANGE over each row's own start_date - NOT
    the same thing as date_range (an exact single (start_date, end_date) pill
    match). Confirmed live this distinction matters: different sweeps of the
    same CAS-era period end up with slightly different end_dates (each was run
    on whatever day it happened to run), so an exact-match pill can never pool
    "everything from Aug 1 onward" in one shot - you'd have to know and pick
    every distinct pill one at a time. date_from alone (date_to is optional,
    rarely needed) answers "which combos' backtest STARTED on/after this date"
    across however many files/pills that spans, with no need to know in
    advance which specific files or pills those combos happen to live in."""
    rows = [r for r in rows if r.get("status") == "ok"]
    if instrument is not None:
        rows = [r for r in rows if r.get("instrument", "") == instrument]
    if dte is not None:
        rows = [r for r in rows if r.get("dte", "") == dte]
    if date_range is not None:
        rows = [r for r in rows if _row_date_range_label(r) == date_range]
    if date_from is not None:
        rows = [r for r in rows if r.get("start_date") and r["start_date"] >= date_from]
    if date_to is not None:
        rows = [r for r in rows if r.get("start_date") and r["start_date"] <= date_to]
    if entry_time_from is not None:
        rows = [r for r in rows if r.get("entry_time") and r["entry_time"] >= entry_time_from]
    if entry_time_to is not None:
        rows = [r for r in rows if r.get("entry_time") and r["entry_time"] <= entry_time_to]
    if exit_time is not None:
        rows = [r for r in rows if r.get("exit_time", "") == exit_time]
    if exit_time_from is not None:
        rows = [r for r in rows if r.get("exit_time") and r["exit_time"] >= exit_time_from]
    if exit_time_to is not None:
        rows = [r for r in rows if r.get("exit_time") and r["exit_time"] <= exit_time_to]
    return rows


@app.get("/api/results")
def get_results(
    limit: int = 50,
    interval: int | None = None,
    bucket: str | None = None,
    sort_by: str = "return_max_dd",
    dte: str | None = None,
    instrument: str | None = None,
    rmdd_weight: float = 0.65,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    combo_search: str | None = None,
) -> dict:
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return {"rows": [], "columns": []}
    rows, columns = _read_rows([Path(csv_path)])
    return _filtered_results(
        rows,
        columns,
        limit=limit,
        interval=interval,
        bucket=bucket,
        sort_by=sort_by,
        dte=dte,
        instrument=instrument,
        rmdd_weight=rmdd_weight,
        entry_time_from=entry_time_from,
        entry_time_to=entry_time_to,
        exit_time=exit_time,
        exit_time_from=exit_time_from,
        exit_time_to=exit_time_to,
        combo_search=combo_search,
    )


def _correlate_top_rows(
    top_n: int, interval: int | None, bucket: str | None, sort_by: str, dte: str | None,
    instrument: str | None, rmdd_weight: float, csv_file: list[str] | None = None,
    entry_time_from: str | None = None, entry_time_to: str | None = None,
    exit_time: str | None = None, exit_time_from: str | None = None, exit_time_to: str | None = None,
    date_range: str | None = None, date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    """The exact top-N rows the results grid is showing right now (same params as
    /api/results, or /api/analyze/results when `csv_file` is given) - shared by both
    Correlate steps so "download" and "compute" always operate on the same set
    without the caller re-deriving it twice.

    `csv_file` lets this follow a *loaded saved execution* instead of whatever the
    live run_state happens to hold - without it, Correlate silently kept acting on
    the last-active run's CSV even after loading a different execution in the UI,
    which looked like it was "downloading the wrong trades."

    `date_range`/`date_from`/`date_to` - the same Backtest-period/date-range filter
    active on the page above - used to be silently dropped here (neither /api/
    correlate/download nor /api/correlate/compute even declared these params, so
    FastAPI discarded them even though the frontend sent them): confirmed live,
    a "since 2026-08-01" filter had zero effect on which candidates Uncorrelated
    strategies actually downloaded/correlated - the top-N pool was drawn from the
    ENTIRE loaded file(s), so a basket built and saved under that filter could be
    dominated by older, no-longer-representative combos and then genuinely
    underperform when judged against the filtered window it was supposed to be
    scoped to."""
    if csv_file:
        paths = [Path(p) for p in csv_file]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise HTTPException(status_code=400, detail=f"CSV file(s) not found: {', '.join(missing)}")
    else:
        csv_path = run_state.snapshot().get("csv_path")
        if not csv_path or not Path(csv_path).exists():
            raise HTTPException(status_code=400, detail="No results yet to correlate - run the sweep first.")
        paths = [Path(csv_path)]

    rows, columns = _read_rows(paths)
    # Same "successful rows only" convention as narrow_config - an error row has no
    # meaningful backtest to re-download, and would just sink to the bottom of any
    # numeric sort anyway.
    rows = [r for r in rows if r.get("status") == "ok"]
    filtered = _filtered_results(
        rows, columns, limit=top_n, interval=interval, bucket=bucket, sort_by=sort_by,
        dte=dte, instrument=instrument, rmdd_weight=rmdd_weight,
        entry_time_from=entry_time_from, entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to,
        date_range=date_range, date_from=date_from, date_to=date_to,
    )
    return filtered["rows"]


@app.post("/api/correlate/download")
def download_correlate_reports(
    top_n: int = 20,
    interval: int | None = None,
    bucket: str | None = None,
    sort_by: str = "combined",
    dte: str | None = None,
    instrument: str | None = None,
    rmdd_weight: float = 0.65,
    csv_file: list[str] = Query(default=[]),
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    force: bool = False,
    parallelism: int | None = None,
    parallelism_account2: int | None = None,
    parallelism_account3: int | None = None,
) -> dict:
    """Step 1: replay+download (or reuse a cached copy of) the trade report for the
    top `top_n` rows exactly as currently filtered/sorted in the results grid. Doesn't
    compute correlation itself - see /api/correlate/compute for that, a separate step
    so downloading and analyzing aren't bundled into one opaque, unstoppable-feeling
    action.

    `force=True` ("Force re-download & update results" in the UI) always replays
    every row - even ones already cached - and also backfills each combo's stored
    row with freshly re-scraped metrics (brokerage/taxes included, see
    config/selectors.yaml), keeping a large candidate pool current before building
    a basket off it instead of it silently drifting stale.

    `parallelism`/`parallelism_account2`/`parallelism_account3` (all optional)
    override the saved sweep config's own worker counts for just this run - see
    CorrelateState.start's own docstring for why this exists as an explicit
    override rather than reusing whatever the homepage's parallel-tabs fields show."""
    if run_state.is_running():
        raise HTTPException(status_code=409, detail="A sweep is running - stop it first.")
    rows = _correlate_top_rows(
        top_n, interval, bucket, sort_by, dte, instrument, rmdd_weight, csv_file or None,
        entry_time_from, entry_time_to, exit_time, exit_time_from, exit_time_to,
        date_range, date_from, date_to,
    )
    # Same resolution _correlate_top_rows itself uses internally - needed here too
    # so a forced merge knows which file(s) to write the refreshed rows back into.
    if csv_file:
        csv_paths = [Path(p) for p in csv_file]
    else:
        run_csv_path = run_state.snapshot().get("csv_path")
        csv_paths = [Path(run_csv_path)] if run_csv_path else []
    try:
        correlate_state.start(
            rows, csv_paths, force=force,
            parallelism=parallelism, parallelism_account2=parallelism_account2,
            parallelism_account3=parallelism_account3,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/correlate/status")
def get_correlate_status() -> dict:
    return correlate_state.snapshot()


@app.post("/api/correlate/stop")
def stop_correlate() -> dict:
    correlate_state.stop()
    return {"ok": True}


@app.get("/api/duplicate-refresh/pending")
def get_pending_duplicate_refresh() -> dict:
    """Every strategy a sweep captured instead of replaying (see SweepUIConfig.
    skip_known_duplicate_strategies) - already known under a different combo_id
    (a different date range), ready to act on via /api/duplicate-refresh/start
    regardless of whether the sweep that found them is still running, stopping,
    or already done."""
    path = registry.PENDING_DUPLICATE_REFRESH_PATH
    if not path.exists():
        return {"pending": [], "count": 0}
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return {"pending": rows, "count": len(rows)}


@app.post("/api/duplicate-refresh/start")
def start_duplicate_refresh(
    parallelism: int | None = None,
    parallelism_account2: int | None = None,
    parallelism_account3: int | None = None,
) -> dict:
    """Force re-downloads every combo currently in the pending-duplicate queue
    against its OWN existing combo_id (resolved from the registry) - this is
    what actually brings a known strategy's data current, instead of it having
    been silently re-discovered and re-downloaded as new. Clears the queue once
    dispatched - a fresh sweep can't run while Correlate is busy (see
    start_run), so nothing new can be appended here while this is in flight."""
    if run_state.is_running():
        raise HTTPException(status_code=409, detail="A sweep is running - stop it first.")
    path = registry.PENDING_DUPLICATE_REFRESH_PATH
    if not path.exists():
        raise HTTPException(status_code=400, detail="Nothing pending.")
    with path.open(newline="") as f:
        pending = list(csv.DictReader(f))
    if not pending:
        raise HTTPException(status_code=400, detail="Nothing pending.")

    registry_rows = registry.read_registry()
    seen: set[str] = set()
    rows = []
    for entry in pending:
        cid = entry.get("combo_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        row = registry_rows.get(cid)
        if row:
            rows.append(row)
    if not rows:
        raise HTTPException(status_code=400, detail="None of the pending combo_ids were found in the registry.")

    try:
        correlate_state.start(
            rows, csv_paths=[registry.REGISTRY_PATH], force=True,
            parallelism=parallelism, parallelism_account2=parallelism_account2,
            parallelism_account3=parallelism_account3,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    path.unlink()
    return {"ok": True, "queued": len(rows)}


@app.post("/api/correlate/compute")
def compute_correlate(
    top_n: int = 20,
    interval: int | None = None,
    bucket: str | None = None,
    sort_by: str = "combined",
    dte: str | None = None,
    instrument: str | None = None,
    rmdd_weight: float = 0.65,
    threshold: float = 0.5,
    csv_file: list[str] = Query(default=[]),
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Step 2: correlate whatever's already on disk for the current top `top_n` rows
    right now - pure computation, no replaying/downloading. Rows with no cached report
    yet come back in the result's `missing` list rather than silently being dropped."""
    rows = _correlate_top_rows(
        top_n, interval, bucket, sort_by, dte, instrument, rmdd_weight, csv_file or None,
        entry_time_from, entry_time_to, exit_time, exit_time_from, exit_time_to,
        date_range, date_from, date_to,
    )
    return compute_correlation(rows, threshold=threshold)


class RedownloadGapsRequest(BaseModel):
    csv_paths: list[str]
    combo_ids: list[str]
    parallelism: int | None = None
    parallelism_account2: int | None = None
    parallelism_account3: int | None = None


@app.post("/api/correlate/redownload-gaps")
def redownload_correlate_gaps(body: RedownloadGapsRequest) -> dict:
    """Targeted counterpart to /api/correlate/download's "Force re-download &amp;
    update results" - instead of replaying an entire top-N pool, replays just the
    specific combo_ids a `data_gaps` table (Uncorrelated strategies or Portfolio
    results) flagged as missing recent expiry data. Always force=True: a data gap's
    whole problem IS its existing cached report stopping short, so a non-forced
    download would see the cache hit and skip it, doing nothing."""
    if run_state.is_running():
        raise HTTPException(status_code=409, detail="A sweep is running - stop it first.")
    if not body.csv_paths:
        raise HTTPException(status_code=400, detail="No source CSV(s) given.")
    if not body.combo_ids:
        raise HTTPException(status_code=400, detail="No combo(s) given.")
    csv_paths = [Path(p) for p in body.csv_paths]
    # Resolve every wanted combo_id with ONE pass per file, not find_row_with_path
    # once per combo_id (a fresh linear scan through every file, from scratch,
    # for EACH one) - confirmed live: a ~1700-combo request against ~60 files
    # pegged the CPU for 15+ minutes and never even started downloading anything.
    # First file in csv_paths to contain a given combo_id still wins, same as
    # find_row_with_path's own single-lookup behavior.
    wanted = set(body.combo_ids)
    row_by_cid: dict[str, dict[str, str]] = {}
    for path in csv_paths:
        if not path.exists():
            continue
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                cid = row.get("combo_id")
                if cid in wanted and cid not in row_by_cid:
                    row_by_cid[cid] = row
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for cid in body.combo_ids:
        if cid in seen or cid not in row_by_cid:
            continue
        seen.add(cid)
        rows.append(row_by_cid[cid])
    try:
        correlate_state.start(
            rows, csv_paths, force=True,
            parallelism=body.parallelism, parallelism_account2=body.parallelism_account2,
            parallelism_account3=body.parallelism_account3,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "count": len(rows)}


@app.get("/api/results/instrument-options")
def get_instrument_options() -> dict:
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return {"options": []}
    rows, _ = _read_rows([Path(csv_path)])
    return _instrument_options(rows)


@app.get("/api/results/dte-options")
def get_dte_options(instrument: str | None = None) -> dict:
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return {"options": []}
    rows, _ = _read_rows([Path(csv_path)])
    return _dte_options(rows, instrument)


@app.get("/api/results/exit-time-options")
def get_exit_time_options(
    instrument: str | None = None, dte: str | None = None,
    entry_time_from: str | None = None, entry_time_to: str | None = None,
) -> dict:
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return {"options": []}
    rows, _ = _read_rows([Path(csv_path)])
    return _exit_time_options(rows, instrument, dte, entry_time_from, entry_time_to)


@app.get("/api/results/time-buckets")
def get_time_buckets(interval: int = 15, instrument: str | None = None) -> dict:
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return {"buckets": time_buckets.bucket_counts([], interval)}
    rows, _ = _read_rows([Path(csv_path)])
    return _time_bucket_counts(rows, interval, instrument)


@app.get("/api/results/coverage-heatmap")
def get_coverage_heatmap(
    interval: int = 15,
    sl_band_width: float = 10.0,
    metric: str = heatmap.DEFAULT_METRIC,
    dte: str | None = None,
    instrument: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
) -> dict:
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return heatmap.build_coverage_heatmap([], interval_minutes=interval, sl_band_width=sl_band_width, metric=metric)
    rows, _ = _read_rows([Path(csv_path)])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to,
    )
    return heatmap.build_coverage_heatmap(rows, interval_minutes=interval, sl_band_width=sl_band_width, metric=metric)


@app.get("/api/results/param-dimensions")
def get_param_dimensions() -> dict:
    return {"dimensions": param_breakdown.available_dimensions(load_ui_config())}


@app.get("/api/results/param-breakdown")
def get_param_breakdown(
    dimension: str,
    metric: str = "return_max_dd",
    dte: str | None = None,
    instrument: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
) -> dict:
    cfg = load_ui_config()
    csv_path = run_state.snapshot().get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return param_breakdown.build_param_breakdown([], cfg, dimension, metric=metric)
    rows, _ = _read_rows([Path(csv_path)])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to,
    )
    return param_breakdown.build_param_breakdown(rows, cfg, dimension, metric=metric)


@app.get("/api/results/coverage-overview")
def get_coverage_overview() -> dict:
    cfg = load_ui_config()
    csv_path = run_state.snapshot().get("csv_path")
    executed = 0
    if csv_path and Path(csv_path).exists():
        rows, _ = _read_rows([Path(csv_path)])
        # Scoped to the config's own instrument - a CSV holding more than one
        # instrument's rows shouldn't inflate "executed" against a single-instrument
        # config's grid size.
        executed = sum(1 for r in rows if r.get("instrument", "") == cfg.instrument)
    return param_breakdown.overall_coverage(cfg, executed)


# --- /api/analyze/*: a read-only view across one or more result CSVs at once, fully
# independent of run_state - never touches or is affected by whatever sweep (if any)
# is currently running. Lets you compare NIFTY/BANKNIFTY/... results side by side even
# though each "Start" writes its own separate results_web_*.csv file. ---


_RESULT_CSV_NAME = re.compile(r"^results_web_\d{8}_\d{6}(_.+)?\.csv$")


@app.get("/api/analyze/csv-files")
def list_result_csvs() -> list[dict]:
    # Also looks in output/archive/ - the "reconcile scattered combo data"
    # migration (scripts/migrate_to_registry.py) moves every results_web_*.csv
    # there once its rows are folded into combo_registry.csv, but the file
    # itself is never deleted, and this picker must keep finding it (confirmed
    # live: archiving a real user's 60 files without this made every past sweep
    # invisible here).
    #
    # A descriptive suffix after the timestamp (e.g. "..._sensex_0dte_cas.csv",
    # or scripts/build_cas_subset.py's own "..._nifty_cas_all.csv") is allowed
    # and matched - confirmed live this was a real, user-visible bug: a
    # manually-renamed execution, and every file build_cas_subset.py produces,
    # were BOTH invisible to this exact picker (the stricter timestamp-only
    # pattern this used to be silently excluded any suffixed name at all), so
    # the user could never load or even see their own CAS-window data. Genuine
    # derived/backup files (e.g. the DTE-backfill backup,
    # "results_web_..._000641.pre-dte-backfill-backup.csv") are still excluded
    # - they use a "." immediately after the timestamp, never "_", which is
    # exactly what distinguishes them from a deliberately-named real execution.
    files = sorted(
        (
            p
            for pattern_dir in (OUTPUT_DIR, OUTPUT_DIR / "archive")
            for p in pattern_dir.glob("results_web_*.csv")
            if _RESULT_CSV_NAME.match(p.name)
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out = []
    for f in files:
        with open(f, newline="") as fh:
            rows = list(csv.DictReader(fh))
        # Distinct instruments actually present, most-populous first - lets the
        # picker UI show e.g. "NIFTY" or "NIFTY, BANKNIFTY" instead of making you
        # guess what a file holds from its timestamp-only filename.
        instrument_counts: dict[str, int] = {}
        for row in rows:
            label = row.get("instrument") or "unknown"
            instrument_counts[label] = instrument_counts.get(label, 0) + 1
        instruments = sorted(instrument_counts, key=lambda label: -instrument_counts[label])
        # Distinct (start_date, end_date) backtest periods actually present in this
        # ONE file - a force-redownload's roll_date_window (correlate_state.py) can
        # silently advance a minority of rows' own dates forward while the rest keep
        # the original window, so a single file can already be internally mixed
        # before the user even combines it with anything else. Surfacing this on the
        # file card itself (before Load) is the earliest point this can be caught.
        date_pairs = {(row.get("start_date") or "", row.get("end_date") or "") for row in rows}
        date_pairs.discard(("", ""))
        date_range_field: dict = {}
        if len(date_pairs) == 1:
            (only_start, only_end), = date_pairs
            date_range_field["date_range"] = f"{only_start} to {only_end}"
        elif len(date_pairs) > 1:
            date_range_field["date_range_mixed_count"] = len(date_pairs)
        out.append(
            {
                "path": str(f),
                "name": f.name,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "row_count": len(rows),
                "instruments": instruments,
                **date_range_field,
            }
        )
    return out


@app.get("/api/analyze/results")
def analyze_results(
    csv_file: list[str] = Query(default=[]),
    limit: int = 50,
    interval: int | None = None,
    bucket: str | None = None,
    sort_by: str = "return_max_dd",
    dte: str | None = None,
    instrument: str | None = None,
    rmdd_weight: float = 0.65,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    combo_search: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    rows, columns = _read_rows([Path(p) for p in csv_file])
    return _filtered_results(
        rows,
        columns,
        limit=limit,
        interval=interval,
        bucket=bucket,
        sort_by=sort_by,
        dte=dte,
        instrument=instrument,
        rmdd_weight=rmdd_weight,
        entry_time_from=entry_time_from,
        entry_time_to=entry_time_to,
        exit_time=exit_time,
        exit_time_from=exit_time_from,
        exit_time_to=exit_time_to,
        combo_search=combo_search,
        date_range=date_range,
        date_from=date_from,
        date_to=date_to,
    )


def _apply_date_from_to(rows: list[dict], date_from: str | None, date_to: str | None) -> list[dict]:
    """The same date_from/date_to range as _scope_ok_rows (see its own
    docstring) - a standalone helper for the options endpoints below, which
    call their own bespoke per-field helpers instead of _scope_ok_rows, but
    still need to respect this filter so their pill lists don't offer a
    combination that's actually outside the range already selected."""
    if date_from is not None:
        rows = [r for r in rows if r.get("start_date") and r["start_date"] >= date_from]
    if date_to is not None:
        rows = [r for r in rows if r.get("start_date") and r["start_date"] <= date_to]
    return rows


@app.get("/api/analyze/instrument-options")
def analyze_instrument_options(
    csv_file: list[str] = Query(default=[]), date_from: str | None = None, date_to: str | None = None,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    return _instrument_options(_apply_date_from_to(rows, date_from, date_to))


@app.get("/api/analyze/date-range-options")
def analyze_date_range_options(
    csv_file: list[str] = Query(default=[]), instrument: str | None = None, dte: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    return _date_range_options(_apply_date_from_to(rows, date_from, date_to), instrument, dte)


@app.get("/api/analyze/dte-options")
def analyze_dte_options(
    csv_file: list[str] = Query(default=[]), instrument: str | None = None, date_range: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    return _dte_options(_apply_date_from_to(rows, date_from, date_to), instrument, date_range)


@app.get("/api/analyze/exit-time-options")
def analyze_exit_time_options(
    csv_file: list[str] = Query(default=[]), instrument: str | None = None, dte: str | None = None,
    entry_time_from: str | None = None, entry_time_to: str | None = None, date_range: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    return _exit_time_options(_apply_date_from_to(rows, date_from, date_to), instrument, dte, entry_time_from, entry_time_to, date_range)


@app.get("/api/analyze/time-buckets")
def analyze_time_buckets(
    csv_file: list[str] = Query(default=[]), interval: int = 15, instrument: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    return _time_bucket_counts(_apply_date_from_to(rows, date_from, date_to), interval, instrument)


@app.get("/api/analyze/coverage-heatmap")
def analyze_coverage_heatmap(
    csv_file: list[str] = Query(default=[]),
    interval: int = 15,
    sl_band_width: float = 10.0,
    metric: str = heatmap.DEFAULT_METRIC,
    dte: str | None = None,
    instrument: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
        date_from=date_from, date_to=date_to,
    )
    return heatmap.build_coverage_heatmap(rows, interval_minutes=interval, sl_band_width=sl_band_width, metric=metric)


@app.get("/api/analyze/timeline-coverage")
def analyze_timeline_coverage(
    csv_file: list[str] = Query(default=[]),
    dte: str | None = None,
    instrument: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Two coverage charts - see src/web/timeline_coverage.py: one bar per DISTINCT
    (entry_time, exit_time) pair actually present, with a count and averaged
    performance metrics for that exact pair (deliberately NOT overlap-aware - see
    strategy_timing_coverage's own docstring), and how many candidates' own
    backtest window covers each calendar month.

    Deliberately does NOT accept entry/exit-TIME filters at all, even though every
    other coverage view on this page does - both charts exist specifically to let
    you compare ACROSS different timings, so narrowing by the very dimension either
    one browses would collapse it down to whatever's already selected instead.
    Confirmed live: clicking one Strategy Timing bar (which sets an exact entry+exit
    time filter, same as picking it by hand) made every OTHER bar vanish, since that
    filter then fed straight back into this same computation and left only the one
    group that still matched.

    backtest_period additionally ignores `date_range` for the identical reason - a
    row's date_range label IS its start_date/end_date, the exact two fields
    backtest_period_coverage groups by month, so scoping to one date_range first
    would always collapse it to a single flat bar (confirmed: this is also why a
    single sweep's own CSV - every row sharing one fixed start/end date - looks like
    a chart with no real information in it; see backtest_period_windows below,
    which the UI uses to say that plainly instead of showing a flat chart).

    date_from/date_to (a RANGE, not an exact pill match - see _scope_ok_rows'
    own docstring) applies to BOTH charts here, unlike date_range - it doesn't
    cause the same self-collapse problem, since a range can still contain many
    distinct windows (e.g. every sweep since 2026-08-01, each with its own
    slightly different end_date) rather than narrowing to exactly one."""
    rows, _ = _read_rows([Path(p) for p in csv_file])
    common = dict(
        instrument=instrument, dte=dte, entry_time_from=None, entry_time_to=None,
        date_from=date_from, date_to=date_to,
    )
    timing_rows = _scope_ok_rows(rows, **common, date_range=date_range)
    period_rows = _scope_ok_rows(rows, **common, date_range=None)
    distinct_windows = sorted({
        _row_date_range_label(r) for r in period_rows if r.get("start_date") and r.get("end_date")
    })
    return {
        "strategy_timing": timeline_coverage.strategy_timing_coverage(timing_rows),
        "backtest_period": timeline_coverage.backtest_period_coverage(period_rows),
        "backtest_period_row_count": len(period_rows),
        # Every distinct start_date/end_date window actually present, e.g.
        # ["2025-01-01 to 2026-09-09"] - almost always exactly ONE for a single
        # sweep's own CSV, since start_date/end_date are sweep-level settings
        # applied identically to every combo it produces, not something that
        # varies per row. The chart above is only ever informative when there's
        # more than one - the UI names the single window directly instead when
        # there isn't.
        "backtest_period_windows": distinct_windows,
    }


@app.get("/api/analyze/param-dimensions")
def analyze_param_dimensions() -> dict:
    # Always reflects the currently *loaded/editable* sweep config (config/sweep_ui.yaml),
    # not anything about which CSV(s) are selected below - "what should I run next"
    # is a question about the config you're about to launch, not the data you're
    # looking at, which is exactly why coverage can show gaps against configured
    # values that never appear in the CSV at all.
    return {"dimensions": param_breakdown.available_dimensions(load_ui_config())}


@app.get("/api/analyze/param-breakdown")
def analyze_param_breakdown(
    dimension: str,
    csv_file: list[str] = Query(default=[]),
    metric: str = "return_max_dd",
    dte: str | None = None,
    instrument: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    cfg = load_ui_config()
    rows, _ = _read_rows([Path(p) for p in csv_file])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
        date_from=date_from, date_to=date_to,
    )
    return param_breakdown.build_param_breakdown(rows, cfg, dimension, metric=metric)


@app.get("/api/analyze/coverage-overview")
def analyze_coverage_overview(csv_file: list[str] = Query(default=[])) -> dict:
    cfg = load_ui_config()
    rows, _ = _read_rows([Path(p) for p in csv_file])
    executed = sum(1 for r in rows if r.get("instrument", "") == cfg.instrument)
    return param_breakdown.overall_coverage(cfg, executed)


class CasSubsetRequest(BaseModel):
    cas_start: str = DEFAULT_CAS_START
    apply: bool = False


@app.post("/api/analyze/cas-subset")
def analyze_cas_subset(body: CasSubsetRequest) -> dict:
    """Builds a per-instrument "CAS window" subset out of every trade report
    already downloaded across every results_web_*.csv sitting in output/ -
    same underlying logic as `uv run python -m scripts.build_cas_subset`, just
    callable from the Analyze page instead of a terminal.

    apply=False (the default, and what the UI's own "Preview" button sends)
    only computes and returns the per-instrument counts - nothing is written.
    apply=True actually writes the new trade report files + per-instrument
    results_web_*_cas_all.csv (what "Create subset" sends, only after a
    preview). _read_rows' own mtime-keyed cache (see _read_rows_cached) is
    untouched by this - a newly-written file is simply new to it, picked up
    the next time it's requested, same as any other CSV added to output/."""
    return run_cas_subset(OUTPUT_DIR, body.cas_start, apply=body.apply)


@app.get("/api/analyze/portfolio-basket")
def analyze_portfolio_basket(
    csv_file: list[str] = Query(default=[]),
    instrument: str = "NIFTY",
    dte: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    threshold: float = 0.5,
    top_n: int = portfolio.DEFAULT_TOP_N,
    short_morning_budget: float = portfolio.DEFAULT_BUDGETS["short_morning"],
    long_morning_budget: float = portfolio.DEFAULT_BUDGETS["long_morning"],
    midday_budget: float = portfolio.DEFAULT_BUDGETS["midday"],
    afternoon_budget: float = portfolio.DEFAULT_BUDGETS["afternoon"],
    max_share: float = portfolio.DEFAULT_MAX_SHARE,
    min_lots: float = portfolio.DEFAULT_MIN_LOTS,
    max_lots: float | None = portfolio.DEFAULT_MAX_LOTS,
    trade_date_from: str | None = None,
    trade_date_to: str | None = None,
    stale_after_days: int | None = portfolio.DEFAULT_STALE_AFTER_DAYS,
    check_stale_pool: bool = False,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    very_long_morning_mode: bool = False,
) -> dict:
    rows, _ = _read_rows([Path(p) for p in csv_file])
    # Same instrument/DTE/backtest-period/entry-time/exit-time scoping as every
    # other analyze endpoint - a filter set on the page (e.g. "entry_time_from=
    # 09:17" to exclude the opening-candle 09:16 entries, a Backtest period pill
    # isolating one recorded (start_date, end_date) window, or a date_from/date_to
    # range like "everything since 2026-08-01") must apply here too, not just to
    # the results table above - otherwise a basket/sweep could silently mix in
    # combos from OUTSIDE the window just isolated on the page, which is exactly
    # the guarantee those filters exist to give. NOTE: this date_from/date_to
    # is which CANDIDATE ROWS are even considered (by their own backtest's
    # start_date) - separate from trade_date_from/trade_date_to below, which
    # restricts the trade-level window WITHIN each already-selected candidate's
    # own downloaded report (see diversify_for_grid's docstring). Don't conflate
    # the two: this page filter narrows the candidate pool; that one narrows
    # what's counted inside each candidate.
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
        date_from=date_from, date_to=date_to,
    )
    budgets = {
        "short_morning": short_morning_budget,
        "long_morning": long_morning_budget,
        "midday": midday_budget,
        "afternoon": afternoon_budget,
    }
    # "Very long morning": collapses short_morning/long_morning/midday's own
    # narrower entry/exit cutoffs into ONE session covering any entry before
    # noon that's held into the afternoon (see classify_very_long_morning_bucket)
    # - reuses the SAME long_morning_budget value/field for its budget rather
    # than adding a new one; short_morning_budget/midday_budget are simply
    # unused in this mode (bucket_order below doesn't include those names, so
    # their budgets, even if still sitting in the dict above, are never
    # summed/allocated against - see size_and_summarize's own "total_lots").
    bucket_order = portfolio.VERY_LONG_MORNING_BUCKET_ORDER if very_long_morning_mode else None
    classify_fn = portfolio.classify_very_long_morning_bucket if very_long_morning_mode else None
    if very_long_morning_mode:
        budgets["very_long_morning"] = long_morning_budget
    return portfolio.build_portfolio(
        rows, REPORTS_DIR, instrument, threshold=threshold, top_n=top_n, budgets=budgets,
        max_share=max_share, min_lots=min_lots, max_lots=max_lots,
        date_from=trade_date_from, date_to=trade_date_to, stale_after_days=stale_after_days,
        check_stale_pool=check_stale_pool,
        bucket_order=bucket_order, classify_fn=classify_fn,
    )


@app.post("/api/portfolio-sweep/start")
def portfolio_sweep_start(
    csv_file: list[str] = Query(default=[]),
    instrument: str = "NIFTY",
    dte: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    threshold_min: float = 0.25,
    threshold_max: float = 0.4,
    threshold_step: float = 0.05,
    top_n_min: int = 50,
    top_n_max: int = 400,
    top_n_step: int = 50,
    min_lots_min: float = 2,
    min_lots_max: float = 2,
    min_lots_step: float = 1,
    max_lots_min: float = 5,
    max_lots_max: float = 5,
    max_lots_step: float = 1,
    short_morning_budget: float = portfolio.DEFAULT_BUDGETS["short_morning"],
    long_morning_budget: float = portfolio.DEFAULT_BUDGETS["long_morning"],
    midday_budget: float = portfolio.DEFAULT_BUDGETS["midday"],
    afternoon_budget: float = portfolio.DEFAULT_BUDGETS["afternoon"],
    max_share: float = portfolio.DEFAULT_MAX_SHARE,
    trade_date_from: str | None = None,
    trade_date_to: str | None = None,
    date_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    very_long_morning_mode: bool = False,
) -> dict:
    """Same rows/filters/budgets as /api/analyze/portfolio-basket, just re-run once
    per grid point (threshold x top_n x min_lots x max_lots) instead of once - see
    PortfolioSweepState. A combination where min_lots > max_lots is contradictory
    (see _size_lots) and is skipped rather than computed.

    date_from/date_to here scope which CANDIDATE ROWS are considered (same page
    filter as /api/analyze/portfolio-basket); trade_date_from/trade_date_to
    restrict the trade-level window inside each candidate's own report - see
    that endpoint's docstring for why the two must stay separate."""
    rows, _ = _read_rows([Path(p) for p in csv_file])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
        date_from=date_from, date_to=date_to,
    )
    try:
        grid = build_grid(
            threshold=frange_inclusive(threshold_min, threshold_max, threshold_step),
            top_n=list(range(top_n_min, top_n_max + 1, top_n_step)) if top_n_step > 0 else [],
            min_lots=frange_inclusive(min_lots_min, min_lots_max, min_lots_step),
            max_lots=frange_inclusive(max_lots_min, max_lots_max, max_lots_step),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    grid = [g for g in grid if g["min_lots"] <= g["max_lots"]]
    if len(grid) > MAX_SWEEP_GRID_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"{len(grid)} combinations is too many for one sweep (max {MAX_SWEEP_GRID_SIZE}) - narrow a range or widen a step.",
        )
    budgets = {
        "short_morning": short_morning_budget,
        "long_morning": long_morning_budget,
        "midday": midday_budget,
        "afternoon": afternoon_budget,
    }
    # See analyze_portfolio_basket's own comment - same "very long morning"
    # scheme, same reuse of long_morning_budget's value, available in the
    # sweep grid too so this bucket definition can be parameter-swept exactly
    # like the regular 4-bucket one already is.
    bucket_order = portfolio.VERY_LONG_MORNING_BUCKET_ORDER if very_long_morning_mode else None
    classify_fn = portfolio.classify_very_long_morning_bucket if very_long_morning_mode else None
    if very_long_morning_mode:
        budgets["very_long_morning"] = long_morning_budget
    try:
        portfolio_sweep_state.start(
            rows, REPORTS_DIR, instrument, grid, budgets=budgets, max_share=max_share,
            date_from=trade_date_from, date_to=trade_date_to,
            bucket_order=bucket_order, classify_fn=classify_fn,
            # A date window (or bucket scheme) is part of what makes
            # previously-stopped results comparable to this run's grid -
            # resuming across a CHANGED window (e.g. dropping a stale "since
            # 2025-08-01" for "since 2026-08-01", trade-level or candidate-
            # scope) or a changed bucket scheme (regular vs "very long
            # morning") must start clean rather than silently keep results
            # computed against the old window/scheme, same reasoning as
            # regime_sweep_state's own context check.
            context=(trade_date_from, trade_date_to, date_from, date_to, very_long_morning_mode),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/portfolio-sweep/status")
def portfolio_sweep_status() -> dict:
    return portfolio_sweep_state.snapshot()


@app.post("/api/portfolio-sweep/stop")
def portfolio_sweep_stop() -> dict:
    portfolio_sweep_state.stop()
    return {"ok": True}


class SaveFavoriteRequest(BaseModel):
    params: dict
    last_result: dict | None = None


@app.get("/api/portfolio-sweep/favorites")
def list_portfolio_sweep_favorites() -> list[dict]:
    return portfolio_sweep_favorites.list_favorites()


@app.post("/api/portfolio-sweep/favorites/{name}")
def save_portfolio_sweep_favorite(name: str, body: SaveFavoriteRequest) -> dict:
    try:
        portfolio_sweep_favorites.save_favorite(name, body.params, body.last_result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/api/portfolio-sweep/favorites/{name}")
def remove_portfolio_sweep_favorite(name: str) -> dict:
    portfolio_sweep_favorites.delete_favorite(name)
    return {"ok": True}


# --- CAS regime analysis: a fully separate download+basket pathway (see
# src/web/regime_state.py's module docstring for why) - never reads or writes
# REPORTS_DIR/output/trade_reports, the results CSVs, or the stable Correlate/
# Portfolio-sweep job state. The basket-building math itself (portfolio.py) is
# reused completely unchanged, just pointed at regime_window_dir(...) instead of
# REPORTS_DIR. ---
def _cas_buckets_or_none(
    cas_slice_mode: bool, entry_time_from: str | None, entry_time_to: str | None, cas_slice_minutes: int
):
    """(bucket_order, classify_fn) from cas_slot_buckets when CAS slice mode is on,
    else (None, None) - which every caller below treats as "use the regular 4
    day-session buckets", so leaving the checkbox off is byte-for-byte the old
    behavior. Requires an explicit Entry time from/to (the same fields already
    used to scope which rows are eligible at all) - without both, there's no
    window to slice into bands."""
    if not cas_slice_mode:
        return None, None
    if not entry_time_from or not entry_time_to:
        raise HTTPException(
            status_code=400,
            detail="CAS slice mode needs both Entry time from and Entry time to set (in the filters above) - "
            "that's the window it slices into bands.",
        )
    return cas_slot_buckets(entry_time_from, entry_time_to, cas_slice_minutes)


def _no_regime_reports_detail(date_from: str, date_to: str) -> str:
    """Error detail for /api/regime/basket and /api/regime/sweep/start when
    regime_window_dir(date_from, date_to) doesn't exist - lists every window
    that HAS actually been downloaded, so a date_to off by even a day or two
    from what was really downloaded doesn't leave the user guessing random
    date ranges against a message that gives no hint what would actually work
    (confirmed live - this is exactly what happened)."""
    available = list_downloaded_regime_windows()
    if not available:
        hint = " No CAS window has been downloaded at all yet - use step 1 above first."
    else:
        listed = "; ".join(f"{f} to {t} ({n} report(s))" for f, t, n in available)
        hint = f" Already-downloaded window(s) you can use instead: {listed}."
    return (
        f"No regime reports downloaded yet for {date_from} to {date_to} - "
        f"download reports for this exact date range first (step 1 above)." + hint
    )


@app.post("/api/regime/download")
def download_regime_reports(
    date_from: str,
    date_to: str | None = None,
    top_n_per_bucket: int = 250,
    csv_file: list[str] = Query(default=[]),
    instrument: str = "NIFTY",
    dte: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    cas_slice_mode: bool = False,
    cas_slice_minutes: int = 5,
    force: bool = False,
    date_range: str | None = None,
) -> dict:
    """Downloads a SEPARATE set of trade reports - each replayed with start_date/
    end_date pinned to [date_from, date_to] instead of its originally-discovered
    window - for the top `top_n_per_bucket` rows in EACH session separately, not
    one flat "best N overall" sort. A flat sort lets whichever session scores best
    historically crowd out the other three almost entirely - see
    shortlist_for_download in src/web/regime_state.py, which this reuses
    (duplicated there rather than added to portfolio.py, so the regular Portfolio
    sweep's own file is never touched). Written to regime_window_dir(date_from,
    date_to), never REPORTS_DIR.

    `cas_slice_mode` (off by default - identical to before this param existed)
    swaps the regular 4 day-of-time sessions for `cas_slice_minutes`-wide bands
    across [entry_time_from, entry_time_to] - see cas_slot_buckets. Without this,
    every CAS-window candidate's entry_time falls in the same narrow window and
    all classify into a single "afternoon" bucket, so `top_n_per_bucket` ends up
    downloading only the historically-best-ranked handful of entry/exit times
    instead of spreading across the window."""
    if run_state.is_running():
        raise HTTPException(status_code=409, detail="A sweep is running - stop it first.")
    if not date_from:
        raise HTTPException(status_code=400, detail="A start date is required.")
    resolved_date_to = date_to or datetime.now().date().isoformat()
    if resolved_date_to < date_from:
        raise HTTPException(status_code=400, detail="End date can't be before start date.")
    bucket_order, classify_fn = _cas_buckets_or_none(cas_slice_mode, entry_time_from, entry_time_to, cas_slice_minutes)
    rows, _ = _read_rows([Path(p) for p in csv_file])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
    )
    rows = shortlist_for_download(rows, top_n_per_bucket, bucket_order=bucket_order, classify_fn=classify_fn)
    try:
        regime_download_state.start(rows, date_from=date_from, date_to=resolved_date_to, force=force)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Echoed back so the UI can lock its date fields to the window ACTUALLY being
    # downloaded - leaving "To" blank here and then blank again later on
    # /api/regime/basket or /api/regime/sweep/start would independently resolve
    # "today" a second time, which silently drifts to a different (empty) window
    # dir the moment the two calls don't happen on the same calendar day.
    return {
        "ok": True,
        "date_from": date_from,
        "date_to": resolved_date_to,
        "window_dir": str(regime_window_dir(date_from, resolved_date_to)),
    }


@app.get("/api/regime/status")
def regime_status() -> dict:
    return regime_download_state.snapshot()


@app.post("/api/regime/stop")
def regime_stop() -> dict:
    regime_download_state.stop()
    return {"ok": True}


@app.get("/api/regime/basket")
def regime_portfolio_basket(
    date_from: str,
    date_to: str | None = None,
    csv_file: list[str] = Query(default=[]),
    instrument: str = "NIFTY",
    dte: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    threshold: float = 0.5,
    top_n: int = portfolio.DEFAULT_TOP_N,
    short_morning_budget: float = portfolio.DEFAULT_BUDGETS["short_morning"],
    long_morning_budget: float = portfolio.DEFAULT_BUDGETS["long_morning"],
    midday_budget: float = portfolio.DEFAULT_BUDGETS["midday"],
    afternoon_budget: float = portfolio.DEFAULT_BUDGETS["afternoon"],
    max_share: float = portfolio.DEFAULT_MAX_SHARE,
    min_lots: float = portfolio.DEFAULT_MIN_LOTS,
    max_lots: float | None = portfolio.DEFAULT_MAX_LOTS,
    cas_slice_mode: bool = False,
    cas_slice_minutes: int = 5,
    cas_overall_budget: float = portfolio.DEFAULT_BUDGETS["afternoon"],
    date_range: str | None = None,
) -> dict:
    """Exactly /api/analyze/portfolio-basket's own logic (build_portfolio itself is
    untouched) pointed at regime_window_dir(date_from, date_to) instead of
    REPORTS_DIR. A combo not yet downloaded for this exact window (see
    /api/regime/download) simply has no report on disk here and is excluded the
    same way any undownloaded combo already is - has_hard_stop_loss/_shortlist don't
    know or care which folder a report came from.

    `cas_slice_mode` - see _cas_buckets_or_none/cas_slot_buckets - replaces the
    regular 4 day-of-time budget fields with ONE shared `cas_overall_budget` pooled
    across every diversified pick from every `cas_slice_minutes`-wide band across
    [entry_time_from, entry_time_to] - not a per-band budget, since CAS's bands
    are all within the same few-minute window and their positions are concurrent,
    not sequential like the regular day-sessions' margin reuse (see
    build_portfolio's overall_budget). Each pick is still individually bounded by
    min_lots/max_lots regardless of which band it's in. Off by default: identical
    to before this param existed."""
    resolved_date_to = date_to or datetime.now().date().isoformat()
    bucket_order, classify_fn = _cas_buckets_or_none(cas_slice_mode, entry_time_from, entry_time_to, cas_slice_minutes)
    window_dir = regime_window_dir(date_from, resolved_date_to)
    if not window_dir.exists():
        # Distinguishes "nothing downloaded for this exact window" from "downloaded,
        # but genuinely too few overlapping trade days" - without this, both look
        # identical to the UI (an empty basket / "not enough overlapping data"),
        # which is exactly what made a date_to that silently drifted from the
        # download's actual resolved date (see /api/regime/download's own comment)
        # look like a data problem instead of a wrong-folder one.
        raise HTTPException(status_code=400, detail=_no_regime_reports_detail(date_from, resolved_date_to))
    rows, _ = _read_rows([Path(p) for p in csv_file])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
    )
    budgets = None if bucket_order is not None else {
        "short_morning": short_morning_budget, "long_morning": long_morning_budget,
        "midday": midday_budget, "afternoon": afternoon_budget,
    }
    return portfolio.build_portfolio(
        rows, window_dir, instrument,
        threshold=threshold, top_n=top_n, budgets=budgets,
        max_share=max_share, min_lots=min_lots, max_lots=max_lots,
        bucket_order=bucket_order, classify_fn=classify_fn,
        overall_budget=cas_overall_budget if bucket_order is not None else None,
    )


@app.post("/api/regime/sweep/start")
def regime_sweep_start(
    date_from: str,
    date_to: str | None = None,
    csv_file: list[str] = Query(default=[]),
    instrument: str = "NIFTY",
    dte: str | None = None,
    entry_time_from: str | None = None,
    entry_time_to: str | None = None,
    exit_time: str | None = None,
    exit_time_from: str | None = None,
    exit_time_to: str | None = None,
    threshold_min: float = 0.25,
    threshold_max: float = 0.4,
    threshold_step: float = 0.05,
    top_n_min: int = 50,
    top_n_max: int = 400,
    top_n_step: int = 50,
    min_lots_min: float = 2,
    min_lots_max: float = 2,
    min_lots_step: float = 1,
    max_lots_min: float = 5,
    max_lots_max: float = 5,
    max_lots_step: float = 1,
    short_morning_budget: float = portfolio.DEFAULT_BUDGETS["short_morning"],
    long_morning_budget: float = portfolio.DEFAULT_BUDGETS["long_morning"],
    midday_budget: float = portfolio.DEFAULT_BUDGETS["midday"],
    afternoon_budget: float = portfolio.DEFAULT_BUDGETS["afternoon"],
    max_share: float = portfolio.DEFAULT_MAX_SHARE,
    cas_slice_mode: bool = False,
    cas_slice_minutes: int = 5,
    cas_overall_budget: float = portfolio.DEFAULT_BUDGETS["afternoon"],
    date_range: str | None = None,
) -> dict:
    """Same grid-search sweep as /api/portfolio-sweep/start (threshold x top_n x
    min_lots x max_lots), but run against regime_sweep_state - its own independent
    job, never sharing resume/dedup state with a regular Portfolio sweep - pointed
    at regime_window_dir(date_from, date_to). "Identifying the best combo" for this
    period only: multi-session basket + full parameter sweep, over the regime-only
    report set.

    `cas_slice_mode`/`cas_overall_budget` - see /api/regime/basket's own doc for
    the same params."""
    resolved_date_to = date_to or datetime.now().date().isoformat()
    bucket_order, classify_fn = _cas_buckets_or_none(cas_slice_mode, entry_time_from, entry_time_to, cas_slice_minutes)
    window_dir = regime_window_dir(date_from, resolved_date_to)
    if not window_dir.exists():
        raise HTTPException(status_code=400, detail=_no_regime_reports_detail(date_from, resolved_date_to))
    rows, _ = _read_rows([Path(p) for p in csv_file])
    rows = _scope_ok_rows(
        rows, instrument=instrument, dte=dte, entry_time_from=entry_time_from,
        entry_time_to=entry_time_to, exit_time=exit_time,
        exit_time_from=exit_time_from, exit_time_to=exit_time_to, date_range=date_range,
    )
    try:
        grid = build_grid(
            threshold=frange_inclusive(threshold_min, threshold_max, threshold_step),
            top_n=list(range(top_n_min, top_n_max + 1, top_n_step)) if top_n_step > 0 else [],
            min_lots=frange_inclusive(min_lots_min, min_lots_max, min_lots_step),
            max_lots=frange_inclusive(max_lots_min, max_lots_max, max_lots_step),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    grid = [g for g in grid if g["min_lots"] <= g["max_lots"]]
    if len(grid) > MAX_SWEEP_GRID_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"{len(grid)} combinations is too many for one sweep (max {MAX_SWEEP_GRID_SIZE}) - narrow a range or widen a step.",
        )
    budgets = None if bucket_order is not None else {
        "short_morning": short_morning_budget, "long_morning": long_morning_budget,
        "midday": midday_budget, "afternoon": afternoon_budget,
    }
    extra_kwargs = (
        {}
        if bucket_order is None
        else {"bucket_order": bucket_order, "classify_fn": classify_fn, "overall_budget": cas_overall_budget}
    )
    # A resume must never reuse results computed against a different window, a
    # different bucket definition (regular sessions vs. a CAS time-slice grid), or
    # a different overall_budget (same grid points, but sized under a completely
    # different pooling algorithm) - see PortfolioSweepState.start's `context`.
    context = (
        str(window_dir),
        tuple(bucket_order) if bucket_order is not None else None,
        cas_overall_budget if bucket_order is not None else None,
    )
    try:
        regime_sweep_state.start(
            rows, window_dir, instrument, grid, budgets=budgets, max_share=max_share, context=context, **extra_kwargs
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/regime/sweep/status")
def regime_sweep_status() -> dict:
    return regime_sweep_state.snapshot()


@app.post("/api/regime/sweep/stop")
def regime_sweep_stop() -> dict:
    regime_sweep_state.stop()
    return {"ok": True}


@app.post("/api/narrow")
def narrow(top_n: int = 10) -> SweepUIConfig:
    """Read the active/most-recent results CSV, and narrow every range in the saved UI
    config to center on whatever won in the top `top_n` rows by Return/MaxDD - one
    round of coarse-grid-then-refine instead of hand-editing every range."""
    snapshot = run_state.snapshot()
    csv_path = snapshot.get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        raise HTTPException(status_code=400, detail="No results yet to narrow from - run the sweep first.")

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    cfg = load_ui_config()
    try:
        narrowed = narrow_config(cfg, rows, top_n=top_n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Narrow overwrites the one saved sweep_ui.yaml in place - back up the pre-narrow
    # config (paired with the CSV it produced) under an auto-generated name first, so
    # the wide sweep's settings are never lost even if you forget to save manually.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"auto-before-narrow-{Path(csv_path).stem}-{timestamp}"
    executions_mod.save_execution(backup_name, cfg, csv_path)

    save_ui_config(narrowed)
    return narrowed


class SaveExecutionRequest(BaseModel):
    cfg: SweepUIConfig
    csv_path: str | None = None


@app.get("/api/executions")
def list_executions() -> list[dict]:
    return executions_mod.list_executions()


@app.post("/api/executions/{name}")
def save_execution(name: str, body: SaveExecutionRequest) -> dict:
    # Always exactly what the caller said - no falling back to run_state's csv_path.
    # That fallback used to fire even when the caller explicitly meant "no CSV yet"
    # (JSON null and "omitted" both decode to Python None here, so the two cases were
    # indistinguishable), silently attaching whichever run happened to be live on the
    # server to a completely unrelated saved execution.
    try:
        executions_mod.save_execution(name, body.cfg, body.csv_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/executions/{name}")
def get_execution(name: str) -> dict:
    try:
        cfg, csv_path = executions_mod.load_execution(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"config": cfg, "csv_path": csv_path}


@app.delete("/api/executions/{name}")
def remove_execution(name: str) -> dict:
    executions_mod.delete_execution(name)
    return {"ok": True}


@app.post("/api/login/open")
def open_login(relogin: bool = False, account: int = 1) -> dict:
    """Opens a headed browser on the given account's primary profile (1, 2, or 3)
    for the user to log into manually. With relogin=True, first logs out of the
    current session (if any) before waiting for a fresh login - use this to start a
    longer-lived session rather than waiting for the current one to expire."""
    try:
        login_helper_state.start(relogin=relogin, account=account)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/login/status")
def get_login_status() -> dict:
    return login_helper_state.snapshot()


@app.post("/api/login/dismiss")
def dismiss_login() -> dict:
    login_helper_state.dismiss()
    return {"ok": True}


class LaunchComboRequest(BaseModel):
    csv_paths: list[str]
    combo_id: str
    save: bool = False
    prefix: str | None = None


@app.post("/api/launch-combo")
def launch_combo(body: LaunchComboRequest) -> dict:
    """Opens a real, headed browser with one stored combo's builder form prefilled
    (and the backtest submitted) - lets a combo whose scraped metrics don't match a
    manual re-entry be inspected directly instead of retyping every setting by hand.
    `save=True` ("Launch & Save") also saves it as a named strategy on AlgoTest's
    side, named "{prefix}_{combo_id}_{entry_time}" (prefix optional) so it's
    traceable back to this exact combo, and identifiable by which portfolio it
    belongs to once there are several saved side by side."""
    try:
        combo_launcher_state.launch(body.csv_paths, body.combo_id, save=body.save, prefix=body.prefix)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/launch-combo/status")
def get_launch_combo_status() -> dict:
    return combo_launcher_state.snapshot()


@app.post("/api/launch-combo/close")
def close_launch_combo() -> dict:
    combo_launcher_state.close()
    return {"ok": True}


class BasketSaveRequest(BaseModel):
    csv_paths: list[str]
    combo_ids: list[str]
    prefix: str = ""


@app.post("/api/basket-save/start")
def start_basket_save(body: BasketSaveRequest) -> dict:
    """Saves every combo in `combo_ids` as a named strategy on AlgoTest, one after
    another in a single headed browser session - the one-click "Save basket in
    AlgoTest" action for a Portfolio basket (or any other multi-combo list), so
    saving 15-20 strategies doesn't mean clicking Launch & Save that many times by
    hand. Same "{prefix}_{combo_id}_{entry_time}" naming as a single Launch & Save."""
    try:
        basket_save_state.start(body.csv_paths, body.combo_ids, body.prefix)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/basket-save/status")
def get_basket_save_status() -> dict:
    return basket_save_state.snapshot()


@app.post("/api/basket-save/stop")
def stop_basket_save() -> dict:
    basket_save_state.stop()
    return {"ok": True}


@app.post("/api/basket-save/close")
def close_basket_save() -> dict:
    basket_save_state.close()
    return {"ok": True}


@app.get("/api/platform")
def get_platform() -> dict:
    """Lets the frontend decide whether to show Windows-only controls (MTQuant
    import) without guessing - see docs/mtquant-integration.md. Checking this
    is how the UI avoids ever showing a "Save to MTQuant" button on macOS that
    would just error when clicked."""
    return {"os": platform.system(), "mtquant_available": platform.system() == "Windows"}


@app.post("/api/mtquant/import")
def mtquant_import() -> dict:
    """Stub for the MTQuant .algtst import automation (src/mtquant/, not built
    yet - see docs/mtquant-integration.md). Gated on Windows BEFORE src.mtquant
    is ever imported, so a macOS session never needs the mtquant optional
    dependency group (pywinauto) installed, or even importable."""
    if platform.system() != "Windows":
        raise HTTPException(
            status_code=400,
            detail="MTQuant integration is only available on Windows (this is a Windows desktop app, not a website).",
        )
    from src import mtquant  # noqa: F401  proves the lazy-import boundary holds; no pywinauto import happens yet

    raise HTTPException(status_code=501, detail="MTQuant integration is not implemented yet.")


