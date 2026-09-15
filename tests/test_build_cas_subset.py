from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts import build_cas_subset
from scripts.build_cas_subset import (
    already_sliced_cas_combo_ids,
    build_cas_rows_for_instrument,
    cas_combo_id,
    find_source_csvs,
    load_rows_by_instrument,
    strategy_keys_already_covered_by_cas,
)
from src.correlate import parse_trade_report, trade_report_path
from src.web import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "unused_default_registry.csv")


def _write_trade_report(reports_dir: Path, instrument: str, cid: str, daily_pnl: dict[str, float]) -> None:
    path = trade_report_path(reports_dir, instrument, cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Index", "Entry Date", "Entry Time", "Exit Date", "Exit Time", "Type", "Strike", "B/S", "Qty", "Entry Price", "Exit Price", "Vix", "P/L"])
        for i, (date, pnl) in enumerate(sorted(daily_pnl.items())):
            writer.writerow([str(i), date, "9:20:00 AM", date, "3:20:00 PM", "", "", "", "", "", "", "", str(pnl)])


def _base_row(cid: str, instrument: str = "NIFTY", **overrides) -> dict:
    row = {
        "combo_id": cid, "status": "ok", "instrument": instrument,
        "start_date": "2025-01-01", "end_date": "2026-09-09",
        "dte": "0", "strategy_key": f"key_{cid}",
        "legs.0.action": "SELL", "legs.0.option_type": "CE",
        "total_pnl": "999", "max_drawdown": "-100", "win_rate": "50",
        "total_trades": "42", "expectancy": "1.23",
    }
    row.update(overrides)
    return row


def test_cas_combo_id_is_a_composite_string():
    assert cas_combo_id("abc123", "2026-08-01") == "abc123_cas20260801"


def test_load_rows_by_instrument_groups_and_dedupes(tmp_path):
    f1 = tmp_path / "results_web_a.csv"
    f2 = tmp_path / "results_web_b.csv"
    fieldnames = ["combo_id", "status", "instrument"]
    with f1.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerow({"combo_id": "n1", "status": "ok", "instrument": "NIFTY"})
        w.writerow({"combo_id": "s1", "status": "ok", "instrument": "SENSEX"})
    with f2.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerow({"combo_id": "n1", "status": "ok", "instrument": "NIFTY"})  # same combo, second file
        w.writerow({"combo_id": "n2", "status": "error", "instrument": "NIFTY"})  # not "ok" - excluded

    result = load_rows_by_instrument([f1, f2])

    assert set(result["NIFTY"].keys()) == {"n1"}  # deduped, error row excluded
    assert set(result["SENSEX"].keys()) == {"s1"}


def test_build_cas_rows_computes_metrics_and_leaves_the_rest_blank(tmp_path):
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {
        "2026-07-15": 5000.0,  # before cas-start, excluded
        "2026-08-05": 2000.0,
        "2026-08-12": -500.0,
        "2026-08-20": 3000.0,
    })
    row = _base_row("c1")

    new_rows, new_series, counts = build_cas_rows_for_instrument({"c1": row}, reports_dir, "2026-08-01")

    assert counts["computed"] == 1
    assert len(new_rows) == 1
    new_row = new_rows[0]
    assert new_row["combo_id"] == "c1_cas20260801"
    assert new_row["start_date"] == "2026-08-01"
    assert new_row["end_date"] == "2026-09-09"  # preserved from the original row, not forced
    assert float(new_row["total_pnl"]) == 4500.0
    assert float(new_row["max_profit"]) == 3000.0
    assert float(new_row["max_loss"]) == -500.0
    assert new_row["trade_days"] == 3
    # Per-trade concepts we can't derive from daily-netted data - left blank,
    # not carried over stale from the original full-window row.
    assert new_row["total_trades"] == ""
    assert new_row["expectancy"] == ""
    # The ORIGINAL row (and its report) must be completely untouched.
    assert row["combo_id"] == "c1"
    assert row["total_pnl"] == "999"
    assert "c1_cas20260801" in new_series
    assert parse_trade_report(trade_report_path(reports_dir, "NIFTY", "c1")) == {
        "2026-07-15": 5000.0, "2026-08-05": 2000.0, "2026-08-12": -500.0, "2026-08-20": 3000.0,
    }


