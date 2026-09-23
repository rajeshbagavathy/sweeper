from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.test_portfolio import _write_report


def test_analyze_portfolio_basket_endpoint_wires_through_to_build_portfolio(tmp_path, monkeypatch):
    """Thin-wrapper check: the /api/analyze/portfolio-basket endpoint reads the
    requested csv_file(s), scopes to 'ok' rows for the given instrument, and hands
    off to portfolio.build_portfolio - not testing the basket math itself (see
    tests/test_portfolio.py for that), just that the endpoint actually wires it up."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "sm1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })
        writer.writerow({
            "combo_id": "not_nifty", "status": "ok", "instrument": "BANKNIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })

    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "sm1.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    # max_share=1.0 - this test checks scoping/wiring, not the lot cap (see
    # tests/test_portfolio.py for that).
    result = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0
    )

    ids = [m["combo_id"] for m in result["buckets"]["short_morning"]["members"]]
    assert ids == ["sm1"]  # picked, and the BANKNIFTY row was correctly scoped out
    assert result["buckets"]["short_morning"]["members"][0]["lots"] == 10


def test_analyze_portfolio_basket_endpoint_very_long_morning_mode(tmp_path, monkeypatch):
    """very_long_morning_mode=True must switch to the 2-bucket scheme (see
    portfolio.VERY_LONG_MORNING_BUCKET_ORDER/classify_very_long_morning_bucket)
    and reuse long_morning_budget as that bucket's own budget - an 11am entry
    held to a 2:45pm exit has no home in the regular 4-bucket scheme at all
    (see test_portfolio.py's own coverage of that gap), so its presence here
    confirms the mode is actually wired through, not just accepted and ignored."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "gap1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "11:30", "exit_time": "14:45",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })

    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "gap1.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    without_mode = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", long_morning_budget=10, max_share=1.0,
    )
    assert without_mode["buckets"].keys() == {"short_morning", "long_morning", "midday", "afternoon"}
    assert all(not b["members"] for b in without_mode["buckets"].values())  # the gap - lands nowhere today

    with_mode = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", long_morning_budget=10, max_share=1.0,
        very_long_morning_mode=True,
    )
    assert with_mode["buckets"].keys() == {"very_long_morning", "afternoon"}
    ids = [m["combo_id"] for m in with_mode["buckets"]["very_long_morning"]["members"]]
    assert ids == ["gap1"]
    assert with_mode["buckets"]["very_long_morning"]["budget"] == 10.0  # reused from long_morning_budget


