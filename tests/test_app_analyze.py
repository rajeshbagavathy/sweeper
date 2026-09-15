from __future__ import annotations

import csv
from pathlib import Path

from src.web.app import (
    analyze_date_range_options,
    analyze_dte_options,
    analyze_instrument_options,
    analyze_param_breakdown,
    analyze_results,
    analyze_time_buckets,
    analyze_timeline_coverage,
    list_result_csvs,
)


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "return_max_dd", "instrument", "dte", "entry_time"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_analyze_results_combines_multiple_files(tmp_path):
    """The whole point: two separate results_web_*.csv files (e.g. one per instrument,
    since each Start writes its own file) can be viewed together without touching
    whatever the live run_state / currently-running sweep is doing."""
    nifty_csv = tmp_path / "results_web_a.csv"
    banknifty_csv = tmp_path / "results_web_b.csv"
    _write_csv(nifty_csv, [{"combo_id": "n1", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"}])
    _write_csv(banknifty_csv, [{"combo_id": "b1", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15"}])

    result = analyze_results(csv_file=[str(nifty_csv), str(banknifty_csv)])
    combo_ids = {r["combo_id"] for r in result["rows"]}
    assert combo_ids == {"n1", "b1"}


def test_analyze_timeline_coverage_scopes_and_aggregates_both_charts(tmp_path):
    """Thin-wrapper check, same spirit as the other analyze endpoint tests: reads
    the requested csv_file(s), scopes to 'ok' rows for the given instrument (a
    BANKNIFTY row must not leak into a NIFTY-scoped chart), and hands off to
    timeline_coverage's two aggregation functions - not testing their own
    bucketing math (see tests/test_timeline_coverage.py for that), just that the
    endpoint actually wires it up."""
    fieldnames = ["combo_id", "status", "instrument", "dte", "entry_time", "exit_time", "start_date", "end_date"]
    csv_path = tmp_path / "results_web_a.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "n1", "status": "ok", "instrument": "NIFTY", "dte": "0",
            "entry_time": "09:20", "exit_time": "10:05",
            "start_date": "2026-06-15", "end_date": "2026-08-10",
        })
        writer.writerow({
            "combo_id": "bn1", "status": "ok", "instrument": "BANKNIFTY", "dte": "0",
            "entry_time": "09:20", "exit_time": "10:05",
            "start_date": "2026-06-15", "end_date": "2026-08-10",
        })

    result = analyze_timeline_coverage(csv_file=[str(csv_path)], instrument="NIFTY")

    timing_by_label = {r["label"]: r["count"] for r in result["strategy_timing"]}
    assert timing_by_label == {"09:20 to 10:05": 1}  # only the NIFTY row's own exact pair
    period_by_label = {r["label"]: r["count"] for r in result["backtest_period"]}
    assert period_by_label["2026-07"] == 1  # a month in the middle of its window, not just the start


def test_analyze_timeline_coverage_no_longer_accepts_entry_exit_time_filters(tmp_path):
    """The actual bug this was fixed for: clicking one Strategy Timing bar sets an
    exact entry+exit time filter (same as picking it by hand) - if this endpoint
    still scoped by that, the very act of clicking a bar would filter every OTHER
    bar out of existence. The fix removes those params entirely, so a caller trying
    to pass them gets a hard TypeError instead of a silent no-op - confirming
    nothing upstream can accidentally resurrect the collapse."""
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "dte", "entry_time", "exit_time", "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "a", "status": "ok", "instrument": "NIFTY", "dte": "0",
            "entry_time": "09:20", "exit_time": "10:05",
            "start_date": "2026-06-15", "end_date": "2026-08-10",
        })

    try:
        analyze_timeline_coverage(csv_file=[str(csv_path)], entry_time_from="09:20")  # type: ignore[call-arg]
        assert False, "entry_time_from should no longer be an accepted parameter"
    except TypeError:
        pass


def test_analyze_timeline_coverage_strategy_timing_shows_every_distinct_pair_regardless_of_which_one_you_picked(tmp_path):
    """Multiple distinct entry/exit pairs must ALL still show up together - this is
    the exact regression: clicking one used to collapse the chart down to just that
    one pair (see this test file's own note on the fix)."""
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "dte", "entry_time", "exit_time", "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for cid, entry, exit_ in [("a", "09:20", "10:05"), ("b", "09:30", "12:00"), ("c", "10:00", "12:00")]:
            writer.writerow({
                "combo_id": cid, "status": "ok", "instrument": "NIFTY", "dte": "0",
                "entry_time": entry, "exit_time": exit_,
                "start_date": "2026-06-15", "end_date": "2026-08-10",
            })

    result = analyze_timeline_coverage(csv_file=[str(csv_path)])
    labels = {r["label"] for r in result["strategy_timing"]}
    assert labels == {"09:20 to 10:05", "09:30 to 12:00", "10:00 to 12:00"}


def test_analyze_timeline_coverage_reports_distinct_backtest_windows(tmp_path):
    """The other real bug: a single sweep's own CSV has every row sharing the SAME
    start_date/end_date, so backtest_period_coverage is always a flat bar per
    month - not broken, just uninformative. This is what lets the UI say that
    plainly instead of showing a chart that looks broken."""
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "dte", "entry_time", "exit_time", "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for cid in ["a", "b", "c"]:
            writer.writerow({
                "combo_id": cid, "status": "ok", "instrument": "NIFTY", "dte": "0",
                "entry_time": "09:20", "exit_time": "10:05",
                "start_date": "2025-01-01", "end_date": "2026-09-09",
            })

    result = analyze_timeline_coverage(csv_file=[str(csv_path)])
    assert result["backtest_period_windows"] == ["2025-01-01 to 2026-09-09"]


def test_analyze_timeline_coverage_date_range_filter_does_not_collapse_backtest_period(tmp_path):
    """date_range IS a row's start_date/end_date - the exact fields
    backtest_period_coverage groups by month - so scoping to one date_range first
    would always flatten it to a single window too. Confirmed here: two genuinely
    different windows both still count, even while date_range narrows the OTHER
    (strategy_timing) chart to just one of them."""
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "dte", "entry_time", "exit_time", "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "a", "status": "ok", "instrument": "NIFTY", "dte": "0",
            "entry_time": "09:20", "exit_time": "10:05",
            "start_date": "2025-01-01", "end_date": "2026-01-01",
        })
        writer.writerow({
            "combo_id": "b", "status": "ok", "instrument": "NIFTY", "dte": "0",
            "entry_time": "09:30", "exit_time": "12:00",
            "start_date": "2025-06-01", "end_date": "2026-06-01",
        })

    result = analyze_timeline_coverage(csv_file=[str(csv_path)], date_range="2025-01-01 to 2026-01-01")
    assert result["backtest_period_windows"] == ["2025-01-01 to 2026-01-01", "2025-06-01 to 2026-06-01"]
    timing_labels = {r["label"] for r in result["strategy_timing"]}
    assert timing_labels == {"09:20 to 10:05"}  # date_range DOES still scope strategy_timing