def test_build_cas_rows_deducts_prorated_brokerage_and_taxes(tmp_path):
    """The real bug this was fixed for: a trade report's raw P/L is gross of
    brokerage/taxes, but every other row in this app's total_pnl is net of
    both - confirmed live (summing a real combo's full-range raw P/L and
    comparing to its own scraped total_pnl showed the gap was exactly its
    recorded brokerage+taxes, to a few paise on a multi-lakh total). Without
    this deduction, a CAS row's total_pnl (and everything derived from it) was
    systematically overstated."""
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {
        "2026-08-05": 2000.0,
        "2026-08-12": -500.0,
        "2026-08-20": 3000.0,
    })
    # Original row: 42 trades total, brokerage 4200 + taxes 2100 across all of
    # them -> 100 + 50 = 150 per trade-day to deduct from each of the 3 window
    # days below.
    row = _base_row("c1", total_trades="42", brokerage_amount="4200", taxes_charges_amount="2100")

    new_rows, new_series, counts = build_cas_rows_for_instrument({"c1": row}, reports_dir, "2026-08-01")

    assert counts["computed"] == 1
    new_row = new_rows[0]
    # (2000-150) + (-500-150) + (3000-150) = 1850 + (-650) + 2850 = 4050
    assert float(new_row["total_pnl"]) == 4050.0
    assert float(new_row["max_profit"]) == 2850.0  # 3000 - 150, not the raw 3000
    assert float(new_row["max_loss"]) == -650.0  # -500 - 150, not the raw -500
    # Prorated across just this window's 3 trade-days, not the original 42.
    assert float(new_row["brokerage_amount"]) == 300.0  # 100/trade * 3 days
    assert float(new_row["taxes_charges_amount"]) == 150.0  # 50/trade * 3 days


def test_build_cas_rows_leaves_charges_blank_when_original_row_has_no_total_trades(tmp_path):
    """Can't prorate without knowing how many trades the charges were spread
    across in the first place - 0 deduction and blank fields, not a guess."""
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {"2026-08-05": 2000.0})
    row = _base_row("c1", total_trades="", brokerage_amount="4200", taxes_charges_amount="2100")

    new_rows, _, _ = build_cas_rows_for_instrument({"c1": row}, reports_dir, "2026-08-01")

    new_row = new_rows[0]
    assert float(new_row["total_pnl"]) == 2000.0  # undeducted - nothing to prorate from
    assert new_row["brokerage_amount"] == ""
    assert new_row["taxes_charges_amount"] == ""


def test_build_cas_rows_skips_a_combo_whose_own_backtest_already_starts_on_or_after_cas_start(tmp_path):
    """The actual gap this was fixed for: a combo from a sweep that was itself
    only ever run from 2026-08-01 onward has nothing before the cutoff to slice
    out - its report already IS exactly the CAS window, so a "_cas" copy of it
    would just duplicate data already sitting there under its own combo_id."""
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {"2026-08-05": 1000.0, "2026-08-12": 500.0})
    row = _base_row("c1", start_date="2026-08-01")  # already starts exactly at the cutoff

    new_rows, new_series, counts = build_cas_rows_for_instrument({"c1": row}, reports_dir, "2026-08-01")

    assert new_rows == []
    assert new_series == {}
    assert counts["already_cas_only"] == 1
    assert counts["computed"] == 0


def test_strategy_keys_already_covered_by_cas_reads_the_registry_not_the_scanned_files(tmp_path, monkeypatch):
    """The real gap this was fixed for: a manually-renamed execution file (e.g.
    "..._sensex_0dte_cas.csv") is invisible to BOTH this script's own file-glob
    and the Analyze page's own file picker (both use the same strict
    auto-generated-name pattern) - but its combos are still sitting in the
    registry regardless, since every sweep/Force re-download upserts into it
    directly. This is what lets the check see them anyway."""
    reports_dir = tmp_path / "trade_reports"
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    registry.upsert_rows({"dedicated_cas_cid": {
        "combo_id": "dedicated_cas_cid", "instrument": "NIFTY",
        "strategy_key": "shared_key", "start_date": "2026-08-01",
    }})
    _write_trade_report(reports_dir, "NIFTY", "dedicated_cas_cid", {"2026-08-10": 2000.0})

    covered = strategy_keys_already_covered_by_cas(reports_dir, "2026-08-01")

    assert covered == {"shared_key"}


