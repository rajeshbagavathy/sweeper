from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.config import SweepConfig
from src.results import parse_number
from src.store import append_row, build_fieldnames, combo_id, flatten, load_existing
from src.sweep import expand, keep_combination


def _sweep(**overrides) -> SweepConfig:
    base = dict(
        fixed={"instrument": "NIFTY"},
        vary={"a": [1, 2], "b": ["x", "y"]},
        exclude=[],
        limit=None,
        shuffle=False,
    )
    base.update(overrides)
    return SweepConfig(**base)


def test_expand_cartesian_product_size():
    combos = expand(_sweep())
    assert len(combos) == 4  # 2 values of a * 2 values of b
    assert all(c["instrument"] == "NIFTY" for c in combos)


def test_expand_applies_limit_after_exclude():
    combos = expand(_sweep(limit=2))
    assert len(combos) == 2


def test_expand_shuffle_preserves_set_and_size():
    combos = expand(_sweep(shuffle=True))
    combos_no_shuffle = expand(_sweep(shuffle=False))
    assert len(combos) == len(combos_no_shuffle)
    key = lambda c: (c["a"], c["b"])
    assert sorted(map(key, combos)) == sorted(map(key, combos_no_shuffle))


def test_keep_combination_excludes_matching_expression():
    combo = {"target_pct": 20, "stoploss_pct": 30}
    assert keep_combination(combo, ["target_pct is not None and target_pct <= stoploss_pct"]) is False

    combo_ok = {"target_pct": 50, "stoploss_pct": 30}
    assert keep_combination(combo_ok, ["target_pct is not None and target_pct <= stoploss_pct"]) is True


def test_keep_combination_ignores_expression_referencing_missing_key():
    combo = {"a": 1}
    # "b" isn't in this combo - should not raise, should not exclude
    assert keep_combination(combo, ["b > 10"]) is True


def test_expand_end_to_end_with_exclude():
    sweep = _sweep(
        vary={"target_pct": [10, 50], "stoploss_pct": [30]},
        exclude=["target_pct is not None and target_pct <= stoploss_pct"],
    )
    combos = expand(sweep)
    assert len(combos) == 1
    assert combos[0]["target_pct"] == 50


def test_combo_id_stable_regardless_of_key_order():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert combo_id(a) == combo_id(b)


def test_combo_id_differs_for_different_values():
    assert combo_id({"x": 1}) != combo_id({"x": 2})


def test_flatten_nested_dict_and_list():
    combo = {"instrument": "NIFTY", "trail_sl": {"x": 20, "y": 10}, "legs": [{"action": "SELL"}]}
    flat = flatten(combo)
    assert flat["instrument"] == "NIFTY"
    assert flat["trail_sl.x"] == 20
    assert flat["trail_sl.y"] == 10
    assert flat["legs.0.action"] == "SELL"


def test_resume_skip_logic(tmp_path: Path):
    csv_path = tmp_path / "results.csv"
    fieldnames = ["combo_id", "run_at", "status", "error", "a"]
    append_row(csv_path, fieldnames, {"combo_id": "abc123", "run_at": "t", "status": "ok", "error": "", "a": 1})
    append_row(csv_path, fieldnames, {"combo_id": "def456", "run_at": "t", "status": "error", "error": "boom", "a": 2})

    statuses, header = load_existing(csv_path)
    assert statuses == {"abc123": "ok", "def456": "error"}
    assert header == fieldnames

    # a combo already marked "ok" should be treated as done; "error" should not be
    assert statuses.get("abc123") == "ok"
    assert statuses.get("nonexistent") is None


def test_build_fieldnames_unions_across_variant_shapes():
    combos = [
        {"trail_sl": None, "target_pct": None},
        {"trail_sl": {"x": 20, "y": 10}, "target_pct": 50},
    ]
    fieldnames = build_fieldnames(combos, metric_names=["total_pnl"])
    assert "trail_sl.x" in fieldnames
    assert "trail_sl.y" in fieldnames
    assert "target_pct" in fieldnames
    assert "total_pnl" in fieldnames
    assert "raw_metrics_json" in fieldnames


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("₹1,23,456", 123456.0),
        ("-45.6%", -45.6),
        ("(2,340)", -2340.0),
        ("1.2L", 120000.0),
        ("2.3Cr", 23000000.0),
        ("—", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected
