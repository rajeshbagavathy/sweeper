from __future__ import annotations

import csv
from pathlib import Path

import src.web.app as app_mod
from src.web.app import get_results


class _FakeRunState:
    def __init__(self, csv_path: str | None):
        self._csv_path = csv_path

    def snapshot(self) -> dict:
        return {"csv_path": self._csv_path}


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "return_max_dd", "entry_time"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_get_results_filters_by_manual_entry_time_range(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "entry_time": "09:16"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "entry_time": "09:30"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "7", "entry_time": "09:52"},
        {"combo_id": "d", "status": "ok", "return_max_dd": "3", "entry_time": "10:15"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(entry_time_from="09:20", entry_time_to="09:52")
    assert {r["combo_id"] for r in result["rows"]} == {"b", "c"}


def test_get_results_entry_time_range_is_inclusive_of_both_bounds(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "entry_time": "09:20"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "entry_time": "09:52"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(entry_time_from="09:20", entry_time_to="09:52")
    assert {r["combo_id"] for r in result["rows"]} == {"a", "b"}


def test_get_results_entry_time_filter_excludes_rows_with_no_entry_time(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "entry_time": "09:30"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "entry_time": ""},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(entry_time_from="09:00", entry_time_to="10:00")
    assert [r["combo_id"] for r in result["rows"]] == ["a"]


def test_get_results_without_entry_time_params_returns_everything(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "entry_time": "09:16"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "entry_time": "14:00"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results()
    assert len(result["rows"]) == 2


def test_get_results_only_from_bound_set(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "entry_time": "09:16"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "entry_time": "14:00"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(entry_time_from="10:00")
    assert [r["combo_id"] for r in result["rows"]] == ["b"]
