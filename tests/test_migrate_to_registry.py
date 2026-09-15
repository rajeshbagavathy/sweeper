from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts import migrate_to_registry
from scripts.migrate_to_registry import archive_files, find_result_files, pick_winners
from src.web import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")


def _write_results_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "run_at", "status", "instrument", "dte", "start_date", "end_date", "total_pnl"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


def test_find_result_files_only_matches_the_exact_naming_convention(tmp_path):
    (tmp_path / "results_web_20260101_000000.csv").write_text("x")
    (tmp_path / "results_web_20260102_000000_sensex_0dte_cas.csv").write_text("x")  # derived, excluded
    (tmp_path / "results_20260101_000000.csv").write_text("x")  # not a "_web_" file
    found = find_result_files(tmp_path)
    assert [p.name for p in found] == ["results_web_20260101_000000.csv"]


def test_find_result_files_sorted_oldest_to_newest(tmp_path):
    import os
    import time

    older = tmp_path / "results_web_20260101_000000.csv"
    newer = tmp_path / "results_web_20260102_000000.csv"
    newer.write_text("x")
    time.sleep(0.01)
    older.write_text("x")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    found = find_result_files(tmp_path)
    assert [p.name for p in found] == ["results_web_20260101_000000.csv", "results_web_20260102_000000.csv"]


def test_pick_winners_prefers_the_complete_row_over_an_incomplete_one(tmp_path):
    """An older file's row happens to be complete; a newer file's row for the
    same combo_id is contaminated (mixed weekdays for a single DTE) - the
    complete one must win even though it's not the most recent."""
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "a.csv", [("1", "2026-08-07", "100"), ("2", "2026-08-14", "-50")])  # Fridays only

    old_file = tmp_path / "results_web_20260101_000000.csv"
    new_file = tmp_path / "results_web_20260102_000000.csv"
    _write_results_csv(old_file, [{
        "combo_id": "a", "run_at": "2026-01-01", "status": "ok", "instrument": "NIFTY",
        "dte": "1", "start_date": "2026-08-05", "end_date": "2026-08-16", "total_pnl": "50",
    }])
    _write_results_csv(new_file, [{
        "combo_id": "a", "run_at": "2026-01-02", "status": "ok", "instrument": "NIFTY",
        "dte": "1", "start_date": "2026-01-01", "end_date": "2026-12-31", "total_pnl": "999",
    }])

    winners = pick_winners([old_file, new_file], reports_dir)
    # old_file's recorded window matches the actual (complete) Friday-only data;
    # new_file's recorded window (a full year) is NOT reached by that same data,
    # so it would be flagged incomplete - old_file's row must win.
    assert winners["a"]["total_pnl"] == "50"
    assert winners["a"]["report_complete"] is True


def test_pick_winners_prefers_the_more_recent_file_when_equally_complete(tmp_path):
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "a.csv", [("1", "2026-08-07", "100")])

    old_file = tmp_path / "results_web_20260101_000000.csv"
    new_file = tmp_path / "results_web_20260102_000000.csv"
    row = {
        "combo_id": "a", "status": "ok", "instrument": "NIFTY", "dte": "1",
        "start_date": "2026-08-05", "end_date": "2026-08-09",
    }
    _write_results_csv(old_file, [{**row, "run_at": "old", "total_pnl": "50"}])
    _write_results_csv(new_file, [{**row, "run_at": "new", "total_pnl": "999"}])

    winners = pick_winners([old_file, new_file], reports_dir)
    assert winners["a"]["total_pnl"] == "999"


def test_pick_winners_parses_each_combos_report_only_once(tmp_path, monkeypatch):
    """Performance guard: the same combo_id appearing in many files must not
    re-parse its trade report once per appearance."""
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "a.csv", [("1", "2026-08-07", "100")])

    files = []
    for i in range(5):
        path = tmp_path / f"results_web_2026010{i}_000000.csv"
        _write_results_csv(path, [{
            "combo_id": "a", "run_at": str(i), "status": "ok", "instrument": "NIFTY",
            "dte": "1", "start_date": "2026-08-05", "end_date": "2026-08-09", "total_pnl": str(i),
        }])
        files.append(path)

    import src.correlate as correlate_mod

    parse_calls = []
    real_parse = correlate_mod.parse_trade_report

    def spy_parse(path):
        parse_calls.append(path)
        return real_parse(path)

    monkeypatch.setattr("scripts.migrate_to_registry.parse_trade_report", spy_parse)

    pick_winners(files, reports_dir)
    assert len(parse_calls) == 1


def test_pick_winners_skips_rows_with_no_combo_id(tmp_path):
    reports_dir = tmp_path / "reports"
    path = tmp_path / "results_web_20260101_000000.csv"
    _write_results_csv(path, [{
        "combo_id": "", "run_at": "x", "status": "ok", "instrument": "NIFTY",
        "dte": "1", "start_date": "", "end_date": "", "total_pnl": "0",
    }])
    winners = pick_winners([path], reports_dir)
    assert winners == {}


def test_main_writes_the_registry_under_the_given_output_dir_not_the_real_one(tmp_path, monkeypatch):
    """Regression guard for a real incident this session: main() must point
    registry.REGISTRY_PATH at --output-dir's own combo_registry.csv, not
    silently keep writing to the real ./output/combo_registry.csv regardless of
    --output-dir - confirmed live, a test run leaked fixture data into the real
    file before this was fixed."""
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "a.csv", [("1", "2026-08-07", "100")])
    _write_results_csv(tmp_path / "results_web_20260101_000000.csv", [{
        "combo_id": "a", "run_at": "x", "status": "ok", "instrument": "NIFTY",
        "dte": "1", "start_date": "2026-08-05", "end_date": "2026-08-09", "total_pnl": "50",
    }])
    real_registry_path = tmp_path / "should_never_be_written" / "combo_registry.csv"
    monkeypatch.setattr(registry, "REGISTRY_PATH", real_registry_path)

    monkeypatch.setattr("sys.argv", ["migrate_to_registry.py", "--output-dir", str(tmp_path), "--apply"])
    migrate_to_registry.main()

    assert not real_registry_path.exists()
    assert (tmp_path / "combo_registry.csv").exists()
    assert registry.read_registry()["a"]["total_pnl"] == "50"


def test_archive_files_moves_without_rewriting(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    f1 = output_dir / "results_web_20260101_000000.csv"
    f1.write_text("original content")

    moved = archive_files([f1], output_dir)

    assert not f1.exists()
    assert moved[0] == output_dir / "archive" / "results_web_20260101_000000.csv"
    assert moved[0].read_text() == "original content"
