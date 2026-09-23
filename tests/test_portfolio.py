from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.web.portfolio import (
    DEFAULT_BUDGETS,
    VERY_LONG_MORNING_BUCKET_ORDER,
    _recompute_stats_for_window,
    _stale_picks,
    build_portfolio,
    charges_from_row,
    classify_bucket,
    classify_very_long_morning_bucket,
    data_coverage_gaps,
    has_hard_stop_loss,
)


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (Index, Entry Date, P/L) - mirrors tests/test_correlate.py's helper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
        lines.append(f'"{index}.1","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","CE","100","Sell","1","10","10","","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


def _row(combo_id, entry_time, exit_time, *, rmdd="2.0", rrr="1.0", pnl="1000", maxdd="-500", has_sl=True):
    """has_sl=True (the default) gives the row a plain overall Stop Loss, same as
    any normal combo - so existing tests that don't care about has_hard_stop_loss
    aren't tripped up by the "no SL at all" exclusion. Pass has_sl=False to build a
    trail-only/no-SL row for testing that exclusion specifically."""
    row = {
        "combo_id": combo_id, "entry_time": entry_time, "exit_time": exit_time,
        "return_max_dd": rmdd, "reward_risk_ratio": rrr, "total_pnl": pnl, "max_drawdown": maxdd,
    }
    if has_sl:
        row["stoploss.kind"] = "amount"
        row["stoploss.value"] = "7000"
    return row


def test_classify_bucket_short_morning():
    assert classify_bucket(_row("a", "09:17", "11:20")) == "short_morning"


def test_classify_bucket_long_morning():
    assert classify_bucket(_row("a", "09:16", "13:15")) == "long_morning"


def test_classify_bucket_midday():
    assert classify_bucket(_row("a", "11:41", "13:40")) == "midday"


def test_classify_bucket_afternoon():
    assert classify_bucket(_row("a", "14:10", "15:25")) == "afternoon"


def test_classify_bucket_ambiguous_gap_is_none():
    # entry before 11:00 but exit lands in the dead zone between the two morning
    # buckets - neither a short quick exit nor a held-to-early-afternoon exit.
    assert classify_bucket(_row("a", "09:20", "12:00")) is None


# --- "Very long morning" - the opt-in 2-bucket scheme covering exactly the gap
# classify_bucket's own regular 4-way split leaves uncovered: an 11:00am-11:59am
# entry held into the afternoon lands in neither long_morning (entry must be
# before 11:00) nor midday (exit must be <= 14:15) - see
# classify_very_long_morning_bucket's own docstring for the real-data numbers
# that surfaced this. ---


def test_classify_very_long_morning_covers_entry_before_11am():
    # The clearest case classify_bucket's long_morning already handles too -
    # confirms the new bucket doesn't regress it.
    assert classify_very_long_morning_bucket(_row("a", "09:17", "14:30")) == "very_long_morning"


def test_classify_very_long_morning_covers_the_11am_to_noon_gap():
    # Exactly the combination classify_bucket has no bucket for at all (entry
    # in 11:00-11:59am, exit past 14:15) - the whole point of this scheme.
    assert classify_very_long_morning_bucket(_row("a", "11:30", "14:45")) == "very_long_morning"
    assert classify_bucket(_row("a", "11:30", "14:45")) is None


def test_classify_very_long_morning_covers_11am_entry_with_an_early_exit_too():
    # Same gap, the other sub-case: classify_bucket puts this in "midday"
    # (exit <= 14:15) instead of long_morning - still covered here as one
    # combined session regardless.
    assert classify_very_long_morning_bucket(_row("a", "11:30", "14:10")) == "very_long_morning"
    assert classify_bucket(_row("a", "11:30", "14:10")) == "midday"


def test_classify_very_long_morning_excludes_entry_before_2pm_exit():
    # Entry before noon but NOT held into the afternoon - not what this bucket
    # is for, same "doesn't cleanly fit" convention as classify_bucket's None.
    assert classify_very_long_morning_bucket(_row("a", "09:20", "11:00")) is None


def test_classify_very_long_morning_excludes_the_noon_to_1pm_slot():
    assert classify_very_long_morning_bucket(_row("a", "12:30", "14:30")) is None


def test_classify_very_long_morning_afternoon_matches_regular_scheme():
    # Same cutoff (entry >= 13:00) as classify_bucket's own afternoon - this
    # bucket behaves identically in either scheme.
    assert classify_very_long_morning_bucket(_row("a", "14:10", "15:25")) == "afternoon"
    assert classify_bucket(_row("a", "14:10", "15:25")) == "afternoon"


def test_very_long_morning_bucket_order_is_just_the_two_buckets():
    assert VERY_LONG_MORNING_BUCKET_ORDER == ["very_long_morning", "afternoon"]


def test_has_hard_stop_loss_true_for_leg_level_percentage():
    assert has_hard_stop_loss({"leg_risk.stoploss_pct.kind": "percentage", "leg_risk.stoploss_pct.value": "25"})


def test_has_hard_stop_loss_true_for_leg_level_underlying_percentage_within_sane_range():
    assert has_hard_stop_loss({"leg_risk.stoploss_pct.kind": "underlying_percentage", "leg_risk.stoploss_pct.value": "0.2"})
    assert has_hard_stop_loss({"leg_risk.stoploss_pct.kind": "underlying_percentage", "leg_risk.stoploss_pct.value": "1"})


def test_has_hard_stop_loss_false_for_leg_level_underlying_percentage_above_sane_range():
    # Confirmed live: a batch of sweeps meant to use 0.14%-0.25% underlying moves
    # were mistakenly entered as whole percent (14-25) - a move that large in the
    # UNDERLYING practically never happens intraday, so the SL never actually trips
    # and the combo isn't really protected, even though the column looks "set".
    assert not has_hard_stop_loss({"leg_risk.stoploss_pct.kind": "underlying_percentage", "leg_risk.stoploss_pct.value": "14"})
    assert not has_hard_stop_loss({"leg_risk.stoploss_pct.kind": "underlying_percentage", "leg_risk.stoploss_pct.value": "25"})


def test_has_hard_stop_loss_falls_back_to_overall_sl_when_underlying_pct_is_insane():
    assert has_hard_stop_loss({
        "leg_risk.stoploss_pct.kind": "underlying_percentage", "leg_risk.stoploss_pct.value": "20",
        "stoploss.kind": "amount", "stoploss.value": "7000",
    })


def test_has_hard_stop_loss_true_for_legacy_flat_leg_stoploss_column():
    # Rows from before the dual-basis leg SL feature stored this as one flat column.
    assert has_hard_stop_loss({"leg_risk.stoploss_pct": "15.0"})


def test_has_hard_stop_loss_true_for_overall_stop_loss():
    assert has_hard_stop_loss({"stoploss.kind": "amount", "stoploss.value": "7000"})
    assert has_hard_stop_loss({"stoploss.kind": "percentage", "stoploss.value": "20"})


def test_has_hard_stop_loss_false_when_only_trail_sl_or_nothing_is_set():
    # This is exactly c743d82c58ff: trail_sl only, no leg or overall Stop Loss -
    # nothing caps the loss before the trail has locked anything in.
    assert not has_hard_stop_loss({
        "leg_risk.stoploss_pct.kind": "", "leg_risk.stoploss_pct.value": "",
        "stoploss.kind": "", "stoploss.value": "",
        "trail_sl.x": "5000.0", "trail_sl.y": "500.0", "trail_sl.step": "4000.0", "trail_sl.trail_by": "2000.0",
    })
    assert not has_hard_stop_loss({})


def test_charges_from_row_reads_both_fields():
    row = {"brokerage_amount": "4814.4", "taxes_charges_amount": "3573.68"}
    assert charges_from_row(row) == {"brokerage": 4814.4, "taxes_charges": 3573.68}


def test_charges_from_row_missing_or_blank_is_none():
    assert charges_from_row({}) == {"brokerage": None, "taxes_charges": None}
    assert charges_from_row({"brokerage_amount": "", "taxes_charges_amount": ""}) == {
        "brokerage": None, "taxes_charges": None,
    }


def test_classify_bucket_missing_times_is_none():
    assert classify_bucket({"combo_id": "a", "entry_time": "", "exit_time": ""}) is None


def test_data_coverage_gaps_none_when_every_report_covers_the_same_expiries(tmp_path):
    candidates = [_row("a", "09:17", "11:20"), _row("b", "09:20", "11:30")]
    same_dates = [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)]
    for r in candidates:
        _write_report(tmp_path / "nifty" / f"{r['combo_id']}.csv", same_dates)

    assert data_coverage_gaps(candidates, tmp_path, "NIFTY") == []


def test_data_coverage_gaps_flags_a_report_missing_the_most_recent_expiry(tmp_path):
    candidates = [_row("full", "09:17", "11:20"), _row("short", "09:20", "11:30")]
    full_dates = [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)]  # through 09-06
    short_dates = full_dates[:4]  # through 09-04 only - missing the last 2 expiries
    _write_report(tmp_path / "nifty" / "full.csv", full_dates)
    _write_report(tmp_path / "nifty" / "short.csv", short_dates)

    gaps = data_coverage_gaps(candidates, tmp_path, "NIFTY")

    assert len(gaps) == 1
    assert gaps[0]["combo_id"] == "short"
    assert gaps[0]["last_date"] == "2025-09-04"
    assert gaps[0]["expected_last_date"] == "2025-09-06"
    assert gaps[0]["missing_count"] == 2


def test_data_coverage_gaps_ignores_candidates_with_no_report_at_all(tmp_path):
    """A combo with no downloaded report yet isn't "missing data" in this sense -
    it's simply not in the comparable pool at all (excluded earlier, same as
    Uncorrelated strategies' own "missing" list) - this check is specifically about
    reports that exist but fall short, not about what hasn't been downloaded yet."""
    candidates = [_row("has_report", "09:17", "11:20"), _row("no_report", "09:20", "11:30")]
    _write_report(tmp_path / "nifty" / "has_report.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])

    assert data_coverage_gaps(candidates, tmp_path, "NIFTY") == []


def test_data_coverage_gaps_sorted_worst_first(tmp_path):
    candidates = [_row("full", "09:17", "11:20"), _row("a_bit_short", "09:20", "11:30"), _row("very_short", "09:29", "13:15")]
    full_dates = [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)]
    _write_report(tmp_path / "nifty" / "full.csv", full_dates)
    _write_report(tmp_path / "nifty" / "a_bit_short.csv", full_dates[:5])   # missing 1
    _write_report(tmp_path / "nifty" / "very_short.csv", full_dates[:2])    # missing 4

    gaps = data_coverage_gaps(candidates, tmp_path, "NIFTY")

    assert [g["combo_id"] for g in gaps] == ["very_short", "a_bit_short"]
    assert [g["missing_count"] for g in gaps] == [4, 1]


def _write_distinct_report(base_dir: Path, combo_id: str, offset_months: int) -> None:
    """A report on its own non-overlapping date range - guarantees "insufficient
    overlap" (None) correlation against every other _write_distinct_report combo in
    the same test, which never blocks inclusion, so tests can focus on budget/timing
    logic without fighting the (now global) correlation check."""
    _write_report(
        base_dir / "nifty" / f"{combo_id}.csv",
        [(str(n), f"2025-{offset_months:02d}-{n+1:02d}", "100") for n in range(6)],
    )


def test_build_portfolio_same_window_dedupes_when_correlation_is_unknown(tmp_path):
    """Confirmed live: a short backtest window (few trading days) leaves
    correlation "unknown" (None) for almost every pair, since real correlation
    needs at least MIN_OVERLAP_DAYS overlapping dates - and "unknown" alone
    never blocks inclusion, so the SAME entry/exit clock-time window kept
    getting picked repeatedly into one bucket even though the picks weren't
    provably correlated. Two afternoon combos sharing the exact same (entry,
    exit) but with too few overlapping days for a real coefficient - only the
    better-ranked one should survive into the basket now."""
    rows = [
        _row("af1", "14:55", "15:38", rmdd="50", pnl="27868", maxdd="-93"),
        _row("af2", "14:55", "15:38", rmdd="40", pnl="23220", maxdd="-93"),
    ]
    # Only 4 overlapping days - one short of MIN_OVERLAP_DAYS (5) - with
    # genuinely different daily P&L (not byte-identical), so this exercises
    # the same_window tie-break specifically, not the exact-duplicate check.
    _write_report(tmp_path / "sensex" / "af1.csv", [
        ("0", "2026-08-26", "7969"), ("1", "2026-09-02", "4932"),
        ("2", "2026-09-09", "11368"), ("3", "2026-09-16", "234"),
    ])
    _write_report(tmp_path / "sensex" / "af2.csv", [
        ("0", "2026-08-26", "8171"), ("1", "2026-09-02", "4932"),
        ("2", "2026-09-09", "11166"), ("3", "2026-09-16", "234"),
    ])

    result = build_portfolio(rows, tmp_path, "SENSEX", threshold=0.5, top_n=5, max_share=1.0)

    members = result["buckets"]["afternoon"]["members"]
    assert [m["combo_id"] for m in members] == ["af1"]  # af2 correctly excluded


