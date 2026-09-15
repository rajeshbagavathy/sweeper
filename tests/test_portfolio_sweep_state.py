from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.web import portfolio_sweep_state as pss
from src.web.portfolio_sweep_state import PortfolioSweepState, build_grid, frange_inclusive


def _wait_until_done(state: PortfolioSweepState, timeout_s: float = 2.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = state.snapshot()
        if snap["status"] in ("done", "stopped", "error"):
            return snap
        time.sleep(0.01)
    raise AssertionError(f"sweep did not finish in time - last status {state.snapshot()['status']!r}")


def test_frange_inclusive_matches_the_worked_example():
    assert frange_inclusive(0.25, 0.4, 0.05) == [0.25, 0.3, 0.35, 0.4]


def test_frange_inclusive_single_value_when_start_equals_stop():
    assert frange_inclusive(50, 50, 50) == [50]


def test_frange_inclusive_rejects_non_positive_step():
    with pytest.raises(ValueError):
        frange_inclusive(0, 1, 0)


def test_frange_inclusive_rejects_stop_before_start():
    with pytest.raises(ValueError):
        frange_inclusive(1, 0, 0.1)


def test_build_grid_is_the_full_cartesian_product():
    grid = build_grid(threshold=[0.25, 0.3], top_n=[50, 100])
    assert grid == [
        {"threshold": 0.25, "top_n": 50},
        {"threshold": 0.25, "top_n": 100},
        {"threshold": 0.3, "top_n": 50},
        {"threshold": 0.3, "top_n": 100},
    ]


def test_build_grid_four_axes():
    grid = build_grid(threshold=[0.25, 0.3], top_n=[50], min_lots=[3, 4], max_lots=[7])
    assert len(grid) == 2 * 1 * 2 * 1
    assert {"threshold": 0.3, "top_n": 50, "min_lots": 4, "max_lots": 7} in grid


def test_build_grid_empty_axis_makes_the_whole_grid_empty():
    assert build_grid(threshold=[0.25], top_n=[]) == []


def test_build_grid_no_axes_gives_one_empty_combination():
    assert build_grid() == [{}]


def test_start_rejects_when_already_running():
    s = PortfolioSweepState()
    s.status = "running"
    with pytest.raises(RuntimeError):
        s.start([{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.3}])


def test_start_rejects_empty_rows():
    s = PortfolioSweepState()
    with pytest.raises(ValueError):
        s.start([], Path("."), "NIFTY", [{"threshold": 0.3}])


def test_start_rejects_empty_grid():
    s = PortfolioSweepState()
    with pytest.raises(ValueError):
        s.start([{"combo_id": "a"}], Path("."), "NIFTY", [])


def test_stop_flips_status_to_stopping_immediately():
    s = PortfolioSweepState()
    s.status = "running"
    s._stop_event = threading.Event()
    s.stop()
    assert s.status == "stopping"
    assert s._stop_event.is_set()


def test_stop_is_a_noop_when_nothing_is_running():
    s = PortfolioSweepState()
    s.stop()
    assert s.status == "idle"


def _fake_result(rr: float, profit: float = 1000.0) -> dict:
    return {
        "buckets": {
            "short_morning": {"members": [{"combo_id": "a"}], "unallocated_lots": 0.0, "dropped_for_min_lots": 0},
            "long_morning": {"members": [], "unallocated_lots": 0.0, "dropped_for_min_lots": 0},
            "midday": {"members": [], "unallocated_lots": 0.0, "dropped_for_min_lots": 0},
            "afternoon": {"members": [], "unallocated_lots": 0.0, "dropped_for_min_lots": 0},
        },
        "portfolio": {"reward_risk_ratio": rr, "overall_profit": profit, "return_max_dd": 10.0,
                      "num_periods": 40, "win_pct": 80.0, "max_drawdown": -500.0},
        "shortlist_pool_size": 12,
    }


def _patch_diversify_and_size(monkeypatch, *, diversify=None, size=None):
    """_run no longer calls portfolio.build_portfolio directly - it calls
    diversify_for_grid once per distinct (threshold, top_n) then size_and_summarize
    once per grid point sharing it (see _run's own comment for why). Default fakes
    just pass threshold/top_n through the (opaque, to _run) diversified dict so a
    test's fake `size` can still see them without needing its own diversify hook."""
    def default_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        return {"threshold": threshold, "top_n": top_n}

    def default_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        return _fake_result(rr=1.0)

    monkeypatch.setattr(pss.portfolio, "diversify_for_grid", diversify or default_diversify)
    monkeypatch.setattr(pss.portfolio, "size_and_summarize", size or default_size)


def test_run_collects_one_entry_per_grid_point_carrying_its_own_params(monkeypatch):
    diversify_calls = []

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        diversify_calls.append((threshold, top_n))
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        return _fake_result(rr=diversified["threshold"] * 10 + diversified["top_n"] / 100)

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    stop_event = threading.Event()
    grid = [{"threshold": 0.25, "top_n": 50}, {"threshold": 0.25, "top_n": 100}, {"threshold": 0.3, "top_n": 50}]
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", grid, {}, stop_event)

    assert diversify_calls == [(0.25, 50), (0.25, 100), (0.3, 50)]
    assert [{"threshold": r["threshold"], "top_n": r["top_n"]} for r in s.results] == grid
    assert s.results[0]["reward_risk_ratio"] == pytest.approx(0.25 * 10 + 50 / 100)
    assert s.status == "done"
    assert s.completed == 3


def test_run_groups_by_threshold_top_n_so_diversify_runs_once_per_distinct_pair(monkeypatch):
    """The optimization this guards: min_lots/max_lots varying across many grid
    points that share the same (threshold, top_n) must NOT re-run the expensive
    diversify_for_grid half for each one - only size_and_summarize (cheap) runs
    per point."""
    diversify_calls = []
    size_calls = []

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        diversify_calls.append((threshold, top_n))
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        size_calls.append(kwargs)
        return _fake_result(rr=1.0)

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    grid = build_grid(threshold=[0.25, 0.3], top_n=[50], min_lots=[2, 3, 4], max_lots=[7])
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", grid, {}, threading.Event())

    assert sorted(diversify_calls) == [(0.25, 50), (0.3, 50)]  # 2 distinct pairs, not 6
    assert len(size_calls) == 6  # every grid point still gets its own lot-sizing
    assert s.completed == 6


def test_run_carries_all_four_swept_params_into_each_result_entry(monkeypatch):
    """min_lots/max_lots must show up in the results table exactly like
    threshold/top_n do - swept params are whatever the caller's grid contains, not
    a hardcoded pair."""
    def fake_size(diversified, reports_dir, instrument, *, threshold, min_lots, max_lots, **kwargs):
        return _fake_result(rr=1.0)

    _patch_diversify_and_size(monkeypatch, size=fake_size)

    s = PortfolioSweepState()
    grid = [{"threshold": 0.3, "top_n": 300, "min_lots": 3, "max_lots": 7}]
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", grid, {}, threading.Event())

    entry = s.results[0]
    assert entry["threshold"] == 0.3
    assert entry["top_n"] == 300
    assert entry["min_lots"] == 3
    assert entry["max_lots"] == 7


def test_run_records_unallocated_and_dropped_for_min_lots_summed_across_buckets(monkeypatch):
    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        result = _fake_result(rr=1.0)
        result["buckets"]["short_morning"]["unallocated_lots"] = 4.0
        result["buckets"]["afternoon"]["unallocated_lots"] = 6.0
        result["buckets"]["midday"]["dropped_for_min_lots"] = 2
        return result

    _patch_diversify_and_size(monkeypatch, size=fake_size)

    s = PortfolioSweepState()
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25, "top_n": 50}], {}, threading.Event())

    entry = s.results[0]
    assert entry["unallocated_lots"] == pytest.approx(10.0)  # 4 + 6 across the two buckets
    assert entry["dropped_for_min_lots"] == 2


