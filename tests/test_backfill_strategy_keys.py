from __future__ import annotations

import pytest

from scripts import backfill_strategy_keys
from scripts.backfill_strategy_keys import compute_verified_keys
from src.store import combo_id, strategy_key
from src.web import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")


def _flatten_combo(combo: dict, instrument: str = "SENSEX") -> dict:
    """A minimal, correctly-flattened registry row for a real combo dict -
    matches exactly what row_to_combo(row) will reconstruct back, so this
    fixture always round-trips (mirrors a real "modern-shape" row)."""
    return {
        "combo_id": combo_id(combo),
        "instrument": instrument,
        "start_date": combo["start_date"],
        "end_date": combo["end_date"],
        "entry_time": combo["entry_time"],
        "exit_time": combo.get("exit_time", "15:10"),
        "legs.0.action": "SELL",
        "legs.0.lots": "10.0",
        "legs.0.option_type": "CE",
        "legs.0.strike.mode": "premium_range",
        "legs.0.strike.value": "50.0",
    }


def _real_combo(**overrides) -> dict:
    combo = {
        "instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09",
        "entry_time": "09:20", "exit_time": "15:10",
        "legs": [{"action": "SELL", "lots": 10.0, "option_type": "CE", "strike": {"mode": "premium_range", "value": 50.0}}],
        "leg_risk": {"target_pct": None, "stoploss_pct": None, "trail": None, "momentum": None, "reentry_sl": None},
        "stoploss": None, "target": None, "trail_sl": None,
    }
    combo.update(overrides)
    return combo


def test_compute_verified_keys_backfills_a_round_tripping_row():
    combo = _real_combo()
    row = _flatten_combo(combo)
    verified, per_instrument = compute_verified_keys([row])
    assert verified[row["combo_id"]] == strategy_key(combo)
    assert per_instrument["SENSEX"] == (1, 1)


def test_compute_verified_keys_skips_a_row_that_already_has_a_strategy_key():
    combo = _real_combo()
    row = {**_flatten_combo(combo), "strategy_key": "already_set"}
    verified, per_instrument = compute_verified_keys([row])
    assert verified == {}
    assert per_instrument == {}


def test_compute_verified_keys_leaves_a_non_round_tripping_row_unverified():
    """A row whose flattened shape row_to_combo can't correctly reconstruct
    (simulated here with a malformed/unexpected strike shape) must NOT get a
    strategy_key - fails safely open rather than trusting a wrong hash."""
    combo = _real_combo()
    row = _flatten_combo(combo)
    row["legs.0.strike.mode"] = "premium_range"
    row["legs.0.strike.value"] = "999.0"  # doesn't match what was actually hashed
    verified, per_instrument = compute_verified_keys([row])
    assert row["combo_id"] not in verified
    assert per_instrument["SENSEX"] == (0, 1)


def test_compute_verified_keys_strips_dte_variant_suffix_before_comparing():
    """A "_dteN"-suffixed combo_id's base hash never included the suffix (see
    runner._capture_individual_dte_reports) - must still verify correctly."""
    combo = _real_combo()
    row = _flatten_combo(combo)
    row["combo_id"] = f"{row['combo_id']}_dte0"
    verified, per_instrument = compute_verified_keys([row])
    assert verified[row["combo_id"]] == strategy_key(combo)


def test_main_writes_only_verified_keys_and_leaves_others_blank(tmp_path, monkeypatch):
    good_combo = _real_combo()
    good_row = _flatten_combo(good_combo)
    bad_row = _flatten_combo(_real_combo(entry_time="10:00"))
    bad_row["combo_id"] = "not_a_real_hash_at_all"  # will never round-trip

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    registry.REGISTRY_PATH = output_dir / "combo_registry.csv"
    registry.upsert_rows({good_row["combo_id"]: good_row, bad_row["combo_id"]: bad_row})

    monkeypatch.setattr("sys.argv", ["backfill_strategy_keys.py", "--output-dir", str(output_dir), "--apply"])
    result = backfill_strategy_keys.main()

    assert result == 0
    reg = registry.read_registry()
    assert reg[good_row["combo_id"]]["strategy_key"] == strategy_key(good_combo)
    assert reg[bad_row["combo_id"]].get("strategy_key", "") == ""


def test_main_reads_registry_under_the_given_output_dir_not_the_real_one(tmp_path, monkeypatch):
    real_registry_path = tmp_path / "should_never_be_read" / "combo_registry.csv"
    monkeypatch.setattr(registry, "REGISTRY_PATH", real_registry_path)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    fake_registry_path = output_dir / "combo_registry.csv"
    combo = _real_combo()
    row = _flatten_combo(combo)
    import csv as csv_mod
    with fake_registry_path.open("w", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)

    monkeypatch.setattr("sys.argv", ["backfill_strategy_keys.py", "--output-dir", str(output_dir)])
    result = backfill_strategy_keys.main()

    assert result == 0
    assert not real_registry_path.exists()
