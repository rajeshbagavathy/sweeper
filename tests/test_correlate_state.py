from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from src.web import correlate_state as cs


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


def _row(cid: str, instrument: str = "NIFTY", **extra) -> dict:
    row = {
        "combo_id": cid,
        "instrument": instrument,
        "entry_time": "09:20",
        "exit_time": "15:10",
        "return_max_dd": "5.0",
        "reward_risk_ratio": "1.2",
        # has_hard_stop_loss default: a plain overall Stop Loss, like any normal
        # combo - so these tests aren't tripped up by the "no SL at all" exclusion
        # unless a test deliberately overrides it (pass stoploss_kind="" to omit).
        "stoploss.kind": "amount",
        "stoploss.value": "7000",
    }
    row.update(extra)
    return row


def test_label_for_includes_instrument_time_window_and_dte():
    label = cs.label_for(_row("a", dte="0"))
    assert label == "NIFTY | 09:20-15:10 | DTE 0"


def test_label_for_omits_dte_when_absent():
    label = cs.label_for(_row("a"))
    assert label == "NIFTY | 09:20-15:10"


def test_compute_correlation_reports_missing_rows_without_dropping_available_ones(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    dates = [(str(i + 1), f"2025-09-{(i % 28) + 1:02d}", str(float(i * 10 - 30))) for i in range(6)]
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "present1"), dates)
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "present2"), dates)

    rows = [_row("present1"), _row("present2"), _row("absent1")]
    result = cs.compute_correlation(rows, threshold=0.5)

    assert set(result["labels"]) == {"present1", "present2"}
    assert result["missing"] == [{"combo_id": "absent1", "label": cs.label_for(_row("absent1"))}]
    assert result["matrix"]["present1"]["present2"] == result["matrix"]["present2"]["present1"]
    # present1/present2 share identical daily P/L, so they're perfectly correlated -
    # the diversifier keeps only one in the basket at threshold 0.5, and
    # basket_portfolio must reflect exactly that one combo's series, not both summed.
    assert len(result["basket"]) == 1
    single_combo_total = sum(float(v) for _, _, v in dates)
    assert result["basket_portfolio"]["overall_profit"] == pytest.approx(single_combo_total)
    # Each basket entry carries its own entry/exit time - lets the UI show when a
    # recommended pick actually runs without a second row lookup.
    assert result["basket"][0]["entry_time"] == "09:20"
    assert result["basket"][0]["exit_time"] == "15:10"


def test_compute_correlation_basket_entry_time_reflects_that_specific_combo(monkeypatch, tmp_path):
    """Two decorrelated combos with different entry/exit times must each keep their
    own, not the other's or a shared default."""
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    dates_a = [(str(i + 1), f"2025-09-{(i % 28) + 1:02d}", str(float(i))) for i in range(6)]
    dates_b = [(str(i + 1), f"2025-09-{(i % 28) + 1:02d}", str(float(-i))) for i in range(6)]
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "morning"), dates_a)
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "afternoon"), dates_b)

    rows = [
        _row("morning", entry_time="09:20", exit_time="12:45"),
        _row("afternoon", entry_time="13:45", exit_time="15:14"),
    ]
    result = cs.compute_correlation(rows, threshold=0.9)
    by_id = {b["combo_id"]: b for b in result["basket"]}
    assert by_id["morning"]["entry_time"] == "09:20"
    assert by_id["morning"]["exit_time"] == "12:45"
    assert by_id["afternoon"]["entry_time"] == "13:45"
    assert by_id["afternoon"]["exit_time"] == "15:14"


def test_compute_correlation_all_missing_returns_empty_matrix_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    rows = [_row("a"), _row("b")]
    result = cs.compute_correlation(rows)
    assert result["labels"] == {}
    assert result["basket"] == []
    assert {m["combo_id"] for m in result["missing"]} == {"a", "b"}
    assert result["basket_portfolio"]["num_periods"] == 0