def test_build_portfolio_sizes_each_bucket_to_its_exact_budget(tmp_path):
    rows = [
        _row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-1000"),
        _row("lm1", "09:16", "13:15", rmdd="10", pnl="2000", maxdd="-1000"),
        _row("md1", "11:30", "13:40", rmdd="10", pnl="2000", maxdd="-1000"),
        _row("af1", "14:10", "15:25", rmdd="10", pnl="2000", maxdd="-1000"),
    ]
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    # max_share=1.0 - this test is about budget bookkeeping across buckets, not
    # capping (each bucket has exactly one pick here; see the dedicated cap test
    # below for what happens to a lone pick under the default cap).
    result = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0)

    for name, budget in DEFAULT_BUDGETS.items():
        members = result["buckets"][name]["members"]
        assert sum(m["lots"] for m in members) == budget, f"{name} lots didn't sum to its budget"
    assert result["total_lots"] == sum(DEFAULT_BUDGETS.values())


def test_build_portfolio_a_lone_pick_does_not_absorb_the_whole_budget_by_default(tmp_path):
    """Regression test for the exact complaint this fixed: a bucket with only one
    diversified pick used to hand it the ENTIRE budget (e.g. all 18 short-morning
    lots into a single strategy) even under the default cap - the cap must apply to
    a lone pick too, with the rest reported as unallocated rather than force-fit."""
    rows = [_row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-1000")]
    _write_distinct_report(tmp_path, "sm1", offset_months=1)

    result = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5)

    bucket = result["buckets"]["short_morning"]
    assert bucket["members"][0]["lots"] < DEFAULT_BUDGETS["short_morning"]
    assert bucket["unallocated_lots"] > 0


