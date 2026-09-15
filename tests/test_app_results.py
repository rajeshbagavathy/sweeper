from __future__ import annotations

import csv

from src.web.narrow import numeric_sort_key
from src.web.narrow import rmdd_sort_key as _rmdd_sort_key


def test_rmdd_sort_key_parses_float():
    assert _rmdd_sort_key({"return_max_dd": "1.69"}) == 1.69


def test_rmdd_sort_key_missing_or_garbage_sinks_to_bottom():
    assert _rmdd_sort_key({"return_max_dd": ""}) == float("-inf")
    assert _rmdd_sort_key({}) == float("-inf")
    assert _rmdd_sort_key({"return_max_dd": "N/A"}) == float("-inf")


def test_rows_sort_descending_with_garbage_row():
    rows = [
        {"return_max_dd": "0.5"},
        {"return_max_dd": "2.1"},
        {"return_max_dd": ""},
        {"return_max_dd": "1.0"},
    ]
    rows.sort(key=_rmdd_sort_key, reverse=True)
    assert [r["return_max_dd"] for r in rows] == ["2.1", "1.0", "0.5", ""]


def test_numeric_sort_key_works_for_any_column():
    rows = [
        {"reward_risk_ratio": "1.2"},
        {"reward_risk_ratio": "3.4"},
        {"reward_risk_ratio": ""},
        {"reward_risk_ratio": "2.0"},
    ]
    rows.sort(key=numeric_sort_key("reward_risk_ratio"), reverse=True)
    assert [r["reward_risk_ratio"] for r in rows] == ["3.4", "2.0", "1.2", ""]


def test_numeric_sort_key_is_independent_per_column():
    """Sorting by a different column shouldn't be affected by another column's
    values - guards against accidentally hardcoding a column name somewhere."""
    rows = [
        {"return_max_dd": "9.0", "reward_risk_ratio": "0.1"},
        {"return_max_dd": "0.1", "reward_risk_ratio": "9.0"},
    ]
    by_rmdd = sorted(rows, key=numeric_sort_key("return_max_dd"), reverse=True)
    by_rrr = sorted(rows, key=numeric_sort_key("reward_risk_ratio"), reverse=True)
    assert by_rmdd[0]["return_max_dd"] == "9.0"
    assert by_rrr[0]["reward_risk_ratio"] == "9.0"
    assert by_rmdd[0] is not by_rrr[0]


class _FakeRunState:
    def __init__(self, csv_path: str) -> None:
        self._csv_path = csv_path

    def snapshot(self) -> dict:
        return {"csv_path": self._csv_path}


