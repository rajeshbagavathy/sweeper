from __future__ import annotations

import csv
from pathlib import Path

import src.web.app as app_mod
from src.web.app import get_instrument_options, get_results


class _FakeRunState:
    def __init__(self, csv_path: str | None):
        self._csv_path = csv_path

    def snapshot(self) -> dict:
        return {"csv_path": self._csv_path}


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "return_max_dd", "instrument", "dte"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_instrument_options_counts_each_distinct_instrument(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "1", "instrument": "NIFTY", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "2", "instrument": "NIFTY", "dte": "0"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "3", "instrument": "BANKNIFTY", "dte": "1"},
        {"combo_id": "d", "status": "ok", "return_max_dd": "4", "instrument": "", "dte": "0"},  # pre-instrument-tracking row
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    options = get_instrument_options()["options"]
    by_label = {o["label"]: o["count"] for o in options}
    assert by_label["NIFTY"] == 2
    assert by_label["BANKNIFTY"] == 1
    assert by_label[""] == 1
    # unknown ("") sorts last regardless of count
    assert options[-1]["label"] == ""


def test_instrument_options_ignores_error_rows(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "error", "return_max_dd": "", "instrument": "NIFTY", "dte": "0"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    assert get_instrument_options()["options"] == []


def test_get_results_filters_by_instrument(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(instrument="NIFTY")
    assert [r["combo_id"] for r in result["rows"]] == ["a"]


def test_get_results_instrument_filter_combines_with_dte_filter(tmp_path, monkeypatch):
    """Instrument is applied first, but must still combine (AND) with other filters -
    e.g. narrowing to NIFTY + DTE 0 must not also pull in BANKNIFTY DTE 0 rows."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "7", "instrument": "NIFTY", "dte": "1"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results(instrument="NIFTY", dte="0")
    assert [r["combo_id"] for r in result["rows"]] == ["a"]


def test_get_results_without_instrument_param_returns_everything(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_results()
    assert len(result["rows"]) == 2


def test_dte_options_scoped_to_instrument(tmp_path, monkeypatch):
    """DTE pill counts must reflect the instrument filter already narrowing the
    results below it (instrument is the first filter) - not the whole CSV."""
    from src.web.app import get_dte_options

    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0"},
        {"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "NIFTY", "dte": "1"},
        {"combo_id": "c", "status": "ok", "return_max_dd": "7", "instrument": "BANKNIFTY", "dte": "0"},
    ])
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    options = get_dte_options(instrument="NIFTY")["options"]
    by_label = {o["label"]: o["count"] for o in options}
    assert by_label == {"0": 1, "1": 1}


def test_time_buckets_scoped_to_instrument(tmp_path, monkeypatch):
    """Time-bucket counts must also be scoped to the instrument filter, same reasoning
    as the DTE pill counts."""
    from src.web.app import get_time_buckets

    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "status", "return_max_dd", "instrument", "dte", "entry_time"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "return_max_dd": "5", "instrument": "NIFTY", "dte": "0", "entry_time": "09:15"})
        writer.writerow({"combo_id": "b", "status": "ok", "return_max_dd": "9", "instrument": "BANKNIFTY", "dte": "0", "entry_time": "09:15"})
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(str(csv_path)))

    result = get_time_buckets(interval=15, instrument="NIFTY")
    total = sum(b["count"] for b in result["buckets"])
    assert total == 1
