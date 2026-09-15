from __future__ import annotations

import csv
from pathlib import Path

import pytest

import src.web.app as app_mod
import src.web.executions as executions_mod
from src.web.app import SaveExecutionRequest, get_execution, list_executions, narrow, remove_execution, save_execution
from src.web.models import NumericRange, SweepUIConfig


class _FakeRunState:
    def __init__(self, csv_path: str | None = None):
        self._csv_path = csv_path

    def snapshot(self) -> dict:
        return {"csv_path": self._csv_path}


@pytest.fixture(autouse=True)
def _isolated_executions_file(tmp_path, monkeypatch):
    monkeypatch.setattr(executions_mod, "EXECUTIONS_PATH", tmp_path / "executions.json")


def test_save_load_list_delete_execution_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(csv_path=None))

    cfg = SweepUIConfig(instrument="NIFTY")
    save_execution("my run", SaveExecutionRequest(cfg=cfg, csv_path="/some/path.csv"))

    listing = list_executions()
    assert len(listing) == 1
    assert listing[0]["name"] == "my run"
    assert listing[0]["csv_path"] == "/some/path.csv"

    fetched = get_execution("my run")
    assert fetched["config"].instrument == "NIFTY"
    assert fetched["csv_path"] == "/some/path.csv"

    remove_execution("my run")
    assert list_executions() == []


def test_save_execution_never_falls_back_to_current_run_state(monkeypatch):
    """Regression test: saving without an explicit csv_path must store None, even
    while some unrelated sweep is live in run_state - the old fallback silently
    attached whichever run happened to be running to a completely different saved
    execution, which is exactly what orphaned "NIFTY MIDDAY_1121_1156"'s csv_path."""
    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(csv_path="/active/run.csv"))

    save_execution("no explicit csv", SaveExecutionRequest(cfg=SweepUIConfig()))

    fetched = get_execution("no explicit csv")
    assert fetched["csv_path"] is None


def test_get_execution_missing_raises_404(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(app_mod, "run_state", _FakeRunState())
    with pytest.raises(HTTPException) as exc_info:
        get_execution("nope")
    assert exc_info.value.status_code == 404


def test_narrow_auto_backs_up_pre_narrow_config(tmp_path, monkeypatch):
    """Regression test for the "don't lose the wide sweep's config" requirement:
    calling /api/narrow must preserve the config exactly as it was beforehand, under
    a discoverable auto-generated name, before overwriting sweep_ui.yaml."""
    csv_path = tmp_path / "results_web_20260101_000000.csv"
    fieldnames = ["combo_id", "status", "return_max_dd", "entry_time", "exit_time"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok", "return_max_dd": "2.5", "entry_time": "09:20", "exit_time": "15:10"})

    monkeypatch.setattr(app_mod, "run_state", _FakeRunState(csv_path=str(csv_path)))

    wide_cfg = SweepUIConfig(instrument="NIFTY")
    wide_cfg.entry_time.start = "09:15"
    wide_cfg.entry_time.end = "10:00"
    wide_cfg.entry_time.interval_minutes = 15
    monkeypatch.setattr(app_mod, "load_ui_config", lambda: wide_cfg)
    monkeypatch.setattr(app_mod, "save_ui_config", lambda cfg: None)  # don't touch the real config/sweep_ui.yaml

    narrow(top_n=10)

    listing = list_executions()
    backups = [e for e in listing if e["name"].startswith("auto-before-narrow-")]
    assert len(backups) == 1
    assert backups[0]["csv_path"] == str(csv_path)

    backed_up_cfg = get_execution(backups[0]["name"])["config"]
    assert backed_up_cfg.entry_time.start == "09:15"
    assert backed_up_cfg.entry_time.end == "10:00"
