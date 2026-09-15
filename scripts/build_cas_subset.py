"""Builds a genuinely separate, first-class "execution" per instrument covering
only the CAS window (default: on/after 2026-08-01), out of whatever trade
reports are already downloaded from your existing long-dated sweeps - see the
"date filter on the Analyze page" conversation this was scoped from.

Explicitly NOT a bolt-on set of extra columns on the original rows (an earlier,
now-abandoned direction for this same request) - each eligible combo gets:
  - its own new combo_id (f"{original_id}_cas{cas_start}", a plain composite
    string, never a fresh hash - combo_id() is never called again here, so no
    EXISTING combo's identity is touched, same convention as runner.py's own
    "_dteN" DTE-variant ids),
  - its own new, genuinely separate trade report file on disk (the original
    report is only ever read, never modified or overwritten),
  - a full, ordinarily-shaped results row (same columns as any other execution)
    with start_date bumped to --cas-start and every metric _metrics_from_daily
    can actually recompute from the sliced daily P/L overwritten to reflect
    JUST that window.

The output is one new results_web_*.csv per instrument (e.g.
"results_web_<timestamp>_nifty_cas_all.csv") - load it on the Analyze page
exactly like any other execution (alongside the original sweep it came from, if
you want to compare) and the page's own existing "Filter by Backtest period"
pill picker (already built, already wired into every chart on that page) does
the rest - no new UI or endpoint code needed anywhere.

Metric honesty: a trade report only gives per-DAY netted P/L (same-day
re-entries collapse into one number), never a true per-trade count or per-trade
expectancy. Only the metrics _metrics_from_daily can genuinely derive from that
- total_pnl, max_drawdown, win_rate, return_max_dd, reward_risk_ratio,
max_profit, max_loss - get recomputed and overwritten, and ONLY after
deducting brokerage_amount/taxes_charges_amount (prorated per trade-day from
the original row's own full-range values - see
_prorated_charges_per_trade_day) - a trade report's raw P/L is gross of both,
but AlgoTest's own total_pnl on every other row in this app is net of both, so
skipping this step (confirmed live in an earlier version of this script)
silently overstated every CAS row's numbers by its share of trading costs.
brokerage_amount/taxes_charges_amount themselves are populated with that same
prorated estimate, not left blank, since they're now an honest (if
approximate, evenly-distributed) part of the calculation rather than omitted
from it. Everything that's a real per-TRADE concept instead of a per-trade-DAY
one (total_trades, expectancy, avg_profit_per_trade, avg_loss_per_trade,
loss_rate, avg_profit_winning_trades) is still left BLANK rather than
mislabeled with a subtly-wrong number - same "leave it blank rather than guess"
convention as every other backfill/enrichment script this session. One new
supplementary column, trade_days (distinct trading days actually inside the
window), is added for sample-size context - a CAS Return/MaxDD built on 4 days
means far less than the same number over 40.

A combo appearing in more than one loaded results_web_*.csv (the same strategy
independently discovered by more than one sweep) is deduplicated - only its
first-seen row is used as the template; its trade report and computed metrics
are identical either way, since both are keyed by the SAME combo_id.

Two separate duplicate-avoidance checks, both skip entirely rather than making
a "_cas..." copy:
  1. A combo whose OWN recorded backtest already starts on/after --cas-start
     (e.g. one from a sweep that was itself only ever run from 2026-08-01
     onward) - its report already IS exactly the CAS window, a copy would just
     duplicate data already sitting there under its own combo_id.
  2. A DIFFERENT combo_id covering the exact same underlying strategy (see
     store.strategy_key - a different date range hashes differently, so this
     can never be caught by combo_id matching alone) that already has a real,
     downloaded, genuinely CAS-window backtest - e.g. a dedicated "...CAS"
     sweep run separately from this long-dated one, covering some of the same
     strategies. Slicing the long-dated combo too would just be a second,
     redundant answer to a question a real backtest already answered.
Both only ever apply to genuinely long-dated backtests (start_date before
--cas-start) that actually have pre-cutoff trades to slice out in the first
place.

Usage:
    uv run python -m scripts.build_cas_subset                       # dry run, every results_web_*.csv in output/
    uv run python -m scripts.build_cas_subset --apply                # write the new report files + per-instrument CSVs
    uv run python -m scripts.build_cas_subset --cas-start 2026-07-01 --apply   # a different cutoff
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.correlate import compute_portfolio_metrics, instrument_slug, parse_trade_report, trade_report_path
from src.web import registry

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "output"
DEFAULT_CAS_START = "2026-08-01"

# Matches ANY descriptive suffix after the timestamp (a manually-renamed
# execution, or this very script's own past output) - genuine backup/derived
# files (e.g. the DTE-backfill backup) still excluded, since those use "."
# immediately after the timestamp, never "_". Deliberately a local copy, not
# scripts/migrate_to_registry.py's own (stricter, timestamp-only)
# find_result_files - confirmed live that the stricter pattern silently hid
# BOTH a manually-renamed execution ("..._sensex_0dte_cas.csv") and this
# script's own first real output ("..._nifty_cas_all.csv") from being
# considered as source combos on a later run, exactly the bug this closes.
# Same pattern (and the same fix) as src/web/app.py's list_result_csvs.
_RESULT_CSV_NAME = re.compile(r"^results_web_\d{8}_\d{6}(_.+)?\.csv$")


def find_source_csvs(output_dir: Path) -> list[Path]:
    """Every real results file in `output_dir` to consider as a long-dated
    sweep to derive CAS rows FROM - oldest to newest by mtime. Deliberately
    does NOT also look in output_dir/archive (unlike list_result_csvs) - this
    script's own combo-registry-backed duplicate check (see
    strategy_keys_already_covered_by_cas) already covers archived data for
    THAT purpose, and re-scanning dozens of older, already-registry-folded
    files as fresh SOURCES here every run would slow every future run down for
    combos this session's actual sweeps never touch."""
    return sorted(
        (p for p in output_dir.glob("results_web_*.csv") if _RESULT_CSV_NAME.match(p.name)),
        key=lambda p: p.stat().st_mtime,
    )