def test_build_portfolio_deducts_charges_when_row_has_them(tmp_path):
    """End-to-end: a row carrying brokerage_amount/taxes_charges_amount must have
    them actually reflected in the combined portfolio's overall profit, not just
    parsed and discarded."""
    row = _row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-1000")
    row["brokerage_amount"] = "1000"
    row["taxes_charges_amount"] = "200"
    _write_distinct_report(tmp_path, "sm1", offset_months=1)

    with_charges = build_portfolio([row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0)
    without_row = dict(row)
    del without_row["brokerage_amount"], without_row["taxes_charges_amount"]
    without_charges = build_portfolio([without_row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0)

    assert with_charges["portfolio"]["overall_profit"] < without_charges["portfolio"]["overall_profit"]


def test_build_portfolio_midday_excludes_entries_before_short_morning_exit(tmp_path):
    """A midday candidate entering before the chosen short_morning pick's own exit
    can't actually use that freed margin yet - it must be excluded, not just ranked
    lower, since that lot budget genuinely isn't free until then."""
    rows = [
        _row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-1000"),
        # Ranked artificially better (higher rmdd) but enters before 11:20 - should
        # be dropped from the midday candidate pool entirely.
        _row("md_early", "11:06", "13:05", rmdd="50", pnl="5000", maxdd="-100"),
        _row("md_ok", "11:41", "13:40", rmdd="5", pnl="1000", maxdd="-500"),
    ]
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    result = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5)

    midday_ids = [m["combo_id"] for m in result["buckets"]["midday"]["members"]]
    assert "md_early" not in midday_ids
    assert "md_ok" in midday_ids


def test_build_portfolio_diversifies_globally_across_buckets_not_just_within_one():
    """The core fix this test locks in: two picks in DIFFERENT buckets that are
    highly correlated with each other must not both make it into the basket - the
    old per-bucket-siloed version would have let this through since it never
    compared a short_morning candidate against a midday one."""
    import src.web.portfolio as portfolio_mod

    def fake_shortlist(candidates, reports_dir, instrument, top_n):
        # No real report files in this test - bypass the on-disk existence filter
        # (that's covered by the other tests) and just pass every candidate through.
        return candidates

    def fake_diversify(shortlisted, reports_dir, instrument, *, threshold, series_cache=None):
        # sm1 (short_morning) ranks first; md1 (midday) is a lower-ranked pick that's
        # highly correlated (0.9) with sm1 - a real diversification pass must reject
        # it. lm1/af1 are uncorrelated with everything (None) and pass through.
        row_by_id = {r["combo_id"]: r for r in shortlisted}
        matrix = {
            "sm1": {"sm1": 1.0, "md1": 0.9, "lm1": None, "af1": None},
            "md1": {"sm1": 0.9, "md1": 1.0, "lm1": None, "af1": None},
            "lm1": {"sm1": None, "md1": None, "lm1": 1.0, "af1": None},
            "af1": {"sm1": None, "md1": None, "lm1": None, "af1": 1.0},
        }
        from src.correlate import pick_diversified_basket

        ranked_ids = ["sm1", "md1", "lm1", "af1"]
        basket, _skipped = pick_diversified_basket(ranked_ids, matrix, threshold=threshold)
        series = {cid: {} for cid in row_by_id}
        return basket, row_by_id, series

    orig_shortlist, orig_diversify = portfolio_mod._shortlist, portfolio_mod._diversify_merged_pool
    portfolio_mod._shortlist = fake_shortlist
    portfolio_mod._diversify_merged_pool = fake_diversify
    try:
        rows = [
            _row("sm1", "09:17", "11:20"),
            _row("md1", "11:41", "13:40"),
            _row("lm1", "09:16", "13:15"),
            _row("af1", "14:10", "15:25"),
        ]
        result = build_portfolio(rows, Path("/nonexistent"), "NIFTY", threshold=0.5)
    finally:
        portfolio_mod._shortlist = orig_shortlist
        portfolio_mod._diversify_merged_pool = orig_diversify

    midday_ids = [m["combo_id"] for m in result["buckets"]["midday"]["members"]]
    assert midday_ids == [], "md1 was correlated 0.9 with sm1 and should have been rejected globally"
    short_morning_ids = [m["combo_id"] for m in result["buckets"]["short_morning"]["members"]]
    assert short_morning_ids == ["sm1"]


def test_build_portfolio_with_custom_bucket_order_diversifies_per_custom_bucket(tmp_path):
    """CAS regime analysis's whole reason for existing: every row here has an
    entry_time in the same narrow afternoon window, so with the regular buckets
    they'd ALL classify as "afternoon" and get ranked/deduped as one flat pool.
    Passing a custom bucket_order/classify_fn (e.g. src/web/regime_state.py's
    cas_slot_buckets) must give each of the two synthetic "slices" here its own
    fair shortlist and its own entry in the returned buckets dict, keyed by the
    custom names - not the regular short_morning/long_morning/midday/afternoon."""
    rows = [
        _row("slice_a_1", "15:15", "15:29", rmdd="10", pnl="2000", maxdd="-1000"),
        _row("slice_b_1", "15:20", "15:29", rmdd="10", pnl="2000", maxdd="-1000"),
    ]
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    def classify(row):
        return "slice_a" if row["entry_time"] == "15:15" else "slice_b" if row["entry_time"] == "15:20" else None

    result = build_portfolio(
        rows, tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0,
        bucket_order=["slice_a", "slice_b"], budgets={"slice_a": 10.0, "slice_b": 20.0},
        classify_fn=classify,
    )

    assert set(result["buckets"]) == {"slice_a", "slice_b"}
    assert [m["combo_id"] for m in result["buckets"]["slice_a"]["members"]] == ["slice_a_1"]
    assert [m["combo_id"] for m in result["buckets"]["slice_b"]["members"]] == ["slice_b_1"]
    assert result["buckets"]["slice_a"]["budget"] == 10.0
    assert result["buckets"]["slice_b"]["budget"] == 20.0
    # The regular short_morning/midday margin-reuse timing filter is specific to
    # those bucket names and must simply not apply here (no KeyError, no dropped
    # picks) - confirmed by both slices' single member surviving into the basket.
    assert result["dropped_for_timing"] == 0


def test_build_portfolio_very_long_morning_mode_end_to_end(tmp_path):
    """The actual feature, not just the classify function in isolation - an
    11am entry held into the afternoon (no bucket in the regular scheme) lands
    in "very_long_morning" here, and the regular short_morning/long_morning/
    midday margin-reuse timing filter (specific to those bucket names) simply
    doesn't apply, same as any other custom bucket_order."""
    rows = [
        _row("a", "11:30", "14:45", rmdd="10", pnl="2000", maxdd="-1000"),
        _row("b", "14:10", "15:25", rmdd="10", pnl="2000", maxdd="-1000"),
    ]
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    result = build_portfolio(
        rows, tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0,
        bucket_order=VERY_LONG_MORNING_BUCKET_ORDER,
        budgets={"very_long_morning": 27.0, "afternoon": 45.0},
        classify_fn=classify_very_long_morning_bucket,
    )

    assert set(result["buckets"]) == {"very_long_morning", "afternoon"}
    assert [m["combo_id"] for m in result["buckets"]["very_long_morning"]["members"]] == ["a"]
    assert [m["combo_id"] for m in result["buckets"]["afternoon"]["members"]] == ["b"]
    assert result["buckets"]["very_long_morning"]["budget"] == 27.0
    assert result["dropped_for_timing"] == 0


def _cas_style_rows(n: int) -> list[dict]:
    """n uncorrelated single-pick "slices" (equal max_drawdown, so _size_lots'
    inverse-drawdown weighting splits any shared budget between them equally) -
    the shape CAS regime analysis's own cas_slot_buckets produces."""
    return [
        _row(f"slice_{i}", f"15:{10 + i}", "15:29", rmdd="10", pnl="2000", maxdd="-1000")
        for i in range(n)
    ]


def test_build_portfolio_overall_budget_pools_all_slices_into_one_shared_cap(tmp_path):
    """The exact scenario CAS "Overall lots" mode exists for: 4 time-slices, one
    pick each, overall_budget=20 with min/max lots per pick 3/5 - the shared
    budget divides evenly to 5 each (the max), fully deployed, nothing left over."""
    rows = _cas_style_rows(4)
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    def classify(row):
        return row["entry_time"]

    bucket_order = [r["entry_time"] for r in rows]
    result = build_portfolio(
        rows, tmp_path, "NIFTY", threshold=0.5, top_n=5,
        bucket_order=bucket_order, classify_fn=classify,
        overall_budget=20.0, min_lots=3.0, max_lots=5.0,
    )

    all_members = [m for b in result["buckets"].values() for m in b["members"]]
    assert len(all_members) == 4
    assert all(m["lots"] == 5 for m in all_members)
    assert result["total_lots"] == 20.0
    assert result["overall_budget"] == 20.0
    assert result["overall_unallocated_lots"] == 0.0
    # Per-bucket budget/unallocated/dropped are meaningless in pooled mode - the
    # UI must read the top-level overall_* fields instead.
    assert all(b["budget"] is None for b in result["buckets"].values())


def test_build_portfolio_overall_budget_still_caps_at_max_lots_when_budget_is_bigger(tmp_path):
    """The other half of the same example: raising overall_budget to 30 doesn't
    let any pick exceed max_lots (5) - still only 20 gets deployed, 10 reported
    as unallocated rather than silently forced past the cap."""
    rows = _cas_style_rows(4)
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    def classify(row):
        return row["entry_time"]

    bucket_order = [r["entry_time"] for r in rows]
    result = build_portfolio(
        rows, tmp_path, "NIFTY", threshold=0.5, top_n=5,
        bucket_order=bucket_order, classify_fn=classify,
        overall_budget=30.0, min_lots=3.0, max_lots=5.0,
    )

    all_members = [m for b in result["buckets"].values() for m in b["members"]]
    assert all(m["lots"] == 5 for m in all_members)
    assert sum(m["lots"] for m in all_members) == 20.0
    assert result["overall_unallocated_lots"] == 10.0
    assert result["total_lots"] == 30.0  # the configured cap, not what was actually deployed


def test_build_portfolio_overall_budget_bounds_each_pick_separately_within_a_slice(tmp_path):
    """A slice with more than one diversified pick: confirmed design decision -
    each pick draws its own independent min/max-bounded share from the shared
    pool, rather than the whole slice being capped as one unit."""
    rows = [
        # slice_a_1/slice_a_2 deliberately differ in EXIT time (classify() below
        # only keys off entry_time, so both still land in "slice_a") - same
        # entry+exit would now trip the same_window dedup tie-break (see
        # src/correlate.py's pick_diversified_basket), which isn't what this
        # test is about; it's testing budget-splitting across multiple GENUINE
        # picks in one slice, not deduplication.
        _row("slice_a_1", "15:14", "15:29", rmdd="10", pnl="2000", maxdd="-1000"),
        _row("slice_a_2", "15:14", "15:32", rmdd="9", pnl="1900", maxdd="-1000"),
        _row("slice_b_1", "15:20", "15:29", rmdd="10", pnl="2000", maxdd="-1000"),
    ]
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    def classify(row):
        return "slice_a" if row["entry_time"] == "15:14" else "slice_b"

    result = build_portfolio(
        rows, tmp_path, "NIFTY", threshold=0.5, top_n=5,
        bucket_order=["slice_a", "slice_b"], classify_fn=classify,
        overall_budget=30.0, min_lots=3.0, max_lots=5.0,
    )

    slice_a_members = result["buckets"]["slice_a"]["members"]
    slice_b_members = result["buckets"]["slice_b"]["members"]
    assert len(slice_a_members) == 2
    assert len(slice_b_members) == 1
    # Every pick individually bounded by min/max regardless of which slice it's
    # in - slice_a's total (2 picks) can exceed slice_b's single pick's max.
    assert all(3.0 <= m["lots"] <= 5.0 for m in slice_a_members + slice_b_members)


def test_build_portfolio_default_unaffected_by_overall_budget_param(tmp_path):
    """overall_budget is optional and defaults to None - a caller that never
    passes it (every existing caller) gets identical results to before this
    param existed."""
    rows = [_row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-1000")]
    _write_distinct_report(tmp_path, "sm1", offset_months=1)

    with_default = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5)
    explicit_none = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5, overall_budget=None)

    # computed_at is a real wall-clock timestamp - legitimately differs between
    # two separate calls a moment apart, not a regression to compare against.
    with_default.pop("computed_at"), explicit_none.pop("computed_at")
    assert with_default == explicit_none
    assert with_default["overall_budget"] is None
    assert all(b["budget"] is not None for b in with_default["buckets"].values())


