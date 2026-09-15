from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

from src.web.combo_launcher import (
    BasketSaveState,
    ComboLauncherState,
    _compare_metrics,
    _parse_dte_values,
    find_row,
    find_row_with_path,
    row_to_combo,
    scale_combo_for_save,
    strategy_save_name,
)

# Mirrors a real row shape (see the fieldnames build_fieldnames() produces) closely
# enough to exercise row_to_combo's reconstruction logic end-to-end.
_BASE_ROW = {
    "combo_id": "b98766fb3c40",
    "instrument": "NIFTY",
    "start_date": "2025-08-22",
    "end_date": "2026-08-22",
    "entry_time": "09:26",
    "exit_time": "15:10",
    "legs.0.action": "SELL",
    "legs.0.option_type": "CE",
    "legs.0.lots": "10.0",
    "legs.0.strike.mode": "premium_closest",
    "legs.0.strike.value": "42.5",
    "legs.1.action": "SELL",
    "legs.1.option_type": "PE",
    "legs.1.lots": "10.0",
    "legs.1.strike.mode": "premium_closest",
    "legs.1.strike.value": "42.5",
    "leg_risk.target_pct": "",
    "leg_risk.stoploss_pct.kind": "percentage",
    "leg_risk.stoploss_pct.value": "15.0",
    "leg_risk.trail.type": "Points",
    "leg_risk.trail.x": "3.0",
    "leg_risk.trail.y": "2.0",
    "leg_risk.trail": "",
    "leg_risk.momentum.direction": "",
    "leg_risk.momentum.value": "",
    "leg_risk.momentum": "",
    "leg_risk.reentry_sl.type": "",
    "leg_risk.reentry_sl.count": "",
    "leg_risk.reentry_sl": "",
    "stoploss.kind": "amount",
    "stoploss.value": "7000.0",
    "stoploss": "",
    "target": "",
    "trail_sl": "",
}


def test_strategy_save_name_combines_combo_id_and_entry_time():
    assert strategy_save_name("4c50d84cb587", {"entry_time": "11:26"}) == "4c50d84cb587_1126"


def test_strategy_save_name_falls_back_to_bare_combo_id_when_entry_time_missing():
    assert strategy_save_name("4c50d84cb587", {}) == "4c50d84cb587"
    assert strategy_save_name("4c50d84cb587", {"entry_time": ""}) == "4c50d84cb587"


def test_strategy_save_name_prepends_prefix_when_given():
    assert (
        strategy_save_name("4c50d84cb587", {"entry_time": "11:26"}, "morning_basket")
        == "morning_basket_4c50d84cb587_1126"
    )


def test_strategy_save_name_prefix_is_optional_and_whitespace_only_is_ignored():
    assert strategy_save_name("4c50d84cb587", {"entry_time": "11:26"}, None) == "4c50d84cb587_1126"
    assert strategy_save_name("4c50d84cb587", {"entry_time": "11:26"}, "") == "4c50d84cb587_1126"
    assert strategy_save_name("4c50d84cb587", {"entry_time": "11:26"}, "   ") == "4c50d84cb587_1126"


def test_row_to_combo_reconstructs_exact_shape():
    combo = row_to_combo(_BASE_ROW)
    assert combo == {
        "instrument": "NIFTY",
        "start_date": "2025-08-22",
        "end_date": "2026-08-22",
        "entry_time": "09:26",
        "exit_time": "15:10",
        "legs": [
            {"action": "SELL", "option_type": "CE", "lots": 10.0, "strike": {"mode": "premium_closest", "value": 42.5}},
            {"action": "SELL", "option_type": "PE", "lots": 10.0, "strike": {"mode": "premium_closest", "value": 42.5}},
        ],
        "leg_risk": {
            "target_pct": None,
            "stoploss_pct": {"kind": "percentage", "value": 15.0},
            "trail": {"type": "Points", "x": 3.0, "y": 2.0},
            "momentum": None,
            "reentry_sl": None,
        },
        "stoploss": {"kind": "amount", "value": 7000.0},
        "target": None,
        "trail_sl": None,
    }


