from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.correlate import parse_trade_report, trade_report_path
from src.web import regime_state as rs
from src.web.models import SweepUIConfig
from src.web.portfolio_sweep_state import PortfolioSweepState


def _row(cid: str, instrument: str = "NIFTY", **extra) -> dict:
    row = {
        "combo_id": cid,
        "instrument": instrument,
        "entry_time": "09:20",
        "exit_time": "15:10",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
    }
    row.update(extra)
    return row


def _scored_row(cid: str, *, entry_time: str, exit_time: str, score: float) -> dict:
    """A row with a hard Stop Loss (so has_hard_stop_loss passes) and a
    return_max_dd/reward_risk_ratio/total_pnl set from a single `score` (so
    combined_sort_key ranks purely by it) - for shortlist_for_download tests."""
    return {
        "combo_id": cid,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "stoploss.kind": "amount",
        "stoploss.value": "7000",
        "return_max_dd": str(score),
        "reward_risk_ratio": str(score),
        "total_pnl": "1",  # profitable, so profitability_gated_key doesn't sink it
    }


def _patch_common(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "REGIME_REPORTS_DIR", tmp_path)
    monkeypatch.setattr(rs, "load_ui_config", lambda: SweepUIConfig())
    monkeypatch.setattr(rs, "load_selectors", lambda path: object())


def test_shortlist_for_download_gives_every_session_its_own_fair_share():
    """The whole point of fix 2: long_morning scores far better than the other
    three sessions here - a flat "best N overall" sort would let it crowd out
    everyone else entirely. Per-session shortlisting must not let that happen."""
    rows = []
    # 5 long_morning candidates, all scoring far higher than every other session.
    for i in range(5):
        rows.append(_scored_row(f"lm{i}", entry_time="09:20", exit_time="15:10", score=100.0 - i))
    # 3 short_morning candidates, much lower score.
    for i in range(3):
        rows.append(_scored_row(f"sm{i}", entry_time="09:20", exit_time="11:30", score=5.0 - i))
    # 1 midday candidate.
    rows.append(_scored_row("md0", entry_time="11:30", exit_time="13:00", score=3.0))
    # 1 afternoon candidate.
    rows.append(_scored_row("af0", entry_time="13:30", exit_time="15:10", score=2.0))

    picked = rs.shortlist_for_download(rows, top_n_per_bucket=2)
    picked_ids = {r["combo_id"] for r in picked}

    # long_morning is capped at top_n_per_bucket (2), not allowed to take every slot.
    assert {r["combo_id"] for r in picked if r["combo_id"].startswith("lm")} == {"lm0", "lm1"}
    # Every other session's only candidate(s) still made it in, despite scoring
    # far lower than long_morning's rejects (lm2/lm3/lm4) - a flat global sort
    # would have picked lm0-lm4 and dropped every other session entirely.
    assert "sm0" in picked_ids and "sm1" in picked_ids
    assert "md0" in picked_ids
    assert "af0" in picked_ids


def test_shortlist_for_download_ranks_within_each_session_by_combined_score():
    rows = [
        _scored_row("worse", entry_time="09:20", exit_time="15:10", score=1.0),
        _scored_row("better", entry_time="09:20", exit_time="15:10", score=9.0),
        _scored_row("best", entry_time="09:20", exit_time="15:10", score=20.0),
    ]
    picked = rs.shortlist_for_download(rows, top_n_per_bucket=2)
    assert {r["combo_id"] for r in picked} == {"best", "better"}


def test_shortlist_for_download_excludes_combos_with_no_hard_stop_loss():
    rows = [
        _scored_row("has_sl", entry_time="09:20", exit_time="15:10", score=1.0),
        {**_scored_row("no_sl", entry_time="09:20", exit_time="15:10", score=100.0), "stoploss.kind": ""},
    ]
    picked = rs.shortlist_for_download(rows, top_n_per_bucket=5)
    assert [r["combo_id"] for r in picked] == ["has_sl"]