def test_compute_correlation_excludes_combos_with_no_hard_stop_loss(monkeypatch, tmp_path):
    """A trail-only (or no-SL-at-all) combo must never be ranked into the basket,
    even if it's the only candidate with a downloaded report - reported in
    excluded_no_stop_loss instead, same treatment as a missing report."""
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    dates = [(str(i + 1), f"2025-09-{(i % 28) + 1:02d}", "100") for i in range(6)]
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "no_sl"), dates)

    rows = [_row("no_sl", **{"stoploss.kind": "", "stoploss.value": ""})]
    result = cs.compute_correlation(rows, threshold=0.5)

    assert result["basket"] == []
    assert result["labels"] == {}
    assert result["excluded_no_stop_loss"] == [{"combo_id": "no_sl", "label": cs.label_for(rows[0])}]


def test_compute_correlation_surfaces_data_gaps(monkeypatch, tmp_path):
    """A candidate whose report stops short of its peers' most recent expiry is
    flagged, same check the Portfolio feature uses (src.web.portfolio.data_coverage_gaps)."""
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    full_dates = [(str(i + 1), f"2025-09-{i+1:02d}", "100") for i in range(6)]
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "full"), full_dates)
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "short"), full_dates[:4])

    rows = [_row("full"), _row("short")]
    result = cs.compute_correlation(rows, threshold=0.9)

    gap_ids = {g["combo_id"] for g in result["data_gaps"]}
    assert gap_ids == {"short"}


def test_compute_correlation_basket_portfolio_deducts_charges_when_present(monkeypatch, tmp_path):
    """A row carrying brokerage_amount/taxes_charges_amount must have them actually
    reflected in the combined basket_portfolio, not just parsed and discarded -
    same fix as build_portfolio's, applied here for the single-session basket too."""
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    dates = [(str(i + 1), f"2025-09-{(i % 28) + 1:02d}", "100") for i in range(6)]
    _write_report(cs.trade_report_path(tmp_path, "NIFTY", "sm1"), dates)

    row = _row("sm1", **{"stoploss.kind": "amount", "stoploss.value": "7000"})
    with_charges = cs.compute_correlation(
        [dict(row, **{"brokerage_amount": "50", "taxes_charges_amount": "10"})], threshold=0.5
    )
    without_charges = cs.compute_correlation([row], threshold=0.5)

    assert with_charges["basket_portfolio"]["overall_profit"] < without_charges["basket_portfolio"]["overall_profit"]


def test_should_skip_download_missing_report_never_skipped(tmp_path):
    target = tmp_path / "nifty" / "missing.csv"
    assert cs.should_skip_download(target, force=False) is False
    assert cs.should_skip_download(target, force=True) is False


def test_should_skip_download_without_force_skips_any_existing_report(tmp_path):
    """Today's behavior, unchanged: no force means the cache alone decides -
    however old the file, it's left alone."""
    target = tmp_path / "old.csv"
    target.write_text("x")
    old = time.time() - cs.FORCE_SKIP_IF_NEWER_THAN_S * 10
    os.utime(target, (old, old))
    assert cs.should_skip_download(target, force=False) is True


def test_should_skip_download_with_force_skips_report_newer_than_window(tmp_path):
    """The fix this test guards: a report downloaded very recently (e.g. by a
    "Force re-download & update results" run that got interrupted and restarted)
    must be left alone even with force on - so restarting doesn't replay work
    that was already just redone."""
    target = tmp_path / "fresh.csv"
    target.write_text("x")
    assert cs.should_skip_download(target, force=True) is True


def test_should_skip_download_with_force_replays_report_older_than_window(tmp_path):
    """The whole point of force: a report from a genuinely stale earlier sweep
    still gets replayed, not skipped just because a file happens to exist."""
    target = tmp_path / "stale.csv"
    target.write_text("x")
    old = time.time() - cs.FORCE_SKIP_IF_NEWER_THAN_S - 60
    os.utime(target, (old, old))
    assert cs.should_skip_download(target, force=True) is False


