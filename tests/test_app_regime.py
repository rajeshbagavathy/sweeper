from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from fastapi import HTTPException
import pytest

import src.web.app as app_mod
from src.web.app import download_regime_reports, regime_portfolio_basket, regime_sweep_start
from src.web.regime_state import regime_window_dir


@pytest.fixture(autouse=True)
def _isolated_regime_reports_dir(tmp_path, monkeypatch):
    """Every test below either creates or checks a regime_window_dir(...) - without
    this, they'd read/write the real output/trade_reports_regime/ on disk."""
    monkeypatch.setattr("src.web.regime_state.REGIME_REPORTS_DIR", tmp_path)


def test_download_regime_reports_requires_a_start_date(monkeypatch):
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        download_regime_reports(date_from="")
    assert exc_info.value.status_code == 400


def test_download_regime_reports_rejects_end_before_start(monkeypatch):
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        download_regime_reports(date_from="2026-09-01", date_to="2026-08-01")
    assert exc_info.value.status_code == 400


def test_download_regime_reports_rejects_while_a_sweep_is_running(monkeypatch):
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: True)
    with pytest.raises(HTTPException) as exc_info:
        download_regime_reports(date_from="2026-08-01")
    assert exc_info.value.status_code == 409


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time",
                  "stoploss.kind", "stoploss.value", "return_max_dd", "reward_risk_ratio", "total_pnl"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _ok_row(cid: str, *, entry_time: str, exit_time: str, score: float) -> dict:
    return {
        "combo_id": cid, "status": "ok", "instrument": "NIFTY",
        "entry_time": entry_time, "exit_time": exit_time,
        "stoploss.kind": "amount", "stoploss.value": "7000",
        "return_max_dd": score, "reward_risk_ratio": score, "total_pnl": 1,
    }


def test_download_regime_reports_shortlists_per_session_not_globally(tmp_path, monkeypatch):
    """The whole point of fix 2: a flat "best N overall" sort would let
    long_morning's much higher scores crowd out every other session's candidates
    entirely - shortlisting must give each session its own fair share instead."""
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)
    csv_path = tmp_path / "results_web_a.csv"
    rows = [_ok_row(f"lm{i}", entry_time="09:20", exit_time="15:10", score=100 - i) for i in range(5)]
    rows.append(_ok_row("sm0", entry_time="09:20", exit_time="11:30", score=1))
    _write_csv(csv_path, rows)

    captured: dict = {}
    monkeypatch.setattr(app_mod.regime_download_state, "start", lambda rows, **kwargs: captured.update(rows=rows))

    download_regime_reports(date_from="2026-08-01", date_to="2026-09-01", top_n_per_bucket=2, csv_file=[str(csv_path)])

    picked_ids = {r["combo_id"] for r in captured["rows"]}
    # long_morning capped at 2 despite having 5 higher-scoring candidates available.
    assert {cid for cid in picked_ids if cid.startswith("lm")} == {"lm0", "lm1"}
    # short_morning's only (much lower-scoring) candidate still made it in.
    assert "sm0" in picked_ids


def _row_with_period(cid: str, start_date: str, end_date: str) -> dict:
    row = _ok_row(cid, entry_time="09:20", exit_time="15:10", score=10)
    row["start_date"] = start_date
    row["end_date"] = end_date
    return row


def test_download_regime_reports_scopes_by_date_range(tmp_path, monkeypatch):
    """A Backtest period pill active on the Analyze page must keep the regime
    download's own shortlist scoped to combos from that SAME recorded window -
    same "same period only" guarantee as the regular Portfolio basket."""
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time",
                  "stoploss.kind", "stoploss.value", "return_max_dd", "reward_risk_ratio", "total_pnl",
                  "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(_row_with_period("in_period", "2026-08-01", "2026-09-03"))
        writer.writerow(_row_with_period("different_period", "2026-08-05", "2026-09-07"))

    captured: dict = {}
    monkeypatch.setattr(app_mod.regime_download_state, "start", lambda rows, **kwargs: captured.update(rows=rows))

    download_regime_reports(
        date_from="2026-08-01", date_to="2026-09-01", csv_file=[str(csv_path)],
        date_range="2026-08-01 to 2026-09-03",
    )

    picked_ids = {r["combo_id"] for r in captured["rows"]}
    assert picked_ids == {"in_period"}