def test_analyze_portfolio_basket_endpoint_scopes_by_date_range(tmp_path, monkeypatch):
    """A Backtest period pill active on the Analyze page must keep the basket
    scoped to combos recorded under that SAME (start_date, end_date) window - not
    silently mix in a combo from a different period. This is exactly the "same
    period only" guarantee the Backtest period filter exists to give (confirmed
    live this session: a force-redownload can leave a minority of rows at a
    different window than the rest of the file)."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value", "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "in_period", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
            "start_date": "2026-08-01", "end_date": "2026-09-03",
        })
        writer.writerow({
            "combo_id": "different_period", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
            "start_date": "2026-08-05", "end_date": "2026-09-07",
        })

    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "in_period.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])
    _write_report(reports_dir / "nifty" / "different_period.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    result = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
        date_range="2026-08-01 to 2026-09-03",
    )

    ids = [m["combo_id"] for m in result["buckets"]["short_morning"]["members"]]
    assert ids == ["in_period"]


def test_analyze_portfolio_basket_endpoint_forwards_stale_after_days(tmp_path, monkeypatch):
    """A pick whose data is behind this threshold shows up in stale_picks - and
    passing None (the "off" state) must suppress it, both reaching build_portfolio
    through the endpoint, not just tested at the portfolio.py level."""
    import src.web.app as app_mod
    from tests.test_portfolio import _write_report

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "sm1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "sm1.csv", [("0", "2026-08-18", "100")])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    flagged = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
        stale_after_days=10,
    )
    assert [s["combo_id"] for s in flagged["stale_picks"]] == ["sm1"]

    suppressed = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
        stale_after_days=None,
    )
    assert suppressed["stale_picks"] == []


def test_analyze_portfolio_basket_endpoint_forwards_check_stale_pool(tmp_path, monkeypatch):
    """check_stale_pool defaults to off at the endpoint too - must be explicitly
    requested to pay for the extra pool-wide disk I/O."""
    import src.web.app as app_mod
    from tests.test_portfolio import _write_report

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "sm1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "sm1.csv", [("0", "2026-08-18", "100")])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    default_off = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
    )
    assert default_off["stale_pool_picks"] == []

    opted_in = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
        check_stale_pool=True,
    )
    assert [s["combo_id"] for s in opted_in["stale_pool_picks"]] == ["sm1"]


def test_analyze_portfolio_basket_endpoint_forwards_date_window(tmp_path, monkeypatch):
    """trade_date_from/trade_date_to must reach portfolio.build_portfolio, not get
    dropped at the endpoint layer - end-to-end check using a combo that's only
    profitable inside the requested window (see test_portfolio.py's own unit-level
    coverage of the ranking effect itself)."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "sm1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })

    reports_dir = tmp_path / "reports"
    _write_report(
        reports_dir / "nifty" / "sm1.csv",
        [(str(n), f"2025-09-{n + 1:02d}", "1000") for n in range(6)]
        + [(str(n + 6), f"2026-08-{n + 10:02d}", "300") for n in range(3)],
    )
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    result = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
        trade_date_from="2026-08-01",
    )

    assert result["portfolio"]["num_periods"] == 3, "only the 3 in-window trade-dates should count"


def test_analyze_portfolio_basket_endpoint_scopes_candidate_rows_by_date_from(tmp_path, monkeypatch):
    """date_from/date_to (distinct from trade_date_from/trade_date_to above) must
    scope WHICH CANDIDATE ROWS are even considered, by each row's own start_date -
    this is the page's "Backtest date from" range filter, and must actually reach
    the basket builder rather than being silently dropped, matching every other
    analyze endpoint's _scope_ok_rows-based date_from/date_to handling."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = [
        "combo_id", "status", "instrument", "entry_time", "exit_time", "start_date",
        "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown",
        "stoploss.kind", "stoploss.value",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "old1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20", "start_date": "2025-01-01",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })
        # Different exit_time from old1 - this test is about date-range scoping,
        # not diversification, but a shared (entry, exit) with unknown/constant
        # correlation would now trip pick_diversified_basket's same_window
        # dedup tie-break (see src/correlate.py) and drop one of them, which
        # isn't what's under test here.
        writer.writerow({
            "combo_id": "new1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:25", "start_date": "2026-08-05",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })

    reports_dir = tmp_path / "reports"
    for cid in ("old1", "new1"):
        _write_report(
            reports_dir / "nifty" / f"{cid}.csv",
            [(str(n), f"2026-01-{n + 1:02d}", "1000") for n in range(6)],
        )
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)

    unscoped = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
    )
    scoped = app_mod.analyze_portfolio_basket(
        csv_file=[str(csv_path)], instrument="NIFTY", short_morning_budget=10, max_share=1.0,
        date_from="2026-08-01",
    )

    unscoped_ids = {m["combo_id"] for bucket in unscoped["buckets"].values() for m in bucket["members"]}
    scoped_ids = {m["combo_id"] for bucket in scoped["buckets"].values() for m in bucket["members"]}
    assert "old1" in unscoped_ids and "new1" in unscoped_ids
    assert scoped_ids == {"new1"}, "date_from=2026-08-01 must exclude the old1 candidate started 2025-01-01"


def test_portfolio_sweep_start_endpoint_forwards_date_window_and_scopes_context(tmp_path, monkeypatch):
    """trade_date_from/trade_date_to must reach PortfolioSweepState.start (so the
    sweep loop actually windows its analysis), AND must be folded into `context` -
    otherwise resuming a stopped sweep after only the date window changed would
    silently keep results computed against the OLD window, the same class of bug
    already fixed once for regime_sweep_state's own context check."""
    import src.web.app as app_mod
    from src.web.portfolio_sweep_state import PortfolioSweepState

    csv_path = tmp_path / "results.csv"
    _write_basic_csv(csv_path)
    monkeypatch.setattr(app_mod, "REPORTS_DIR", tmp_path / "reports")

    seen_kwargs = {}
    fresh_state = PortfolioSweepState()
    orig_start = fresh_state.start

    def spy_start(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return orig_start(*args, **kwargs)

    monkeypatch.setattr(fresh_state, "start", spy_start)
    monkeypatch.setattr(app_mod, "portfolio_sweep_state", fresh_state)

    app_mod.portfolio_sweep_start(
        csv_file=[str(csv_path)], instrument="NIFTY",
        threshold_min=0.5, threshold_max=0.5, threshold_step=0.05,
        top_n_min=20, top_n_max=20, top_n_step=1,
        min_lots_min=2, min_lots_max=2, min_lots_step=1,
        max_lots_min=5, max_lots_max=5, max_lots_step=1,
        short_morning_budget=10, max_share=1.0,
        trade_date_from="2026-08-01", trade_date_to="2026-08-31",
    )

    assert seen_kwargs["date_from"] == "2026-08-01"
    assert seen_kwargs["date_to"] == "2026-08-31"
    assert seen_kwargs["context"] == ("2026-08-01", "2026-08-31", None, None, False)


def _wait_until_done(state, timeout_s: float = 2.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = state.snapshot()
        if snap["status"] in ("done", "error", "stopped"):
            return snap
        time.sleep(0.01)
    raise AssertionError(f"sweep did not finish in time - last status {state.snapshot()['status']!r}")


def _write_basic_csv(csv_path: Path) -> None:
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd",
                  "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "sm1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
        })