def test_strategy_keys_already_covered_by_cas_requires_an_actual_downloaded_report(tmp_path, monkeypatch):
    reports_dir = tmp_path / "trade_reports"
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    registry.upsert_rows({"no_report_cid": {
        "combo_id": "no_report_cid", "instrument": "NIFTY",
        "strategy_key": "shared_key", "start_date": "2026-08-01",
    }})
    # deliberately no trade report written

    covered = strategy_keys_already_covered_by_cas(reports_dir, "2026-08-01")

    assert covered == set()


def test_build_cas_rows_skips_a_long_dated_combo_whose_strategy_already_has_a_real_cas_backtest(tmp_path):
    """The deeper case this was actually fixed for: a DIFFERENT combo_id (a
    dedicated CAS-only sweep run separately) covering the exact same strategy_key
    already has a genuine, downloaded CAS-window backtest - combo_id alone can
    never catch this (a different date range hashes completely differently), so
    the cross-check has to go through strategy_key."""
    reports_dir = tmp_path / "trade_reports"
    long_row = _base_row("long_cid", start_date="2025-01-01", strategy_key="shared_key")
    _write_trade_report(reports_dir, "NIFTY", "long_cid", {"2025-06-01": 1000.0, "2026-08-05": 500.0})

    new_rows, new_series, counts = build_cas_rows_for_instrument(
        {"long_cid": long_row}, reports_dir, "2026-08-01", already_covered={"shared_key"}
    )

    assert new_rows == []
    assert counts["strategy_already_has_cas_backtest"] == 1


def test_already_sliced_cas_combo_ids_reads_prior_cas_all_files(tmp_path):
    """The whole point: re-running the tool anytime, over every CSV in output/,
    must never re-slice a combo it already sliced in an earlier run - found by
    scanning this tool's own PAST OUTPUT files, not the registry (that's a
    separate, unrelated check - see strategy_keys_already_covered_by_cas)."""
    cas_file = tmp_path / "results_web_20260101_000000_nifty_cas_all.csv"
    with cas_file.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["combo_id", "status"])
        w.writeheader()
        w.writerow({"combo_id": "abc123_cas20260801", "status": "ok"})

    # A non-CAS file in the same directory must not be scanned as if it were
    # prior output - only "*_cas_all.csv" files count.
    (tmp_path / "results_web_20260102_000000.csv").write_text("combo_id,status\nxyz,ok\n")

    ids = already_sliced_cas_combo_ids(tmp_path)
    assert ids == {"abc123_cas20260801"}


def test_build_cas_rows_skips_a_combo_already_sliced_previously(tmp_path):
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {"2026-08-05": 1000.0})
    row = _base_row("c1")

    new_rows, new_series, counts = build_cas_rows_for_instrument(
        {"c1": row}, reports_dir, "2026-08-01", already_sliced={"c1_cas20260801"}
    )

    assert new_rows == []
    assert counts["already_sliced_previously"] == 1
    assert counts["computed"] == 0


def test_build_cas_rows_still_slices_when_not_already_sliced(tmp_path):
    """Sanity check the previous test isn't vacuously true - an UNRELATED
    already_sliced id doesn't block this combo."""
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {"2026-08-05": 1000.0})
    row = _base_row("c1")

    new_rows, new_series, counts = build_cas_rows_for_instrument(
        {"c1": row}, reports_dir, "2026-08-01", already_sliced={"some_other_cid_cas20260801"}
    )

    assert counts["computed"] == 1
    assert new_rows[0]["combo_id"] == "c1_cas20260801"


def test_build_cas_rows_still_slices_when_the_strategy_is_not_covered(tmp_path):
    reports_dir = tmp_path / "trade_reports"
    long_row = _base_row("long_cid", start_date="2025-01-01", strategy_key="shared_key")
    _write_trade_report(reports_dir, "NIFTY", "long_cid", {"2026-08-05": 1000.0})

    new_rows, new_series, counts = build_cas_rows_for_instrument(
        {"long_cid": long_row}, reports_dir, "2026-08-01", already_covered=set()
    )

    assert counts["computed"] == 1
    assert new_rows[0]["combo_id"] == "long_cid_cas20260801"


