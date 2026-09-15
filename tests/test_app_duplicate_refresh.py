from __future__ import annotations

import csv

import pytest
from fastapi import HTTPException

import src.web.app as app_mod
from src.web.app import get_pending_duplicate_refresh, start_duplicate_refresh
from src.web import registry


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    monkeypatch.setattr(registry, "PENDING_DUPLICATE_REFRESH_PATH", tmp_path / "pending_duplicate_refresh.csv")
    monkeypatch.setattr(app_mod, "run_state", type("R", (), {"is_running": staticmethod(lambda: False)})())


def _write_pending(rows: list[dict]) -> None:
    with registry.PENDING_DUPLICATE_REFRESH_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["combo_id", "strategy_key", "detected_at"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_get_pending_empty_when_no_queue_file_exists():
    assert get_pending_duplicate_refresh() == {"pending": [], "count": 0}


def test_get_pending_reads_the_queue_file():
    _write_pending([{"combo_id": "a", "strategy_key": "k1", "detected_at": "2026-09-09T00:00:00"}])
    result = get_pending_duplicate_refresh()
    assert result["count"] == 1
    assert result["pending"][0]["combo_id"] == "a"


def test_start_rejects_when_nothing_pending():
    with pytest.raises(HTTPException) as exc_info:
        start_duplicate_refresh()
    assert exc_info.value.status_code == 400


def test_start_rejects_while_a_sweep_is_running(monkeypatch):
    monkeypatch.setattr(app_mod, "run_state", type("R", (), {"is_running": staticmethod(lambda: True)})())
    with pytest.raises(HTTPException) as exc_info:
        start_duplicate_refresh()
    assert exc_info.value.status_code == 409


def test_start_dispatches_correlate_state_and_clears_the_queue(monkeypatch):
    registry.upsert_rows({"existing_cid": {"combo_id": "existing_cid", "instrument": "SENSEX", "strategy_key": "k1"}})
    _write_pending([{"combo_id": "existing_cid", "strategy_key": "k1", "detected_at": "2026-09-09T00:00:00"}])

    captured = {}

    def fake_start(rows, csv_paths=None, force=False, **kwargs):
        captured["rows"] = rows
        captured["csv_paths"] = csv_paths
        captured["force"] = force

    monkeypatch.setattr(app_mod.correlate_state, "start", fake_start)

    result = start_duplicate_refresh()

    assert result == {"ok": True, "queued": 1}
    assert captured["force"] is True
    assert captured["csv_paths"] == [registry.REGISTRY_PATH]
    assert [r["combo_id"] for r in captured["rows"]] == ["existing_cid"]
    assert not registry.PENDING_DUPLICATE_REFRESH_PATH.exists()


def test_start_dedupes_repeated_combo_ids_in_the_queue(monkeypatch):
    """The same combo_id could be captured more than once (e.g. across several
    sweeps before anyone acts on the queue) - must only be force-refreshed once."""
    registry.upsert_rows({"existing_cid": {"combo_id": "existing_cid", "strategy_key": "k1"}})
    _write_pending([
        {"combo_id": "existing_cid", "strategy_key": "k1", "detected_at": "t1"},
        {"combo_id": "existing_cid", "strategy_key": "k1", "detected_at": "t2"},
    ])

    captured = {}
    monkeypatch.setattr(app_mod.correlate_state, "start", lambda rows, **kw: captured.update(rows=rows))

    start_duplicate_refresh()
    assert len(captured["rows"]) == 1


def test_start_rejects_when_no_pending_combo_id_is_in_the_registry():
    _write_pending([{"combo_id": "long_gone", "strategy_key": "k1", "detected_at": "t1"}])
    with pytest.raises(HTTPException) as exc_info:
        start_duplicate_refresh()
    assert exc_info.value.status_code == 400


def test_start_surfaces_correlate_state_runtime_error_as_409(monkeypatch):
    registry.upsert_rows({"existing_cid": {"combo_id": "existing_cid", "strategy_key": "k1"}})
    _write_pending([{"combo_id": "existing_cid", "strategy_key": "k1", "detected_at": "t1"}])

    def raise_running(rows, **kwargs):
        raise RuntimeError("A report download is already in progress.")

    monkeypatch.setattr(app_mod.correlate_state, "start", raise_running)

    with pytest.raises(HTTPException) as exc_info:
        start_duplicate_refresh()
    assert exc_info.value.status_code == 409
    # Queue must survive an actually-failed dispatch, not be cleared regardless.
    assert registry.PENDING_DUPLICATE_REFRESH_PATH.exists()