def test_portfolio_sweep_start_endpoint_scopes_by_date_range(tmp_path, monkeypatch):
    """Same "same period only" guarantee as the single-basket endpoint
    (test_analyze_portfolio_basket_endpoint_scopes_by_date_range), but for the
    sweep - a Backtest period pill active on the page must scope every grid
    point's candidate pool, not just a one-off basket compute."""
    import src.web.app as app_mod
    from src.web.portfolio_sweep_state import PortfolioSweepState

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time", "return_max_dd",
                  "reward_risk_ratio", "total_pnl", "max_drawdown", "stoploss.kind", "stoploss.value",
                  "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "combo_id": "in_period", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
            "start_date": "2026-08-01", "end_date": "2026-09-03",
        })
        writer.writerow({
            "combo_id": "different_period", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
            "stoploss.kind": "amount", "stoploss.value": "7000",
            "start_date": "2026-08-05", "end_date": "2026-09-07",
        })
    monkeypatch.setattr(app_mod, "REPORTS_DIR", tmp_path / "reports")

    seen_rows: list = []
    fresh_state = PortfolioSweepState()

    def spy_start(rows, *args, **kwargs):
        seen_rows.extend(rows)

    monkeypatch.setattr(fresh_state, "start", spy_start)
    monkeypatch.setattr(app_mod, "portfolio_sweep_state", fresh_state)

    app_mod.portfolio_sweep_start(
        csv_file=[str(csv_path)], instrument="NIFTY",
        threshold_min=0.5, threshold_max=0.5, threshold_step=0.05,
        top_n_min=20, top_n_max=20, top_n_step=1,
        min_lots_min=2, min_lots_max=2, min_lots_step=1,
        max_lots_min=5, max_lots_max=5, max_lots_step=1,
        short_morning_budget=10, max_share=1.0,
        date_range="2026-08-01 to 2026-09-03",
    )

    ids = {r["combo_id"] for r in seen_rows}
    assert ids == {"in_period"}