def test_build_cas_rows_still_computes_when_start_date_is_strictly_before_cas_start(tmp_path):
    """Sanity check for the boundary itself - one day earlier must still slice
    normally, not get swept up in the same-or-after skip."""
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {"2026-07-31": 200.0, "2026-08-05": 1000.0})
    row = _base_row("c1", start_date="2026-07-31")

    new_rows, new_series, counts = build_cas_rows_for_instrument({"c1": row}, reports_dir, "2026-08-01")

    assert counts["computed"] == 1
    assert len(new_rows) == 1


def test_build_cas_rows_skips_combo_with_no_downloaded_report(tmp_path):
    reports_dir = tmp_path / "trade_reports"
    new_rows, new_series, counts = build_cas_rows_for_instrument({"c1": _base_row("c1")}, reports_dir, "2026-08-01")
    assert new_rows == []
    assert counts["no_report"] == 1


def test_build_cas_rows_skips_combo_with_nothing_since_cas_start(tmp_path):
    reports_dir = tmp_path / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "c1", {"2026-05-01": 100.0})
    new_rows, new_series, counts = build_cas_rows_for_instrument({"c1": _base_row("c1")}, reports_dir, "2026-08-01")
    assert new_rows == []
    assert counts["no_trades_in_window"] == 1


def test_main_dry_run_writes_no_files(tmp_path, monkeypatch, capsys):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    csv_path = output_dir / "results_web_20260101_000000.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["combo_id", "status", "instrument", "start_date", "end_date"])
        w.writeheader()
        w.writerow({"combo_id": "c1", "status": "ok", "instrument": "NIFTY", "start_date": "2025-01-01", "end_date": "2026-09-09"})
    _write_trade_report(output_dir / "trade_reports", "NIFTY", "c1", {"2026-08-05": 1000.0})

    monkeypatch.setattr("sys.argv", ["build_cas_subset.py", "--output-dir", str(output_dir)])
    result = build_cas_subset.main()

    assert result == 0
    assert not any(output_dir.glob("*cas_all*"))
    assert "Dry run only" in capsys.readouterr().out


def test_main_apply_writes_per_instrument_files_and_sliced_reports(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    csv_path = output_dir / "results_web_20260101_000000.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["combo_id", "status", "instrument", "start_date", "end_date"])
        w.writeheader()
        w.writerow({"combo_id": "n1", "status": "ok", "instrument": "NIFTY", "start_date": "2025-01-01", "end_date": "2026-09-09"})
        w.writerow({"combo_id": "s1", "status": "ok", "instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09"})
        w.writerow({"combo_id": "n2", "status": "ok", "instrument": "NIFTY", "start_date": "2025-01-01", "end_date": "2026-09-09"})  # no report - excluded

    reports_dir = output_dir / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "n1", {"2026-08-05": 1000.0})
    _write_trade_report(reports_dir, "SENSEX", "s1", {"2026-08-05": 2000.0})

    monkeypatch.setattr("sys.argv", ["build_cas_subset.py", "--output-dir", str(output_dir), "--apply"])
    result = build_cas_subset.main()
    assert result == 0

    nifty_files = list(output_dir.glob("*_nifty_cas_all.csv"))
    sensex_files = list(output_dir.glob("*_sensex_cas_all.csv"))
    assert len(nifty_files) == 1
    assert len(sensex_files) == 1

    with nifty_files[0].open(newline="") as f:
        nifty_rows = list(csv.DictReader(f))
    assert len(nifty_rows) == 1
    assert nifty_rows[0]["combo_id"] == "n1_cas20260801"

    # The new sliced trade report must exist, separate from the original.
    assert trade_report_path(reports_dir, "NIFTY", "n1_cas20260801").exists()
    assert trade_report_path(reports_dir, "NIFTY", "n1").exists()  # original untouched


