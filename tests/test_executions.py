from __future__ import annotations

import pytest

import src.web.executions as executions_mod
from src.web.models import SweepUIConfig


@pytest.fixture(autouse=True)
def _isolated_executions_file(tmp_path, monkeypatch):
    monkeypatch.setattr(executions_mod, "EXECUTIONS_PATH", tmp_path / "executions.json")


def test_save_and_load_roundtrips_config_and_csv_path():
    cfg = SweepUIConfig(instrument="NIFTY", limit=5)
    executions_mod.save_execution("main sweep", cfg, "/tmp/results.csv")

    loaded_cfg, loaded_csv = executions_mod.load_execution("main sweep")
    assert loaded_cfg.instrument == "NIFTY"
    assert loaded_cfg.limit == 5
    assert loaded_csv == "/tmp/results.csv"


def test_load_missing_execution_raises_key_error():
    with pytest.raises(KeyError):
        executions_mod.load_execution("does not exist")


def test_save_rejects_empty_name():
    with pytest.raises(ValueError):
        executions_mod.save_execution("   ", SweepUIConfig(), None)


def test_list_executions_sorted_newest_first():
    executions_mod.save_execution("first", SweepUIConfig(), None)
    executions_mod.save_execution("second", SweepUIConfig(), None)

    names = [e["name"] for e in executions_mod.list_executions()]
    assert names == ["second", "first"]


def test_saving_same_name_twice_updates_not_duplicates():
    executions_mod.save_execution("main", SweepUIConfig(instrument="NIFTY"), "/a.csv")
    executions_mod.save_execution("main", SweepUIConfig(instrument="BANKNIFTY"), "/b.csv")

    listing = executions_mod.list_executions()
    assert len(listing) == 1
    cfg, csv_path = executions_mod.load_execution("main")
    assert cfg.instrument == "BANKNIFTY"
    assert csv_path == "/b.csv"


def test_created_at_preserved_across_updates_but_updated_at_changes():
    executions_mod.save_execution("main", SweepUIConfig(), "/a.csv")
    first = executions_mod.list_executions()[0]

    executions_mod.save_execution("main", SweepUIConfig(instrument="BANKNIFTY"), "/b.csv")
    second = executions_mod.list_executions()[0]

    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


def test_delete_execution_removes_it():
    executions_mod.save_execution("temp", SweepUIConfig(), None)
    executions_mod.delete_execution("temp")
    assert executions_mod.list_executions() == []