# Only these get recomputed from the sliced daily P/L - see the module
# docstring's "Metric honesty" note for why the rest are left blank instead.
_RECOMPUTED_METRIC_MAP = {
    "total_pnl": "overall_profit",
    "max_drawdown": "max_drawdown",
    "win_rate": "win_pct",
    "return_max_dd": "return_max_dd",
    "reward_risk_ratio": "reward_risk_ratio",
    "max_profit": "max_profit_single_period",
    "max_loss": "max_loss_single_period",
}
_BLANKED_METRIC_FIELDS = [
    "total_trades", "avg_profit_per_trade", "avg_loss_per_trade", "expectancy",
    "loss_rate", "avg_profit_winning_trades",
]


def _prorated_charges_per_trade_day(row: dict) -> tuple[float, float]:
    """(brokerage_per_trade_day, taxes_per_trade_day), each = the ORIGINAL
    (full-range) row's own brokerage_amount / taxes_charges_amount divided by
    its total_trades - the per-trade-day cost to deduct from each day in the
    sliced window before computing metrics.

    Confirmed live this was missing entirely in an earlier version of this
    script: summing a combo's raw trade-report P/L over its FULL range and
    comparing to that row's own scraped total_pnl showed the gap was exactly
    its recorded brokerage+taxes (to within a few paise on a multi-lakh total)
    - AlgoTest's own total_pnl is net of both, but a trade report's raw P/L
    values are not (see compute_portfolio_metrics_weighted_with_charges's own
    docstring in src/correlate.py, which already establishes this same fact
    for a different caller). Leaving them undeducted here made every CAS row's
    total_pnl (and everything derived from it - max_drawdown, return_max_dd,
    reward_risk_ratio) systematically overstated.

    (0.0, 0.0) - deduct nothing - whenever total_trades isn't present/positive
    on the original row, same "don't guess, leave it honestly absent"
    convention as _BLANKED_METRIC_FIELDS, rather than fabricating a deduction
    from incomplete data."""
    try:
        total_trades = float(row.get("total_trades") or 0)
        brokerage = float(row.get("brokerage_amount") or 0)
        taxes = float(row.get("taxes_charges_amount") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if total_trades <= 0:
        return 0.0, 0.0
    return brokerage / total_trades, taxes / total_trades


def cas_combo_id(base_cid: str, cas_start: str) -> str:
    return f"{base_cid}_cas{cas_start.replace('-', '')}"


def load_rows_by_instrument(csv_paths: list[Path]) -> dict[str, dict[str, dict]]:
    """instrument -> {combo_id: row}, first-seen wins across however many
    source files a combo_id happens to appear in."""
    out: dict[str, dict[str, dict]] = {}
    for path in csv_paths:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                cid = row.get("combo_id")
                if not cid or row.get("status") != "ok":
                    continue
                instrument = row.get("instrument") or "unknown"
                bucket = out.setdefault(instrument, {})
                bucket.setdefault(cid, row)
    return out


def strategy_keys_already_covered_by_cas(reports_dir: Path, cas_start: str) -> set[str]:
    """strategy_key of every combo that ALREADY has a genuine (not sliced)
    CAS-window backtest with a real downloaded report SOMEWHERE the system has
    ever recorded - checked against combo_registry.csv, not just whichever
    results_web_*.csv files happen to still be sitting loose in output/ under
    their original auto-generated name.

    This matters: confirmed live against the real data this was built for - a
    manually-renamed execution (e.g. "results_web_..._sensex_0dte_cas.csv",
    1,674 combos, every one genuinely start_date=2026-08-01) is invisible to
    both this script's own file-glob AND the Analyze page's own file picker
    (both use the same strict auto-generated-name pattern) - but every one of
    its combo_ids, with strategy_key intact, IS already sitting in the
    registry regardless, because every sweep and Force re-download upserts
    into it directly, independent of what the source file happens to be named.
    Scanning only the loose results_web_*.csv files would have silently missed
    this and created duplicate CAS rows for anything overlapping it.

    A combo_id sharing one of these strategy_keys from a genuinely long-dated
    sweep has nothing new to add by being sliced - a real CAS-window backtest
    of that strategy already exists and downloaded fine on its own, so slicing
    the long one too would just be a redundant second opinion on the same
    question. Only registry rows with BOTH a strategy_key and an
    actually-downloaded report count - a row that merely exists with a late
    start_date but no report yet contributes nothing usable to compare
    against."""
    covered: set[str] = set()
    for cid, row in registry.read_registry().items():
        key = row.get("strategy_key")
        recorded_start = row.get("start_date")
        if not key or not recorded_start or recorded_start < cas_start:
            continue
        instrument = row.get("instrument") or ""
        if trade_report_path(reports_dir, instrument, cid).exists():
            covered.add(key)
    return covered


def already_sliced_cas_combo_ids(output_dir: Path) -> set[str]:
    """Every combo_id already sitting in a PREVIOUSLY-generated
    "*_cas_all.csv" file - this script's own past output, across every prior
    run, regardless of which timestamp that run happened to be saved under.
    Lets the tool be re-run anytime ("load all CSVs and build a subset
    whenever") without ever producing a duplicate row for a combo it already
    sliced - a genuinely different check from strategy_keys_already_covered_
    by_cas above (that one is "a DIFFERENT combo_id already has a REAL,
    separately-downloaded CAS backtest for the same strategy"; this one is
    "I, this exact script, already sliced this exact combo_id before").

    Since cas_combo_id() is a pure function of (base combo_id, cas_start),
    re-running with the SAME cutoff on a combo already sliced under it would
    otherwise produce the identical combo_id a second time, in a second file -
    not wrong data, just pointless duplication the caller has to manually
    notice and avoid re-running into."""
    ids: set[str] = set()
    for path in output_dir.glob("results_web_*_cas_all.csv"):
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                cid = row.get("combo_id")
                if cid:
                    ids.add(cid)
    return ids


def build_cas_rows_for_instrument(
    rows_by_id: dict[str, dict],
    reports_dir: Path,
    cas_start: str,
    already_covered: set[str] | None = None,
    already_sliced: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict[str, float]], Counter]:
    """New rows + {new_combo_id: sliced_series} for one instrument's own
    combo_id -> row map, plus an outcome Counter for the dry-run summary.
    `already_covered` (see strategy_keys_already_covered_by_cas) is passed in
    rather than recomputed per instrument - it's the same registry-wide set
    regardless of which instrument is currently being processed."""
    new_rows: list[dict] = []
    new_series: dict[str, dict[str, float]] = {}
    counts: Counter = Counter()
    now = datetime.now(timezone.utc).isoformat()
    already_covered = already_covered if already_covered is not None else set()
    already_sliced = already_sliced if already_sliced is not None else set()

    for cid, row in rows_by_id.items():
        # Case 1: THIS row's own recorded backtest already starts on/after
        # cas_start - nothing before the cutoff to slice out, its report
        # already IS exactly the CAS window. Only applies when start_date is
        # actually known; a row missing it falls through to the checks below
        # rather than being assumed either way.
        recorded_start = row.get("start_date")
        if recorded_start and recorded_start >= cas_start:
            counts["already_cas_only"] += 1
            continue

        # Case 2: a DIFFERENT combo_id (a different date range hashes
        # differently - see store.strategy_key) covering the exact same
        # underlying strategy already has a real, downloaded, genuinely
        # CAS-window backtest (e.g. from a dedicated "...CAS" sweep run
        # separately from this long-dated one). Slicing this long-dated combo
        # too would just duplicate that already-answered question.
        key = row.get("strategy_key")
        if key and key in already_covered:
            counts["strategy_already_has_cas_backtest"] += 1
            continue

        # Case 3: THIS exact combo (same base id, same cutoff) was already
        # sliced by an earlier run of this very script - see
        # already_sliced_cas_combo_ids. Checked before ever touching the
        # trade report file (cas_combo_id is a pure function of cid+cas_start,
        # no I/O needed), so a re-run over everything in output/ stays cheap
        # even when almost all of it was already handled last time.
        if cas_combo_id(cid, cas_start) in already_sliced:
            counts["already_sliced_previously"] += 1
            continue

        instrument = row.get("instrument") or ""
        path = trade_report_path(reports_dir, instrument, cid)
        if not path.exists():
            counts["no_report"] += 1
            continue
        full_series = parse_trade_report(path)
        windowed = {d: pnl for d, pnl in full_series.items() if d >= cas_start}
        if not windowed:
            counts["no_trades_in_window"] += 1
            continue

        # Deduct brokerage+taxes BEFORE computing metrics (not just off the
        # final total) so max_drawdown/return_max_dd also reflect the real,
        # net-of-charges equity curve, not an inflated gross one - see
        # _prorated_charges_per_trade_day's own docstring for why this exists.
        brokerage_per_trade, taxes_per_trade = _prorated_charges_per_trade_day(row)
        per_trade_charge = brokerage_per_trade + taxes_per_trade
        windowed_net = {d: pnl - per_trade_charge for d, pnl in windowed.items()}

        new_cid = cas_combo_id(cid, cas_start)
        metrics = compute_portfolio_metrics([cid], {cid: windowed_net})

        new_row = dict(row)
        new_row["combo_id"] = new_cid
        new_row["run_at"] = now
        new_row["start_date"] = cas_start
        # end_date deliberately left as the ORIGINAL row's own recorded
        # end_date, not forced to a single global value - honestly reflects
        # exactly how far this row's underlying data actually reaches, and
        # stays uniform across rows automatically in the common case (one
        # recent sweep per instrument, all sharing the same end_date already).
        # Rounded to 2dp - matching how every other metric column in this app
        # is already formatted (raw floating-point noise like
        # "16954.269999999993" would stick out next to "16954.27" everywhere
        # else on the same page).
        recomputed = {
            dest_field: (round(metrics[src_field], 2) if metrics.get(src_field) is not None else "")
            for dest_field, src_field in _RECOMPUTED_METRIC_MAP.items()
        }
        new_row.update(recomputed)
        for field in _BLANKED_METRIC_FIELDS:
            new_row[field] = ""
        # Prorated across just the window's own trade-days - blank (not 0)
        # whenever the original row didn't have enough data to prorate from
        # in the first place, same as every other blanked field above (see
        # _prorated_charges_per_trade_day).
        new_row["brokerage_amount"] = round(brokerage_per_trade * len(windowed), 2) if brokerage_per_trade > 0 else ""
        new_row["taxes_charges_amount"] = round(taxes_per_trade * len(windowed), 2) if taxes_per_trade > 0 else ""
        new_row["trade_days"] = len(windowed)
        new_row["raw_metrics_json"] = json.dumps(recomputed)

        new_rows.append(new_row)
        new_series[new_cid] = windowed
        counts["computed"] += 1

    return new_rows, new_series, counts