def test_roll_date_window_shifts_both_dates_forward_keeping_length():
    """The fix this guards: force-replaying a combo used to redo the exact same
    stale window forever, since row_to_combo just copies whatever start_date/
    end_date the row was first swept with. A stale year-long window ending
    2026-08-22 must roll to one still a year long, ending today."""
    combo = {"start_date": "2025-08-22", "end_date": "2026-08-22"}
    cs.roll_date_window(combo, today=date(2026, 8, 31))
    assert combo == {"start_date": "2025-08-31", "end_date": "2026-08-31"}


def test_roll_date_window_is_a_noop_when_already_current():
    """A combo whose window already ends today (or later) isn't touched - nothing
    stale to roll forward."""
    combo = {"start_date": "2025-08-22", "end_date": "2026-08-31"}
    cs.roll_date_window(combo, today=date(2026, 8, 31))
    assert combo == {"start_date": "2025-08-22", "end_date": "2026-08-31"}


def test_row_dte_values_parses_a_single_value():
    assert cs.row_dte_values({"dte": "0"}, fallback=[9]) == [0]


def test_row_dte_values_parses_a_comma_separated_list():
    assert cs.row_dte_values({"dte": "0,1,2,3,4"}, fallback=[9]) == [0, 1, 2, 3, 4]


def test_row_dte_values_falls_back_when_blank():
    assert cs.row_dte_values({"dte": ""}, fallback=[9]) == [9]
    assert cs.row_dte_values({}, fallback=[9]) == [9]


def test_row_dte_values_falls_back_on_unparseable_value():
    assert cs.row_dte_values({"dte": "not-a-number"}, fallback=[9]) == [9]


def _dte_row(cid: str) -> dict:
    return _row(cid, start_date="2025-01-01", end_date="2025-12-31")


def test_run_force_refresh_logs_old_and_new_date_window(tmp_path, monkeypatch):
    """B4: a force-refresh that actually rolls a combo's date window forward must
    append a durable, before/after record to the refresh audit log - the one
    piece of history output/run.log's own per-combo ok/error pings never
    capture (confirmed live this session: "the starting and ending times are
    not matching at all" had no way to be answered otherwise)."""
    from src.web.models import SweepUIConfig

    audit_log = tmp_path / "refresh_audit.log"
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cs, "REFRESH_AUDIT_LOG_PATH", audit_log)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())

    def fake(work_items, selectors, reports_dir, **kwargs):
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = cs.CorrelateState()
    state.start([_dte_row("combo1")], force=True)  # start/end 2025 - genuinely in the past
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    lines = audit_log.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["combo_id"] == "combo1"
    assert entry["old_start_date"] == "2025-01-01"
    assert entry["old_end_date"] == "2025-12-31"
    assert entry["new_end_date"] == date.today().isoformat()
    assert "ts" in entry


def test_run_reports_rolled_forward_combos_in_the_live_snapshot(tmp_path, monkeypatch):
    """Same event test_run_force_refresh_logs_old_and_new_date_window checks gets
    written to refresh_audit.log must ALSO be visible in the live status response -
    the UI needs to explain "why don't all my reports have the same period now" at
    the moment it happens, not only after the fact by reading a log file."""
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cs, "REFRESH_AUDIT_LOG_PATH", tmp_path / "refresh_audit.log")
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda *a, **k: {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": []},
    )

    state = cs.CorrelateState()
    state.start([_dte_row("combo1")], force=True)  # start/end 2025 - genuinely in the past
    state._thread.join(timeout=5)

    snap = state.snapshot()
    assert snap["status"] == "done"
    assert len(snap["rolled_forward"]) == 1
    entry = snap["rolled_forward"][0]
    assert entry["combo_id"] == "combo1"
    assert entry["old_start"] == "2025-01-01"
    assert entry["old_end"] == "2025-12-31"
    assert entry["new_end"] == date.today().isoformat()


def test_run_rolled_forward_is_empty_when_nothing_needed_rolling(tmp_path, monkeypatch):
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cs, "REFRESH_AUDIT_LOG_PATH", tmp_path / "refresh_audit.log")
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda *a, **k: {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": []},
    )

    today = date.today().isoformat()
    row = _row("current_combo", start_date="2026-01-01", end_date=today)

    state = cs.CorrelateState()
    state.start([row], force=True)
    state._thread.join(timeout=5)

    assert state.snapshot()["rolled_forward"] == []


def test_run_no_log_entry_when_window_already_current(tmp_path, monkeypatch):
    """roll_date_window is a no-op when the window already ends today - nothing
    actually changed, so nothing should be logged (a log full of "refreshed,
    same as before" entries would defeat the point of it being a real history)."""
    from src.web.models import SweepUIConfig

    audit_log = tmp_path / "refresh_audit.log"
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cs, "REFRESH_AUDIT_LOG_PATH", audit_log)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", lambda *a, **k: {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": []})

    today = date.today().isoformat()
    row = _row("current_combo", start_date="2026-01-01", end_date=today)

    state = cs.CorrelateState()
    state.start([row], force=True)
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    assert not audit_log.exists()


def test_run_no_log_entry_when_force_is_false(tmp_path, monkeypatch):
    """roll_date_window (and therefore any log entry) only ever happens under
    force=True - a plain download never touches an existing combo's window."""
    from src.web.models import SweepUIConfig

    audit_log = tmp_path / "refresh_audit.log"
    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cs, "REFRESH_AUDIT_LOG_PATH", audit_log)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", lambda *a, **k: {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": []})

    state = cs.CorrelateState()
    state.start([_dte_row("combo1")], force=False)
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    assert not audit_log.exists()


def test_start_parallelism_overrides_win_over_saved_config(tmp_path, monkeypatch):
    """These overrides exist specifically so this job's parallelism never depends
    on whatever's saved in config/sweep_ui.yaml or shown on the homepage (see
    start()'s own docstring for the exact incident) - an explicit override here
    must reach run_correlate_multiprocess regardless of what the saved config says."""
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig(parallelism=7, parallelism_account2=7, parallelism_account3=0))
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    seen_kwargs: dict = {}

    def fake(work_items, selectors, reports_dir, **kwargs):
        seen_kwargs.update(kwargs)
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = cs.CorrelateState()
    state.start([_dte_row("a")], parallelism=2, parallelism_account2=0)
    state._thread.join(timeout=5)

    assert seen_kwargs["parallelism"] == 2
    assert state.snapshot()["status"] == "done"


def test_start_parallelism_left_unset_falls_back_to_saved_config(tmp_path, monkeypatch):
    """Byte-for-byte the old behavior when no override is given at all - every
    existing caller that doesn't pass these kwargs must be unaffected."""
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig(parallelism=7, parallelism_account2=7, parallelism_account3=0))
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    seen_kwargs: dict = {}

    def fake(work_items, selectors, reports_dir, **kwargs):
        seen_kwargs.update(kwargs)
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = cs.CorrelateState()
    state.start([_dte_row("a")])
    state._thread.join(timeout=5)

    assert seen_kwargs["parallelism"] == 7


def test_run_isolates_a_dte_variant_combo_to_just_its_own_dte(tmp_path, monkeypatch):
    """A "_dte0" composite id (see src/runner.py's capture-individual-DTE feature)
    must be replayed with dte_values=[0] only - NOT the config's own multi-select
    dte_values (e.g. [0, 1, 2]) - or Force re-download's own report silently mixes
    in other DTEs' trades and no longer matches what a "_dte0" id promises."""
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig(dte_values=[0, 1, 2]))
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    calls: list[dict] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append({"dte_values": kwargs["dte_values"], "cids": [item[1] for item in work_items]})
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = cs.CorrelateState()
    state.start([_dte_row("base_combo"), _dte_row("variant_combo_dte0")])
    state._thread.join(timeout=5)

    by_cids = {tuple(sorted(c["cids"])): c["dte_values"] for c in calls}
    assert by_cids[("base_combo",)] == [0, 1, 2]
    assert by_cids[("variant_combo_dte0",)] == [0]
    assert state.snapshot()["status"] == "done"


