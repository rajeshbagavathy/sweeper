from __future__ import annotations

from src.web.heatmap import (
    NO_SL_BAND,
    build_coverage_heatmap,
    sl_band_label,
    stoploss_pct_value,
)


def _row(entry_time, sl_kind, sl_value, **metrics):
    row = {"entry_time": entry_time, "status": "ok"}
    if sl_kind is not None:
        row["leg_risk.stoploss_pct.kind"] = sl_kind
        row["leg_risk.stoploss_pct.value"] = sl_value
        row["leg_risk.stoploss_pct"] = ""
    else:
        row["leg_risk.stoploss_pct.kind"] = ""
        row["leg_risk.stoploss_pct.value"] = ""
        row["leg_risk.stoploss_pct"] = sl_value
    row.update(metrics)
    return row


def test_stoploss_pct_value_reads_dual_basis_percentage():
    row = _row("09:20", "percentage", "25.0")
    assert stoploss_pct_value(row) == 25.0


def test_stoploss_pct_value_excludes_underlying_basis_as_not_comparable():
    row = _row("09:20", "underlying_percentage", "15.0")
    assert stoploss_pct_value(row) is None


def test_stoploss_pct_value_falls_back_to_legacy_flat_column():
    row = _row("09:20", None, "20.0")
    assert stoploss_pct_value(row) == 20.0


def test_stoploss_pct_value_handles_missing_and_garbage():
    assert stoploss_pct_value({}) is None
    assert stoploss_pct_value(_row("09:20", "percentage", "")) is None
    assert stoploss_pct_value(_row("09:20", "percentage", "not-a-number")) is None


def test_sl_band_label_buckets_by_width():
    assert sl_band_label(15.0, 10.0) == "10-20%"
    assert sl_band_label(20.0, 10.0) == "20-30%"
    assert sl_band_label(9.9, 10.0) == "0-10%"


def test_sl_band_label_none_is_na():
    assert sl_band_label(None, 10.0) == NO_SL_BAND


def test_build_coverage_heatmap_counts_and_averages_per_cell():
    rows = [
        _row("09:17", "percentage", "20.0", return_max_dd="2.0"),
        _row("09:20", "percentage", "20.0", return_max_dd="4.0"),
        _row("09:22", "percentage", "45.0", return_max_dd="1.0"),
    ]
    result = build_coverage_heatmap(rows, interval_minutes=15, sl_band_width=10.0)
    assert result["total"] == 3
    # 09:17 and 09:20 both fall in the 09:15 slot, both in the 20-30% band.
    cell = result["cells"]["09:15"]["20-30%"]
    assert cell["count"] == 2
    assert cell["avg_metric"] == 3.0  # (2.0 + 4.0) / 2
    assert round(cell["pct_of_total"], 2) == round(200 / 3, 2)

    other_cell = result["cells"]["09:15"]["40-50%"]
    assert other_cell["count"] == 1
    assert other_cell["avg_metric"] == 1.0


def test_build_coverage_heatmap_rows_with_no_recognizable_sl_go_to_na_band():
    rows = [_row("09:17", None, "", return_max_dd="2.0")]
    result = build_coverage_heatmap(rows, interval_minutes=15, sl_band_width=10.0)
    assert result["cells"]["09:15"][NO_SL_BAND]["count"] == 1


def test_build_coverage_heatmap_rows_with_unparseable_entry_time_are_skipped():
    rows = [_row("not-a-time", "percentage", "20.0", return_max_dd="2.0")]
    result = build_coverage_heatmap(rows, interval_minutes=15, sl_band_width=10.0)
    assert result["total"] == 0
    assert result["cells"] == {}


def test_build_coverage_heatmap_missing_metric_values_excluded_from_average_not_zero():
    rows = [
        _row("09:17", "percentage", "20.0", return_max_dd=""),
        _row("09:18", "percentage", "20.0", return_max_dd="4.0"),
    ]
    result = build_coverage_heatmap(rows, interval_minutes=15, sl_band_width=10.0)
    cell = result["cells"]["09:15"]["20-30%"]
    assert cell["count"] == 2
    assert cell["avg_metric"] == 4.0  # blank row excluded from the average, not treated as 0


def test_build_coverage_heatmap_unknown_metric_falls_back_to_default():
    rows = [_row("09:17", "percentage", "20.0", return_max_dd="2.0")]
    result = build_coverage_heatmap(rows, metric="not_a_real_metric")
    assert result["metric"] == "return_max_dd"


def test_build_coverage_heatmap_empty_rows():
    result = build_coverage_heatmap([])
    assert result["total"] == 0
    assert result["cells"] == {}
    assert result["time_labels"][0] == "09:15"