def test_shortlist_for_download_does_not_require_a_report_on_disk():
    """Unlike portfolio._shortlist (which only considers already-downloaded
    combos), this picks WHICH rows to download a report FOR in the first place -
    it must never touch the filesystem at all."""
    rows = [_scored_row("a", entry_time="09:20", exit_time="15:10", score=1.0)]
    picked = rs.shortlist_for_download(rows, top_n_per_bucket=5)
    assert [r["combo_id"] for r in picked] == ["a"]


def test_cas_slot_buckets_covers_the_requested_window_in_order():
    bucket_order, _classify = rs.cas_slot_buckets("15:14", "15:30", 5)
    # Anchored to the shared time_buckets grid (market open 09:15), not to
    # entry_time_from itself - 15:14 falls in the 15:10 slot on that grid.
    assert bucket_order == ["15:10", "15:15", "15:20", "15:25", "15:30"]


def test_cas_slot_buckets_classifies_rows_into_the_matching_slice():
    bucket_order, classify = rs.cas_slot_buckets("15:14", "15:30", 5)
    assert classify({"entry_time": "15:16"}) == "15:15"
    assert classify({"entry_time": "15:14"}) == "15:10"
    assert classify({"entry_time": "15:29"}) == "15:25"


def test_shortlist_for_download_with_cas_buckets_gives_every_5min_slice_its_own_share():
    """The exact failure the user hit: every CAS candidate's entry_time falls in
    the same narrow window, so with the regular day-session buckets they'd all
    classify as "afternoon" and one session's per-bucket cap (not per-slice)
    would apply - letting whichever 5-min slice scores best crowd out the rest.
    Passing cas_slot_buckets's bucket_order/classify_fn fixes that, the same way
    the regular 4-session case is already fixed."""
    bucket_order, classify = rs.cas_slot_buckets("15:14", "15:30", 5)
    rows = []
    for i in range(5):
        rows.append(_scored_row(f"best_slice_{i}", entry_time="15:16", exit_time="15:29", score=100.0 - i))
    rows.append(_scored_row("other_slice", entry_time="15:26", exit_time="15:29", score=1.0))

    picked = rs.shortlist_for_download(rows, top_n_per_bucket=2, bucket_order=bucket_order, classify_fn=classify)
    picked_ids = {r["combo_id"] for r in picked}

    assert {r for r in picked_ids if r.startswith("best_slice")} == {"best_slice_0", "best_slice_1"}
    assert "other_slice" in picked_ids


def test_shortlist_for_download_default_bucket_order_unchanged_when_cas_args_omitted():
    """bucket_order/classify_fn are optional and default to the regular 4
    day-session buckets - a caller (like the existing regime download path with
    CAS slice mode off) that never passes them gets byte-for-byte the old
    behavior."""
    rows = [_scored_row("a", entry_time="09:20", exit_time="15:10", score=1.0)]
    assert rs.shortlist_for_download(rows, top_n_per_bucket=5) == rs.shortlist_for_download(
        rows, top_n_per_bucket=5, bucket_order=None, classify_fn=None
    )


def test_reuse_regular_report_copies_when_window_matches_exactly(tmp_path, monkeypatch):
    """A row whose OWN start_date/end_date are an exact match for the requested
    regime window already reflects the exact same backtest - the regular report
    is copied in rather than replayed."""
    regular_dir = tmp_path / "regular"
    window_dir = tmp_path / "window"
    regular_path = trade_report_path(regular_dir, "NIFTY", "a")
    regular_path.parent.mkdir(parents=True, exist_ok=True)
    regular_path.write_text("real trade data")
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)

    row = _row("a", start_date="2026-08-01", end_date="2026-09-03")
    reused = rs._reuse_regular_report_if_same_window(row, "NIFTY", "a", "2026-08-01", "2026-09-03", window_dir)

    assert reused is True
    assert trade_report_path(window_dir, "NIFTY", "a").read_text() == "real trade data"