def test_run_bare_combo_uses_its_own_recorded_dte_not_the_saved_configs(tmp_path, monkeypatch):
    """The bare-id counterpart of the test above, and the exact bug this fixed:
    a row whose OWN "dte" column says "0" (e.g. isolated via the results table's
    DTE=0 filter) must replay with dte_values=[0] - NOT whatever the CURRENTLY
    SAVED sweep config's dte_values happens to be right now, which can have since
    changed to something broader and silently mix in other DTEs' trades."""
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    # The saved config has since moved on to a broad multi-select - exactly the
    # real-world state that caused this bug.
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig(dte_values=[0, 1, 2, 3, 4]))
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    calls: list[dict] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append({"dte_values": kwargs["dte_values"], "cids": [item[1] for item in work_items]})
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    single_dte_row = _dte_row("only_dte0_row")
    single_dte_row["dte"] = "0"
    multi_dte_row = _dte_row("multi_dte_row")
    multi_dte_row["dte"] = "0,1,2,3,4"

    state = cs.CorrelateState()
    state.start([single_dte_row, multi_dte_row])
    state._thread.join(timeout=5)

    by_cids = {tuple(sorted(c["cids"])): c["dte_values"] for c in calls}
    assert by_cids[("only_dte0_row",)] == [0], "must replay isolated to DTE 0, not the saved config's [0,1,2,3,4]"
    assert by_cids[("multi_dte_row",)] == [0, 1, 2, 3, 4], "a genuinely multi-DTE row keeps its own full set"
    assert state.snapshot()["status"] == "done"


def test_run_bare_combo_with_no_dte_column_falls_back_to_saved_config(tmp_path, monkeypatch):
    """An older row from before DTE tracking existed (blank "dte") has nothing of
    its own to go on - the saved config is the only reasonable fallback left."""
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig(dte_values=[1, 2]))
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    calls: list[dict] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append(kwargs["dte_values"])
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    row = _dte_row("no_dte_column")
    row["dte"] = ""

    state = cs.CorrelateState()
    state.start([row])
    state._thread.join(timeout=5)

    assert calls == [[1, 2]]


