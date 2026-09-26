from __future__ import annotations

import csv
from pathlib import Path

import src.web.app as app_mod


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "combo_id", "status", "instrument", "entry_time", "exit_time", "start_date",
        "return_max_dd", "reward_risk_ratio", "total_pnl", "max_drawdown",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_correlate_top_rows_respects_date_from(tmp_path):
    """Confirmed live: neither /api/correlate/download nor /api/correlate/compute
    declared date_range/date_from/date_to in their own signature, so FastAPI
    silently dropped those query params even though the page's "Backtest date
    from" filter sends them (same as every other analyze endpoint) - the
    top-N candidate pool Uncorrelated strategies downloads/correlates was drawn
    from the ENTIRE loaded file, ignoring the filter entirely. _correlate_top_rows
    is the shared helper both endpoints funnel through - this is the fix."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {
            "combo_id": "old1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20", "start_date": "2025-01-01",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
        },
        {
            "combo_id": "new1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:20", "exit_time": "11:25", "start_date": "2026-08-05",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
        },
    ])

    unscoped = app_mod._correlate_top_rows(
        20, None, None, "combined", None, "NIFTY", 0.65, [str(csv_path)],
    )
    assert {r["combo_id"] for r in unscoped} == {"old1", "new1"}

    scoped = app_mod._correlate_top_rows(
        20, None, None, "combined", None, "NIFTY", 0.65, [str(csv_path)],
        date_from="2026-08-01",
    )
    assert {r["combo_id"] for r in scoped} == {"new1"}


def test_compute_correlate_endpoint_forwards_date_from(tmp_path, monkeypatch):
    """End-to-end through the actual endpoint function (not just the shared
    helper) - the old1/pre-cutoff row must never reach compute_correlation at
    all when date_from is set, matching /api/analyze/portfolio-basket's own
    date_from wiring (see that endpoint's own comment in this same file)."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {
            "combo_id": "old1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:17", "exit_time": "11:20", "start_date": "2025-01-01",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
        },
        {
            "combo_id": "new1", "status": "ok", "instrument": "NIFTY",
            "entry_time": "09:20", "exit_time": "11:25", "start_date": "2026-08-05",
            "return_max_dd": "10", "reward_risk_ratio": "1.5", "total_pnl": "2000", "max_drawdown": "-1000",
        },
    ])

    seen_rows = {}

    def fake_compute_correlation(rows, *, threshold):
        seen_rows["combo_ids"] = {r["combo_id"] for r in rows}
        return {"basket": [], "labels": {}, "matrix": {}, "skipped": [], "missing": [],
                "excluded_no_stop_loss": [], "excluded_not_profitable": [], "data_gaps": [],
                "threshold": threshold, "basket_portfolio": None}

    monkeypatch.setattr(app_mod, "compute_correlation", fake_compute_correlation)

    app_mod.compute_correlate(
        csv_file=[str(csv_path)], instrument="NIFTY", date_from="2026-08-01",
    )
    assert seen_rows["combo_ids"] == {"new1"}
