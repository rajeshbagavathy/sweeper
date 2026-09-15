from __future__ import annotations

from src.web.time_buckets import all_slot_labels, bucket_counts, bucket_label, filter_by_bucket


def test_bucket_label_15min_matches_exact_slot():
    assert bucket_label("09:20", 15) == "09:15"
    assert bucket_label("09:29", 15) == "09:15"
    assert bucket_label("09:30", 15) == "09:30"


def test_bucket_label_30min_anchored_to_market_open_not_midnight():
    # 30-min grid anchored at 09:15 -> 09:15, 09:45, 10:15... NOT 09:00, 09:30, 10:00
    assert bucket_label("09:15", 30) == "09:15"
    assert bucket_label("09:44", 30) == "09:15"
    assert bucket_label("09:45", 30) == "09:45"
    assert bucket_label("10:14", 30) == "09:45"
    assert bucket_label("10:15", 30) == "10:15"


def test_bucket_label_1hour_anchored_to_market_open():
    assert bucket_label("09:15", 60) == "09:15"
    assert bucket_label("10:14", 60) == "09:15"
    assert bucket_label("10:15", 60) == "10:15"


def test_bucket_label_handles_missing_or_garbage_time():
    assert bucket_label("", 15) is None
    assert bucket_label(None, 15) is None
    assert bucket_label("not a time", 15) is None


def test_all_slot_labels_spans_full_trading_day():
    labels = all_slot_labels(15)
    assert labels[0] == "09:15"
    assert labels[-1] == "15:30"
    assert "12:00" in labels
    assert len(labels) == len(set(labels))  # no duplicates


def test_all_slot_labels_1hour_grid():
    labels = all_slot_labels(60)
    assert labels[0] == "09:15"
    assert "10:15" in labels
    assert "13:15" in labels


def test_bucket_counts_includes_zero_count_slots():
    rows = [{"entry_time": "09:20"}, {"entry_time": "09:20"}, {"entry_time": "10:05"}]
    counts = bucket_counts(rows, 15)
    by_label = {c["label"]: c["count"] for c in counts}
    assert by_label["09:15"] == 2
    assert by_label["10:00"] == 1
    assert by_label["09:30"] == 0  # present with count 0, not omitted


def test_bucket_counts_ignores_rows_with_unparseable_time():
    rows = [{"entry_time": "09:20"}, {"entry_time": ""}, {"entry_time": None}]
    counts = bucket_counts(rows, 15)
    total = sum(c["count"] for c in counts)
    assert total == 1


def test_filter_by_bucket_returns_only_matching_rows():
    rows = [
        {"entry_time": "09:20", "id": "a"},
        {"entry_time": "09:35", "id": "b"},
        {"entry_time": "09:22", "id": "c"},
    ]
    filtered = filter_by_bucket(rows, 15, "09:15")
    assert {r["id"] for r in filtered} == {"a", "c"}