def test_run_merges_force_refreshed_rows_back_with_one_batched_write_per_file(tmp_path, monkeypatch):
    """Regression guard for the exact incident this fixed: merging N freshly
    re-scraped rows back into their source CSV must cost ONE read+write per FILE,
    not one per ROW - otherwise a large force-refresh (hundreds/thousands of
    combos) looks hung well after every download already finished, stuck inside an
    unstoppable, unreported per-row rewrite loop."""
    import csv as csv_mod

    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr(cs.registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "instrument", "status", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for cid in ("a", "b", "c"):
            writer.writerow({"combo_id": cid, "instrument": "NIFTY", "status": "ok", "total_pnl": "0"})

    def fake_multiprocess(work_items, selectors, reports_dir, **kwargs):
        updated = [
            {"combo_id": item[1], "instrument": "NIFTY", "status": "ok", "total_pnl": "999"}
            for item in work_items
        ]
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": updated}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake_multiprocess)

    update_rows_calls = []
    real_update_rows = cs.store.update_rows

    def spy_update_rows(path, updates):
        update_rows_calls.append(len(updates))
        return real_update_rows(path, updates)

    monkeypatch.setattr(cs.store, "update_rows", spy_update_rows)

    state = cs.CorrelateState()
    state.start([_dte_row("a"), _dte_row("b"), _dte_row("c")], csv_paths=[csv_path], force=True)
    state._thread.join(timeout=5)

    snap = state.snapshot()
    assert snap["status"] == "done", snap.get("message")
    # Every update_rows call must be batched (cover all 3 combos at once), never
    # one call per row - the whole point of the fix. Two calls are expected now
    # (the source CSV merge-back, plus the registry sync added later this
    # session - see registry.upsert_rows), not just one; what matters is that
    # NEITHER degrades into a per-row loop.
    assert update_rows_calls and all(n == 3 for n in update_rows_calls), (
        f"every update_rows call must cover all 3 combos at once, never one call "
        f"per row - got call sizes {update_rows_calls}"
    )
    with csv_path.open(newline="") as f:
        rows = list(csv_mod.DictReader(f))
    assert {r["combo_id"]: r["total_pnl"] for r in rows} == {"a": "999", "b": "999", "c": "999"}


def test_run_force_refresh_also_syncs_the_registry_not_just_csv_paths(tmp_path, monkeypatch):
    """A Force re-download's refreshed rows must land in the one authoritative
    combo registry too, not just the specific csv_paths this refresh happened to
    be told about - otherwise the SAME combo_id in some OTHER, non-refreshed
    results_web_*.csv file stays looking stale forever, with nothing ever going
    looking for it (the exact gap src/web/registry.py exists to close)."""
    import csv as csv_mod

    from src.web.models import SweepUIConfig
    from src.web import registry

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "instrument", "status", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "z", "instrument": "NIFTY", "status": "ok", "total_pnl": "0"})

    def fake_multiprocess(work_items, selectors, reports_dir, **kwargs):
        updated = [{"combo_id": "z", "instrument": "NIFTY", "status": "ok", "total_pnl": "777"}]
        return {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": updated}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake_multiprocess)

    state = cs.CorrelateState()
    state.start([_dte_row("z")], csv_paths=[csv_path], force=True)
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    reg = registry.read_registry()
    assert reg["z"]["total_pnl"] == "777"


def test_run_force_refresh_preserves_the_original_strategy_key(tmp_path, monkeypatch):
    """A Force re-download must NEVER recompute strategy_key from row_to_combo's
    reconstruction (which _build_row would otherwise do, via the same combo
    dict this force replay used) - confirmed live this session that
    row_to_combo does not reliably reconstruct every historical combo shape,
    which would silently corrupt an already-correct strategy_key (set once,
    live, at original discovery time) on every single refresh."""
    from src.web.models import SweepUIConfig
    from src.web import registry

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")

    def fake_multiprocess(work_items, selectors, reports_dir, **kwargs):
        # Simulates _build_row recomputing a WRONG strategy_key from a
        # mis-reconstructed combo dict - the exact corruption risk this guards.
        updated = [{"combo_id": "z", "instrument": "NIFTY", "status": "ok",
                    "total_pnl": "777", "strategy_key": "wrong_recomputed_key"}]
        return {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": updated}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake_multiprocess)

    state = cs.CorrelateState()
    original_row = _dte_row("z")
    original_row["strategy_key"] = "correct_original_key"
    state.start([original_row], force=True)
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    reg = registry.read_registry()
    assert reg["z"]["strategy_key"] == "correct_original_key"


def test_run_force_refresh_falls_back_to_freshly_computed_key_for_a_pre_strategy_key_row(tmp_path, monkeypatch):
    """A genuinely older row that predates strategy_key existing at all has
    nothing to preserve - keeping whatever the worker freshly computed is
    strictly better than leaving it blank."""
    from src.web.models import SweepUIConfig
    from src.web import registry

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")

    def fake_multiprocess(work_items, selectors, reports_dir, **kwargs):
        updated = [{"combo_id": "z", "instrument": "NIFTY", "status": "ok",
                    "total_pnl": "777", "strategy_key": "freshly_computed_key"}]
        return {"downloaded": 1, "failed": 0, "failures": [], "updated_rows": updated}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake_multiprocess)

    state = cs.CorrelateState()
    original_row = _dte_row("z")  # no strategy_key column at all - pre-existing row
    state.start([original_row], force=True)
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    reg = registry.read_registry()
    assert reg["z"]["strategy_key"] == "freshly_computed_key"


def test_run_groups_multiple_distinct_dte_variants_into_separate_calls(tmp_path, monkeypatch):
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(cs, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(cs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(cs, "load_selectors", lambda path: object())
    calls: list[list[int]] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append(kwargs["dte_values"])
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = cs.CorrelateState()
    state.start([_dte_row("cid_dte0"), _dte_row("cid_dte1")])
    state._thread.join(timeout=5)

    assert sorted(calls) == [[0], [1]]
    assert state.snapshot()["status"] == "done"