def test_run_reads_overall_unallocated_in_pooled_mode_not_zeroed_per_bucket_sum(monkeypatch):
    """CAS regime analysis's "Overall lots" (overall_budget) pooled mode zeroes
    out every bucket's OWN unallocated_lots/dropped_for_min_lots (see
    size_and_summarize) - the real, whole-session numbers live at the top-level
    overall_unallocated_lots/overall_dropped_for_min_lots keys instead. Confirmed
    live: summing the (zeroed) per-bucket values here silently showed "0 unused"
    even when a large chunk of the overall cap genuinely went undeployed (2
    picks capped at Max lots each, 14 of a 30-lot cap unaccounted for)."""
    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        result = _fake_result(rr=1.0)
        # Pooled mode's own convention: per-bucket fields zeroed, real numbers at
        # the top level.
        for bucket in result["buckets"].values():
            bucket["unallocated_lots"] = 0.0
            bucket["dropped_for_min_lots"] = 0
        result["overall_budget"] = 30.0
        result["overall_unallocated_lots"] = 14.0
        result["overall_dropped_for_min_lots"] = 1
        return result

    _patch_diversify_and_size(monkeypatch, size=fake_size)

    s = PortfolioSweepState()
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25, "top_n": 50}], {}, threading.Event())

    entry = s.results[0]
    assert entry["unallocated_lots"] == pytest.approx(14.0)
    assert entry["dropped_for_min_lots"] == 1


