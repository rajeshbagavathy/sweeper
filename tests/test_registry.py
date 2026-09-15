from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.web import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")


def _write_report(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (Index, Entry Date, P/L) - mirrors tests/test_portfolio.py's helper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"Index","Entry Date","Entry Time","Exit Date","Exit Time","Type","Strike","B/S","Qty","Entry Price","Exit Price","Vix","P/L"']
    for index, date, pnl in rows:
        lines.append(f'"{index}","{date}"," 9:20:00 AM","{date}"," 3:14:00 PM","","","","","","","10.5","{pnl}"')
    path.write_text("\n".join(lines) + "\n")


# --- upsert_rows / read_registry ---

def test_upsert_rows_creates_the_registry_when_it_does_not_exist_yet():
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok", "total_pnl": "100"}})
    assert registry.REGISTRY_PATH.exists()
    assert registry.read_registry() == {"a": {"combo_id": "a", "status": "ok", "total_pnl": "100"}}


def test_upsert_rows_overwrites_an_existing_combo_id_in_place():
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok", "total_pnl": "100"}})
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok", "total_pnl": "999"}})
    assert registry.read_registry()["a"]["total_pnl"] == "999"


def test_upsert_rows_appends_a_genuinely_new_combo_id_alongside_existing_ones():
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok", "total_pnl": "100"}})
    registry.upsert_rows({"b": {"combo_id": "b", "status": "ok", "total_pnl": "200"}})
    reg = registry.read_registry()
    assert set(reg) == {"a", "b"}
    assert reg["a"]["total_pnl"] == "100"
    assert reg["b"]["total_pnl"] == "200"


def test_upsert_rows_batch_mixes_updates_and_new_inserts_in_one_call():
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok", "total_pnl": "100"}})
    registry.upsert_rows({
        "a": {"combo_id": "a", "status": "ok", "total_pnl": "111"},
        "b": {"combo_id": "b", "status": "ok", "total_pnl": "200"},
    })
    reg = registry.read_registry()
    assert reg["a"]["total_pnl"] == "111"
    assert reg["b"]["total_pnl"] == "200"


def test_upsert_rows_expands_the_header_for_a_new_column(tmp_path):
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok"}})
    registry.upsert_rows({"b": {"combo_id": "b", "status": "ok", "brokerage_amount": "50"}})
    with registry.REGISTRY_PATH.open(newline="") as f:
        header = csv.DictReader(f).fieldnames
    assert "brokerage_amount" in header
    reg = registry.read_registry()
    assert reg["a"]["brokerage_amount"] == ""  # blank-filled for the older row


def test_upsert_rows_batched_append_handles_many_new_rows_in_one_call():
    """Regression guard: new rows must be appended in ONE file open, not once per
    row via store.append_row - see upsert_rows' own docstring for the exact O(N)
    file-I/O incident this mirrors. Correctness check at a scale that would make a
    per-row-open bug obvious if reintroduced (each row must still land intact)."""
    rows = {f"c{i}": {"combo_id": f"c{i}", "status": "ok", "total_pnl": str(i)} for i in range(500)}
    registry.upsert_rows(rows)
    reg = registry.read_registry()
    assert len(reg) == 500
    assert reg["c0"]["total_pnl"] == "0"
    assert reg["c499"]["total_pnl"] == "499"


def test_upsert_rows_does_not_drop_columns_unique_to_a_non_first_row():
    """Regression guard for a real bug confirmed live this session: rows from
    DIFFERENT sweep configs over time can have genuinely different column sets
    (different leg counts/strike modes/etc) - using just one row's keys as the
    header silently dropped every OTHER row's extra columns via csv.DictWriter's
    extrasaction="ignore" (a live migration run lost legs.0.strike.upper/lower,
    target.kind/value, target_pct, and stoploss_pct this way before the fix)."""
    registry.upsert_rows({
        "a": {"combo_id": "a", "status": "ok", "total_pnl": "100"},
        "b": {"combo_id": "b", "status": "ok", "total_pnl": "200", "target_pct": "50"},
    })
    reg = registry.read_registry()
    assert reg["b"]["target_pct"] == "50"
    assert reg["a"]["target_pct"] == ""  # blank-filled, not dropped from the header