def test_reuse_regular_report_false_when_window_differs(tmp_path, monkeypatch):
    regular_dir = tmp_path / "regular"
    window_dir = tmp_path / "window"
    regular_path = trade_report_path(regular_dir, "NIFTY", "a")
    regular_path.parent.mkdir(parents=True, exist_ok=True)
    regular_path.write_text("real trade data")
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)

    # Row's own window is a full year, not the exact regime window requested -
    # NOT a "close enough" case, so no reuse.
    row = _row("a", start_date="2025-01-01", end_date="2025-12-31")
    reused = rs._reuse_regular_report_if_same_window(row, "NIFTY", "a", "2026-08-01", "2026-09-03", window_dir)

    assert reused is False
    assert not trade_report_path(window_dir, "NIFTY", "a").exists()


def test_reuse_regular_report_false_when_regular_report_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "REPORTS_DIR", tmp_path / "regular")
    row = _row("a", start_date="2026-08-01", end_date="2026-09-03")
    reused = rs._reuse_regular_report_if_same_window(row, "NIFTY", "a", "2026-08-01", "2026-09-03", tmp_path / "window")
    assert reused is False


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (Index, Entry Date, P/L) - mirrors tests/test_portfolio.py's helper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


def test_slice_regular_report_writes_only_the_requested_sub_range(tmp_path, monkeypatch):
    """The wider fast path: the regular report's own window doesn't have to
    match exactly, only to fully CONTAIN the requested regime window - the
    sliced-out file must hold only the in-window dates' P/L, reconstructable by
    parse_trade_report exactly like a genuine pinned replay would produce."""
    regular_dir = tmp_path / "regular"
    window_dir = tmp_path / "window"
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)
    _write_report(trade_report_path(regular_dir, "NIFTY", "a"), [
        ("1", "2026-07-15", "500"),   # before the requested window
        ("2", "2026-08-07", "100"),
        ("3", "2026-08-14", "-50"),
        ("4", "2026-09-10", "999"),   # after the requested window
    ])

    row = _row("a", start_date="2026-07-01", end_date="2026-09-30")
    sliced = rs._slice_regular_report_into_window(row, "NIFTY", "a", "2026-08-01", "2026-08-20", window_dir)

    assert sliced is True
    result = parse_trade_report(trade_report_path(window_dir, "NIFTY", "a"))
    assert result == {"2026-08-07": 100.0, "2026-08-14": -50.0}


def test_slice_regular_report_false_when_window_not_fully_covered(tmp_path, monkeypatch):
    regular_dir = tmp_path / "regular"
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)
    _write_report(trade_report_path(regular_dir, "NIFTY", "a"), [("1", "2026-08-07", "100")])

    # Row's own recorded window (Aug 5 - Aug 20) does NOT reach all the way to
    # the requested Sep 3 end date - not a fair slice, must fall through.
    row = _row("a", start_date="2026-08-05", end_date="2026-08-20")
    sliced = rs._slice_regular_report_into_window(row, "NIFTY", "a", "2026-08-01", "2026-09-03", tmp_path / "window")
    assert sliced is False


def test_slice_regular_report_false_when_regular_report_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "REPORTS_DIR", tmp_path / "regular")
    row = _row("a", start_date="2026-08-01", end_date="2026-09-03")
    sliced = rs._slice_regular_report_into_window(row, "NIFTY", "a", "2026-08-01", "2026-09-03", tmp_path / "window")
    assert sliced is False


def test_slice_regular_report_false_when_no_dates_actually_fall_in_range(tmp_path, monkeypatch):
    """A recorded window can technically contain [date_from, date_to] while the
    report's own real trade-dates (DTE/weekday-constrained) happen to land
    nowhere inside it - must fall through to a real replay rather than write an
    empty, useless sliced file."""
    regular_dir = tmp_path / "regular"
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)
    _write_report(trade_report_path(regular_dir, "NIFTY", "a"), [("1", "2026-07-01", "100")])

    row = _row("a", start_date="2026-06-01", end_date="2026-09-30")
    sliced = rs._slice_regular_report_into_window(row, "NIFTY", "a", "2026-08-01", "2026-08-20", tmp_path / "window")
    assert sliced is False