def test_run_stops_early_when_stop_event_set_mid_loop(monkeypatch):
    """A combo already in flight finishes, but no further ones start - status ends
    as 'stopped', not 'done', and completed count reflects only what ran."""
    stop_event = threading.Event()

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        if threshold == 0.3:
            stop_event.set()  # simulates Stop being clicked while this one's running
        return {"threshold": threshold, "top_n": top_n}

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify)

    s = PortfolioSweepState()
    grid = [{"threshold": 0.25, "top_n": 50}, {"threshold": 0.3, "top_n": 50}, {"threshold": 0.35, "top_n": 50}]
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", grid, {}, stop_event)

    assert s.completed == 2  # the third combo never ran
    assert s.status == "stopped"


def test_run_sets_error_status_on_exception(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pss.portfolio, "diversify_for_grid", boom)

    s = PortfolioSweepState()
    s._run([{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25, "top_n": 50}], {}, threading.Event())

    assert s.status == "error"
    assert s.message == "disk on fire"


def test_run_passes_fixed_portfolio_kwargs_through_unchanged(monkeypatch):
    """budgets/max_share must reach size_and_summarize unchanged on every grid
    point - only what's actually in the grid varies."""
    seen_kwargs = {}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        seen_kwargs.update(kwargs)
        return _fake_result(rr=1.0)

    _patch_diversify_and_size(monkeypatch, size=fake_size)

    s = PortfolioSweepState()
    s._run(
        [{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25, "top_n": 50}],
        {"max_share": 0.4, "budgets": {"short_morning": 18}}, threading.Event(),
    )

    assert seen_kwargs == {"max_share": 0.4, "budgets": {"short_morning": 18}, "stale_after_days": None}


def test_run_passes_date_window_to_diversify_not_size(monkeypatch):
    """date_from/date_to are diversify-side (see portfolio._recompute_stats_for_window,
    which must run before shortlisting) - they must reach diversify_for_grid, and
    must NOT be forwarded to size_and_summarize, which has no such parameter and
    would raise on an unexpected kwarg."""
    seen_diversify_kwargs = {}

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n, **kwargs):
        seen_diversify_kwargs.update(kwargs)
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        assert "date_from" not in kwargs and "date_to" not in kwargs
        return _fake_result(rr=1.0)

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    s._run(
        [{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25, "top_n": 50}],
        {"date_from": "2026-08-01", "date_to": None}, threading.Event(),
    )

    assert seen_diversify_kwargs == {"date_from": "2026-08-01", "date_to": None}