def test_row_to_combo_falls_back_to_legacy_flat_leg_stoploss_column():
    # Rows saved before the leg Stop Loss dual-basis feature (2026-08-28) stored this
    # as one flat numeric column instead of .kind/.value - must still come back as a
    # percentage-basis dict, not silently vanish (which would drop the Stop Loss while
    # Trail SL stayed active, a combination AlgoTest rejects outright).
    row = dict(_BASE_ROW)
    del row["leg_risk.stoploss_pct.kind"]
    del row["leg_risk.stoploss_pct.value"]
    row["leg_risk.stoploss_pct"] = "40.0"
    combo = row_to_combo(row)
    assert combo["leg_risk"]["stoploss_pct"] == {"kind": "percentage", "value": 40.0}


def test_row_to_combo_legacy_leg_stoploss_column_blank_stays_none():
    row = dict(_BASE_ROW)
    del row["leg_risk.stoploss_pct.kind"]
    del row["leg_risk.stoploss_pct.value"]
    row["leg_risk.stoploss_pct"] = ""
    combo = row_to_combo(row)
    assert combo["leg_risk"]["stoploss_pct"] is None


def test_row_to_combo_handles_offset_strike_not_just_premium_closest():
    row = dict(_BASE_ROW)
    row["legs.0.strike.mode"] = ""
    row["legs.0.strike.value"] = ""
    row["legs.0.strike"] = "ATM"
    combo = row_to_combo(row)
    assert combo["legs"][0]["strike"] == "ATM"


def test_row_to_combo_reconstructs_momentum_and_reentry_when_present():
    row = dict(_BASE_ROW)
    row["leg_risk.momentum.direction"] = "DOWN"
    row["leg_risk.momentum.value"] = "8.0"
    row["leg_risk.reentry_sl.type"] = "RE_COST"
    row["leg_risk.reentry_sl.count"] = "2.0"  # stored as a float string, like everything else
    combo = row_to_combo(row)
    assert combo["leg_risk"]["momentum"] == {"direction": "DOWN", "value": 8.0}
    # count must come back as a plain int - it's matched against a literal button
    # value ("1".."6") when applied, not a free-typed number.
    assert combo["leg_risk"]["reentry_sl"] == {"type": "RE_COST", "count": 2}
    assert isinstance(combo["leg_risk"]["reentry_sl"]["count"], int)


def test_row_to_combo_lazy_leg_type_with_no_count_column_does_not_crash():
    """Confirmed live: relaunching an actual Lazy-Leg row crashed with "int()
    argument must be ... not 'NoneType'" - leg_risk.reentry_sl.count is always
    blank for this type (see src/web/expand.py's _reentry_sl_choices, which
    never gives LAZY_LEG a count), unlike RE_ASAP/RE_COST above which always
    have one."""
    row = dict(_BASE_ROW)
    row["leg_risk.reentry_sl.type"] = "LAZY_LEG"
    row["leg_risk.reentry_sl.count"] = ""

    combo = row_to_combo(row)

    assert combo["leg_risk"]["reentry_sl"] == {"type": "LAZY_LEG"}


def test_row_to_combo_reconstructs_a_populated_lazy_leg_per_leg():
    """The eligible case - a leg whose "legs.{i}.lazy_leg.*" columns are
    actually populated (see src/lazy_leg.py's derive_lazy_leg) gets that same
    nested dict rebuilt onto ITS OWN leg, not onto leg_risk - matching exactly
    what src/web/expand.py's nest_combo originally attached, so
    apply_combination's own per-leg lazy_leg check (see src/form.py) can act
    on it identically whether the combo came from a live sweep or a relaunch."""
    row = dict(_BASE_ROW)
    row["leg_risk.reentry_sl.type"] = "LAZY_LEG"
    row["leg_risk.reentry_sl.count"] = ""
    row["legs.0.lazy_leg.option_type"] = "CE"
    row["legs.0.lazy_leg.strike.mode"] = "premium_closest"
    row["legs.0.lazy_leg.strike.value"] = "21.0"
    row["legs.0.lazy_leg.stoploss_pct.kind"] = "percentage"
    row["legs.0.lazy_leg.stoploss_pct.value"] = "35.0"
    row["legs.0.lazy_leg.trail.type"] = ""
    row["legs.0.lazy_leg.momentum.direction"] = "UNDERLYING_DOWN"
    row["legs.0.lazy_leg.momentum.value"] = "20.0"
    # leg 1 (PE) was NOT eligible for this combo - no lazy_leg columns set at all.

    combo = row_to_combo(row)

    assert combo["legs"][0]["lazy_leg"] == {
        "option_type": "CE",
        "strike": {"mode": "premium_closest", "value": 21.0},
        "stoploss_pct": {"kind": "percentage", "value": 35.0},
        "trail": None,
        "momentum": {"direction": "UNDERLYING_DOWN", "value": 20.0},
    }
    assert "lazy_leg" not in combo["legs"][1]