def test_build_portfolio_default_bucket_order_unaffected_by_new_params(tmp_path):
    """bucket_order/classify_fn are optional and default to the regular 4
    day-session buckets - a caller that never passes them (every existing caller)
    gets identical results to before either param existed."""
    rows = [_row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-1000")]
    _write_distinct_report(tmp_path, "sm1", offset_months=1)

    with_defaults = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5)
    explicit_none = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5, bucket_order=None, classify_fn=None)

    with_defaults.pop("computed_at"), explicit_none.pop("computed_at")
    assert with_defaults == explicit_none
    assert set(with_defaults["buckets"]) == set(DEFAULT_BUDGETS)


def test_build_portfolio_lot_sizing_favors_smaller_drawdown():
    """Inverse-|max_drawdown| weighting: the smaller-drawdown pick should get more
    lots than the bigger-drawdown one out of the same budget."""
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": "big_loss"}, {"combo_id": "small_loss"}]
    row_by_id = {
        "big_loss": {"max_drawdown": "-10000"},
        "small_loss": {"max_drawdown": "-1000"},
    }
    # max_share=1.0 (no cap) isolates the weighting itself - capping behavior with
    # only 2 picks has its own dedicated tests below.
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 20, max_share=1.0)
    assert sum(lots) == 20
    assert unallocated == 0
    assert lots[1] > lots[0]  # small_loss (index 1) sized bigger than big_loss (index 0)


def test_size_lots_caps_a_dominant_pick_so_it_cant_absorb_the_whole_budget():
    """Regression test: one strategy with a tiny drawdown relative to the rest used
    to be able to absorb almost the entire bucket - a single-strategy concentration
    the diversification step was supposed to prevent, not just relabel."""
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": c} for c in ("tiny_dd", "b", "c", "d", "e")]
    row_by_id = {
        "tiny_dd": {"max_drawdown": "-10"},  # would dominate every other pick's weight
        "b": {"max_drawdown": "-5000"},
        "c": {"max_drawdown": "-5000"},
        "d": {"max_drawdown": "-5000"},
        "e": {"max_drawdown": "-5000"},
    }
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 18, max_share=0.4, min_lots=2)
    assert sum(lots) == 18
    assert unallocated == 0
    assert dropped == 0
    assert lots[0] <= round(0.4 * 18) + 1  # tiny_dd capped near max_share, not left to dominate
    assert all(l >= 2 for l in lots[1:])  # nobody sized down to a token 1-lot amount


def test_size_lots_floors_a_negligible_pick_up_to_a_real_position():
    """A pick with a much larger drawdown than its basket-mates used to be sized down
    to 1 lot - technically "diversified" but not a real position worth deploying."""
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": c} for c in ("a", "b", "c", "big_dd")]
    row_by_id = {
        "a": {"max_drawdown": "-500"},
        "b": {"max_drawdown": "-500"},
        "c": {"max_drawdown": "-500"},
        "big_dd": {"max_drawdown": "-50000"},  # would round to 0-1 lots unfloored
    }
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 18, max_share=0.4, min_lots=2)
    assert sum(lots) == 18
    assert unallocated == 0
    assert dropped == 0
    assert lots[3] >= 2


def test_size_lots_absolute_max_lots_caps_tighter_than_percentage_share():
    """max_lots=7 must win out over a looser 40%-of-budget share cap (40% of 45 is
    18 - exactly the "still assigning maximum lots to 18" complaint this fixes)."""
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": c} for c in ("dominant", "b", "c", "d", "e")]
    row_by_id = {
        "dominant": {"max_drawdown": "-10"},
        "b": {"max_drawdown": "-5000"},
        "c": {"max_drawdown": "-5000"},
        "d": {"max_drawdown": "-5000"},
        "e": {"max_drawdown": "-5000"},
    }
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 45, max_share=0.4, min_lots=2, max_lots=7)
    assert max(lots) <= 7
    assert sum(lots) + unallocated == 45


