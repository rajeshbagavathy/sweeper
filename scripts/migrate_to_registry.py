"""One-time migration: build combo_registry.csv from every existing
results_web_*.csv, then archive the sources - see the "reconcile scattered
combo data" plan (/Users/rajeshkumarbagavathy/.claude/plans/enchanted-pondering-
raccoon.md) for the full design/reasoning.

The SAME combo_id can appear in many results_web_*.csv files with different
metrics and different recorded start_date/end_date, with nothing today picking
a winner. This scans every file (oldest to newest by mtime) and keeps, per
combo_id, whichever row is most COMPLETE (see src/web/registry.py's
report_completeness - does the row's own trade report actually reach as far as
its claimed start_date/end_date, uncontaminated by other DTEs), tie-broken by
most recent file. Source files are archived (moved to output/archive/), never
deleted or rewritten - fully reversible if anything here turns out wrong.

Usage:
    uv run python scripts/migrate_to_registry.py              # dry run - report only, nothing written
    uv run python scripts/migrate_to_registry.py --apply      # write the registry and archive sources
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
from datetime import datetime
from pathlib import Path

from src.correlate import parse_trade_report, trade_report_path
from src.web import registry

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "output"
_RESULT_CSV_NAME = re.compile(r"^results_web_\d{8}_\d{6}\.csv$")


def find_result_files(output_dir: Path) -> list[Path]:
    """Every real per-run results file, oldest to newest by mtime - excludes
    derived backup files (e.g. the DTE-backfill "*.pre-dte-backfill-backup.csv"),
    same convention as app.py's own list_result_csvs."""
    return sorted(
        (p for p in output_dir.glob("results_web_*.csv") if _RESULT_CSV_NAME.match(p.name)),
        key=lambda p: p.stat().st_mtime,
    )


class _ReportInfoCache:
    """Parses each combo_id's trade report at most ONCE, even though the same
    combo_id can appear in dozens of result files - the report itself doesn't
    change between rows, only each row's own recorded start_date/end_date/dte
    (which is exactly the scatter this migration is reconciling), so only the
    cheap completeness check (pure Python, no I/O) needs to run per row."""

    def __init__(self, reports_dir: Path) -> None:
        self._reports_dir = reports_dir
        self._cache: dict[str, tuple[list[str], str | None]] = {}

    def entry_dates_and_mtime(self, instrument: str, cid: str) -> tuple[list[str], str | None]:
        if cid not in self._cache:
            report_path = trade_report_path(self._reports_dir, instrument, cid)
            if not report_path.exists():
                self._cache[cid] = ([], None)
            else:
                series = parse_trade_report(report_path)
                mtime = datetime.fromtimestamp(report_path.stat().st_mtime).isoformat()
                self._cache[cid] = (sorted(series), mtime)
        return self._cache[cid]


def pick_winners(result_files: list[Path], reports_dir: Path) -> dict[str, dict]:
    """combo_id -> winning row (with the four FRESHNESS_FIELDS merged in),
    keeping whichever row is most complete, tie-broken by most recent file
    (result_files must already be oldest-to-newest - a later file wins a tie)."""
    winners: dict[str, dict] = {}
    winner_complete: dict[str, bool] = {}
    cache = _ReportInfoCache(reports_dir)

    for path in result_files:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                cid = row.get("combo_id")
                if not cid:
                    continue
                instrument = row.get("instrument") or ""
                entry_dates, last_replayed_at = cache.entry_dates_and_mtime(instrument, cid)
                complete = registry.report_completeness(
                    entry_dates, dte=row.get("dte"),
                    recorded_start=row.get("start_date"), recorded_end=row.get("end_date"),
                )
                freshness = {
                    "last_replayed_at": last_replayed_at,
                    "report_window_start": entry_dates[0] if entry_dates else None,
                    "report_window_end": entry_dates[-1] if entry_dates else None,
                    "report_complete": complete,
                }
                if cid not in winners or complete >= winner_complete[cid]:
                    winners[cid] = {**row, **freshness}
                    winner_complete[cid] = complete

    return winners


def archive_files(paths: list[Path], output_dir: Path) -> list[Path]:
    """Moves (never copies-then-deletes, never rewrites) every path into
    output/archive/, preserving its filename - byte-identical, fully reversible."""
    archive_dir = output_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in paths:
        dest = archive_dir / path.name
        shutil.move(str(path), str(dest))
        moved.append(dest)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually write the registry and archive sources (default: dry run, report only)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory to migrate (default: ./output)")
    args = parser.parse_args()

    output_dir = args.output_dir
    # registry.REGISTRY_PATH defaults to the REAL ./output/combo_registry.csv -
    # when --output-dir points somewhere else (a verification copy, never the
    # live directory for a real run), the registry must be written there too,
    # not silently back into the real one.
    registry.REGISTRY_PATH = output_dir / "combo_registry.csv"
    reports_dir = output_dir / "trade_reports"
    result_files = find_result_files(output_dir)

    print(f"Found {len(result_files)} results_web_*.csv file(s) under {output_dir}")
    if not result_files:
        print("Nothing to migrate.")
        return 0

    winners = pick_winners(result_files, reports_dir)
    incomplete = [cid for cid, row in winners.items() if row["report_complete"] is False]

    print(f"{len(winners)} distinct combo_id(s) found across all files.")
    print(f"{len(incomplete)} winning row(s) have an INCOMPLETE trade report "
          f"(contaminated and/or short of their own recorded window) - these will "
          f"still be migrated as-is (they're still the best data available), flagged "
          f"report_complete=False in the registry for later attention.")
    if incomplete:
        print("First 10 incomplete combo_id(s):", incomplete[:10])

    if not args.apply:
        print("\nDry run only - nothing written. Re-run with --apply to write the "
              "registry and archive the source files.")
        return 0

    if registry.REGISTRY_PATH.exists():
        print(f"\nRefusing to overwrite an existing registry at {registry.REGISTRY_PATH} - "
              f"remove or archive it yourself first if you intend to re-run this migration.")
        return 1

    registry.upsert_rows(winners)
    print(f"\nWrote {len(winners)} row(s) to {registry.REGISTRY_PATH}")

    archived = archive_files(result_files, output_dir)
    print(f"Archived {len(archived)} source file(s) to {output_dir / 'archive'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
