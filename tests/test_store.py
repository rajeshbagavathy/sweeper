from __future__ import annotations

import csv
from pathlib import Path

from src import store


def test_combo_id_matches_pre_dual_basis_bare_float_shape():
    # Before the leg-level Stop Loss dual-basis feature, leg_risk.stoploss_pct was a
    # bare float, not {"kind": "percentage", "value": v}. A combo using the ordinary
    # percentage basis must still hash identically to that legacy shape, or resuming
    # any older saved execution with leg-level Stop Loss enabled looks like starting
    # from scratch even though nothing about the actual strategy changed.
    legacy_shape = {"leg_risk": {"stoploss_pct": 35.0, "target_pct": None}}
    new_shape = {"leg_risk": {"stoploss_pct": {"kind": "percentage", "value": 35.0}, "target_pct": None}}
    assert store.combo_id(legacy_shape) == store.combo_id(new_shape)


def test_combo_id_underlying_percentage_is_a_genuinely_different_combo():
    # Underlying % never existed before this feature - it must NOT collapse to the
    # same id as the percentage basis, even at the same numeric value.
    pct = {"leg_risk": {"stoploss_pct": {"kind": "percentage", "value": 35.0}, "target_pct": None}}
    underlying = {"leg_risk": {"stoploss_pct": {"kind": "underlying_percentage", "value": 35.0}, "target_pct": None}}
    assert store.combo_id(pct) != store.combo_id(underlying)


def test_strategy_key_same_for_identical_combo_differing_only_in_end_date():
    """The whole point: a strategy re-swept with a trailing "today" end_date
    must be recognized as the SAME strategy, not a brand new one - confirmed
    live this session that combo_id() itself gives these two totally different
    hashes."""
    today = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09", "entry_time": "09:20"}
    next_week = {**today, "end_date": "2026-09-16"}
    assert store.combo_id(today) != store.combo_id(next_week)  # sanity: combo_id DOES differ
    assert store.strategy_key(today) == store.strategy_key(next_week)


def test_strategy_key_same_for_identical_combo_differing_only_in_start_date():
    a = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09", "entry_time": "09:20"}
    b = {**a, "start_date": "2025-02-01"}
    assert store.strategy_key(a) == store.strategy_key(b)


def test_strategy_key_differs_for_a_genuinely_different_strategy():
    a = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09", "entry_time": "09:20"}
    b = {**a, "entry_time": "09:30"}
    assert store.strategy_key(a) != store.strategy_key(b)


def test_strategy_key_reuses_the_same_backward_compat_normalization_as_combo_id():
    """The leg-risk stoploss_pct dual-basis normalization (_canonical_for_hash)
    must apply here too, not just to combo_id - otherwise a strategy_key
    computed today (new dict shape) would fail to match one computed before
    the dual-basis feature existed (legacy bare-float shape), for no reason
    related to date ranges at all."""
    legacy_shape = {"start_date": "2025-01-01", "end_date": "2026-09-09", "leg_risk": {"stoploss_pct": 35.0, "target_pct": None}}
    new_shape = {"start_date": "2025-01-01", "end_date": "2026-09-16", "leg_risk": {"stoploss_pct": {"kind": "percentage", "value": 35.0}, "target_pct": None}}
    assert store.strategy_key(legacy_shape) == store.strategy_key(new_shape)


def test_combo_id_stoploss_pct_none_is_unaffected():
    a = {"leg_risk": {"stoploss_pct": None, "target_pct": None}}
    b = {"leg_risk": {"stoploss_pct": None, "target_pct": None}}
    assert store.combo_id(a) == store.combo_id(b)


def test_build_fieldnames_includes_dte_column():
    fieldnames = store.build_fieldnames([{"a": 1}], ["total_pnl"])
    assert "dte" in fieldnames
    assert fieldnames.index("dte") < fieldnames.index("total_pnl")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_migrate_header_if_needed_is_a_noop_when_file_missing():
    result = store.migrate_header_if_needed(Path("/tmp/does-not-exist-xyz.csv"), ["combo_id", "dte"])
    assert result == ["combo_id", "dte"]


def test_migrate_header_if_needed_is_a_noop_when_already_up_to_date(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status", "dte"], [{"combo_id": "a", "status": "ok", "dte": "0"}])

    result = store.migrate_header_if_needed(csv_path, ["combo_id", "status", "dte"])

    assert result == ["combo_id", "status", "dte"]
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"combo_id": "a", "status": "ok", "dte": "0"}]


def test_migrate_header_if_needed_adds_missing_column_preserving_old_rows(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [
        {"combo_id": "a", "status": "ok"},
        {"combo_id": "b", "status": "error"},
    ])

    result = store.migrate_header_if_needed(csv_path, ["combo_id", "status", "dte"])

    assert result == ["combo_id", "status", "dte"]
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [
        {"combo_id": "a", "status": "ok", "dte": ""},
        {"combo_id": "b", "status": "error", "dte": ""},
    ]


def test_migrate_header_if_needed_does_not_touch_existing_data_values(tmp_path):
    """Regression guard: migrating must never alter pre-existing column values,
    only add blank columns for genuinely new fields."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "return_max_dd"], [
        {"combo_id": "a", "return_max_dd": "9.72"},
    ])

    store.migrate_header_if_needed(csv_path, ["combo_id", "return_max_dd", "dte"])

    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["return_max_dd"] == "9.72"


def test_append_row_after_migration_produces_a_consistent_file(tmp_path):
    """The real end-to-end concern: migrate an old file, then append a new-format
    row, and the file must stay a single, consistently-shaped CSV (same column
    count on every line) - not old rows with N columns and new rows with N+1."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [{"combo_id": "old", "status": "ok"}])

    fieldnames = store.migrate_header_if_needed(csv_path, ["combo_id", "status", "dte"])
    store.append_row(csv_path, fieldnames, {"combo_id": "new", "status": "ok", "dte": "0,1"})

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["combo_id", "status", "dte"]
        rows = list(reader)
    assert rows[0] == {"combo_id": "old", "status": "ok", "dte": ""}
    assert rows[1] == {"combo_id": "new", "status": "ok", "dte": "0,1"}


