from __future__ import annotations

from src.web.models import LegRiskConfig, NumericRange, OverallRiskConfig, SweepUIConfig, TimeRange
from src.web.param_breakdown import available_dimensions, build_param_breakdown, overall_coverage


def _cfg(**overrides) -> SweepUIConfig:
    base = SweepUIConfig(
        entry_time=TimeRange(start="09:15", end="09:15", interval_minutes=15, fixed=True),
        leg_risk=LegRiskConfig(
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=20, max=40, step=10),
        ),
        # Off by default here so "a dimension nobody varies is excluded" (the test
        # below) has a genuine off case - overall_target defaults to a 3-value
        # percentage range on, which would otherwise vary too.
        overall_target=OverallRiskConfig(use_percentage=False, use_amount=False),
    )
    return base.model_copy(update=overrides)


def _ok_row(entry_time, sl_kind, sl_value, return_max_dd):
    row = {"status": "ok", "entry_time": entry_time, "return_max_dd": str(return_max_dd)}
    if sl_kind:
        row["leg_risk.stoploss_pct.kind"] = sl_kind
        row["leg_risk.stoploss_pct.value"] = str(sl_value)
    return row


def test_available_dimensions_only_lists_varying_ones():
    cfg = _cfg()
    dims = {d["key"] for d in available_dimensions(cfg)}
    assert "legrisk_stoploss_pct" in dims
    # entry_time is fixed (single value) in this config - not worth a breakdown.
    assert "entry_time" not in dims
    # overall_target is off by default - always "not set", not a real dimension.
    assert "overall_target" not in dims


def test_build_param_breakdown_counts_and_averages_tried_values():
    cfg = _cfg()
    rows = [
        _ok_row("09:15", "percentage", 20, 2.0),
        _ok_row("09:15", "percentage", 20, 4.0),
        _ok_row("09:15", "percentage", 30, 1.0),
    ]
    result = build_param_breakdown(rows, cfg, "legrisk_stoploss_pct")
    tried = {v["label"]: v for v in result["values"]}
    assert tried["20%"]["count"] == 2
    assert tried["20%"]["avg_metric"] == 3.0
    assert tried["30%"]["count"] == 1
    assert tried["30%"]["avg_metric"] == 1.0


def test_build_param_breakdown_flags_untried_configured_values():
    cfg = _cfg()  # configured: 20%, 30%, 40%, plus "not set" baseline
    rows = [_ok_row("09:15", "percentage", 20, 2.0)]
    result = build_param_breakdown(rows, cfg, "legrisk_stoploss_pct")
    assert result["configured_count"] == 4  # 20, 30, 40, not-set
    assert result["covered_count"] == 1
    untried_labels = {v["label"] for v in result["values"] if v["count"] == 0}
    assert "30%" in untried_labels
    assert "40%" in untried_labels
    for v in result["values"]:
        if v["count"] == 0:
            assert v["avg_metric"] is None


def test_build_param_breakdown_keeps_historical_values_outside_current_config():
    cfg = _cfg()  # configured range is 20-40
    rows = [_ok_row("09:15", "percentage", 95, 5.0)]  # e.g. an older, wider sweep
    result = build_param_breakdown(rows, cfg, "legrisk_stoploss_pct")
    hist = next(v for v in result["values"] if v["label"] == "95%")
    assert hist["count"] == 1
    assert hist["in_current_config"] is False
    # Doesn't count toward "configured" coverage, and isn't silently dropped.
    assert result["configured_count"] == 4


def test_build_param_breakdown_underlying_and_percentage_bases_stay_distinct():
    cfg = _cfg(leg_risk=LegRiskConfig(
        stoploss_enabled=True,
        stoploss_pct=NumericRange(min=20, max=20, step=10),
        stoploss_underlying_enabled=True,
        stoploss_underlying_pct=NumericRange(min=0.15, max=0.15, step=0.1),
    ))
    rows = [
        _ok_row("09:15", "percentage", 20, 1.0),
        _ok_row("09:15", "underlying_percentage", 0.15, 9.0),
    ]
    result = build_param_breakdown(rows, cfg, "legrisk_stoploss_pct")
    labels = {v["label"]: v for v in result["values"]}
    assert labels["20%"]["avg_metric"] == 1.0
    assert labels["0.15% (Underlying)"]["avg_metric"] == 9.0


def test_build_param_breakdown_missing_metric_excluded_from_average():
    cfg = _cfg()
    rows = [
        _ok_row("09:15", "percentage", 20, 2.0),
        {"status": "ok", "entry_time": "09:15", "leg_risk.stoploss_pct.kind": "percentage",
         "leg_risk.stoploss_pct.value": "20", "return_max_dd": ""},
    ]
    result = build_param_breakdown(rows, cfg, "legrisk_stoploss_pct")
    cell = next(v for v in result["values"] if v["label"] == "20%")
    assert cell["count"] == 2
    assert cell["avg_metric"] == 2.0


def test_build_param_breakdown_unknown_dimension_returns_empty():
    cfg = _cfg()
    result = build_param_breakdown([], cfg, "not_a_real_dimension")
    assert result["values"] == []
    assert result["total"] == 0


def test_overall_coverage_matches_full_configured_grid_ignoring_limit():
    cfg = _cfg(limit=1)  # limit caps execution, not the space itself
    coverage = overall_coverage(cfg, executed_count=1)
    assert coverage["total_configured"] > 1
    assert coverage["executed"] == 1
    assert 0 < coverage["pct"] < 100


def test_overall_coverage_zero_executed():
    cfg = _cfg()
    coverage = overall_coverage(cfg, executed_count=0)
    assert coverage["executed"] == 0
    assert coverage["pct"] == 0.0
