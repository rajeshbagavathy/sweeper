from __future__ import annotations

import multiprocessing
from pathlib import Path
from queue import Empty

import pytest

from src.correlate import (
    _download_from_queue,
    _download_one_combo,
    compute_portfolio_metrics,
    compute_portfolio_metrics_weighted_with_charges,
    correlation_matrix,
    instrument_slug,
    parse_trade_report,
    pick_diversified_basket,
    trade_report_path,
)
from src.results import ResultOutcome


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (Index, Entry Date, P/L) - only parent-row columns matter for parsing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
        # one child leg row per parent, mirroring the real download shape - must be
        # ignored by parse_trade_report (it would double-count P/L otherwise).
        lines.append(f'"{index}.1","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","CE","100","Sell","1","10","10","","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


def test_instrument_slug_sanitizes_for_filesystem_use():
    assert instrument_slug("NIFTY") == "nifty"
    assert instrument_slug("BANK NIFTY!") == "bank_nifty"
    assert instrument_slug("") == "unknown"
    assert instrument_slug(None) == "unknown"  # type: ignore[arg-type]


def test_trade_report_path_scopes_by_instrument_and_combo_id(tmp_path):
    p = trade_report_path(tmp_path, "NIFTY", "abc123")
    assert p == tmp_path / "nifty" / "abc123.csv"


def test_parse_trade_report_reads_parent_rows_only_and_ignores_legs(tmp_path):
    path = tmp_path / "report.csv"
    _write_report(path, [("1", "2025-09-03", "100.5"), ("2", "2025-09-10", "-50.0")])

    result = parse_trade_report(path)

    # If child ".1" rows were counted too, these would be doubled.
    assert result == {"2025-09-03": 100.5, "2025-09-10": -50.0}


def test_parse_trade_report_sums_duplicate_dates(tmp_path):
    path = tmp_path / "report.csv"
    _write_report(path, [("1", "2025-09-03", "100.0"), ("2", "2025-09-03", "50.0")])

    result = parse_trade_report(path)

    assert result == {"2025-09-03": 150.0}


def test_correlation_matrix_perfectly_correlated_series():
    series = {
        "a": {"d1": 10.0, "d2": -5.0, "d3": 20.0, "d4": -10.0, "d5": 15.0},
        "b": {"d1": 20.0, "d2": -10.0, "d3": 40.0, "d4": -20.0, "d5": 30.0},  # a * 2
    }
    matrix = correlation_matrix(series)
    assert matrix["a"]["b"] == pytest.approx(1.0)
    assert matrix["b"]["a"] == pytest.approx(1.0)
    assert matrix["a"]["a"] == 1.0


def test_correlation_matrix_insufficient_overlap_is_none_not_a_crash():
    series = {
        "a": {"d1": 1.0, "d2": 2.0},  # only 2 shared dates - below MIN_OVERLAP_DAYS
        "b": {"d1": 5.0, "d2": 6.0},
    }
    matrix = correlation_matrix(series, min_overlap=5)
    assert matrix["a"]["b"] is None
    assert matrix["b"]["a"] is None


def test_correlation_matrix_no_overlap_at_all_is_none():
    series = {"a": {"d1": 1.0, "d2": 2.0, "d3": 3.0, "d4": 4.0, "d5": 5.0}, "b": {"e1": 1.0, "e2": 2.0}}
    matrix = correlation_matrix(series)
    assert matrix["a"]["b"] is None


def test_correlation_matrix_identical_series_below_min_overlap_is_treated_as_correlated():
    # Confirmed live: a short backtest window (few trading days) routinely
    # produces near-duplicate combos - same entry/exit, identical P&L to the
    # rupee, because whatever parameter differs between them never actually
    # fires against that window's price action. With only 2 overlapping days
    # (below MIN_OVERLAP_DAYS), a plain correlation coefficient can't be
    # computed - but two BYTE-IDENTICAL series aren't "unknown", they're
    # definitely the same strategy, and must be excluded rather than let
    # through as "insufficient data to know".
    series = {
        "a": {"d1": 1.0, "d2": 2.0},
        "b": {"d1": 1.0, "d2": 2.0},
    }
    matrix = correlation_matrix(series, min_overlap=5)
    assert matrix["a"]["b"] == 1.0
    assert matrix["b"]["a"] == 1.0


def test_correlation_matrix_below_min_overlap_but_not_identical_is_still_none():
    # Same tiny overlap as above, but the values genuinely differ - must stay
    # "unknown", not be misread as identical just because it's a small pair.
    series = {
        "a": {"d1": 1.0, "d2": 2.0},
        "b": {"d1": 1.0, "d2": 2.5},
    }
    matrix = correlation_matrix(series, min_overlap=5)
    assert matrix["a"]["b"] is None


def test_correlation_matrix_two_empty_series_is_not_treated_as_identical():
    # Both candidates have zero trades in this window - genuinely unknown
    # whether they're the same strategy, not "identical" just because two
    # empty dicts compare equal.
    series = {"a": {}, "b": {}}
    matrix = correlation_matrix(series)
    assert matrix["a"]["b"] is None


def test_pick_diversified_basket_skips_a_highly_correlated_higher_scorer():
    # "a" ranks best but is a near-clone of "b" (already picked) - should be skipped
    # in favor of "c", which ranks lower but is genuinely uncorrelated.
    matrix = {
        "a": {"a": 1.0, "b": 0.95, "c": 0.1},
        "b": {"a": 0.95, "b": 1.0, "c": 0.05},
        "c": {"a": 0.1, "b": 0.05, "c": 1.0},
    }
    basket, skipped = pick_diversified_basket(["b", "a", "c"], matrix, threshold=0.5)

    basket_ids = [b["combo_id"] for b in basket]
    assert basket_ids == ["b", "c"]
    assert skipped == [{"combo_id": "a", "reason": "correlation 0.95 with b"}]


def test_pick_diversified_basket_unknown_correlation_never_blocks_inclusion():
    # No overlap data between "a" and "b" (None) - must not be treated as "too
    # correlated" and block "b" from the basket.
    matrix = {"a": {"a": 1.0, "b": None}, "b": {"a": None, "b": 1.0}}
    basket, skipped = pick_diversified_basket(["a", "b"], matrix, threshold=0.5)
    assert [b["combo_id"] for b in basket] == ["a", "b"]
    assert skipped == []
    assert basket[1]["max_corr_to_basket"] is None


def test_pick_diversified_basket_same_window_excludes_unknown_correlation_pair():
    # Confirmed live: a short backtest window leaves correlation "unknown" for
    # almost every pair in a narrow-session bucket, letting the exact same
    # entry/exit clock-time window get picked repeatedly even though no pair
    # is individually PROVEN correlated. same_window() is the tie-break for
    # exactly this: "b" shares a's own (entry, exit) and correlation to it is
    # unknown (None) - must be treated as maximally correlated, not "unknown
    # so keep it".
    matrix = {"a": {"a": 1.0, "b": None}, "b": {"a": None, "b": 1.0}}
    basket, skipped = pick_diversified_basket(
        ["a", "b"], matrix, threshold=0.5, same_window=lambda x, y: True,
    )
    assert [b["combo_id"] for b in basket] == ["a"]
    assert skipped == [{"combo_id": "b", "reason": "correlation 1.00 with a"}]


def test_pick_diversified_basket_same_window_does_not_override_a_real_correlation():
    # A genuinely LOW correlation (proven, not unknown) must still win even if
    # same_window would say True - same_window only ever fills in for unknown
    # (None), never overrides an actual computed number.
    matrix = {"a": {"a": 1.0, "b": 0.1}, "b": {"a": 0.1, "b": 1.0}}
    basket, skipped = pick_diversified_basket(
        ["a", "b"], matrix, threshold=0.5, same_window=lambda x, y: True,
    )
    assert [b["combo_id"] for b in basket] == ["a", "b"]
    assert skipped == []


def test_pick_diversified_basket_same_window_false_leaves_unknown_pair_included():
    # Different windows, still unknown correlation - unchanged "keep it"
    # behavior, same_window is only a tie-break when it actually applies.
    matrix = {"a": {"a": 1.0, "b": None}, "b": {"a": None, "b": 1.0}}
    basket, skipped = pick_diversified_basket(
        ["a", "b"], matrix, threshold=0.5, same_window=lambda x, y: False,
    )
    assert [b["combo_id"] for b in basket] == ["a", "b"]
    assert skipped == []


def test_pick_diversified_basket_same_window_unset_matches_old_behavior():
    matrix = {"a": {"a": 1.0, "b": None}, "b": {"a": None, "b": 1.0}}
    basket, skipped = pick_diversified_basket(["a", "b"], matrix, threshold=0.5)
    assert [b["combo_id"] for b in basket] == ["a", "b"]
    assert skipped == []


def test_compute_portfolio_metrics_sums_daily_pl_across_the_basket_first():
    # "a" and "b" each show a small loss on d2 alone, but combined they land on the
    # same date and offset - the combined series, not either member's own, is what
    # must drive every stat here.
    series = {
        "a": {"d1": 100.0, "d2": -20.0, "d3": 50.0},
        "b": {"d1": 30.0, "d2": 20.0, "d3": -10.0},
    }
    m = compute_portfolio_metrics(["a", "b"], series)
    assert m["num_periods"] == 3
    assert m["overall_profit"] == pytest.approx(170.0)
    assert m["avg_profit_per_period"] == pytest.approx(170.0 / 3)
    # combined daily P/L: d1=130, d2=0, d3=40 - only d1 and d3 are strictly positive
    assert m["win_pct"] == pytest.approx(200.0 / 3)
    assert m["max_profit_single_period"] == pytest.approx(130.0)
    assert m["max_loss_single_period"] == pytest.approx(0.0)


def test_compute_portfolio_metrics_max_drawdown_and_reward_risk():
    series = {"a": {"d1": 100.0, "d2": -150.0, "d3": 80.0}}
    m = compute_portfolio_metrics(["a"], series)
    # equity path: 100 -> -50 (peak 100, dd -150) -> 30
    assert m["max_drawdown"] == pytest.approx(-150.0)
    assert m["max_drawdown_start"] == "d1"
    assert m["max_drawdown_end"] == "d2"
    assert m["overall_profit"] == pytest.approx(30.0)
    assert m["return_max_dd"] == pytest.approx(30.0 / 150.0)
    assert m["reward_risk_ratio"] == pytest.approx(90.0 / 150.0)  # avg win 90 / abs(avg loss) 150


def test_compute_portfolio_metrics_empty_basket_does_not_crash():
    m = compute_portfolio_metrics([], {})
    assert m["num_periods"] == 0
    assert m["overall_profit"] == 0.0
    assert m["return_max_dd"] is None
    assert m["reward_risk_ratio"] is None


def test_charges_with_brokerage_flat_and_taxes_scaled_by_lots():
    """Regression test for the validated finding: brokerage is a flat per-order fee
    (unaffected by lot size), taxes & charges scale with traded value (lot size).
    A combo at half its recorded base lots should have its taxes halved but its
    brokerage deducted in full, not halved too."""
    series = {"a": {"d1": 100.0, "d2": 100.0}}  # 2 periods, recorded at base_lots=10
    charges = {"a": {"brokerage": 20.0, "taxes_charges": 10.0}}

    m = compute_portfolio_metrics_weighted_with_charges({"a": 5.0}, series, charges, base_lots=10.0)
    # scale = 0.5: pnl halved (50+50), taxes halved (5), brokerage NOT halved (20)
    # per-day deduction = (20 + 10*0.5) / 2 periods = 12.5/period
    # combined: (50-12.5) + (50-12.5) = 75.0
    assert m["overall_profit"] == pytest.approx(75.0)


def test_charges_missing_for_a_combo_gets_no_adjustment():
    """A combo with no recorded brokerage/taxes (older rows, before this was
    tracked) falls back to unadjusted P/L rather than a guessed deduction."""
    series = {"a": {"d1": 100.0}}
    m = compute_portfolio_metrics_weighted_with_charges({"a": 10.0}, series, {}, base_lots=10.0)
    assert m["overall_profit"] == pytest.approx(100.0)

    # explicit None values behave the same as the key being absent entirely
    m2 = compute_portfolio_metrics_weighted_with_charges(
        {"a": 10.0}, series, {"a": {"brokerage": None, "taxes_charges": 5.0}}, base_lots=10.0
    )
    assert m2["overall_profit"] == pytest.approx(100.0)


def test_download_from_queue_never_picks_up_new_work_once_already_stopped(monkeypatch):
    import src.correlate as correlate_mod

    processed: list[str] = []

    def fake_download_one_combo(page, combo, cid, instrument, selectors, reports_dir, **kwargs):
        processed.append(cid)
        return {"combo_id": cid, "status": "downloaded"}

    monkeypatch.setattr(correlate_mod, "_download_one_combo", fake_download_one_combo)

    queue: multiprocessing.Queue = multiprocessing.Queue()
    queue.put(({"instrument": "NIFTY"}, "cid1", "NIFTY"))
    progress_queue: multiprocessing.Queue = multiprocessing.Queue()
    stop_event = multiprocessing.Event()
    stop_event.set()

    _download_from_queue(
        None, queue, None, Path("reports"), progress_queue,
        delay_s=0, result_timeout_s=1, max_retries=0, email=None, password=None,
        slippage_pct=1.0, dte_values=[0], brokerage_rate=None, stop_event=stop_event,
    )

    assert processed == []
    with pytest.raises(Empty):
        progress_queue.get_nowait()


def test_download_from_queue_finishes_in_flight_item_before_stopping(monkeypatch):
    import src.correlate as correlate_mod

    processed: list[str] = []
    stop_event = multiprocessing.Event()

    def fake_download_one_combo(page, combo, cid, instrument, selectors, reports_dir, **kwargs):
        processed.append(cid)
        stop_event.set()
        return {"combo_id": cid, "status": "downloaded"}

    monkeypatch.setattr(correlate_mod, "_download_one_combo", fake_download_one_combo)

    queue: multiprocessing.Queue = multiprocessing.Queue()
    queue.put(({"instrument": "NIFTY"}, "cid1", "NIFTY"))
    queue.put(({"instrument": "NIFTY"}, "cid2", "NIFTY"))
    progress_queue: multiprocessing.Queue = multiprocessing.Queue()

    _download_from_queue(
        None, queue, None, Path("reports"), progress_queue,
        delay_s=0, result_timeout_s=1, max_retries=0, email=None, password=None,
        slippage_pct=1.0, dte_values=[0], brokerage_rate=None, stop_event=stop_event,
    )

    assert processed == ["cid1"]
    # get() right after put() races multiprocessing.Queue's own feeder thread flushing
    # the item into its pipe - a short timeout avoids a spurious Empty here. One
    # "started" event is pushed before the (fake) download begins, then the real
    # completion status.
    assert progress_queue.get(timeout=2) == {"combo_id": "cid1", "status": "started"}
    assert progress_queue.get(timeout=2) == {"combo_id": "cid1", "status": "downloaded"}


def test_download_one_combo_default_skips_replay_when_already_cached(tmp_path, monkeypatch):
    """Default (update_results=False) behavior must stay exactly as before this
    feature - "Force re-download & update results" only changes anything when
    explicitly requested."""
    import src.correlate as correlate_mod

    cid = "combo1"
    target = trade_report_path(tmp_path, "NIFTY", cid)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("already here")

    def _must_not_be_called(*a, **k):
        raise AssertionError("should not replay - the cached shortcut must fire first")

    monkeypatch.setattr(correlate_mod, "is_logged_in", _must_not_be_called)

    result = _download_one_combo(
        None, {"instrument": "NIFTY"}, cid, "NIFTY", None, tmp_path,
        result_timeout_s=1, max_retries=0, email=None, password=None,
        slippage_pct=1.0, dte_values=[0], brokerage_rate=None, brokerage_configured=[False],
    )
    assert result == {"combo_id": cid, "status": "cached"}


def test_download_one_combo_update_results_bypasses_cache_and_returns_fresh_row(tmp_path, monkeypatch):
    """update_results=True must ignore an existing cached report (a top-N backfill's
    whole point is refreshing combos that were already auto-downloaded once) and
    return the freshly re-scraped row for the caller to merge back into its CSV."""
    import src.correlate as correlate_mod

    cid = "combo1"
    target = trade_report_path(tmp_path, "NIFTY", cid)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stale copy")

    monkeypatch.setattr(correlate_mod, "is_logged_in", lambda page, selectors: True)
    monkeypatch.setattr(correlate_mod, "apply_combination", lambda page, selectors, combo: None)
    monkeypatch.setattr(
        correlate_mod, "wait_for_result", lambda page, selectors, timeout_s=180: ResultOutcome(status="ok")
    )
    monkeypatch.setattr(
        correlate_mod, "apply_result_settings", lambda page, selectors, slippage_pct, dte_values: None
    )
    monkeypatch.setattr(correlate_mod, "scrape_metrics", lambda page, selectors: {"total_pnl": "1234"})
    downloaded: list[str] = []
    monkeypatch.setattr(
        correlate_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    combo = {"instrument": "NIFTY"}
    result = _download_one_combo(
        None, combo, cid, "NIFTY", None, tmp_path,
        result_timeout_s=1, max_retries=0, email=None, password=None,
        slippage_pct=1.0, dte_values=[0], brokerage_rate=None, brokerage_configured=[False],
        update_results=True,
    )

    assert result["status"] == "downloaded"
    assert downloaded == [cid]  # re-downloaded despite the stale copy already on disk
    assert result["row"]["combo_id"] == cid
    assert result["row"]["status"] == "ok"
    assert result["row"]["total_pnl"] == 1234.0
