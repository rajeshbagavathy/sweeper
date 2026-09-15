from __future__ import annotations

from pathlib import Path

import pytest

from scripts import reconcile_regime_reports
from scripts.reconcile_regime_reports import (
    archive_files,
    find_redundant_files,
    find_regime_window_dirs,
    is_redundant,
)
from src.correlate import trade_report_path
from src.web import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


def test_find_regime_window_dirs_matches_the_naming_convention(tmp_path):
    regime_dir = tmp_path / "trade_reports_regime"
    (regime_dir / "2026-08-01_to_2026-09-03").mkdir(parents=True)
    (regime_dir / "not-a-window-dir").mkdir(parents=True)
    windows = find_regime_window_dirs(regime_dir)
    assert [(f, t) for f, t, _ in windows] == [("2026-08-01", "2026-09-03")]


def test_find_regime_window_dirs_empty_when_missing():
    assert find_regime_window_dirs(Path("/does/not/exist")) == []


def test_is_redundant_true_when_recorded_window_contains_the_regime_window():
    assert is_redundant("2026-08-01", "2026-09-30", "2026-08-05", "2026-09-03") is True


def test_is_redundant_false_when_recorded_window_does_not_fully_cover():
    assert is_redundant("2026-08-10", "2026-09-30", "2026-08-05", "2026-09-03") is False
    assert is_redundant("2026-08-01", "2026-08-20", "2026-08-05", "2026-09-03") is False


def test_is_redundant_false_when_no_recorded_window():
    assert is_redundant(None, None, "2026-08-01", "2026-09-03") is False


def test_find_redundant_files_flags_a_covered_combo_and_skips_an_uncovered_one(tmp_path):
    regime_dir = tmp_path / "trade_reports_regime"
    reports_dir = tmp_path / "trade_reports"
    window_dir = regime_dir / "2026-08-01_to_2026-08-20"

    # "covered" - its full report's recorded window contains the regime window,
    # and genuinely has trade-dates inside it.
    (window_dir / "nifty").mkdir(parents=True)
    (window_dir / "nifty" / "covered.csv").write_text("placeholder")
    _write_report(trade_report_path(reports_dir, "NIFTY", "covered"), [("1", "2026-08-07", "100")])

    # "notcovered" - its full report's recorded window does NOT reach the regime window.
    (window_dir / "nifty" / "notcovered.csv").write_text("placeholder")
    _write_report(trade_report_path(reports_dir, "NIFTY", "notcovered"), [("1", "2026-09-15", "100")])

    registry_rows = {
        "covered": {"instrument": "NIFTY", "start_date": "2026-08-01", "end_date": "2026-08-25"},
        "notcovered": {"instrument": "NIFTY", "start_date": "2026-09-10", "end_date": "2026-09-20"},
    }
    window_dirs = find_regime_window_dirs(regime_dir)
    redundant = find_redundant_files(window_dirs, registry_rows, reports_dir)

    assert redundant == [window_dir / "nifty" / "covered.csv"]


def test_find_redundant_files_skips_a_combo_missing_from_the_registry(tmp_path):
    regime_dir = tmp_path / "trade_reports_regime"
    reports_dir = tmp_path / "trade_reports"
    window_dir = regime_dir / "2026-08-01_to_2026-08-20"
    (window_dir / "nifty").mkdir(parents=True)
    (window_dir / "nifty" / "unknown.csv").write_text("placeholder")

    redundant = find_redundant_files(find_regime_window_dirs(regime_dir), {}, reports_dir)
    assert redundant == []


def test_find_redundant_files_skips_when_claimed_window_has_no_actual_trades_inside_it(tmp_path):
    """A recorded window can technically contain the regime window while the
    report's real trade-dates land nowhere inside it - must not be treated as
    redundant (mirrors _slice_regular_report_into_window's own caution)."""
    regime_dir = tmp_path / "trade_reports_regime"
    reports_dir = tmp_path / "trade_reports"
    window_dir = regime_dir / "2026-08-01_to_2026-08-20"
    (window_dir / "nifty").mkdir(parents=True)
    (window_dir / "nifty" / "a.csv").write_text("placeholder")
    _write_report(trade_report_path(reports_dir, "NIFTY", "a"), [("1", "2026-07-01", "100")])

    registry_rows = {"a": {"instrument": "NIFTY", "start_date": "2026-06-01", "end_date": "2026-09-30"}}
    redundant = find_redundant_files(find_regime_window_dirs(regime_dir), registry_rows, reports_dir)
    assert redundant == []


def test_main_reads_the_registry_under_the_given_output_dir_not_the_real_one(tmp_path, monkeypatch):
    """Same regression guard as migrate_to_registry.py's own identical fix:
    main() must point registry.REGISTRY_PATH at --output-dir's own
    combo_registry.csv, not silently look for the real ./output/combo_registry.csv
    regardless of --output-dir."""
    output_dir = tmp_path / "output"
    (output_dir / "combo_registry.csv").parent.mkdir(parents=True, exist_ok=True)
    import csv as csv_mod
    with (output_dir / "combo_registry.csv").open("w", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=["combo_id", "instrument", "start_date", "end_date"])
        writer.writeheader()
        writer.writerow({"combo_id": "a", "instrument": "NIFTY", "start_date": "2026-08-01", "end_date": "2026-09-03"})

    real_registry_path = tmp_path / "should_never_be_read" / "combo_registry.csv"
    monkeypatch.setattr(registry, "REGISTRY_PATH", real_registry_path)

    monkeypatch.setattr("sys.argv", ["reconcile_regime_reports.py", "--output-dir", str(output_dir)])
    result = reconcile_regime_reports.main()

    assert result == 0  # found the registry at output_dir, not an error
    assert not real_registry_path.exists()


def test_archive_files_preserves_relative_structure(tmp_path):
    output_dir = tmp_path / "output"
    regime_dir = output_dir / "trade_reports_regime"
    window_dir = regime_dir / "2026-08-01_to_2026-08-20"
    (window_dir / "nifty").mkdir(parents=True)
    f = window_dir / "nifty" / "a.csv"
    f.write_text("real data")

    moved = archive_files([f], regime_dir, output_dir)

    assert not f.exists()
    expected = output_dir / "archive" / "trade_reports_regime" / "2026-08-01_to_2026-08-20" / "nifty" / "a.csv"
    assert moved == [expected]
    assert expected.read_text() == "real data"