def _write_sliced_report(target: Path, series: dict[str, float]) -> None:
    """Same minimal synthetic-report shape as regime_state.py's own
    _slice_regular_report_into_window - one parent row per date, which is all
    parse_trade_report ever reads back out of a report file anyway."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Index", "Entry Date", "P/L"])
        for i, d in enumerate(sorted(series)):
            writer.writerow([str(i), d, series[d]])


def _write_output_csv(path: Path, rows: list[dict]) -> None:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def run_cas_subset(
    output_dir: Path,
    cas_start: str = DEFAULT_CAS_START,
    csv_paths: list[Path] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """The actual orchestration - CLI's main() and the web app's own
    /api/analyze/cas-subset endpoint both call this directly (rather than the
    endpoint shelling out to the CLI) so there's exactly one place this logic
    lives. `apply=False` (the default) only computes and returns the plan,
    writing nothing - the caller (CLI or endpoint) decides whether/when to
    call this again with `apply=True`, same dry-run-then-confirm shape as
    every other "preview, then explicitly commit" flow in this app (e.g.
    /api/narrow).

    Returns a plain dict (JSON-serializable as-is, which is exactly what the
    endpoint needs) - never raises for "no source files found" or "nothing
    eligible", since both are normal, common outcomes here (an empty
    output/ dir, or a --cas-start with nothing past it yet), not errors."""
    reports_dir = output_dir / "trade_reports"
    resolved_csv_paths = csv_paths if csv_paths else find_source_csvs(output_dir)
    if not resolved_csv_paths:
        return {
            "cas_start": cas_start,
            "source_file_count": 0,
            "already_covered_strategy_count": 0,
            "by_instrument": [],
            "total_new_rows": 0,
            "applied": False,
            "message": f"No results_web_*.csv files found under {output_dir}.",
        }

    registry.REGISTRY_PATH = output_dir / "combo_registry.csv"
    already_covered = strategy_keys_already_covered_by_cas(reports_dir, cas_start)
    already_sliced = already_sliced_cas_combo_ids(output_dir)

    rows_by_instrument = load_rows_by_instrument(resolved_csv_paths)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan: dict[str, tuple[Path, list[dict], dict[str, dict[str, float]]]] = {}
    by_instrument: list[dict[str, Any]] = []
    for instrument, rows_by_id in sorted(rows_by_instrument.items()):
        new_rows, new_series, counts = build_cas_rows_for_instrument(
            rows_by_id, reports_dir, cas_start, already_covered, already_sliced
        )
        out_name = f"results_web_{timestamp}_{instrument_slug(instrument)}_cas_all.csv"
        entry = {
            "instrument": instrument,
            "combo_count": len(rows_by_id),
            "computed": counts["computed"],
            "already_cas_only": counts["already_cas_only"],
            "strategy_already_has_cas_backtest": counts["strategy_already_has_cas_backtest"],
            "already_sliced_previously": counts["already_sliced_previously"],
            "no_report": counts["no_report"],
            "no_trades_in_window": counts["no_trades_in_window"],
            "output_file": None,
        }
        if new_rows:
            plan[instrument] = (output_dir / out_name, new_rows, new_series)
            entry["output_file"] = out_name
        by_instrument.append(entry)

    total = sum(len(rows) for _, rows, _ in plan.values())

    if apply:
        for instrument, (out_path, new_rows, new_series) in plan.items():
            for new_cid, series in new_series.items():
                _write_sliced_report(trade_report_path(reports_dir, instrument, new_cid), series)
            _write_output_csv(out_path, new_rows)

    return {
        "cas_start": cas_start,
        "source_file_count": len(resolved_csv_paths),
        "already_covered_strategy_count": len(already_covered),
        "already_sliced_combo_count": len(already_sliced),
        "by_instrument": by_instrument,
        "total_new_rows": total,
        "applied": apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually write the new trade reports + per-instrument CSVs (default: dry run, report only)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory (default: ./output)")
    parser.add_argument("--cas-start", default=DEFAULT_CAS_START, help=f"only trades on/after this date count (default: {DEFAULT_CAS_START})")
    parser.add_argument("--csv", type=Path, nargs="*", default=None, help="specific results CSV(s) to source combos from (default: every results_web_*.csv found in --output-dir)")
    args = parser.parse_args()

    result = run_cas_subset(args.output_dir, args.cas_start, args.csv, apply=args.apply)

    if result.get("message"):
        print(result["message"])
        return 1

    if result["already_covered_strategy_count"]:
        print(f"{result['already_covered_strategy_count']} distinct strategy/strategies already have a real, downloaded CAS-window backtest on record (won't be duplicated).")
        print()
    if result["already_sliced_combo_count"]:
        print(f"{result['already_sliced_combo_count']} combo(s) already sliced by an earlier run of this tool (won't be duplicated).")
        print()

    total_combos = sum(e["combo_count"] for e in result["by_instrument"])
    print(f"{total_combos} distinct combo_id(s) across {len(result['by_instrument'])} instrument(s) in {result['source_file_count']} source file(s).")
    print()

    for e in result["by_instrument"]:
        print(
            f"{e['instrument']}: {e['combo_count']} combo(s), {e['computed']} will get a CAS row, "
            f"{e['already_cas_only']} already start on/after {args.cas_start} (no duplicate made), "
            f"{e['strategy_already_has_cas_backtest']} skipped - same strategy already has a real CAS-window backtest elsewhere, "
            f"{e['already_sliced_previously']} already sliced in an earlier run, "
            f"{e['no_report']} no downloaded report, {e['no_trades_in_window']} no trades since {args.cas_start}"
        )
        if e["output_file"]:
            print(f"  -> {e['output_file']}")

    print(f"\nTotal: {result['total_new_rows']} new CAS row(s) across {sum(1 for e in result['by_instrument'] if e['output_file'])} new file(s).")

    if not args.apply:
        print("\nDry run only - nothing written. Re-run with --apply to write the new trade reports + CSVs.")
    else:
        for e in result["by_instrument"]:
            if e["output_file"]:
                print(f"Wrote {e['computed']} row(s) -> {e['output_file']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
