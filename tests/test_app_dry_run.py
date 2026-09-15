from __future__ import annotations

import pytest

import src.web.app as app_mod
from src.store import strategy_key
from src.web.app import dry_run
from src.web.expand import expand_ui_config
from src.web.models import SweepUIConfig
from src.web import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")


def test_dry_run_duplicate_count_zero_when_registry_empty():
    result = dry_run(SweepUIConfig())
    assert result["duplicate_count"] == 0


def test_dry_run_duplicate_count_exact_for_a_small_sweep():
    """The exact-count path (small enough for expand_ui_config to be fast) must
    report an EXACT duplicate_count, not an estimate."""
    cfg = SweepUIConfig()
    combos = expand_ui_config(cfg)
    assert not app_mod.estimate_ui_config(cfg)["estimated"]  # sanity: this test is exercising the exact path

    # Pretend 2 of these combos already have a registry record under a
    # different combo_id (as if swept before with a different end_date).
    registry.upsert_rows({
        f"existing_{i}": {"combo_id": f"existing_{i}", "strategy_key": strategy_key(combos[i])}
        for i in range(2)
    })

    result = dry_run(cfg)
    assert result["duplicate_count"] == 2


def test_dry_run_duplicate_count_zero_when_flag_is_off():
    cfg = SweepUIConfig(skip_known_duplicate_strategies=False)
    combos = expand_ui_config(cfg)
    registry.upsert_rows({"existing": {"combo_id": "existing", "strategy_key": strategy_key(combos[0])}})

    result = dry_run(cfg)
    assert result["duplicate_count"] == 0