def test_update_row_replaces_matching_row_in_place_preserving_others(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status", "total_pnl"], [
        {"combo_id": "a", "status": "ok", "total_pnl": "100"},
        {"combo_id": "b", "status": "ok", "total_pnl": "200"},
    ])

    ok = store.update_row(csv_path, "a", {"combo_id": "a", "status": "ok", "total_pnl": "999"})

    assert ok is True
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0] == {"combo_id": "a", "status": "ok", "total_pnl": "999"}
    assert rows[1] == {"combo_id": "b", "status": "ok", "total_pnl": "200"}  # untouched


def test_update_row_expands_header_for_new_columns(tmp_path):
    """A refreshed row can carry columns the file was never written with (e.g.
    brokerage_amount, added after this file was first created) - the header must
    expand, with every OTHER existing row getting a blank value for it, not a
    file with inconsistent column counts per line."""
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [
        {"combo_id": "a", "status": "ok"},
        {"combo_id": "b", "status": "ok"},
    ])

    store.update_row(csv_path, "a", {"combo_id": "a", "status": "ok", "brokerage_amount": "4814.4"})

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["combo_id", "status", "brokerage_amount"]
        rows = list(reader)
    assert rows[0] == {"combo_id": "a", "status": "ok", "brokerage_amount": "4814.4"}
    assert rows[1] == {"combo_id": "b", "status": "ok", "brokerage_amount": ""}


def test_update_row_returns_false_when_combo_id_not_in_this_file(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [{"combo_id": "a", "status": "ok"}])

    ok = store.update_row(csv_path, "does-not-exist", {"combo_id": "does-not-exist", "status": "ok"})

    assert ok is False
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"combo_id": "a", "status": "ok"}]  # file untouched


def test_update_row_missing_file_returns_false():
    from pathlib import Path
    assert store.update_row(Path("/nonexistent/results.csv"), "a", {"combo_id": "a"}) is False


def test_update_rows_replaces_every_matching_row_in_one_pass(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status", "total_pnl"], [
        {"combo_id": "a", "status": "ok", "total_pnl": "100"},
        {"combo_id": "b", "status": "ok", "total_pnl": "200"},
        {"combo_id": "c", "status": "ok", "total_pnl": "300"},
    ])

    found = store.update_rows(csv_path, {
        "a": {"combo_id": "a", "status": "ok", "total_pnl": "999"},
        "c": {"combo_id": "c", "status": "ok", "total_pnl": "777"},
    })

    assert found == {"a", "c"}
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [
        {"combo_id": "a", "status": "ok", "total_pnl": "999"},
        {"combo_id": "b", "status": "ok", "total_pnl": "200"},  # untouched
        {"combo_id": "c", "status": "ok", "total_pnl": "777"},
    ]


def test_update_rows_returns_only_the_combo_ids_actually_found_in_this_file(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [{"combo_id": "a", "status": "ok"}])

    found = store.update_rows(csv_path, {
        "a": {"combo_id": "a", "status": "ok"},
        "not-in-this-file": {"combo_id": "not-in-this-file", "status": "ok"},
    })

    assert found == {"a"}


def test_update_rows_expands_header_for_new_columns(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [
        {"combo_id": "a", "status": "ok"},
        {"combo_id": "b", "status": "ok"},
    ])

    store.update_rows(csv_path, {"a": {"combo_id": "a", "status": "ok", "brokerage_amount": "4814.4"}})

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["combo_id", "status", "brokerage_amount"]
        rows = list(reader)
    assert rows[0] == {"combo_id": "a", "status": "ok", "brokerage_amount": "4814.4"}
    assert rows[1] == {"combo_id": "b", "status": "ok", "brokerage_amount": ""}  # untouched row gets blank


def test_update_rows_empty_updates_leaves_file_untouched_and_unopened(tmp_path):
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, ["combo_id", "status"], [{"combo_id": "a", "status": "ok"}])
    before_mtime = csv_path.stat().st_mtime_ns

    found = store.update_rows(csv_path, {})

    assert found == set()
    assert csv_path.stat().st_mtime_ns == before_mtime


def test_update_rows_missing_file_returns_empty_set():
    from pathlib import Path
    assert store.update_rows(Path("/nonexistent/results.csv"), {"a": {"combo_id": "a"}}) == set()


def test_update_rows_matches_update_row_called_once_per_id(tmp_path):
    """The whole point of update_rows is to replace N calls to update_row with one
    batched pass - the end result for a given file must be identical either way."""
    rows = [{"combo_id": f"c{i}", "status": "ok", "total_pnl": str(i * 10)} for i in range(20)]
    updates = {f"c{i}": {"combo_id": f"c{i}", "status": "ok", "total_pnl": str(i * 999)} for i in range(0, 20, 3)}

    via_batch = tmp_path / "batch.csv"
    _write_csv(via_batch, ["combo_id", "status", "total_pnl"], rows)
    store.update_rows(via_batch, updates)

    via_loop = tmp_path / "loop.csv"
    _write_csv(via_loop, ["combo_id", "status", "total_pnl"], rows)
    for cid, new_row in updates.items():
        store.update_row(via_loop, cid, new_row)

    with via_batch.open(newline="") as f:
        batch_rows = list(csv.DictReader(f))
    with via_loop.open(newline="") as f:
        loop_rows = list(csv.DictReader(f))
    assert batch_rows == loop_rows
