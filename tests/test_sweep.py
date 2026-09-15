from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.config import SweepConfig
from src.results import parse_number
from src.store import append_row, build_fieldnames, combo_id, flatten, load_existing
from src.sweep import (
    EXACT_COUNT_THRESHOLD,
    MAX_COMBINATIONS,
    TooManyCombinations,
    count_or_estimate,
    estimate_survival,
    expand,
    iter_shuffled_combos,
    keep_combination,
    raw_count,
    unrank,
)


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


def test_expand_raises_before_hanging_on_huge_configs():
    """A config whose raw Cartesian product blows past MAX_COMBINATIONS must fail
    fast with a clear error, not hang trying to materialize a giant list - this is
    exactly what happened with a real over-ranged web UI config."""
    n = MAX_COMBINATIONS // 1000 + 1
    huge = _sweep(vary={"a": list(range(n)), "b": list(range(1000))})  # just over MAX_COMBINATIONS raw
    with pytest.raises(TooManyCombinations):
        expand(huge)


def test_expand_allows_exactly_at_the_cap():
    at_cap = _sweep(vary={"a": list(range(MAX_COMBINATIONS)), "b": [1]})
    combos = expand(at_cap)
    assert len(combos) == MAX_COMBINATIONS


def test_raw_count_matches_manual_product():
    assert raw_count([[1, 2], ["x", "y", "z"]]) == 6
    assert raw_count([]) == 1  # no dimensions - one (empty) combination
    assert raw_count([[1], [1, 2], [1, 2, 3]]) == 6


def test_unrank_matches_product_order():
    """unrank(i) must agree with plain itertools.product order at every index - it's
    only useful as a drop-in for expand()'s Cartesian product if it really is one."""
    import itertools

    keys = ["a", "b", "c"]
    value_lists = [[1, 2], ["x", "y", "z"], [True, False]]
    fixed = {"instrument": "NIFTY"}
    expected = [dict(fixed, **dict(zip(keys, values))) for values in itertools.product(*value_lists)]
    for i, combo in enumerate(expected):
        assert unrank(keys, value_lists, fixed, i) == combo


def test_estimate_survival_is_exact_when_sample_covers_the_whole_space():
    sweep = _sweep(
        vary={"target_pct": [10, 50], "stoploss_pct": [30]},
        exclude=["target_pct is not None and target_pct <= stoploss_pct"],
    )
    # sample_size >= raw_count -> every combo gets checked, so the "estimate" is exact
    estimated, sample = estimate_survival(sweep, sample_size=100, seed=1)
    assert estimated == 1
    assert sample[0]["target_pct"] == 50


def test_estimate_survival_respects_limit():
    sweep = _sweep(vary={"a": list(range(100)), "b": [1]}, limit=5)
    estimated, _ = estimate_survival(sweep, sample_size=100, seed=1)
    assert estimated == 5


def test_count_or_estimate_is_exact_below_threshold():
    sweep = _sweep()  # 4 raw combos, well under EXACT_COUNT_THRESHOLD
    result = count_or_estimate(sweep)
    assert result == {"raw_count": 4, "count": 4, "estimated": False, "sample": expand(sweep)}


def test_count_or_estimate_is_estimated_above_threshold():
    n = EXACT_COUNT_THRESHOLD + 1
    sweep = _sweep(vary={"a": list(range(n)), "b": [1]})
    result = count_or_estimate(sweep, sample_size=500, seed=1)
    assert result["raw_count"] == n
    assert result["estimated"] is True
    assert result["count"] == n  # nothing excluded - every sampled combo survives
    assert len(result["sample"]) == 10


def test_iter_shuffled_combos_covers_every_surviving_combo_exactly_once():
    sweep = _sweep(
        vary={"target_pct": [10, 50, 90], "stoploss_pct": [30]},
        exclude=["target_pct is not None and target_pct <= stoploss_pct"],
    )
    from_iter = list(iter_shuffled_combos(sweep, seed=1))
    from_expand = expand(sweep)
    key = lambda c: (c["target_pct"], c["stoploss_pct"])
    assert sorted(map(key, from_iter)) == sorted(map(key, from_expand))


def test_iter_shuffled_combos_order_differs_from_raw_product_order():
    """The whole point - a large batch pulled from the front of this stream must not
    just be the first N raw itertools.product combos in disguise."""
    sweep = _sweep(vary={"a": list(range(50)), "b": list(range(50))})
    shuffled_order = [c["a"] for c in iter_shuffled_combos(sweep, seed=1)]
    raw_order = [c["a"] for c in expand(_sweep(vary={"a": list(range(50)), "b": list(range(50))}))]
    assert shuffled_order != raw_order
    assert sorted(shuffled_order) == sorted(raw_order)


def test_iter_shuffled_combos_respects_limit():
    sweep = _sweep(vary={"a": list(range(100)), "b": [1]}, limit=7)
    assert len(list(iter_shuffled_combos(sweep, seed=1))) == 7


def test_iter_shuffled_combos_raises_before_hanging_on_huge_configs():
    n = MAX_COMBINATIONS // 1000 + 1
    huge = _sweep(vary={"a": list(range(n)), "b": list(range(1000))})
    with pytest.raises(TooManyCombinations):
        next(iter_shuffled_combos(huge))


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
