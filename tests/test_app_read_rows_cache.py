from __future__ import annotations

import csv
import os
import time
from pathlib import Path

from src.web.app import _read_rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["combo_id", "status"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_read_rows_cache_hit_returns_equal_data_without_rereading(tmp_path, monkeypatch):
    path = tmp_path / "results_web_a.csv"
    _write_csv(path, [{"combo_id": "a", "status": "ok"}])

    read_count = 0
    real_open = open

    def counting_open(file, *a, **kw):
        nonlocal read_count
        if str(file) == str(path):
            read_count += 1
        return real_open(file, *a, **kw)

    rows1, _ = _read_rows([path])
    monkeypatch.setattr("builtins.open", counting_open)
    rows2, _ = _read_rows([path])  # same path, same mtime - must be a cache hit, no open() call

    assert rows1 == rows2 == [{"combo_id": "a", "status": "ok"}]
    assert read_count == 0


def test_read_rows_cache_invalidates_when_the_file_is_modified(tmp_path):
    path = tmp_path / "results_web_a.csv"
    _write_csv(path, [{"combo_id": "a", "status": "ok"}])
    rows1, _ = _read_rows([path])
    assert len(rows1) == 1

    # Force a distinct mtime regardless of filesystem clock resolution - the
    # cache key is (path, mtime_ns), so this must be enough to bypass the
    # cached (now stale) single-row result.
    _write_csv(path, [{"combo_id": "a", "status": "ok"}, {"combo_id": "b", "status": "ok"}])
    new_mtime = time.time() + 5
    os.utime(path, (new_mtime, new_mtime))

    rows2, _ = _read_rows([path])
    assert len(rows2) == 2


def test_read_rows_returns_a_fresh_list_each_call_not_the_cached_object(tmp_path):
    """The actual correctness risk this cache had to be built around: some
    callers (e.g. app._filtered_results, when every optional filter is None)
    call .sort() directly on the list _read_rows hands back, in place. If two
    calls ever returned the SAME list object, one caller's sort would silently
    reorder it out from under every other caller sharing that cache entry."""
    path = tmp_path / "results_web_a.csv"
    _write_csv(path, [{"combo_id": "b", "status": "ok"}, {"combo_id": "a", "status": "ok"}])

    rows1, _ = _read_rows([path])
    rows1.sort(key=lambda r: r["combo_id"])  # in-place, exactly like _filtered_results does
    assert [r["combo_id"] for r in rows1] == ["a", "b"]

    rows2, _ = _read_rows([path])
    assert [r["combo_id"] for r in rows2] == ["b", "a"]  # untouched by the other caller's sort


def test_read_rows_skips_a_path_that_does_not_exist(tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    rows, columns = _read_rows([missing])
    assert rows == []
    assert columns == []


def test_read_rows_concatenates_multiple_files(tmp_path):
    a = tmp_path / "results_web_a.csv"
    b = tmp_path / "results_web_b.csv"
    _write_csv(a, [{"combo_id": "a1", "status": "ok"}])
    _write_csv(b, [{"combo_id": "b1", "status": "ok"}])

    rows, _ = _read_rows([a, b])
    assert {r["combo_id"] for r in rows} == {"a1", "b1"}