def test_size_lots_reports_unallocated_when_cap_makes_full_budget_infeasible():
    """5 picks capped at 7 lots each can reach at most 35 lots - a 45-lot budget's
    other 10 lots have nowhere to go without breaching the cap, so they must come
    back as unallocated rather than silently overflow it."""
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": c} for c in ("a", "b", "c", "d", "e")]
    row_by_id = {b["combo_id"]: {"max_drawdown": "-1000"} for b in basket}
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 45, max_share=1.0, min_lots=2, max_lots=7)
    assert all(l <= 7 for l in lots)
    assert unallocated == pytest.approx(10, abs=1)


def test_size_lots_drops_weakest_picks_rather_than_sizing_everyone_below_min_lots():
    """The bug this fixes: 5 picks sharing an 18-lot budget with min_lots=4 (18/5=3.6
    each) used to silently shrink the floor to 3.6, so a rounded-down pick could land
    at 3 - below the min_lots the user actually configured, with no signal it had
    happened. Now the weakest pick(s) (basket is best-first) are dropped instead, so
    everyone who survives gets a genuine >=4."""
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": c} for c in ("a", "b", "c", "d", "e")]
    row_by_id = {b["combo_id"]: {"max_drawdown": "-1000"} for b in basket}  # equal weights
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 18, max_share=1.0, min_lots=4)

    assert all(l >= 4 for l in lots)  # the real bug: this used to fail (a 3 could sneak in)
    assert dropped == 1  # 18 // 4 == 4 picks fit; the 5th (weakest-ranked) is cut
    assert len(lots) == 4


def test_size_lots_dropped_for_min_lots_zero_when_budget_is_plenty():
    from src.web.portfolio import _size_lots

    basket = [{"combo_id": c} for c in ("a", "b")]
    row_by_id = {b["combo_id"]: {"max_drawdown": "-1000"} for b in basket}
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 20, max_share=1.0, min_lots=2)
    assert dropped == 0
    assert len(lots) == 2


def test_size_lots_zero_budget_returns_no_lots_not_forced_to_one_each(tmp_path):
    """Regression test for the exact complaint this fixed: a bucket deliberately
    zeroed out (e.g. Short-morning lots set to 0 to hand that session's whole
    margin to another bucket) still showed lots assigned there - the general
    water-fill logic unconditionally floors every surviving pick to at least 1
    lot (see the `max(1, ...)` calls below in the real function), with no check
    for whether there was ever any real budget to floor UP from in the first
    place. A single pick and a multi-pick basket both covered, since the n==1
    branch has its own separate forced-floor line."""
    from src.web.portfolio import _size_lots

    row_by_id = {"only1": {"max_drawdown": "-500"}}
    lots, unallocated, dropped = _size_lots([{"combo_id": "only1"}], row_by_id, 0, max_share=0.4, min_lots=2)
    assert lots == []
    assert unallocated == 0
    assert dropped == 0

    basket = [{"combo_id": c} for c in ("a", "b", "c")]
    row_by_id = {b["combo_id"]: {"max_drawdown": "-1000"} for b in basket}
    lots, unallocated, dropped = _size_lots(basket, row_by_id, 0, max_share=0.4, min_lots=2)
    assert lots == []
    assert unallocated == 0
    assert dropped == 0


def test_build_portfolio_zero_budget_bucket_has_no_members(tmp_path):
    """End-to-end: a bucket with real, diversified, downloaded candidates but a
    budget of 0 must come back with an empty basket, not lots quietly forced onto
    picks in a session the user explicitly zeroed out."""
    row = _row("sm1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-500")
    _write_distinct_report(tmp_path, "sm1", offset_months=1)

    result = build_portfolio(
        [row], tmp_path, "NIFTY", threshold=0.5, top_n=5,
        budgets={"short_morning": 0, "long_morning": 27, "midday": 0, "afternoon": 45},
    )

    assert result["buckets"]["short_morning"]["members"] == []
    assert result["buckets"]["short_morning"]["unallocated_lots"] == 0


def test_build_portfolio_never_ranks_a_combo_with_no_hard_stop_loss(tmp_path):
    """A trail-only (or no-SL-at-all) combo must never make it into the basket, even
    when it clearly outscores everything else - c743d82c58ff's exact situation."""
    rows = [
        _row("no_sl_but_best", "09:17", "11:20", rmdd="99", pnl="999999", maxdd="-100", has_sl=False),
        _row("has_sl_but_worse", "09:20", "11:30", rmdd="5", pnl="1000", maxdd="-500", has_sl=True),
    ]
    for i, r in enumerate(rows):
        _write_distinct_report(tmp_path, r["combo_id"], offset_months=i + 1)

    result = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=5)

    short_morning_ids = [m["combo_id"] for m in result["buckets"]["short_morning"]["members"]]
    assert "no_sl_but_best" not in short_morning_ids
    assert result["buckets"]["short_morning"]["excluded_no_stop_loss"] == 1


def test_build_portfolio_missing_reports_are_simply_excluded_not_errors(tmp_path):
    """No downloaded trade report for anything - every bucket should come back empty
    rather than raising."""
    rows = [_row("sm1", "09:17", "11:20")]
    result = build_portfolio(rows, tmp_path, "NIFTY")
    assert result["buckets"]["short_morning"]["members"] == []
    assert result["buckets"]["short_morning"]["with_report"] == 0
    assert result["portfolio"]["num_periods"] == 0


def test_recompute_stats_for_window_overrides_row_stats_from_filtered_series(tmp_path):
    row = _row("c1", "09:17", "11:20", rmdd="10", rrr="5", pnl="5000", maxdd="-999")
    _write_report(
        tmp_path / "nifty" / "c1.csv",
        [("0", "2025-01-01", "1000"), ("1", "2026-08-05", "-200"), ("2", "2026-08-06", "300")],
    )

    out_rows, series_cache, full_series_cache = _recompute_stats_for_window(
        [row], tmp_path, "NIFTY", date_from="2026-08-01", date_to=None
    )

    assert out_rows[0]["total_pnl"] == 100.0  # -200 + 300 - the 2025 day falls outside the window
    assert series_cache["c1"] == {"2026-08-05": -200.0, "2026-08-06": 300.0}
    assert row["total_pnl"] == "5000", "the original row dict must not be mutated in place"
    # The full (un-windowed) history, kept for free alongside the windowed one -
    # includes the 2025 date the windowed series correctly excludes.
    assert full_series_cache["c1"] == {"2025-01-01": 1000.0, "2026-08-05": -200.0, "2026-08-06": 300.0}