def test_run_data_fingerprint_fields_do_not_break_resume(monkeypatch):
    """Regression guard for the exact incident this fixed: adding computed_at/
    data_window_min/data_window_max to each result row broke resume entirely,
    because _grid_key's "already done" reconstruction picked them up as if
    they'd been part of the original combo_params (they weren't in
    _RESULT_METRIC_FIELDS yet) - every previously-done row looked brand new
    again on the next start()."""
    calls = []

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        calls.append(threshold)
        if threshold == 0.3:
            s._stop_event.set()
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        result = _fake_result(rr=1.0)
        result["computed_at"] = "2026-09-08T12:00:00+00:00"
        result["data_window_min"] = "2026-08-01"
        result["data_window_max"] = "2026-08-18"
        return result

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    grid = [{"threshold": 0.25, "top_n": 50}, {"threshold": 0.3, "top_n": 50}, {"threshold": 0.35, "top_n": 50}]
    s.start([{"combo_id": "a"}], Path("."), "NIFTY", grid)
    _wait_until_done(s)
    assert calls == [0.25, 0.3]

    s.start([{"combo_id": "a"}], Path("."), "NIFTY", grid)
    _wait_until_done(s)
    assert calls == [0.25, 0.3, 0.35]  # only the leftover point re-ran, not all 3


def test_start_resumes_from_stopped_skipping_already_computed_points(monkeypatch):
    """The fix this guards: Stop then Run sweep again with the same ranges used to
    redo the whole grid from row one - it must now only compute what's left."""
    calls = []

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        calls.append(threshold)
        if threshold == 0.3:
            s._stop_event.set()  # simulates clicking Stop while this one's running
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        return _fake_result(rr=diversified["threshold"])

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    grid = [{"threshold": 0.25}, {"threshold": 0.3}, {"threshold": 0.35}, {"threshold": 0.4}]
    s.start([{"combo_id": "a"}], Path("."), "NIFTY", grid)
    snap = _wait_until_done(s)
    assert snap["status"] == "stopped"
    assert calls == [0.25, 0.3]  # 0.35/0.4 never ran

    # Run sweep again, same grid, nothing changed in between.
    s.start([{"combo_id": "a"}], Path("."), "NIFTY", grid)
    snap = _wait_until_done(s)

    assert calls == [0.25, 0.3, 0.35, 0.4]  # only the leftover two actually ran
    assert snap["status"] == "done"
    assert snap["total"] == 4
    assert snap["completed"] == 4
    assert [r["threshold"] for r in snap["results"]] == [0.25, 0.3, 0.35, 0.4]


def test_start_does_not_resume_after_a_fully_completed_run(monkeypatch):
    """'done' means nothing was left to resume - a second Run sweep with the same
    grid is a deliberate fresh run (e.g. after downloading more reports), not a
    resume, and must recompute everything."""
    calls = []

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        calls.append(threshold)
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        return _fake_result(rr=diversified["threshold"])

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    grid = [{"threshold": 0.25}, {"threshold": 0.3}]
    s.start([{"combo_id": "a"}], Path("."), "NIFTY", grid)
    snap = _wait_until_done(s)
    assert snap["status"] == "done"

    s.start([{"combo_id": "a"}], Path("."), "NIFTY", grid)
    _wait_until_done(s)

    assert calls == [0.25, 0.3, 0.25, 0.3]  # both ran again, not skipped


def test_start_resume_with_a_different_grid_only_computes_the_new_points(monkeypatch):
    """A deliberately different (not identical) grid on resume still skips
    whatever already-done points it happens to overlap with."""
    calls = []

    def fake_diversify(rows, reports_dir, instrument, *, threshold, top_n):
        calls.append(threshold)
        if threshold == 0.3:
            s._stop_event.set()
        return {"threshold": threshold, "top_n": top_n}

    def fake_size(diversified, reports_dir, instrument, *, threshold, **kwargs):
        return _fake_result(rr=diversified["threshold"])

    _patch_diversify_and_size(monkeypatch, diversify=fake_diversify, size=fake_size)

    s = PortfolioSweepState()
    s.start([{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25}, {"threshold": 0.3}, {"threshold": 0.35}])
    _wait_until_done(s)
    assert calls == [0.25, 0.3]

    # New grid: 0.25 (already done, skip), 0.3 (already done, skip), 0.5 (new).
    s.start([{"combo_id": "a"}], Path("."), "NIFTY", [{"threshold": 0.25}, {"threshold": 0.3}, {"threshold": 0.5}])
    snap = _wait_until_done(s)

    assert calls == [0.25, 0.3, 0.5]
    assert sorted(r["threshold"] for r in snap["results"]) == [0.25, 0.3, 0.5]