def test_portfolio_sweep_start_endpoint_builds_grid_and_runs_it(tmp_path, monkeypatch):
    """Thin-wrapper check, same spirit as the basket endpoint test above: the
    endpoint builds the (threshold, top_n, min_lots, max_lots) grid from the
    from/to/step params and hands it to PortfolioSweepState.start - not testing
    the basket math itself (see test_portfolio.py) or the sweep loop itself (see
    test_portfolio_sweep_state.py), just that the endpoint wires the two
    together correctly."""
    import src.web.app as app_mod
    from src.web.portfolio_sweep_state import PortfolioSweepState

    csv_path = tmp_path / "results.csv"
    _write_basic_csv(csv_path)
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "sm1.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)
    fresh_state = PortfolioSweepState()
    monkeypatch.setattr(app_mod, "portfolio_sweep_state", fresh_state)

    app_mod.portfolio_sweep_start(
        csv_file=[str(csv_path)], instrument="NIFTY",
        threshold_min=0.5, threshold_max=0.5, threshold_step=0.05,
        top_n_min=20, top_n_max=20, top_n_step=1,
        min_lots_min=2, min_lots_max=2, min_lots_step=1,
        max_lots_min=5, max_lots_max=5, max_lots_step=1,
        short_morning_budget=10, max_share=1.0,
    )
    snap = _wait_until_done(fresh_state)

    assert snap["status"] == "done"
    assert len(snap["results"]) == 1
    entry = snap["results"][0]
    assert (entry["threshold"], entry["top_n"], entry["min_lots"], entry["max_lots"]) == (0.5, 20, 2, 5)


def test_portfolio_sweep_start_endpoint_skips_min_lots_over_max_lots_combinations(tmp_path, monkeypatch):
    """A grid point where min_lots > max_lots is contradictory (see _size_lots) -
    it must be filtered out before reaching build_portfolio, not computed."""
    import src.web.app as app_mod
    from src.web.portfolio_sweep_state import PortfolioSweepState

    csv_path = tmp_path / "results.csv"
    _write_basic_csv(csv_path)
    reports_dir = tmp_path / "reports"
    _write_report(reports_dir / "nifty" / "sm1.csv", [(str(n), f"2025-09-{n+1:02d}", "100") for n in range(6)])
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports_dir)
    fresh_state = PortfolioSweepState()
    monkeypatch.setattr(app_mod, "portfolio_sweep_state", fresh_state)

    # min_lots in {5, 6} x max_lots in {3, 4} - every combination has min > max,
    # so the whole grid comes back empty and start() must raise, surfaced as 409.
    with pytest.raises(HTTPException) as exc_info:
        app_mod.portfolio_sweep_start(
            csv_file=[str(csv_path)], instrument="NIFTY",
            threshold_min=0.5, threshold_max=0.5, threshold_step=0.05,
            top_n_min=20, top_n_max=20, top_n_step=1,
            min_lots_min=5, min_lots_max=6, min_lots_step=1,
            max_lots_min=3, max_lots_max=4, max_lots_step=1,
            short_morning_budget=10, max_share=1.0,
        )
    assert exc_info.value.status_code == 409


def test_portfolio_sweep_start_endpoint_rejects_an_oversized_grid(tmp_path, monkeypatch):
    import src.web.app as app_mod
    from src.web.portfolio_sweep_state import PortfolioSweepState

    csv_path = tmp_path / "results.csv"
    _write_basic_csv(csv_path)
    monkeypatch.setattr(app_mod, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(app_mod, "portfolio_sweep_state", PortfolioSweepState())
    monkeypatch.setattr(app_mod, "MAX_SWEEP_GRID_SIZE", 10)

    with pytest.raises(HTTPException) as exc_info:
        app_mod.portfolio_sweep_start(
            csv_file=[str(csv_path)], instrument="NIFTY",
            threshold_min=0.1, threshold_max=0.9, threshold_step=0.05,  # 17 values, already > 10
            top_n_min=20, top_n_max=20, top_n_step=1,
            min_lots_min=2, min_lots_max=2, min_lots_step=1,
            max_lots_min=5, max_lots_max=5, max_lots_step=1,
        )
    assert exc_info.value.status_code == 400