def test_recompute_stats_for_window_respects_date_to_upper_bound(tmp_path):
    row = _row("c1", "09:17", "11:20")
    _write_report(
        tmp_path / "nifty" / "c1.csv",
        [("0", "2026-07-31", "100"), ("1", "2026-08-05", "200"), ("2", "2026-08-20", "300")],
    )

    _, series_cache, _ = _recompute_stats_for_window(
        [row], tmp_path, "NIFTY", date_from="2026-08-01", date_to="2026-08-10"
    )

    assert series_cache["c1"] == {"2026-08-05": 200.0}


def test_recompute_stats_for_window_passes_through_unchanged_with_no_window(tmp_path):
    """No date_from/date_to given at all - byte-for-byte the old behavior: not even
    a single trade report gets opened."""
    row = _row("c1", "09:17", "11:20")
    out_rows, series_cache, full_series_cache = _recompute_stats_for_window(
        [row], tmp_path, "NIFTY", date_from=None, date_to=None
    )
    assert out_rows[0] is row
    assert series_cache == {}
    assert full_series_cache == {}


def test_recompute_stats_for_window_leaves_row_unchanged_when_no_report_on_disk(tmp_path):
    """A candidate with no downloaded report yet can't be recomputed - passed
    through as-is, same as it would be excluded downstream by _shortlist's own
    existence check regardless."""
    row = _row("missing1", "09:17", "11:20")
    out_rows, series_cache, full_series_cache = _recompute_stats_for_window(
        [row], tmp_path, "NIFTY", date_from="2026-08-01", date_to=None
    )
    assert out_rows[0] is row
    assert series_cache == {}
    assert full_series_cache == {}


def test_build_portfolio_date_window_ranks_by_windowed_stats_not_whole_history(tmp_path):
    """A combo that looks great over its WHOLE downloaded history but has actually
    been losing money since date_from must not out-rank one that's specifically
    strong in that window - see _recompute_stats_for_window. Each candidate has
    fewer than MIN_OVERLAP_DAYS days inside the window (and the 2025 dates never
    overlap at all), so correlation can never block either one - this isolates
    the ranking effect alone."""
    early_strong = _row("early_strong", "09:17", "11:20", rmdd="10", rrr="5", pnl="5000", maxdd="-500")
    late_strong = _row("late_strong", "09:17", "11:20", rmdd="1", rrr="0.5", pnl="100", maxdd="-500")
    rows = [early_strong, late_strong]

    _write_report(
        tmp_path / "nifty" / "early_strong.csv",
        [(str(n), f"2025-01-{n + 1:02d}", "1000") for n in range(6)]
        + [(str(n + 6), f"2026-08-{n + 10:02d}", "-500") for n in range(3)],
    )
    _write_report(
        tmp_path / "nifty" / "late_strong.csv",
        [(str(n), f"2025-02-{n + 1:02d}", "-1000") for n in range(6)]
        + [(str(n + 6), f"2026-08-{n + 20:02d}", "800") for n in range(3)],
    )

    before = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=1, max_share=1.0)
    before_ids = [m["combo_id"] for m in before["buckets"]["short_morning"]["members"]]
    assert before_ids == ["early_strong"], "without a date window, whole-history stats rank early_strong first"

    after = build_portfolio(rows, tmp_path, "NIFTY", threshold=0.5, top_n=1, max_share=1.0, date_from="2026-08-01")
    after_ids = [m["combo_id"] for m in after["buckets"]["short_morning"]["members"]]
    assert after_ids == ["late_strong"], "since 2026-08-01 late_strong is the profitable one and should rank first instead"


