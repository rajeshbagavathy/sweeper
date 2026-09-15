from __future__ import annotations

import csv
from pathlib import Path

import src.web.app as app_mod
from src.web.app import get_dte_options, get_results


class _FakeRunState:
    def __init__(self, csv_path: str | None):
        self._csv_path = csv_path

    def snapshot(self) -> dict:
        return {"csv_path": self._csv_path}


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "return_max_dd", "dte"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_dte_options_counts_each_distinct_combination(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "1", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "2", "dte": "0"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "3", "dte": "0,1,2"},
        {"combo_id": "d", "status": "ok", "return_max_dd": "4", "dte": ""},  # pre-DTE-tracking row
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    options = get_dte_options()["options"]
    by_label = {o["label"]: o["count"] for o in options}
    assert by_label["0"] == 2
    assert by_label["0,1,2"] == 1
    assert by_label[""] == 1
    # unknown ("") sorts last regardless of count
    assert options[-1]["label"] == ""


def test_dte_options_ignores_error_rows(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "error", "return_max_dd": "", "dte": "0"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    assert get_dte_options()["options"] == []


def test_get_results_filters_by_exact_dte_combination(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "dte": "1"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "7", "dte": "0,1"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(dte="0")
    assert [r["combo_id"] for r in result["rows"]] == ["a"]


def test_get_results_dte_filter_does_not_match_a_superset_combination(tmp_path, monkeypatch):
    """Filtering by "0" must not also return rows tagged "0,1" - exact combination
    match only, per the user's explicit ask (single vs multi-DTE, not "contains")."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "dte": "0,1"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(dte="0")
    assert [r["combo_id"] for r in result["rows"]] == ["a"]


def test_get_results_without_dte_param_returns_everything(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "dte": "1"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results()
    assert len(result["rows"]) == 2
