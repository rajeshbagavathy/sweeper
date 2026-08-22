from __future__ import annotations

from src.web.app import _rmdd_sort_key


def test_rmdd_sort_key_parses_float():
    assert _rmdd_sort_key({"return_max_dd": "1.69"}) == 1.69


def test_rmdd_sort_key_missing_or_garbage_sinks_to_bottom():
    assert _rmdd_sort_key({"return_max_dd": ""}) == float("-inf")
    assert _rmdd_sort_key({}) == float("-inf")
    assert _rmdd_sort_key({"return_max_dd": "N/A"}) == float("-inf")


def test_rows_sort_descending_with_garbage_row():
    rows = [
        {"return_max_dd": "0.5"},
        {"return_max_dd": "2.1"},
        {"return_max_dd": ""},
        {"return_max_dd": "1.0"},
    ]
    rows.sort(key=_rmdd_sort_key, reverse=True)
    assert [r["return_max_dd"] for r in rows] == ["2.1", "1.0", "0.5", ""]