def test_regime_portfolio_basket_scopes_by_date_range(tmp_path, monkeypatch):
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time",
                  "stoploss.kind", "stoploss.value", "return_max_dd", "reward_risk_ratio", "total_pnl",
                  "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(_row_with_period("in_period", "2026-08-01", "2026-09-03"))
        writer.writerow(_row_with_period("different_period", "2026-08-05", "2026-09-07"))

    regime_window_dir("2026-08-01", "2026-09-01").mkdir(parents=True, exist_ok=True)
    captured: dict = {}

    def fake_build_portfolio(rows, reports_dir, instrument, **kwargs):
        captured["rows"] = rows
        return {"buckets": {}, "portfolio": {}, "total_lots": 0, "threshold": 0.5,
                "shortlist_pool_size": 0, "dropped_for_timing": 0, "data_gaps": []}

    monkeypatch.setattr(app_mod.portfolio, "build_portfolio", fake_build_portfolio)

    regime_portfolio_basket(
        date_from="2026-08-01", date_to="2026-09-01", csv_file=[str(csv_path)], instrument="NIFTY",
        date_range="2026-08-01 to 2026-09-03",
    )

    ids = {r["combo_id"] for r in captured["rows"]}
    assert ids == {"in_period"}


def test_regime_sweep_start_scopes_by_date_range(tmp_path, monkeypatch):
    csv_path = tmp_path / "results_web_a.csv"
    fieldnames = ["combo_id", "status", "instrument", "entry_time", "exit_time",
                  "stoploss.kind", "stoploss.value", "return_max_dd", "reward_risk_ratio", "total_pnl",
                  "start_date", "end_date"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(_row_with_period("in_period", "2026-08-01", "2026-09-03"))
        writer.writerow(_row_with_period("different_period", "2026-08-05", "2026-09-07"))

    regime_window_dir("2026-08-01", "2026-09-01").mkdir(parents=True, exist_ok=True)
    captured: dict = {}

    def fake_start(rows, reports_dir, instrument, grid, **kwargs):
        captured["rows"] = rows

    monkeypatch.setattr(app_mod.regime_sweep_state, "start", fake_start)

    regime_sweep_start(
        date_from="2026-08-01", date_to="2026-09-01", csv_file=[str(csv_path)],
        threshold_min=0.5, threshold_max=0.5, threshold_step=0.05,
        top_n_min=20, top_n_max=20, top_n_step=20,
        min_lots_min=2, min_lots_max=2, min_lots_step=1,
        max_lots_min=5, max_lots_max=5, max_lots_step=1,
        date_range="2026-08-01 to 2026-09-03",
    )

    ids = {r["combo_id"] for r in captured["rows"]}
    assert ids == {"in_period"}


def test_regime_portfolio_basket_reads_from_the_regime_window_not_reports_dir(monkeypatch):
    """The whole point: build_portfolio itself is untouched, just pointed at the
    regime window's own folder instead of the stable REPORTS_DIR."""
    captured: dict = {}
    regime_window_dir("2026-08-01", "2026-09-01").mkdir(parents=True, exist_ok=True)

    def fake_build_portfolio(rows, reports_dir, instrument, **kwargs):
        captured["reports_dir"] = reports_dir
        return {"buckets": {}, "portfolio": {}, "total_lots": 0, "threshold": 0.5,
                "shortlist_pool_size": 0, "dropped_for_timing": 0, "data_gaps": []}

    monkeypatch.setattr(app_mod.portfolio, "build_portfolio", fake_build_portfolio)

    result = regime_portfolio_basket(date_from="2026-08-01", date_to="2026-09-01", csv_file=[], instrument="NIFTY")

    assert captured["reports_dir"] == regime_window_dir("2026-08-01", "2026-09-01")
    assert result["portfolio"] == {}


def test_regime_portfolio_basket_defaults_end_date_to_today(monkeypatch):
    captured: dict = {}
    today = date.today().isoformat()
    regime_window_dir("2026-08-01", today).mkdir(parents=True, exist_ok=True)

    def fake_build_portfolio(rows, reports_dir, instrument, **kwargs):
        captured["reports_dir"] = reports_dir
        return {"buckets": {}, "portfolio": {}, "total_lots": 0, "threshold": 0.5,
                "shortlist_pool_size": 0, "dropped_for_timing": 0, "data_gaps": []}

    monkeypatch.setattr(app_mod.portfolio, "build_portfolio", fake_build_portfolio)

    regime_portfolio_basket(date_from="2026-08-01", csv_file=[], instrument="NIFTY")

    assert captured["reports_dir"] == regime_window_dir("2026-08-01", today)


def test_regime_portfolio_basket_gives_a_clear_error_when_the_window_was_never_downloaded():
    """The bug this guards against: date_to left blank on both the download and a
    LATER "Recompute basket" call independently re-resolves "today" each time - if
    those two calls don't happen on the same calendar day, the compute step would
    silently look in an empty folder and report a misleading "not enough
    overlapping data" instead of the real problem."""
    with pytest.raises(HTTPException) as exc_info:
        regime_portfolio_basket(date_from="2026-01-01", date_to="2026-01-31", csv_file=[], instrument="NIFTY")
    assert exc_info.value.status_code == 400
    assert "download reports for this exact date range first" in exc_info.value.detail


def test_regime_portfolio_basket_error_lists_windows_actually_available(tmp_path, monkeypatch):
    """The exact gap this fixed: a date_to off by even a day or two from what was
    really downloaded looked identical to nothing being downloaded at all, leaving
    the user guessing random ranges against a message with no hint what would
    actually work - it must now name the real, already-downloaded window(s)."""
    real_window = regime_window_dir("2026-08-01", "2026-09-03")
    (real_window / "nifty").mkdir(parents=True)
    (real_window / "nifty" / "a.csv").write_text("x")

    with pytest.raises(HTTPException) as exc_info:
        regime_portfolio_basket(date_from="2026-08-01", date_to="2026-09-01", csv_file=[], instrument="NIFTY")

    assert "2026-08-01 to 2026-09-03" in exc_info.value.detail


def test_regime_portfolio_basket_error_says_so_when_nothing_at_all_is_downloaded():
    with pytest.raises(HTTPException) as exc_info:
        regime_portfolio_basket(date_from="2026-08-01", date_to="2026-09-01", csv_file=[], instrument="NIFTY")

    assert "No CAS window has been downloaded at all yet" in exc_info.value.detail


def test_regime_sweep_start_error_lists_windows_actually_available(tmp_path, monkeypatch):
    real_window = regime_window_dir("2026-08-01", "2026-09-03")
    (real_window / "nifty").mkdir(parents=True)
    (real_window / "nifty" / "a.csv").write_text("x")

    with pytest.raises(HTTPException) as exc_info:
        regime_sweep_start(date_from="2026-08-01", date_to="2026-09-01", csv_file=[])

    assert "2026-08-01 to 2026-09-03" in exc_info.value.detail


def test_regime_sweep_start_targets_its_own_independent_state_not_the_shared_one(monkeypatch):
    captured: dict = {}
    regime_window_dir("2026-08-01", "2026-09-01").mkdir(parents=True, exist_ok=True)

    def fake_start(rows, reports_dir, instrument, grid, **kwargs):
        captured["reports_dir"] = reports_dir
        captured["grid"] = grid

    monkeypatch.setattr(app_mod.regime_sweep_state, "start", fake_start)
    # If this endpoint ever accidentally called the SHARED sweep state instead, this
    # would blow up the test (it's never monkeypatched here) rather than silently
    # passing - a stronger guarantee than just asserting on regime_sweep_state alone.
    monkeypatch.setattr(
        app_mod.portfolio_sweep_state, "start",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch the shared portfolio_sweep_state")),
    )

    regime_sweep_start(
        date_from="2026-08-01", date_to="2026-09-01", csv_file=[],
        threshold_min=0.5, threshold_max=0.5, threshold_step=0.05,
        top_n_min=20, top_n_max=20, top_n_step=20,
        min_lots_min=2, min_lots_max=2, min_lots_step=1,
        max_lots_min=5, max_lots_max=5, max_lots_step=1,
    )

    assert captured["reports_dir"] == regime_window_dir("2026-08-01", "2026-09-01")
    assert len(captured["grid"]) == 1


def test_regime_sweep_start_gives_a_clear_error_when_the_window_was_never_downloaded():
    with pytest.raises(HTTPException) as exc_info:
        regime_sweep_start(date_from="2026-02-01", date_to="2026-02-28", csv_file=[])
    assert exc_info.value.status_code == 400
    assert "download reports for this exact date range first" in exc_info.value.detail
