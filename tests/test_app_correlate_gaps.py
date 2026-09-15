from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi import HTTPException

import src.web.app as app_mod
from src.web.app import RedownloadGapsRequest, redownload_correlate_gaps


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status", "instrument", "entry_time"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_redownload_gaps_resolves_rows_by_combo_id_and_forces(tmp_path, monkeypatch):
    """The whole point of this endpoint: given just a handful of combo_ids (the ones
    a data_gaps table flagged), look up their full rows from the source CSV(s) and
    kick off the SAME background download job as "Force re-download", but scoped to
    only those rows - not the entire top-N pool."""
    csv_path = tmp_path / "results_web_a.csv"
    _write_csv(csv_path, [
        {"combo_id": "a1", "status": "ok", "instrument": "NIFTY", "entry_time": "09:15"},
        {"combo_id": "a2", "status": "ok", "instrument": "NIFTY", "entry_time": "09:20"},
        {"combo_id": "a3", "status": "ok", "instrument": "NIFTY", "entry_time": "09:25"},
    ])

    recorded: dict = {}

    def fake_start(rows, csv_paths, force=False, **kwargs):
        recorded["rows"] = rows
        recorded["csv_paths"] = csv_paths
        recorded["force"] = force
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(app_mod.correlate_state, "start", fake_start)
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)

    result = redownload_correlate_gaps(
        RedownloadGapsRequest(csv_paths=[str(csv_path)], combo_ids=["a1", "a3"])
    )

    assert result == {"ok": True, "count": 2}
    assert {r["combo_id"] for r in recorded["rows"]} == {"a1", "a3"}
    assert recorded["csv_paths"] == [csv_path]
    assert recorded["force"] is True  # a gap's whole problem is the existing cache - must always replay


def test_redownload_gaps_forwards_parallelism_overrides(tmp_path, monkeypatch):
    """These fields exist specifically so this action's own parallelism is never
    silently tied to the homepage's (see CorrelateState.start's docstring) - the
    endpoint must actually pass them through, not drop them on the floor."""
    csv_path = tmp_path / "results_web_a.csv"
    _write_csv(csv_path, [{"combo_id": "a1", "status": "ok", "instrument": "NIFTY", "entry_time": "09:15"}])

    recorded: dict = {}

    def fake_start(rows, csv_paths, force=False, **kwargs):
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(app_mod.correlate_state, "start", fake_start)
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)

    redownload_correlate_gaps(RedownloadGapsRequest(
        csv_paths=[str(csv_path)], combo_ids=["a1"],
        parallelism=3, parallelism_account2=1, parallelism_account3=0,
    ))

    assert recorded["kwargs"] == {"parallelism": 3, "parallelism_account2": 1, "parallelism_account3": 0}


def test_redownload_gaps_ignores_duplicate_and_unknown_combo_ids(tmp_path, monkeypatch):
    csv_path = tmp_path / "results_web_a.csv"
    _write_csv(csv_path, [{"combo_id": "a1", "status": "ok", "instrument": "NIFTY", "entry_time": "09:15"}])

    recorded: dict = {}
    monkeypatch.setattr(app_mod.correlate_state, "start", lambda rows, csv_paths, force=False, **kw: recorded.update(rows=rows))
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)

    result = redownload_correlate_gaps(
        RedownloadGapsRequest(csv_paths=[str(csv_path)], combo_ids=["a1", "a1", "does-not-exist"])
    )

    assert result == {"ok": True, "count": 1}
    assert len(recorded["rows"]) == 1


def test_redownload_gaps_first_matching_file_wins_for_a_duplicate_combo_id(tmp_path, monkeypatch):
    """The same combo_id can legitimately appear in more than one loaded CSV
    (different sweep runs of the same underlying strategy) - the first file in
    csv_paths order that has it wins, same as the old single-lookup
    find_row_with_path's own "stop at first match" behavior."""
    first = tmp_path / "results_web_a.csv"
    second = tmp_path / "results_web_b.csv"
    _write_csv(first, [{"combo_id": "dup1", "status": "ok", "instrument": "NIFTY", "entry_time": "09:15"}])
    _write_csv(second, [{"combo_id": "dup1", "status": "ok", "instrument": "NIFTY", "entry_time": "13:45"}])

    recorded: dict = {}
    monkeypatch.setattr(app_mod.correlate_state, "start", lambda rows, csv_paths, force=False, **kw: recorded.update(rows=rows))
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)

    redownload_correlate_gaps(RedownloadGapsRequest(csv_paths=[str(first), str(second)], combo_ids=["dup1"]))

    assert recorded["rows"][0]["entry_time"] == "09:15"  # from `first`, not `second`


def test_redownload_gaps_resolves_with_one_pass_per_file_not_per_combo_id(tmp_path, monkeypatch):
    """Regression guard for the exact incident this fixed: resolving each wanted
    combo_id with its own fresh linear scan through every file pegged the CPU for
    15+ minutes on a real ~1700-combo, ~60-file request and never even started
    downloading anything. Reading each file ONCE and looking up all wanted
    combo_ids against that index must cost open() calls proportional to the
    number of FILES, not files times combo_ids."""
    paths = []
    for i in range(3):
        p = tmp_path / f"results_web_{i}.csv"
        _write_csv(p, [{"combo_id": f"c{i}_{j}", "status": "ok", "instrument": "NIFTY", "entry_time": "09:15"} for j in range(5)])
        paths.append(p)

    monkeypatch.setattr(app_mod.correlate_state, "start", lambda rows, csv_paths, force=False, **kw: None)
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)

    open_calls = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        if self in paths:
            open_calls.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    # 15 wanted combo_ids (5 per file x 3 files) - if resolution were still one
    # scan per combo_id, this would open each file up to 15 times each (45
    # opens); reading each file once should cost exactly 3.
    wanted = [f"c{i}_{j}" for i in range(3) for j in range(5)]
    redownload_correlate_gaps(RedownloadGapsRequest(csv_paths=[str(p) for p in paths], combo_ids=wanted))

    assert len(open_calls) == 3


def test_redownload_gaps_rejects_while_a_sweep_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: True)
    with pytest.raises(HTTPException) as exc_info:
        redownload_correlate_gaps(RedownloadGapsRequest(csv_paths=[str(tmp_path / "x.csv")], combo_ids=["a1"]))
    assert exc_info.value.status_code == 409


def test_redownload_gaps_requires_combo_ids(monkeypatch):
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        redownload_correlate_gaps(RedownloadGapsRequest(csv_paths=["/tmp/does-not-matter.csv"], combo_ids=[]))
    assert exc_info.value.status_code == 400


def test_redownload_gaps_requires_csv_paths(monkeypatch):
    monkeypatch.setattr(app_mod.run_state, "is_running", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        redownload_correlate_gaps(RedownloadGapsRequest(csv_paths=[], combo_ids=["a1"]))
    assert exc_info.value.status_code == 400