def test_upsert_rows_does_not_drop_columns_when_updating_an_existing_row():
    """Same guard, but for the update_rows path (an existing combo_id being
    overwritten) rather than the append path - a batch mixing an update whose
    row has fewer columns than another row in the SAME call must not truncate
    the update's own written row to just the smaller shape."""
    registry.upsert_rows({"a": {"combo_id": "a", "status": "ok", "total_pnl": "100"}})
    registry.upsert_rows({
        "a": {"combo_id": "a", "status": "ok", "total_pnl": "150", "target_pct": "50"},
        "b": {"combo_id": "b", "status": "ok", "total_pnl": "200"},
    })
    reg = registry.read_registry()
    assert reg["a"]["target_pct"] == "50"


def test_upsert_rows_noop_on_empty_dict():
    registry.upsert_rows({})
    assert not registry.REGISTRY_PATH.exists()


def test_read_registry_empty_when_file_does_not_exist():
    assert registry.read_registry() == {}


# --- report_completeness ---

def test_report_completeness_true_for_a_clean_single_dte_report():
    dates = ["2026-08-07", "2026-08-14", "2026-08-21", "2026-08-28"]  # all Fridays
    assert registry.report_completeness(
        dates, dte="1", recorded_start="2026-08-05", recorded_end="2026-08-30",
    ) is True


def test_report_completeness_false_when_single_dte_has_mixed_weekdays():
    """The exact contamination bug confirmed live this session: a single-DTE
    combo's report mixing in multiple weekdays' trades."""
    dates = ["2026-08-03", "2026-08-04", "2026-08-07", "2026-08-10", "2026-08-11"]
    assert registry.report_completeness(
        dates, dte="0", recorded_start="2026-08-01", recorded_end="2026-08-14",
    ) is False


def test_report_completeness_ignores_weekday_check_for_a_multi_dte_combo():
    """dte="0,1,2,3,4" (a genuine multi-select) legitimately spans every weekday -
    must not be flagged just because more than one weekday appears."""
    dates = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
    assert registry.report_completeness(
        dates, dte="0,1,2,3,4", recorded_start="2026-08-01", recorded_end="2026-08-10",
    ) is True


def test_report_completeness_false_when_short_of_recorded_start_or_end():
    """The exact partial-download bug confirmed live this session: a report
    recorded as covering 2026-08-05 to 2026-09-07 but only actually reaching
    2026-08-11 to 2026-09-01 - nearly a week short at both ends."""
    dates = ["2026-08-11", "2026-08-18", "2026-08-25", "2026-09-01"]
    assert registry.report_completeness(
        dates, dte="0", recorded_start="2026-08-05", recorded_end="2026-09-07",
    ) is False


def test_report_completeness_tolerates_a_small_boundary_gap_from_weekday_alignment():
    """recorded_start a Wednesday, but this combo only trades Fridays - the
    nearest real trade is a few days later, which is fine, not a red flag."""
    dates = ["2026-08-07", "2026-08-14", "2026-08-21"]  # Fridays
    assert registry.report_completeness(
        dates, dte="1", recorded_start="2026-08-05", recorded_end="2026-08-21",  # Wed start
    ) is True


def test_report_completeness_false_when_no_entry_dates_at_all():
    assert registry.report_completeness(
        [], dte="0", recorded_start="2026-08-01", recorded_end="2026-08-30",
    ) is False


# --- compute_freshness_fields ---

def test_compute_freshness_fields_from_a_real_report(tmp_path):
    report_path = tmp_path / "reports" / "nifty" / "abc.csv"
    _write_report(report_path, [("1", "2026-08-07", "100"), ("2", "2026-08-14", "-50")])

    fields = registry.compute_freshness_fields(
        report_path, dte="1", recorded_start="2026-08-05", recorded_end="2026-08-17",
    )
    assert fields["report_window_start"] == "2026-08-07"
    assert fields["report_window_end"] == "2026-08-14"
    assert fields["report_complete"] is True
    assert fields["last_replayed_at"] is not None


def test_compute_freshness_fields_when_report_file_does_not_exist(tmp_path):
    fields = registry.compute_freshness_fields(
        tmp_path / "missing.csv", dte="0", recorded_start="2026-08-01", recorded_end="2026-08-30",
    )
    assert fields == {
        "last_replayed_at": None, "report_window_start": None,
        "report_window_end": None, "report_complete": False,
    }


def test_compute_freshness_fields_when_report_has_no_parseable_rows(tmp_path):
    report_path = tmp_path / "empty.csv"
    report_path.write_text('"Index","Entry Date","P/L"\n')
    fields = registry.compute_freshness_fields(
        report_path, dte="0", recorded_start="2026-08-01", recorded_end="2026-08-30",
    )
    assert fields["report_window_start"] is None
    assert fields["report_complete"] is False
    assert fields["last_replayed_at"] is not None  # file exists, so this is still knowable