# --- date_from/date_to: a genuine RANGE over each row's own start_date, distinct
# from date_range's exact single-window pill match - see _scope_ok_rows' own
# docstring for exactly why both exist. The concrete need this was built for:
# pooling every combo from every sweep run since a given date (e.g. 2026-08-01),
# regardless of each one's own slightly different end_date, without having to
# know in advance which specific files/pills those combos happen to live in. ---

def _write_dated_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "instrument", "dte", "entry_time", "exit_time", "start_date", "end_date", "return_max_dd"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_analyze_results_date_from_excludes_rows_starting_before_it(tmp_path):
    csv_path = tmp_path / "results_web_a.csv"
    _write_dated_csv(csv_path, [
        {"combo_id": "old", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2025-01-01", "end_date": "2026-09-09", "return_max_dd": "1"},
        {"combo_id": "cas", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2026-08-01", "end_date": "2026-09-03", "return_max_dd": "2"},
        {"combo_id": "cas2", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2026-08-01", "end_date": "2026-09-09", "return_max_dd": "3"},
    ])

    result = analyze_results(csv_file=[str(csv_path)], date_from="2026-08-01")

    assert {r["combo_id"] for r in result["rows"]} == {"cas", "cas2"}


def test_analyze_results_date_to_excludes_rows_starting_after_it(tmp_path):
    csv_path = tmp_path / "results_web_a.csv"
    _write_dated_csv(csv_path, [
        {"combo_id": "early", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2025-01-01", "end_date": "2026-09-09", "return_max_dd": "1"},
        {"combo_id": "late", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2026-08-01", "end_date": "2026-09-09", "return_max_dd": "2"},
    ])

    result = analyze_results(csv_file=[str(csv_path)], date_to="2025-12-31")

    assert {r["combo_id"] for r in result["rows"]} == {"early"}


def test_analyze_timeline_coverage_date_from_applies_to_both_charts_unlike_date_range(tmp_path):
    """The key difference from date_range: date_from is a range, so it can pool
    MULTIPLE distinct windows together (every sweep since the cutoff) instead of
    collapsing backtest_period_coverage to one flat bar - confirmed here by two
    genuinely different post-cutoff windows both surviving."""
    csv_path = tmp_path / "results_web_a.csv"
    _write_dated_csv(csv_path, [
        {"combo_id": "old", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "exit_time": "10:00", "start_date": "2025-01-01", "end_date": "2026-09-09", "return_max_dd": "1"},
        {"combo_id": "cas1", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:20", "exit_time": "10:05", "start_date": "2026-08-01", "end_date": "2026-09-02", "return_max_dd": "2"},
        {"combo_id": "cas2", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:30", "exit_time": "12:00", "start_date": "2026-08-01", "end_date": "2026-09-09", "return_max_dd": "3"},
    ])

    result = analyze_timeline_coverage(csv_file=[str(csv_path)], date_from="2026-08-01")

    timing_labels = {r["label"] for r in result["strategy_timing"]}
    assert timing_labels == {"09:20 to 10:05", "09:30 to 12:00"}  # "old" excluded from both charts
    assert result["backtest_period_windows"] == ["2026-08-01 to 2026-09-02", "2026-08-01 to 2026-09-09"]
    assert result["backtest_period_row_count"] == 2


def test_analyze_param_breakdown_respects_date_from(tmp_path, monkeypatch):
    import src.web.app as app_mod
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(app_mod, "load_ui_config", lambda: SweepUIConfig())
    csv_path = tmp_path / "results_web_a.csv"
    _write_dated_csv(csv_path, [
        {"combo_id": "old", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2025-01-01", "end_date": "2026-09-09", "return_max_dd": "1"},
        {"combo_id": "cas", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2026-08-01", "end_date": "2026-09-09", "return_max_dd": "2"},
    ])

    result = analyze_param_breakdown(dimension="entry_time", csv_file=[str(csv_path)], date_from="2026-08-01")

    assert result["total"] == 1  # only "cas" survives the date_from filter


def test_analyze_instrument_options_respects_date_from(tmp_path):
    csv_path = tmp_path / "results_web_a.csv"
    _write_dated_csv(csv_path, [
        {"combo_id": "old", "status": "ok", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2025-01-01", "end_date": "2026-09-09", "return_max_dd": "1"},
        {"combo_id": "cas", "status": "ok", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15", "start_date": "2026-08-01", "end_date": "2026-09-09", "return_max_dd": "2"},
    ])

    result = analyze_instrument_options(csv_file=[str(csv_path)], date_from="2026-08-01")

    assert {o["label"] for o in result["options"]} == {"NIFTY"}  # BANKNIFTY-only-before-cutoff excluded


def test_analyze_results_sorted_and_limited(tmp_path):
    csv_path = tmp_path / "results_web_a.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    result = analyze_results(csv_file=[str(csv_path)], limit=1)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["combo_id"] == "b"


def test_analyze_results_no_files_selected_returns_empty(tmp_path):
    result = analyze_results(csv_file=[])
    assert result == {"rows": [], "columns": []}


def test_analyze_results_instrument_filter_applies_across_files(tmp_path):
    nifty_csv = tmp_path / "results_web_a.csv"
    banknifty_csv = tmp_path / "results_web_b.csv"
    _write_csv(nifty_csv, [{"combo_id": "n1", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"}])
    _write_csv(banknifty_csv, [{"combo_id": "b1", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15"}])

    result = analyze_results(csv_file=[str(nifty_csv), str(banknifty_csv)], instrument="BANKNIFTY")
    assert [r["combo_id"] for r in result["rows"]] == ["b1"]


def test_analyze_instrument_options_counts_across_files(tmp_path):
    nifty_csv = tmp_path / "results_web_a.csv"
    banknifty_csv = tmp_path / "results_web_b.csv"
    _write_csv(nifty_csv, [
        {"combo_id": "n1", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
        {"combo_id": "n2", "status": "ok", "return_max_dd": "6", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    _write_csv(banknifty_csv, [{"combo_id": "b1", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15"}])

    options = analyze_instrument_options(csv_file=[str(nifty_csv), str(banknifty_csv)])["options"]
    by_label = {o["label"]: o["count"] for o in options}
    assert by_label == {"NIFTY": 2, "BANKNIFTY": 1}


def test_analyze_dte_options_scoped_to_instrument(tmp_path):
    csv_path = tmp_path / "results_web_a.csv"
    _write_csv(csv_path, [
        {"combo_id": "n1", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
        {"combo_id": "b1", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "1", "entry_time": "09:15"},
    ])
    options = analyze_dte_options(csv_file=[str(csv_path)], instrument="NIFTY")["options"]
    assert {o["label"]: o["count"] for o in options} == {"0": 1}


def test_analyze_date_range_options_counts_distinct_periods(tmp_path):
    """The whole point of this endpoint: a force-redownload can silently advance a
    MINORITY of rows' own start_date/end_date forward while the rest keep the
    original window (see correlate_state.roll_date_window) - this must surface as
    two distinct, correctly-counted options, not get silently merged or hidden."""
    fieldnames = ["combo_id", "status", "instrument", "dte", "start_date", "end_date"]
    csv_path = tmp_path / "results_web_a.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for cid in ["a", "b", "c"]:
            writer.writerow({"combo_id": cid, "status": "ok", "instrument": "NIFTY", "dte": "0",
                              "start_date": "2026-08-01", "end_date": "2026-09-03"})
        writer.writerow({"combo_id": "d", "status": "ok", "instrument": "NIFTY", "dte": "0",
                          "start_date": "2026-08-05", "end_date": "2026-09-07"})

    options = analyze_date_range_options(csv_file=[str(csv_path)])["options"]
    by_label = {o["label"]: o["count"] for o in options}
    assert by_label == {"2026-08-01 to 2026-09-03": 3, "2026-08-05 to 2026-09-07": 1}
    # Most-populous first, so the UI's default/first pill is the majority period.
    assert options[0]["label"] == "2026-08-01 to 2026-09-03"


def test_analyze_results_date_range_filter_isolates_one_period(tmp_path):
    fieldnames = ["combo_id", "status", "return_max_dd", "instrument", "dte", "entry_time", "start_date", "end_date"]
    csv_path = tmp_path / "results_web_a.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0",
                          "entry_time": "09:15", "start_date": "2026-08-01", "end_date": "2026-09-03"})
        writer.writerow({"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "NIFTY", "dte": "0",
                          "entry_time": "09:15", "start_date": "2026-08-05", "end_date": "2026-09-07"})

    result = analyze_results(csv_file=[str(csv_path)], date_range="2026-08-05 to 2026-09-07")
    assert [r["combo_id"] for r in result["rows"]] == ["b"]


def test_analyze_time_buckets_across_files(tmp_path):
    nifty_csv = tmp_path / "results_web_a.csv"
    banknifty_csv = tmp_path / "results_web_b.csv"
    _write_csv(nifty_csv, [{"combo_id": "n1", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"}])
    _write_csv(banknifty_csv, [{"combo_id": "b1", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15"}])

    result = analyze_time_buckets(csv_file=[str(nifty_csv), str(banknifty_csv)], interval=15)
    total = sum(b["count"] for b in result["buckets"])
    assert total == 2


def test_list_result_csvs_reports_row_counts(tmp_path, monkeypatch):
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    _write_csv(tmp_path / "results_web_20260101_000000.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "6", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    files = list_result_csvs()
    assert len(files) == 1
    assert files[0]["row_count"] == 2
    assert files[0]["name"] == "results_web_20260101_000000.csv"
    assert files[0]["instruments"] == ["NIFTY"]


def test_list_result_csvs_reports_multiple_instruments_most_populous_first(tmp_path, monkeypatch):
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    _write_csv(tmp_path / "results_web_20260101_000000.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "6", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "7", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    files = list_result_csvs()
    assert files[0]["instruments"] == ["NIFTY", "BANKNIFTY"]


def test_list_result_csvs_reports_a_uniform_date_range(tmp_path, monkeypatch):
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    fieldnames = ["combo_id", "status", "instrument", "dte", "start_date", "end_date"]
    path = tmp_path / "results_web_20260101_000000.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for cid in ["a", "b"]:
            writer.writerow({"combo_id": cid, "status": "ok", "instrument": "NIFTY", "dte": "0",
                              "start_date": "2026-08-01", "end_date": "2026-09-03"})
    files = list_result_csvs()
    assert files[0]["date_range"] == "2026-08-01 to 2026-09-03"
    assert "date_range_mixed_count" not in files[0]


def test_list_result_csvs_flags_a_file_already_mixing_periods(tmp_path, monkeypatch):
    """The exact real-world case this was built for: a force-redownload's
    roll_date_window rolled a minority of rows forward while the rest kept the
    original window - visible on the file card before the user even loads it."""
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    fieldnames = ["combo_id", "status", "instrument", "dte", "start_date", "end_date"]
    path = tmp_path / "results_web_20260101_000000.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "instrument": "NIFTY", "dte": "0",
                          "start_date": "2026-08-01", "end_date": "2026-09-03"})
        writer.writerow({"combo_id": "b", "status": "ok", "instrument": "NIFTY", "dte": "0",
                          "start_date": "2026-08-05", "end_date": "2026-09-07"})
    files = list_result_csvs()
    assert files[0]["date_range_mixed_count"] == 2
    assert "date_range" not in files[0]


def test_list_result_csvs_excludes_derived_backup_files(tmp_path, monkeypatch):
    """A backup file like the DTE-backfill one (results_web_<ts>.pre-dte-backfill-
    backup.csv) must not show up as a selectable result file - it's not a real
    independent sweep run."""
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    _write_csv(tmp_path / "results_web_20260101_000000.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    _write_csv(tmp_path / "results_web_20260101_000000.pre-dte-backfill-backup.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    files = list_result_csvs()
    assert [f["name"] for f in files] == ["results_web_20260101_000000.csv"]


def test_list_result_csvs_includes_a_manually_renamed_or_script_generated_file(tmp_path, monkeypatch):
    """The actual bug this was fixed for: a descriptive suffix after the
    timestamp (e.g. a manually renamed "..._sensex_0dte_cas.csv", or
    scripts/build_cas_subset.py's own "..._nifty_cas_all.csv") used to make a
    file invisible to this exact picker - confirmed live, the user could never
    load or even see their own CAS-window data because of this. Distinguished
    from a genuine backup/derived file (see the test above) by "_" vs "."
    immediately after the timestamp."""
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    _write_csv(tmp_path / "results_web_20260101_000000_sensex_0dte_cas.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "SENSEX", "dte": "0", "entry_time": "09:15"},
    ])
    _write_csv(tmp_path / "results_web_20260102_000000_nifty_cas_all.csv", [
        {"combo_id": "b", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    files = list_result_csvs()
    assert {f["name"] for f in files} == {
        "results_web_20260101_000000_sensex_0dte_cas.csv",
        "results_web_20260102_000000_nifty_cas_all.csv",
    }


def test_list_result_csvs_also_finds_files_archived_by_the_registry_migration(tmp_path, monkeypatch):
    """scripts/migrate_to_registry.py moves results_web_*.csv into output/archive/
    once its rows are folded into combo_registry.csv - the file itself is never
    deleted, and this picker must keep finding it there (confirmed live: archiving
    a real user's 60 files without this made every past sweep invisible on the
    Analyze page)."""
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    _write_csv(tmp_path / "results_web_20260101_000000.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    (tmp_path / "archive").mkdir()
    _write_csv(tmp_path / "archive" / "results_web_20251231_000000.csv", [
        {"combo_id": "b", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    files = list_result_csvs()
    assert {f["name"] for f in files} == {"results_web_20260101_000000.csv", "results_web_20251231_000000.csv"}


def test_list_result_csvs_fine_when_archive_dir_does_not_exist(tmp_path, monkeypatch):
    import src.web.app as app_mod

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    _write_csv(tmp_path / "results_web_20260101_000000.csv", [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"},
    ])
    files = list_result_csvs()
    assert [f["name"] for f in files] == ["results_web_20260101_000000.csv"]


def test_analyze_cas_subset_preview_then_apply(tmp_path, monkeypatch):
    """The Analyze page's own "Preview" then "Create subset" flow - same
    underlying scripts.build_cas_subset.run_cas_subset the CLI uses, just
    reached via the endpoint with dry-run-then-apply, same shape as
    /api/narrow."""
    import csv as csv_mod

    import src.web.app as app_mod
    from src.web.app import CasSubsetRequest, analyze_cas_subset

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app_mod.registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")

    src_csv = tmp_path / "results_web_20260101_000000.csv"
    with src_csv.open("w", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=[
            "combo_id", "status", "instrument", "start_date", "end_date", "strategy_key",
        ])
        writer.writeheader()
        writer.writerow({
            "combo_id": "c1", "status": "ok", "instrument": "NIFTY",
            "start_date": "2025-01-01", "end_date": "2026-09-09", "strategy_key": "k1",
        })

    report_path = tmp_path / "trade_reports" / "nifty" / "c1.csv"
    report_path.parent.mkdir(parents=True)
    with report_path.open("w", newline="") as f:
        writer = csv_mod.writer(f)
        writer.writerow(["Index", "Entry Date", "P/L"])
        writer.writerow(["0", "2026-07-15", "1000"])  # before cutoff, excluded
        writer.writerow(["1", "2026-08-05", "2000"])

    preview = analyze_cas_subset(CasSubsetRequest(cas_start="2026-08-01", apply=False))
    assert preview["applied"] is False
    entry = next(e for e in preview["by_instrument"] if e["instrument"] == "NIFTY")
    assert entry["computed"] == 1
    assert not (tmp_path / entry["output_file"]).exists()  # preview writes nothing

    result = analyze_cas_subset(CasSubsetRequest(cas_start="2026-08-01", apply=True))
    assert result["applied"] is True
    entry = next(e for e in result["by_instrument"] if e["instrument"] == "NIFTY")
    out_path = tmp_path / entry["output_file"]
    assert out_path.exists()
    with out_path.open(newline="") as f:
        rows = list(csv_mod.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["combo_id"] == "c1_cas20260801"
    assert float(rows[0]["total_pnl"]) == 2000.0
