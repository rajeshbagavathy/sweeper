"""One-time reconciliation: archive every existing output/trade_reports_regime/
file that's provably redundant - i.e. re-derivable at any time by slicing its
combo's own output/trade_reports/ full-history report, exactly the same
containment check src/web/regime_state.py's _slice_regular_report_into_window
already applies going forward for NEW CAS downloads (see that function's own
docstring, and the "reconcile scattered combo data" plan this belongs to).

A regime file whose window is NOT fully covered by its combo's full report is
left completely untouched - per an explicit decision this session, nothing here
ever merges or rewrites a genuine AlgoTest download; only the provably-redundant
case is ever archived.

Requires scripts/migrate_to_registry.py to have already been run (reads each
combo's recorded start_date/end_date from the registry, not from re-scanning
every results_web_*.csv again).

Usage:
    uv run python scripts/reconcile_regime_reports.py              # dry run - report only, nothing moved
    uv run python scripts/reconcile_regime_reports.py --apply      # actually archive redundant files
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from src.correlate import parse_trade_report, trade_report_path
from src.web import registry

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "output"
_WINDOW_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})$")


def find_regime_window_dirs(regime_reports_dir: Path) -> list[tuple[str, str, Path]]:
    """Every [date_from, date_to, dir] triple actually present - same directory-
    naming convention as src/web/regime_state.py's own list_downloaded_regime_windows."""
    if not regime_reports_dir.exists():
        return []
    windows = []
    for child in sorted(regime_reports_dir.iterdir()):
        m = _WINDOW_DIR_RE.match(child.name)
        if m and child.is_dir():
            windows.append((m.group(1), m.group(2), child))
    return windows


def is_redundant(recorded_start: str | None, recorded_end: str | None, date_from: str, date_to: str) -> bool:
    """Same containment logic as regime_state._slice_regular_report_into_window:
    the combo's own recorded window must fully CONTAIN [date_from, date_to] for
    its regime-window copy to be re-derivable by slicing at any time."""
    if not recorded_start or not recorded_end:
        return False
    return recorded_start <= date_from and recorded_end >= date_to


def find_redundant_files(
    window_dirs: list[tuple[str, str, Path]], registry_rows: dict[str, dict], reports_dir: Path,
) -> list[Path]:
    """Every file under a window dir whose combo is provably redundant - the
    combo's registry row says its full report already covers this exact window,
    AND that full report genuinely has trade-dates inside the window (matching
    _slice_regular_report_into_window's own "don't trust a claimed window that
    turns out empty" caution)."""
    redundant: list[Path] = []
    for date_from, date_to, window_dir in window_dirs:
        for path in window_dir.rglob("*.csv"):
            cid = path.stem
            row = registry_rows.get(cid)
            if not row:
                continue
            if not is_redundant(row.get("start_date"), row.get("end_date"), date_from, date_to):
                continue
            instrument = row.get("instrument") or path.parent.name
            regular_path = trade_report_path(reports_dir, instrument, cid)
            if not regular_path.exists():
                continue
            full_series = parse_trade_report(regular_path)
            if not any(date_from <= d <= date_to for d in full_series):
                continue
            redundant.append(path)
    return redundant


def archive_files(paths: list[Path], regime_reports_dir: Path, output_dir: Path) -> list[Path]:
    """Moves each file into output/archive/trade_reports_regime/<same relative
    path>, preserving the window/instrument/combo_id structure - byte-identical,
    fully reversible."""
    archive_root = output_dir / "archive" / "trade_reports_regime"
    moved = []
    for path in paths:
        rel = path.relative_to(regime_reports_dir)
        dest = archive_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        moved.append(dest)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually archive redundant files (default: dry run, report only)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory to reconcile (default: ./output)")
    args = parser.parse_args()

    output_dir = args.output_dir
    # See migrate_to_registry.py's own identical fix - registry.REGISTRY_PATH
    # defaults to the REAL ./output/combo_registry.csv; when --output-dir points
    # somewhere else (a verification copy), the registry must be read from there.
    registry.REGISTRY_PATH = output_dir / "combo_registry.csv"
    regime_reports_dir = output_dir / "trade_reports_regime"
    reports_dir = output_dir / "trade_reports"

    if not registry.REGISTRY_PATH.exists():
        print(f"No registry found at {registry.REGISTRY_PATH} - run "
              f"scripts/migrate_to_registry.py first.")
        return 1

    window_dirs = find_regime_window_dirs(regime_reports_dir)
    print(f"Found {len(window_dirs)} regime window folder(s) under {regime_reports_dir}")
    if not window_dirs:
        print("Nothing to reconcile.")
        return 0

    total_files = sum(1 for _, _, d in window_dirs for _ in d.rglob("*.csv"))
    registry_rows = registry.read_registry()
    redundant = find_redundant_files(window_dirs, registry_rows, reports_dir)

    print(f"{total_files} total file(s) across all window folders.")
    print(f"{len(redundant)} file(s) are provably redundant (already re-derivable "
          f"by slicing their combo's own full report) - these will be archived.")
    print(f"{total_files - len(redundant)} file(s) will be left untouched (their "
          f"combo's full report doesn't fully cover that window).")

    if not args.apply:
        print("\nDry run only - nothing moved. Re-run with --apply to archive the "
              "redundant files.")
        return 0

    archived = archive_files(redundant, regime_reports_dir, output_dir)
    print(f"\nArchived {len(archived)} file(s) to {output_dir / 'archive' / 'trade_reports_regime'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