def test_main_rerun_over_everything_never_duplicates_an_already_sliced_combo(tmp_path, monkeypatch):
    """The actual feature this was built for: re-running the tool anytime over
    every CSV in output/ (not remembering which ones were already handled)
    must not create a duplicate row for a combo it already sliced last time -
    the second run's own new file must be empty/absent for that combo."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    csv_path = output_dir / "results_web_20260101_000000.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["combo_id", "status", "instrument", "start_date", "end_date"])
        w.writeheader()
        w.writerow({"combo_id": "n1", "status": "ok", "instrument": "NIFTY", "start_date": "2025-01-01", "end_date": "2026-09-09"})

    reports_dir = output_dir / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "n1", {"2026-08-05": 1000.0})

    monkeypatch.setattr("sys.argv", ["build_cas_subset.py", "--output-dir", str(output_dir), "--apply"])
    assert build_cas_subset.main() == 0
    first_run_files = list(output_dir.glob("*_nifty_cas_all.csv"))
    assert len(first_run_files) == 1

    # Re-run with nothing new added to source data - same combo, same cutoff.
    assert build_cas_subset.main() == 0
    second_run_files = [p for p in output_dir.glob("*_nifty_cas_all.csv") if p not in first_run_files]
    # Either no second file was written at all, or it exists but is empty of
    # rows - either way, n1's combo must never appear a second time anywhere.
    all_cas_rows = []
    for p in output_dir.glob("*_nifty_cas_all.csv"):
        with p.open(newline="") as f:
            all_cas_rows.extend(csv.DictReader(f))
    n1_rows = [r for r in all_cas_rows if r["combo_id"] == "n1_cas20260801"]
    assert len(n1_rows) == 1


def test_main_end_to_end_skips_a_combo_whose_strategy_is_covered_via_the_registry(tmp_path, monkeypatch):
    """Full CLI path: a combo living ONLY in combo_registry.csv (simulating a
    manually-renamed execution file this script's own glob can't see) with an
    already-downloaded CAS-window report must still suppress a long-dated combo
    sharing its strategy_key, end to end through main()."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    csv_path = output_dir / "results_web_20260101_000000.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["combo_id", "status", "instrument", "start_date", "end_date", "strategy_key"])
        w.writeheader()
        w.writerow({"combo_id": "long_cid", "status": "ok", "instrument": "NIFTY", "start_date": "2025-01-01", "end_date": "2026-09-09", "strategy_key": "shared_key"})

    reports_dir = output_dir / "trade_reports"
    _write_trade_report(reports_dir, "NIFTY", "long_cid", {"2025-06-01": 100.0, "2026-08-05": 1000.0})
    _write_trade_report(reports_dir, "NIFTY", "dedicated_cas_cid", {"2026-08-10": 2000.0})

    monkeypatch.setattr(registry, "REGISTRY_PATH", output_dir / "combo_registry.csv")
    registry.upsert_rows({"dedicated_cas_cid": {
        "combo_id": "dedicated_cas_cid", "instrument": "NIFTY",
        "strategy_key": "shared_key", "start_date": "2026-08-01",
    }})

    monkeypatch.setattr("sys.argv", ["build_cas_subset.py", "--output-dir", str(output_dir), "--apply"])
    result = build_cas_subset.main()
    assert result == 0

    nifty_files = list(output_dir.glob("*_nifty_cas_all.csv"))
    assert nifty_files == []  # long_cid was the only candidate, and it got suppressed


def test_main_returns_1_when_no_source_csvs_found(tmp_path, monkeypatch):
    output_dir = tmp_path / "empty_output"
    output_dir.mkdir()
    monkeypatch.setattr("sys.argv", ["build_cas_subset.py", "--output-dir", str(output_dir)])
    assert build_cas_subset.main() == 1


def test_find_source_csvs_includes_a_manually_renamed_or_previously_generated_file(tmp_path):
    """The actual bug this was fixed for: a descriptive suffix after the
    timestamp (a manually renamed "..._sensex_0dte_cas.csv", or this script's
    own past output, "..._nifty_cas_all.csv") used to make a file invisible to
    this exact discovery step - confirmed live, so combos in those files were
    silently never even considered as CAS-subset candidates or, for this
    script's own re-runs, as sources at all. Same pattern (and fix) as
    src/web/app.py's list_result_csvs."""
    (tmp_path / "results_web_20260101_000000.csv").write_text("combo_id,status\n")
    (tmp_path / "results_web_20260102_000000_sensex_0dte_cas.csv").write_text("combo_id,status\n")
    (tmp_path / "results_web_20260103_000000_nifty_cas_all.csv").write_text("combo_id,status\n")
    (tmp_path / "results_web_20260104_000000.pre-dte-backfill-backup.csv").write_text("combo_id,status\n")

    found = {p.name for p in find_source_csvs(tmp_path)}

    assert found == {
        "results_web_20260101_000000.csv",
        "results_web_20260102_000000_sensex_0dte_cas.csv",
        "results_web_20260103_000000_nifty_cas_all.csv",
    }