def test_get_results_never_ranks_a_loser_above_a_profitable_row(tmp_path, monkeypatch):
    """End-to-end through the real /api/results endpoint - a strategy with a
    deceptively great return_max_dd (negative total_pnl / negative max_drawdown =
    a positive ratio) must not outrank an actually-profitable one, on any sort."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "return_max_dd", "reward_risk_ratio", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "loser", "status": "ok", "instrument": "NIFTY", "return_max_dd": "9.0", "reward_risk_ratio": "9.0", "total_pnl": "-5000"})
        writer.writerow({"combo_id": "winner", "status": "ok", "instrument": "NIFTY", "return_max_dd": "1.0", "reward_risk_ratio": "1.0", "total_pnl": "200"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    for sort_by in ("return_max_dd", "reward_risk_ratio", "combined"):
        rows = app_mod.get_results(sort_by=sort_by)["rows"]
        assert rows[0]["combo_id"] == "winner", f"sort_by={sort_by} ranked the loser first"


def test_get_results_combo_search_finds_a_match_by_substring(tmp_path, monkeypatch):
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "return_max_dd", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "4c50d84cb587", "status": "ok", "instrument": "NIFTY", "return_max_dd": "1.0", "total_pnl": "100"})
        writer.writerow({"combo_id": "aaaaaaaaaaaa", "status": "ok", "instrument": "NIFTY", "return_max_dd": "5.0", "total_pnl": "500"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = app_mod.get_results(combo_search="4c50d84")
    assert [r["combo_id"] for r in result["rows"]] == ["4c50d84cb587"]


def test_get_results_combo_search_is_case_insensitive_and_bypasses_other_filters(tmp_path, monkeypatch):
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "instrument", "return_max_dd", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        # instrument is BANKNIFTY, but an active instrument=NIFTY filter must not
        # hide it from a direct combo_id search - search is a lookup, not one more
        # AND-ed filter.
        writer.writerow({"combo_id": "ABCDEF123456", "status": "ok", "instrument": "BANKNIFTY", "return_max_dd": "1.0", "total_pnl": "100"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = app_mod.get_results(combo_search="abcdef", instrument="NIFTY")
    assert [r["combo_id"] for r in result["rows"]] == ["ABCDEF123456"]


def test_get_results_combo_search_no_match_returns_empty(tmp_path, monkeypatch):
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "return_max_dd", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "aaaaaaaaaaaa", "status": "ok", "return_max_dd": "1.0", "total_pnl": "100"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = app_mod.get_results(combo_search="zzzzzz")
    assert result["rows"] == []


def test_get_results_exit_time_filter_matches_exact_value(tmp_path, monkeypatch):
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "exit_time", "return_max_dd", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "exit_time": "13:15", "return_max_dd": "1.0", "total_pnl": "100"})
        writer.writerow({"combo_id": "b", "status": "ok", "exit_time": "15:10", "return_max_dd": "2.0", "total_pnl": "200"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = app_mod.get_results(exit_time="15:10")
    assert [r["combo_id"] for r in result["rows"]] == ["b"]


def test_exit_time_options_sorted_chronologically_with_unknown_last(tmp_path, monkeypatch):
    import src.web.app as app_mod

    rows = [
        {"status": "ok", "exit_time": "15:10"},
        {"status": "ok", "exit_time": "13:15"},
        {"status": "ok", "exit_time": "13:15"},
        {"status": "ok", "exit_time": ""},
        {"status": "error", "exit_time": "09:20"},  # not "ok" - excluded
    ]
    options = app_mod._exit_time_options(rows, instrument=None, dte=None)["options"]
    assert options == [
        {"label": "13:15", "count": 2},
        {"label": "15:10", "count": 1},
        {"label": "", "count": 1},
    ]


def test_get_results_exit_time_range_filter_is_inclusive(tmp_path, monkeypatch):
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "exit_time", "return_max_dd", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "exit_time": "12:45", "return_max_dd": "1.0", "total_pnl": "100"})
        writer.writerow({"combo_id": "b", "status": "ok", "exit_time": "13:15", "return_max_dd": "2.0", "total_pnl": "200"})
        writer.writerow({"combo_id": "c", "status": "ok", "exit_time": "15:10", "return_max_dd": "3.0", "total_pnl": "300"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = app_mod.get_results(exit_time_from="12:45", exit_time_to="13:50")
    assert sorted(r["combo_id"] for r in result["rows"]) == ["a", "b"]


def test_get_results_exit_time_exact_and_range_combine(tmp_path, monkeypatch):
    """exit_time (exact pill) and exit_time_from/to (manual range) AND together,
    not one replacing the other."""
    import src.web.app as app_mod

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "exit_time", "return_max_dd", "total_pnl"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "exit_time": "13:15", "return_max_dd": "1.0", "total_pnl": "100"})
        writer.writerow({"combo_id": "b", "status": "ok", "exit_time": "13:25", "return_max_dd": "2.0", "total_pnl": "200"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = app_mod.get_results(exit_time="13:15", exit_time_from="12:00", exit_time_to="14:00")
    assert [r["combo_id"] for r in result["rows"]] == ["a"]


def test_exit_time_options_scoped_by_instrument_and_dte():
    import src.web.app as app_mod

    rows = [
        {"status": "ok", "instrument": "NIFTY", "dte": "0", "exit_time": "13:15"},
        {"status": "ok", "instrument": "NIFTY", "dte": "1", "exit_time": "15:10"},
        {"status": "ok", "instrument": "BANKNIFTY", "dte": "0", "exit_time": "09:20"},
    ]
    options = app_mod._exit_time_options(rows, instrument="NIFTY", dte="0")["options"]
    assert options == [{"label": "13:15", "count": 1}]


def test_exit_time_options_scoped_by_entry_time_range():
    """Without this, loading several sweeps together (e.g. a CAS-window sweep
    entering 15:14-15:35 alongside an unrelated midday sweep entering 11:40-12:00)
    showed exit times from EVERY sweep regardless of the selected entry-time range -
    confirmed live: filtering entry_time to 15:14-15:35 still surfaced 11:15/11:30/
    11:45/13:25 as exit-time options, none of which belong to that entry window."""
    import src.web.app as app_mod

    rows = [
        {"status": "ok", "entry_time": "15:14", "exit_time": "15:38"},
        {"status": "ok", "entry_time": "15:35", "exit_time": "15:38"},
        {"status": "ok", "entry_time": "11:40", "exit_time": "13:25"},  # outside the entry-time range
    ]
    options = app_mod._exit_time_options(
        rows, instrument=None, dte=None, entry_time_from="15:14", entry_time_to="15:35",
    )["options"]
    assert options == [{"label": "15:38", "count": 2}]


def test_exit_time_options_with_no_entry_time_range_is_unaffected():
    """Leaving the entry-time range unset must reproduce the exact same result as
    before this filter existed - a no-op, not a behavior change for existing callers."""
    import src.web.app as app_mod

    rows = [
        {"status": "ok", "entry_time": "15:14", "exit_time": "15:38"},
        {"status": "ok", "entry_time": "11:40", "exit_time": "13:25"},
    ]
    options = app_mod._exit_time_options(rows, instrument=None, dte=None)["options"]
    assert options == [{"label": "13:25", "count": 1}, {"label": "15:38", "count": 1}]