def test_run_reuses_regular_report_instead_of_a_fresh_replay_when_window_matches(tmp_path, monkeypatch):
    """End-to-end: a combo whose row already records the exact requested window
    must never hit run_correlate_multiprocess at all - the regular report is
    copied straight into the window dir."""
    _patch_common(monkeypatch, tmp_path)
    regular_dir = tmp_path / "regular"
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)
    regular_path = trade_report_path(regular_dir, "NIFTY", "a")
    regular_path.parent.mkdir(parents=True, exist_ok=True)
    regular_path.write_text("real trade data")

    calls: list = []
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda *a, **k: calls.append(1) or {"downloaded": 0, "failed": 0, "failures": [], "updated_rows": []},
    )

    state = rs.RegimeDownloadState()
    state.start([_row("a", start_date="2026-08-01", end_date="2026-09-03")], date_from="2026-08-01", date_to="2026-09-03")
    state._thread.join(timeout=5)

    assert calls == []  # never replayed - the regular report was reused instead
    window_dir = rs.regime_window_dir("2026-08-01", "2026-09-03")
    assert trade_report_path(window_dir, "NIFTY", "a").read_text() == "real trade data"
    assert state.snapshot()["downloaded"] == 1


def test_run_slices_regular_report_instead_of_a_fresh_replay_when_window_is_covered(tmp_path, monkeypatch):
    """End-to-end for the wider fast path: a combo whose row's window merely
    CONTAINS (not equals) the requested regime window must also never hit
    run_correlate_multiprocess - this is the concrete throughput win the
    "reconcile scattered combo data" plan was built for."""
    _patch_common(monkeypatch, tmp_path)
    regular_dir = tmp_path / "regular"
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)
    _write_report(trade_report_path(regular_dir, "NIFTY", "a"), [
        ("1", "2026-07-15", "500"),
        ("2", "2026-08-07", "100"),
        ("3", "2026-09-10", "999"),
    ])

    calls: list = []
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda *a, **k: calls.append(1) or {"downloaded": 0, "failed": 0, "failures": [], "updated_rows": []},
    )

    state = rs.RegimeDownloadState()
    # Row's own recorded window (Jul 1 - Sep 30) fully contains the narrower
    # requested regime window (Aug 1 - Aug 20) - not an exact match.
    state.start([_row("a", start_date="2026-07-01", end_date="2026-09-30")], date_from="2026-08-01", date_to="2026-08-20")
    state._thread.join(timeout=5)

    assert calls == []  # never replayed - sliced from the regular report instead
    window_dir = rs.regime_window_dir("2026-08-01", "2026-08-20")
    assert parse_trade_report(trade_report_path(window_dir, "NIFTY", "a")) == {"2026-08-07": 100.0}
    assert state.snapshot()["downloaded"] == 1
    assert state.snapshot()["status"] == "done"


def test_run_force_bypasses_reuse_and_replays_anyway(tmp_path, monkeypatch):
    """force=True means "genuinely redo this" - the reuse fast path must not
    stand in for the fresh replay the user explicitly asked for."""
    _patch_common(monkeypatch, tmp_path)
    regular_dir = tmp_path / "regular"
    monkeypatch.setattr(rs, "REPORTS_DIR", regular_dir)
    regular_path = trade_report_path(regular_dir, "NIFTY", "a")
    regular_path.parent.mkdir(parents=True, exist_ok=True)
    regular_path.write_text("real trade data")

    calls: list = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append([item[1] for item in work_items])
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = rs.RegimeDownloadState()
    state.start(
        [_row("a", start_date="2026-08-01", end_date="2026-09-03")],
        date_from="2026-08-01", date_to="2026-09-03", force=True,
    )
    state._thread.join(timeout=5)

    assert calls == [["a"]]  # replayed despite the regular report being an exact-window match