def test_row_to_combo_reconstructs_overall_trail_sl_when_present():
    row = dict(_BASE_ROW)
    row["trail_sl.x"] = "20.0"
    row["trail_sl.y"] = "10.0"
    row["trail_sl.step"] = "5.0"
    row["trail_sl.trail_by"] = "5.0"
    combo = row_to_combo(row)
    assert combo["trail_sl"] == {"x": 20.0, "y": 10.0, "step": 5.0, "trail_by": 5.0}


def test_scale_combo_for_save_reduces_lots_and_amount_fields_proportionally():
    row = dict(_BASE_ROW)
    row["trail_sl.x"] = "20.0"
    row["trail_sl.y"] = "10.0"
    row["trail_sl.step"] = "5.0"
    row["trail_sl.trail_by"] = "5.0"
    combo = row_to_combo(row)  # 10 lots, stoploss amount=7000, trail_sl set above

    scaled = scale_combo_for_save(combo, target_lots=1.0)

    assert all(leg["lots"] == 1.0 for leg in scaled["legs"])
    assert scaled["stoploss"] == {"kind": "amount", "value": 700.0}  # 7000 / 10
    assert scaled["trail_sl"] == {"x": 2.0, "y": 1.0, "step": 0.5, "trail_by": 0.5}


def test_scale_combo_for_save_leaves_percentage_basis_fields_untouched():
    row = dict(_BASE_ROW)
    row["stoploss.kind"] = "percentage"
    row["stoploss.value"] = "50.0"
    combo = row_to_combo(row)

    scaled = scale_combo_for_save(combo, target_lots=1.0)

    # leg_risk.stoploss_pct is already percentage-basis in _BASE_ROW - must be
    # unchanged, and so must an overall Stop Loss on the percentage basis.
    assert scaled["leg_risk"]["stoploss_pct"] == combo["leg_risk"]["stoploss_pct"]
    assert scaled["stoploss"] == {"kind": "percentage", "value": 50.0}