def test_build_portfolio_date_window_final_metrics_reflect_only_the_window(tmp_path):
    row = _row("only1", "09:17", "11:20", rmdd="10", rrr="5", pnl="5000", maxdd="-500")
    _write_report(
        tmp_path / "nifty" / "only1.csv",
        [(str(n), f"2025-01-{n + 1:02d}", "1000") for n in range(6)]
        + [(str(n + 6), f"2026-08-{n + 10:02d}", "300") for n in range(3)],
    )

    result = build_portfolio(
        [row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0, date_from="2026-08-01"
    )

    assert result["portfolio"]["num_periods"] == 3, "only the 3 in-window trade-dates should count, not the 6 historical ones"


def test_stale_picks_flags_a_pick_with_no_data_in_the_window(tmp_path):
    """The exact blind spot this fixed: every candidate can share the same old
    ceiling (nothing looks behind ITS OWN POOL - see data_coverage_gaps), while
    still being genuinely behind the real calendar - this check is the one that
    catches that, since it compares against `today` directly, not the pool."""
    row = _row("stale1", "09:17", "11:20")
    _write_report(tmp_path / "nifty" / "stale1.csv", [("0", "2026-08-18", "100")])

    stale = _stale_picks(
        ["stale1"], {"stale1": row}, tmp_path, "NIFTY",
        max_age_days=10, today=date(2026, 9, 7),
    )

    assert stale == [{"combo_id": "stale1", "last_date": "2026-08-18", "age_days": 20}]


def test_stale_picks_does_not_flag_a_pick_within_the_window(tmp_path):
    row = _row("fresh1", "09:17", "11:20")
    _write_report(tmp_path / "nifty" / "fresh1.csv", [("0", "2026-09-01", "100")])

    stale = _stale_picks(
        ["fresh1"], {"fresh1": row}, tmp_path, "NIFTY",
        max_age_days=10, today=date(2026, 9, 7),
    )

    assert stale == []


def test_stale_picks_sorted_worst_first(tmp_path):
    row_a = _row("a", "09:17", "11:20")
    row_b = _row("b", "09:17", "11:20")
    _write_report(tmp_path / "nifty" / "a.csv", [("0", "2026-08-20", "100")])  # 18 days old
    _write_report(tmp_path / "nifty" / "b.csv", [("0", "2026-08-01", "100")])  # 37 days old

    stale = _stale_picks(
        ["a", "b"], {"a": row_a, "b": row_b}, tmp_path, "NIFTY",
        max_age_days=10, today=date(2026, 9, 7),
    )

    assert [s["combo_id"] for s in stale] == ["b", "a"]


def test_stale_picks_missing_report_is_simply_skipped(tmp_path):
    row = _row("missing1", "09:17", "11:20")
    stale = _stale_picks(["missing1"], {"missing1": row}, tmp_path, "NIFTY", max_age_days=10, today=date(2026, 9, 7))
    assert stale == []


def test_stale_picks_uses_the_cache_without_touching_disk_at_all(tmp_path):
    """The whole point of full_series_cache: a candidate present in it (checked
    by `in`, not truthiness - a genuinely empty series must still count as
    cached) is never looked up on disk, even if no report exists there at all -
    proves the cache is actually consulted first, not just consulted as a
    fallback after the same disk read already happened."""
    row = _row("cached1", "09:17", "11:20")
    stale = _stale_picks(
        ["cached1"], {"cached1": row}, tmp_path, "NIFTY", max_age_days=10, today=date(2026, 9, 7),
        full_series_cache={"cached1": {"2026-08-01": 100.0}},  # 37 days old - no report file backs this up at all
    )
    assert stale == [{"combo_id": "cached1", "last_date": "2026-08-01", "age_days": 37}]


def test_stale_picks_falls_back_to_disk_for_a_combo_not_in_the_cache(tmp_path):
    row = _row("uncached1", "09:17", "11:20")
    _write_report(tmp_path / "nifty" / "uncached1.csv", [("0", "2026-08-01", "100")])
    stale = _stale_picks(
        ["uncached1"], {"uncached1": row}, tmp_path, "NIFTY", max_age_days=10, today=date(2026, 9, 7),
        full_series_cache={"some_other_combo": {"2026-09-01": 1.0}},
    )
    assert stale == [{"combo_id": "uncached1", "last_date": "2026-08-01", "age_days": 37}]


def test_build_portfolio_surfaces_stale_picks_for_the_final_basket_only(tmp_path):
    """End-to-end: a pick that made it into the basket, with data behind
    stale_after_days, shows up in the result - regardless of what the rest of the
    (here, single-candidate) pool looks like."""
    row = _row("only1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-500")
    _write_report(tmp_path / "nifty" / "only1.csv", [("0", "2026-08-18", "100")])

    result = build_portfolio(
        [row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0,
        stale_after_days=10, today=date(2026, 9, 7),
    )

    assert result["stale_picks"] == [{"combo_id": "only1", "last_date": "2026-08-18", "age_days": 20}]
    assert result["stale_after_days"] == 10  # echoed back so the UI can show what threshold was actually applied


def test_build_portfolio_stale_after_days_none_disables_the_check(tmp_path):
    row = _row("only1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-500")
    _write_report(tmp_path / "nifty" / "only1.csv", [("0", "2026-08-18", "100")])

    result = build_portfolio(
        [row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0,
        stale_after_days=None, today=date(2026, 9, 7),
    )

    assert result["stale_picks"] == []


def test_build_portfolio_stale_pool_picks_off_by_default(tmp_path):
    """check_stale_pool defaults to False - byte-for-byte unchanged behavior for
    every existing caller that doesn't ask for it (it's meaningfully more disk
    I/O, so it must never turn on silently)."""
    row = _row("only1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-500")
    _write_report(tmp_path / "nifty" / "only1.csv", [("0", "2026-08-18", "100")])

    result = build_portfolio(
        [row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0, today=date(2026, 9, 7),
    )

    assert result["stale_pool_picks"] == []


def test_build_portfolio_stale_pool_picks_catches_a_candidate_the_basket_rejected(tmp_path):
    """The exact gap this fixed: a candidate correlated out of the final basket
    (never a "pick") is invisible to stale_picks (final-picks-only) but must
    still show up here, since it's the runner-up that would win once the current
    stale pick gets refreshed and re-ranked - refreshing it up front avoids that
    next whack-a-mole round entirely."""
    winner = _row("winner", "09:17", "11:20", rmdd="10", rrr="5", pnl="5000", maxdd="-500")
    runner_up = _row("runner_up", "09:17", "11:20", rmdd="1", rrr="0.5", pnl="100", maxdd="-500")
    # Both share 6 identical-P/L August dates (varying values, not a flat
    # constant - a constant series has zero variance and comes back as
    # "insufficient data" (None) rather than a real correlation, which would
    # NOT block inclusion) - correlation ~1.0 on that overlap, comfortably above
    # the default 0.5 threshold, so pick_diversified_basket keeps only the
    # higher-ranked "winner" and rejects "runner_up" outright. "winner" ALSO has
    # one fresh September date runner_up doesn't - current on its own, while
    # runner_up's own data stops in August.
    varying_pnls = ["100", "-50", "200", "-30", "150", "-80"]
    shared_dates = [(str(n), f"2026-08-{n + 1:02d}", pnl) for n, pnl in enumerate(varying_pnls)]
    _write_report(tmp_path / "nifty" / "winner.csv", shared_dates + [("6", "2026-09-05", "100")])
    _write_report(tmp_path / "nifty" / "runner_up.csv", shared_dates)

    result = build_portfolio(
        [winner, runner_up], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0,
        stale_after_days=10, check_stale_pool=True, today=date(2026, 9, 7),
    )

    members = result["buckets"]["short_morning"]["members"]
    assert [m["combo_id"] for m in members] == ["winner"], "runner_up should be correlated out, not a final pick"
    assert result["stale_picks"] == [], "the only actual pick (winner) is current on its own"
    assert {s["combo_id"] for s in result["stale_pool_picks"]} == {"runner_up"}


def test_build_portfolio_data_window_reflects_the_final_picks_actual_dates(tmp_path):
    """A real fingerprint of what the result was actually computed from - min/max
    across the FINAL picks' own combined series, not the whole shortlist pool and
    not any date-window filter's own bounds (which can be wider than what the
    picks actually traded on)."""
    row = _row("only1", "09:17", "11:20", rmdd="10", pnl="2000", maxdd="-500")
    _write_report(tmp_path / "nifty" / "only1.csv", [
        ("0", "2026-08-04", "100"), ("1", "2026-08-11", "100"), ("2", "2026-08-18", "100"),
    ])

    result = build_portfolio([row], tmp_path, "NIFTY", threshold=0.5, top_n=5, max_share=1.0)

    assert result["data_window_min"] == "2026-08-04"
    assert result["data_window_max"] == "2026-08-18"
    assert result["computed_at"]  # a real timestamp string, not asserting its exact value


def test_build_portfolio_data_window_none_when_basket_is_empty(tmp_path):
    row = _row("sm1", "09:17", "11:20")
    result = build_portfolio([row], tmp_path, "NIFTY")
    assert result["data_window_min"] is None
    assert result["data_window_max"] is None