def test_regime_window_dir_is_scoped_by_exact_date_range(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "REGIME_REPORTS_DIR", tmp_path)
    a = rs.regime_window_dir("2026-08-01", "2026-09-01")
    b = rs.regime_window_dir("2026-07-15", "2026-09-01")
    assert a != b
    assert a == tmp_path / "2026-08-01_to_2026-09-01"


def test_list_downloaded_regime_windows_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "REGIME_REPORTS_DIR", tmp_path / "does-not-exist")
    assert rs.list_downloaded_regime_windows() == []


def test_list_downloaded_regime_windows_reports_each_window_with_its_count(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "REGIME_REPORTS_DIR", tmp_path)
    (tmp_path / "2026-08-01_to_2026-09-03" / "nifty").mkdir(parents=True)
    (tmp_path / "2026-08-01_to_2026-09-03" / "nifty" / "a.csv").write_text("x")
    (tmp_path / "2026-08-01_to_2026-09-03" / "nifty" / "b.csv").write_text("x")
    (tmp_path / "2026-08-01_to_2026-09-05" / "nifty").mkdir(parents=True)
    (tmp_path / "2026-08-01_to_2026-09-05" / "nifty" / "a.csv").write_text("x")

    windows = rs.list_downloaded_regime_windows()

    assert windows == [
        ("2026-08-01", "2026-09-03", 2),
        ("2026-08-01", "2026-09-05", 1),
    ]


def test_list_downloaded_regime_windows_skips_an_empty_leftover_folder(tmp_path, monkeypatch):
    """An interrupted/failed download can leave a window folder behind with zero
    reports in it - that's not "available," so it must not be listed as an option."""
    monkeypatch.setattr(rs, "REGIME_REPORTS_DIR", tmp_path)
    (tmp_path / "2026-08-01_to_2026-09-03").mkdir(parents=True)

    assert rs.list_downloaded_regime_windows() == []


def test_list_downloaded_regime_windows_ignores_non_matching_folder_names(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "REGIME_REPORTS_DIR", tmp_path)
    junk = tmp_path / "not_a_window_folder"
    junk.mkdir()
    (junk / "a.csv").write_text("x")

    assert rs.list_downloaded_regime_windows() == []


def test_start_requires_a_start_date():
    state = rs.RegimeDownloadState()
    with pytest.raises(ValueError, match="start date"):
        state.start([_row("a")], date_from="", date_to="2026-09-01")


def test_start_rejects_end_date_before_start_date():
    state = rs.RegimeDownloadState()
    with pytest.raises(ValueError, match="before start"):
        state.start([_row("a")], date_from="2026-09-01", date_to="2026-08-01")


def test_start_requires_at_least_one_row():
    state = rs.RegimeDownloadState()
    with pytest.raises(ValueError):
        state.start([], date_from="2026-08-01", date_to="2026-09-01")


def test_start_rejects_a_second_concurrent_run():
    state = rs.RegimeDownloadState()
    with state._lock:
        state.status = "running"
    with pytest.raises(RuntimeError):
        state.start([_row("a")], date_from="2026-08-01", date_to="2026-09-01")


def test_run_pins_each_combo_to_the_requested_window_not_its_original_dates(tmp_path, monkeypatch):
    """The whole point of this feature: NOT roll_date_window's "same length, rolled
    to today" - an explicit, independently-configured window replacing whatever the
    combo was originally discovered with."""
    _patch_common(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_run_correlate_multiprocess(work_items, selectors, reports_dir, **kwargs):
        captured["work_items"] = work_items
        captured["reports_dir"] = reports_dir
        captured["update_results"] = kwargs.get("update_results")
        return {"ok": len(work_items), "error": 0}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake_run_correlate_multiprocess)

    state = rs.RegimeDownloadState()
    state.start([_row("a")], date_from="2026-08-01", date_to="2026-09-01")
    state._thread.join(timeout=5)

    combo, cid, instrument = captured["work_items"][0]
    assert cid == "a"
    assert combo["start_date"] == "2026-08-01"
    assert combo["end_date"] == "2026-09-01"
    # Original discovery window must be fully overridden, not preserved/rolled.
    assert combo["start_date"] != "2025-01-01"


