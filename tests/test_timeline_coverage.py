from __future__ import annotations

from src.web.timeline_coverage import backtest_period_coverage, strategy_timing_coverage


def _row(entry_time="", exit_time="", start_date="", end_date="", **metrics):
    row = {"entry_time": entry_time, "exit_time": exit_time, "start_date": start_date, "end_date": end_date}
    row.update(metrics)
    return row


def test_strategy_timing_coverage_groups_by_exact_entry_exit_pair_only():
    """The whole point: two groups whose spans overlap (09:30-12:00 and
    10:00-12:00) must stay entirely separate counts, never merged - this is the
    exact misunderstanding an earlier fixed-slot version had (it blended
    overlapping spans together), which the user explicitly rejected."""
    rows = (
        [_row(entry_time="09:30", exit_time="12:00")] * 10
        + [_row(entry_time="10:00", exit_time="12:00")] * 25
    )
    result = strategy_timing_coverage(rows)
    by_label = {r["label"]: r["count"] for r in result}
    assert by_label["09:30 to 12:00"] == 10
    assert by_label["10:00 to 12:00"] == 25


def test_strategy_timing_coverage_sorted_by_count_descending():
    rows = [_row(entry_time="09:15", exit_time="10:00")] * 3 + [_row(entry_time="09:30", exit_time="11:00")] * 7
    result = strategy_timing_coverage(rows)
    assert [r["label"] for r in result] == ["09:30 to 11:00", "09:15 to 10:00"]
    assert [r["count"] for r in result] == [7, 3]


def test_strategy_timing_coverage_includes_entry_and_exit_time_fields():
    rows = [_row(entry_time="09:30", exit_time="12:00")]
    result = strategy_timing_coverage(rows)
    assert result[0]["entry_time"] == "09:30"
    assert result[0]["exit_time"] == "12:00"


def test_strategy_timing_coverage_skips_rows_with_missing_times():
    rows = [_row(entry_time="", exit_time="10:00"), _row(entry_time="09:20", exit_time="")]
    assert strategy_timing_coverage(rows) == []


def test_strategy_timing_coverage_averages_performance_metrics_per_group():
    rows = [
        _row(entry_time="09:30", exit_time="12:00", return_max_dd="10", reward_risk_ratio="1.5", win_rate="60"),
        _row(entry_time="09:30", exit_time="12:00", return_max_dd="20", reward_risk_ratio="2.5", win_rate="80"),
    ]
    result = strategy_timing_coverage(rows)
    assert result[0]["avg_return_max_dd"] == 15.0
    assert result[0]["avg_reward_risk_ratio"] == 2.0
    assert result[0]["avg_win_rate"] == 70.0


def test_strategy_timing_coverage_metric_is_none_when_no_row_has_a_value():
    rows = [_row(entry_time="09:30", exit_time="12:00")]
    result = strategy_timing_coverage(rows)
    assert result[0]["avg_return_max_dd"] is None


def test_strategy_timing_coverage_ignores_unparseable_metric_values_in_the_average():
    rows = [
        _row(entry_time="09:30", exit_time="12:00", return_max_dd="10"),
        _row(entry_time="09:30", exit_time="12:00", return_max_dd=""),
        _row(entry_time="09:30", exit_time="12:00", return_max_dd="not-a-number"),
    ]
    result = strategy_timing_coverage(rows)
    assert result[0]["avg_return_max_dd"] == 10.0  # only the one valid value counted


def test_strategy_timing_coverage_empty_when_no_rows():
    assert strategy_timing_coverage([]) == []


def test_backtest_period_coverage_counts_a_row_in_every_month_it_spans():
    """A combo backtested 2026-06-15 to 2026-08-10 should count toward June, July,
    AND August - not just the month it starts in."""
    rows = [_row(start_date="2026-06-15", end_date="2026-08-10")]
    result = backtest_period_coverage(rows)
    by_label = {r["label"]: r["count"] for r in result}
    assert by_label["2026-06"] == 1
    assert by_label["2026-07"] == 1
    assert by_label["2026-08"] == 1


def test_backtest_period_coverage_fills_a_gap_with_a_zero_count_month():
    """Two combos with a real gap between them (no combo covers the middle month)
    must still show that middle month as a zero-count bar, not skip it - a
    contiguous range from the earliest to latest date seen, same "show the gap"
    choice session_coverage makes for time-of-day slots."""
    rows = [
        _row(start_date="2026-06-01", end_date="2026-06-30"),
        _row(start_date="2026-08-01", end_date="2026-08-31"),
    ]
    result = backtest_period_coverage(rows)
    by_label = {r["label"]: r["count"] for r in result}
    assert by_label["2026-06"] == 1
    assert by_label["2026-07"] == 0  # the gap - no row covers this month
    assert by_label["2026-08"] == 1


def test_backtest_period_coverage_skips_rows_with_missing_or_backwards_dates():
    rows = [
        _row(start_date="", end_date="2026-08-31"),
        _row(start_date="2026-08-01", end_date=""),
        _row(start_date="2026-08-31", end_date="2026-07-01"),  # end month before start month
    ]
    assert backtest_period_coverage(rows) == []


def test_backtest_period_coverage_same_month_start_and_end_is_not_backwards():
    """start/end land in the SAME month even though the exact days are reversed
    (e.g. a combo replayed start=end-of-month, end=start-of-month) - this is fine
    at month granularity, not a "backwards" row to skip."""
    rows = [_row(start_date="2026-08-31", end_date="2026-08-01")]
    result = backtest_period_coverage(rows)
    assert result == [{"label": "2026-08", "count": 1}]


def test_backtest_period_coverage_empty_when_no_rows():
    assert backtest_period_coverage([]) == []