def test_scale_combo_for_save_does_not_mutate_the_original_combo():
    combo = row_to_combo(_BASE_ROW)
    original_lots = combo["legs"][0]["lots"]

    scale_combo_for_save(combo, target_lots=1.0)

    assert combo["legs"][0]["lots"] == original_lots  # unchanged - a copy was scaled, not this one


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = list(_BASE_ROW.keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_find_row_locates_combo_across_multiple_files(tmp_path):
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"
    _write_csv(file_a, [dict(_BASE_ROW, combo_id="other")])
    _write_csv(file_b, [dict(_BASE_ROW, combo_id="b98766fb3c40")])

    row = find_row([file_a, file_b], "b98766fb3c40")
    assert row is not None
    assert row["combo_id"] == "b98766fb3c40"


def test_find_row_returns_none_when_not_found(tmp_path):
    file_a = tmp_path / "a.csv"
    _write_csv(file_a, [dict(_BASE_ROW, combo_id="other")])
    assert find_row([file_a], "missing") is None


def test_parse_dte_values_single():
    assert _parse_dte_values("1") == [1]


def test_parse_dte_values_multi():
    assert _parse_dte_values("0,1,2") == [0, 1, 2]


def test_parse_dte_values_empty_or_none():
    assert _parse_dte_values("") == []
    assert _parse_dte_values(None) == []


def test_compare_metrics_matches_within_tolerance():
    row = {"return_max_dd": "-0.63", "win_rate": "21.15"}
    raw_metrics = {"return_max_dd": "-0.63", "win_rate": "21.15"}
    comparison = _compare_metrics(row, raw_metrics)
    assert all(c["match"] for c in comparison)


def test_compare_metrics_flags_a_real_mismatch():
    row = {"reward_risk_ratio": "2.91"}
    raw_metrics = {"reward_risk_ratio": "1.50"}
    comparison = _compare_metrics(row, raw_metrics)
    assert comparison[0]["match"] is False
    assert comparison[0]["stored"] == "2.91"
    assert comparison[0]["live"] == "1.50"


def test_compare_metrics_both_blank_counts_as_a_match():
    """A metric that's genuinely absent both times (e.g. no losing trades at all)
    shouldn't be flagged as a mismatch just because both sides parse to None."""
    row = {"max_loss": ""}
    raw_metrics = {"max_loss": "—"}
    comparison = _compare_metrics(row, raw_metrics)
    assert comparison[0]["match"] is True


def test_compare_metrics_currency_formatting_does_not_cause_a_false_mismatch():
    """Stored values are plain floats ("38132.0") but freshly-scraped ones are raw
    AlgoTest text ("₹ 38,132") - parse_number must normalize both before comparing."""
    row = {"total_pnl": "-38132.0"}
    raw_metrics = {"total_pnl": "₹ -38,132"}
    comparison = _compare_metrics(row, raw_metrics)
    assert comparison[0]["match"] is True


def test_launcher_snapshot_starts_idle():
    state = ComboLauncherState()
    assert state.snapshot() == {
        "status": "idle",
        "message": None,
        "combo_id": None,
        "comparison": None,
        "settings_used": None,
        "saved_strategy_name": None,
    }


def test_launcher_raises_if_already_in_progress():
    state = ComboLauncherState()
    with state._lock:
        state.status = "filling"
    with pytest.raises(RuntimeError):
        state.launch(["x.csv"], "abc")


def test_launcher_reports_error_when_combo_not_found(tmp_path):
    file_a = tmp_path / "a.csv"
    _write_csv(file_a, [dict(_BASE_ROW, combo_id="other")])

    state = ComboLauncherState()
    state.launch([str(file_a)], "does-not-exist")
    for _ in range(50):
        if state.snapshot()["status"] != "opening":
            break
        time.sleep(0.01)
    snap = state.snapshot()
    assert snap["status"] == "error"
    assert "does-not-exist" in snap["message"]


def _wait_until_done(state: BasketSaveState, timeout_s: float = 2.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = state.snapshot()
        if snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.01)
    raise AssertionError(f"basket save did not finish in time - last status {state.snapshot()['status']!r}")


def test_basket_save_snapshot_starts_idle():
    state = BasketSaveState()
    assert state.snapshot() == {
        "status": "idle", "total": 0, "current_index": 0, "prefix": "",
        "results": {}, "message": None,
    }


def test_basket_save_raises_if_already_in_progress():
    state = BasketSaveState()
    with state._lock:
        state.status = "running"
    with pytest.raises(RuntimeError):
        state.start(["x.csv"], ["abc"], "prefix")


def test_basket_save_all_combos_missing_finishes_without_opening_a_browser(tmp_path):
    """Every combo_id invalid - each gets its own "not found" error and the state
    reaches "done" without ever needing a real browser (see the module docstring:
    combo_ids are validated against the CSV(s) up front, before the browser opens)."""
    file_a = tmp_path / "a.csv"
    _write_csv(file_a, [dict(_BASE_ROW, combo_id="other")])

    state = BasketSaveState()
    state.start([str(file_a)], ["missing-1", "missing-2"], "myprefix")
    snap = _wait_until_done(state)

    assert snap["status"] == "done"
    assert snap["total"] == 2
    assert snap["prefix"] == "myprefix"
    assert snap["results"]["missing-1"]["status"] == "error"
    assert "missing-1" in snap["results"]["missing-1"]["error"]
    assert snap["results"]["missing-2"]["status"] == "error"


def test_basket_save_stop_before_start_does_nothing_harmful():
    state = BasketSaveState()
    state.stop()  # never started - must not raise
    assert state.snapshot()["status"] == "idle"


def test_find_row_with_path_returns_the_file_it_was_found_in(tmp_path):
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"
    _write_csv(file_a, [dict(_BASE_ROW, combo_id="other")])
    _write_csv(file_b, [dict(_BASE_ROW, combo_id="b98766fb3c40")])

    found = find_row_with_path([file_a, file_b], "b98766fb3c40")

    assert found is not None
    path, row = found
    assert path == file_b
    assert row["combo_id"] == "b98766fb3c40"


def test_find_row_with_path_returns_none_when_not_found(tmp_path):
    file_a = tmp_path / "a.csv"
    _write_csv(file_a, [dict(_BASE_ROW, combo_id="other")])
    assert find_row_with_path([file_a], "does-not-exist") is None