def test_run_writes_to_regime_window_dir_not_reports_dir(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda work_items, selectors, reports_dir, **kwargs: captured.update(reports_dir=reports_dir) or {"ok": 0, "error": 0},
    )

    state = rs.RegimeDownloadState()
    state.start([_row("a")], date_from="2026-08-01", date_to="2026-09-01")
    state._thread.join(timeout=5)

    assert captured["reports_dir"] == rs.regime_window_dir("2026-08-01", "2026-09-01")
    assert captured["reports_dir"] != rs.REGIME_REPORTS_DIR  # the window subfolder, not the bare parent


def test_run_never_mutates_the_stable_results_csv(tmp_path, monkeypatch):
    """update_results=True (Force re-download's own CSV backfill) must never be
    passed here - this pathway has no csv_paths to merge back into at all."""
    _patch_common(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda work_items, selectors, reports_dir, **kwargs: captured.update(update_results=kwargs.get("update_results")) or {"ok": 0, "error": 0},
    )

    state = rs.RegimeDownloadState()
    state.start([_row("a")], date_from="2026-08-01", date_to="2026-09-01", force=True)
    state._thread.join(timeout=5)

    assert captured["update_results"] is False


def test_run_skips_a_report_already_downloaded_for_the_same_exact_window(tmp_path, monkeypatch):
    """Same window => same real backtest => safe to reuse, unlike REPORTS_DIR's
    age-based cache - a report already on disk for THIS window must not be
    re-fetched unless force=True."""
    _patch_common(monkeypatch, tmp_path)
    window_dir = rs.regime_window_dir("2026-08-01", "2026-09-01")
    existing = trade_report_path(window_dir, "NIFTY", "a")
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("already here")

    captured: dict = {}
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda work_items, selectors, reports_dir, **kwargs: captured.update(work_items=work_items) or {"ok": 0, "error": 0},
    )

    state = rs.RegimeDownloadState()
    state.start([_row("a"), _row("b")], date_from="2026-08-01", date_to="2026-09-01")
    state._thread.join(timeout=5)

    # "a" already has a report for this exact window - only "b" should be replayed.
    assert [item[1] for item in captured["work_items"]] == ["b"]
    assert state.snapshot()["downloaded"] == 1  # "a" counted as already-cached


def test_run_force_redoes_a_report_already_downloaded_for_the_same_window(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    window_dir = rs.regime_window_dir("2026-08-01", "2026-09-01")
    existing = trade_report_path(window_dir, "NIFTY", "a")
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("already here")

    captured: dict = {}
    monkeypatch.setattr(
        "src.correlate.run_correlate_multiprocess",
        lambda work_items, selectors, reports_dir, **kwargs: captured.update(work_items=work_items) or {"ok": 0, "error": 0},
    )

    state = rs.RegimeDownloadState()
    state.start([_row("a")], date_from="2026-08-01", date_to="2026-09-01", force=True)
    state._thread.join(timeout=5)

    assert [item[1] for item in captured["work_items"]] == ["a"]


def test_run_isolates_a_dte_variant_combo_to_just_its_own_dte(tmp_path, monkeypatch):
    """A "_dte0" composite id (see src/runner.py's capture-individual-DTE feature)
    must be replayed with dte_values=[0] only - NOT the config's own multi-select
    dte_values (e.g. [0, 1, 2]) - or the resulting report silently mixes in other
    DTEs' trades and no longer matches what a "_dte0" id promises. Confirmed live:
    this was exactly the bug (a "_dte0" regime report came back with 3x the real
    DTE-0-only trade count)."""
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(rs, "load_ui_config", lambda: SweepUIConfig(dte_values=[0, 1, 2]))
    calls: list[dict] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append({"dte_values": kwargs["dte_values"], "cids": [item[1] for item in work_items]})
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = rs.RegimeDownloadState()
    state.start(
        [_row("base_combo"), _row("variant_combo_dte0")],
        date_from="2026-08-01", date_to="2026-09-01",
    )
    state._thread.join(timeout=5)

    by_cids = {tuple(sorted(c["cids"])): c["dte_values"] for c in calls}
    assert by_cids[("base_combo",)] == [0, 1, 2]
    assert by_cids[("variant_combo_dte0",)] == [0]


def test_run_bare_combo_uses_its_own_recorded_dte_not_the_saved_configs(tmp_path, monkeypatch):
    """The bare-id counterpart of the test above, and the exact bug this fixed
    (mirroring correlate_state.py's identical fix): a row whose OWN "dte" column
    says "0" (e.g. isolated via the results table's DTE=0 filter) must replay
    with dte_values=[0] - NOT whatever the CURRENTLY SAVED sweep config's
    dte_values happens to be right now, which can have since changed to
    something broader and silently mix in other DTEs' trades."""
    _patch_common(monkeypatch, tmp_path)
    # The saved config has since moved on to a broad multi-select - exactly the
    # real-world state that caused this bug.
    monkeypatch.setattr(rs, "load_ui_config", lambda: SweepUIConfig(dte_values=[0, 1, 2, 3, 4]))
    calls: list[dict] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append({"dte_values": kwargs["dte_values"], "cids": [item[1] for item in work_items]})
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    single_dte_row = _row("only_dte0_row", dte="0")
    multi_dte_row = _row("multi_dte_row", dte="0,1,2,3,4")

    state = rs.RegimeDownloadState()
    state.start([single_dte_row, multi_dte_row], date_from="2026-08-01", date_to="2026-09-01")
    state._thread.join(timeout=5)

    by_cids = {tuple(sorted(c["cids"])): c["dte_values"] for c in calls}
    assert by_cids[("only_dte0_row",)] == [0], "must replay isolated to DTE 0, not the saved config's [0,1,2,3,4]"
    assert by_cids[("multi_dte_row",)] == [0, 1, 2, 3, 4], "a genuinely multi-DTE row keeps its own full set"


def test_run_groups_multiple_distinct_dte_variants_into_separate_calls(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    calls: list[list[int]] = []

    def fake(work_items, selectors, reports_dir, **kwargs):
        calls.append(kwargs["dte_values"])
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = rs.RegimeDownloadState()
    state.start(
        [_row("cid_dte0"), _row("cid_dte1")],
        date_from="2026-08-01", date_to="2026-09-01",
    )
    state._thread.join(timeout=5)

    assert sorted(calls) == [[0], [1]]


def test_run_progress_accumulates_across_dte_groups_without_resetting(tmp_path, monkeypatch):
    """Each dte-group's own run_correlate_multiprocess call reports progress
    starting from 0 for ITS batch - the running total shown to the UI must keep
    accumulating across groups, not regress when a later group starts."""
    _patch_common(monkeypatch, tmp_path)

    def fake(work_items, selectors, reports_dir, *, on_progress=None, **kwargs):
        if on_progress:
            on_progress({"downloaded": len(work_items), "failed": 0, "in_progress": 0, "in_progress_ids": [], "failures": []})
        return {"downloaded": len(work_items), "failed": 0, "failures": [], "updated_rows": []}

    monkeypatch.setattr("src.correlate.run_correlate_multiprocess", fake)

    state = rs.RegimeDownloadState()
    state.start(
        [_row("cid_dte0"), _row("cid_dte1")],
        date_from="2026-08-01", date_to="2026-09-01",
    )
    state._thread.join(timeout=5)

    assert state.snapshot()["status"] == "done"
    assert state.snapshot()["downloaded"] == 2


def test_regime_sweep_state_is_independent_from_a_regular_portfolio_sweep_state():
    """Critical: sharing the singleton would let a regime sweep's resume/grid-dedup
    logic wrongly treat a regular sweep's results (or vice versa) as "already done"
    for a completely different report set."""
    from src.web.portfolio_sweep_state import portfolio_sweep_state

    assert isinstance(rs.regime_sweep_state, PortfolioSweepState)
    assert rs.regime_sweep_state is not portfolio_sweep_state
