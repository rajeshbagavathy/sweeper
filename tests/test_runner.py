from __future__ import annotations

import csv
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

import src.runner as runner_mod
from src.auth import LoginNotConfigured
from src.results import ResultOutcome
from src.runner import (
    AccountConfig,
    _auto_download_eligible,
    _build_row,
    _capture_individual_dte_reports,
    _combo_already_done,
    _combo_label,
    _count_rows,
    _merge_csvs,
    _merge_logs,
    _run_from_queue,
    _run_one_combo,
    known_duplicate_combo_id,
    parse_dte_variant_suffix,
    queue_duplicate_for_refresh,
    run_sweep_multiprocess,
)
from src.store import combo_id, strategy_key


def _combo(i: int) -> dict:
    return {"instrument": "NIFTY", "entry_time": f"09:{i:02d}"}


def _drain(queue) -> list[dict]:
    """Mirrors _run_from_queue's own poison-pill protocol: pull combos until a None
    pill is hit. Used by these tests' fake_worker_main stand-ins since the real
    worker no longer receives a fixed list of combos - every worker shares one queue
    (see run_sweep_multiprocess) and just pulls whatever's next."""
    items = []
    while True:
        item = queue.get()
        if item is None:
            break
        items.append(item)
    return items


def test_merge_csvs_combines_worker_files(tmp_path: Path):
    fieldnames = ["combo_id", "status"]
    w0 = tmp_path / "w0.csv"
    w1 = tmp_path / "w1.csv"
    with w0.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok"})
    with w1.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "b", "status": "error"})

    merged = tmp_path / "merged.csv"
    _merge_csvs([w0, w1], merged, fieldnames)

    with merged.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert {r["combo_id"] for r in rows} == {"a", "b"}


def test_merge_csvs_tolerates_missing_worker_file(tmp_path: Path):
    fieldnames = ["combo_id", "status"]
    w0 = tmp_path / "w0.csv"
    with w0.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok"})

    merged = tmp_path / "merged.csv"
    _merge_csvs([w0, tmp_path / "does-not-exist.csv"], merged, fieldnames)

    with merged.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1


def test_merge_csvs_dedupes_by_combo_id_keeping_last(tmp_path: Path):
    """Regression test: a stale worker file containing a combo_id that also appears
    in a later source must not produce two rows for it in the merged output."""
    fieldnames = ["combo_id", "status"]
    stale = tmp_path / "stale.csv"
    fresh = tmp_path / "fresh.csv"
    with stale.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok"})
    with fresh.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok"})
        writer.writerow({"combo_id": "b", "status": "ok"})

    merged = tmp_path / "merged.csv"
    _merge_csvs([stale, fresh], merged, fieldnames)

    with merged.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {r["combo_id"] for r in rows} == {"a", "b"}


def test_merge_logs_concatenates(tmp_path: Path):
    w0 = tmp_path / "w0.log"
    w1 = tmp_path / "w1.log"
    w0.write_text('{"combo_id": "a"}\n')
    w1.write_text('{"combo_id": "b"}\n')
    merged = tmp_path / "merged.log"
    _merge_logs([w0, w1], merged)
    lines = merged.read_text().splitlines()
    assert len(lines) == 2


def test_count_rows_tallies_ok_and_error(tmp_path: Path):
    fieldnames = ["combo_id", "status"]
    w0 = tmp_path / "w0.csv"
    with w0.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "a", "status": "ok"})
        writer.writerow({"combo_id": "b", "status": "error"})
        writer.writerow({"combo_id": "c", "status": "ok"})
    completed, ok, error = _count_rows([w0])
    assert (completed, ok, error) == (3, 2, 1)


def test_count_rows_counts_a_dte_split_combo_once_not_once_per_variant_row(tmp_path: Path):
    """capture_dte_individually writes one row per DTE for the SAME combo
    (see _capture_individual_dte_reports) - the live progress readout this
    feeds must count that as ONE combo done, not three, or it silently
    desynchronizes from combos_total/`skipped` (both always per-combo) and
    inflates "current" well past what's actually finished."""
    fieldnames = ["combo_id", "status"]
    w0 = tmp_path / "w0.csv"
    with w0.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        # One combo, split into 3 DTE rows - must count as 1 "ok" combo.
        writer.writerow({"combo_id": "abc123_dte0", "status": "ok"})
        writer.writerow({"combo_id": "abc123_dte1", "status": "ok"})
        writer.writerow({"combo_id": "abc123_dte2", "status": "ok"})
        # A second, ordinary (non-split) combo alongside it.
        writer.writerow({"combo_id": "def456", "status": "ok"})
    completed, ok, error = _count_rows([w0])
    assert (completed, ok, error) == (2, 2, 0)


def test_count_rows_dedupes_a_dte_split_combo_across_worker_files(tmp_path: Path):
    """The same base combo's variant rows can legitimately land in different
    worker CSVs across polls/merges - still one combo, not one per file."""
    fieldnames = ["combo_id", "status"]
    w0 = tmp_path / "w0.csv"
    w1 = tmp_path / "w1.csv"
    with w0.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "abc123_dte0", "status": "ok"})
    with w1.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": "abc123_dte1", "status": "ok"})
    completed, ok, error = _count_rows([w0, w1])
    assert (completed, ok, error) == (1, 1, 0)


class _FakeProcess:
    """Runs the target synchronously in-process instead of really spawning an OS
    process - lets these tests exercise run_sweep_multiprocess's dispatch/merge
    orchestration without needing multiprocessing or a real browser."""

    def __init__(self, target, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._done = False

    def start(self):
        self._target(*self._args, **self._kwargs)
        self._done = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return

    def terminate(self):
        return


class _ThreadFakeProcess:
    """Like _FakeProcess, but runs the target in a background THREAD instead of
    synchronously in .start() - lets a test observe run_sweep_multiprocess's own
    monitor loop mid-flight (is_alive() genuinely True for a while), which
    _FakeProcess can't do: everything it runs finishes before the loop's first
    is_alive() check even happens, so it can never exercise the "while a worker is
    still alive" reporting branch at all (confirmed - that branch had zero test
    coverage before these in_progress_labels tests)."""

    def __init__(self, target, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._target, args=self._args, kwargs=self._kwargs)
        self._thread.start()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def terminate(self):
        pass  # no real OS process here - the thread just runs to completion


def test_run_sweep_multiprocess_dispatches_every_combo_exactly_once(monkeypatch, tmp_path):
    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(10)]
    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=4,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )

    assert sorted(seen) == sorted(c["entry_time"] for c in combos)
    assert len(seen) == 10  # no duplicates
    assert stats["ok"] == 10
    assert stats["error"] == 0

    with (tmp_path / "out.csv").open(newline="") as f:
        merged_rows = list(csv.DictReader(f))
    assert len(merged_rows) == 10


def test_run_sweep_multiprocess_surfaces_in_progress_labels_while_a_worker_is_busy(monkeypatch, tmp_path):
    """The actual fix this was all for: on_progress must see WHICH combo is in
    flight (not just a bare worker-alive count) while a worker is genuinely still
    replaying one - needs _ThreadFakeProcess (see its own docstring for why
    _FakeProcess can't exercise this at all)."""
    combo = {"instrument": "SENSEX", "entry_time": "09:17", "exit_time": "13:15"}

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        progress_queue = kwargs["progress_queue"]
        item = queue.get()
        cid = runner_mod.combo_id(item)
        progress_queue.put_nowait({"combo_id": cid, "label": runner_mod._combo_label(item), "event": "started"})
        time.sleep(0.25)  # long enough for the monitor loop (poll_interval_s below) to observe it mid-flight
        progress_queue.put_nowait({"combo_id": cid, "event": "finished"})
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({"combo_id": cid, "status": "ok", "error": ""})
        worker_log_path.write_text("")
        queue.get()  # the poison pill run_sweep_multiprocess queues per worker

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _ThreadFakeProcess)

    updates: list[dict] = []
    stats = run_sweep_multiprocess(
        [combo],
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=1,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.03,
        on_progress=lambda u: updates.append(dict(u)),
    )

    assert stats["ok"] == 1
    labeled = [u for u in updates if u["in_progress_labels"]]
    assert labeled, "expected at least one progress update while the combo was in flight"
    assert labeled[0]["in_progress_labels"] == ["SENSEX 09:17-13:15"]
    # And the FINAL update (after the worker finished) must not still be showing it.
    assert updates[-1]["in_progress_labels"] == []


def test_run_sweep_multiprocess_accepts_a_generator_with_total_hint(monkeypatch, tmp_path):
    """The large-sweep path (src.web.expand.iter_shuffled_ui_combos) passes a lazy
    generator instead of a list - dispatch/merge must work identically, driven off
    total_hint instead of len(combos)."""
    seen: list[str] = []
    progress_totals: list[int] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(10)]
    stats = run_sweep_multiprocess(
        (c for c in combos),  # a generator, not a list - no __len__
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=4,
        total_hint=10,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
        on_progress=lambda update: progress_totals.append(update["total"]),
    )

    assert sorted(seen) == sorted(c["entry_time"] for c in combos)
    assert len(seen) == 10
    assert stats["ok"] == 10
    assert stats["skipped"] == 0
    assert progress_totals and all(t == 10 for t in progress_totals)


def test_run_sweep_multiprocess_generator_requires_total_hint(tmp_path):
    with pytest.raises(ValueError, match="total_hint"):
        run_sweep_multiprocess(
            (c for c in [_combo(0)]),
            selectors=object(),
            csv_path=tmp_path / "out.csv",
            log_path=tmp_path / "run.log",
            fieldnames=["combo_id", "status", "error"],
            existing_statuses={},
            parallelism=1,
        )


def test_run_sweep_multiprocess_generator_skips_already_done_combos(monkeypatch, tmp_path):
    from src.store import combo_id as _combo_id

    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(5)]
    already_done = _combo_id(combos[0])
    stats = run_sweep_multiprocess(
        (c for c in combos),
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={already_done: "ok"},
        parallelism=2,
        total_hint=5,
        # The generator branch can't discover "1 already done" on its own without
        # walking the whole stream (see run_sweep_multiprocess's docstring) - the
        # caller (RunState.start) already knows this count upfront and passes it here.
        already_done_hint=1,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )

    assert combos[0]["entry_time"] not in seen
    assert len(seen) == 4
    assert stats["skipped"] == 1


def test_run_sweep_multiprocess_generator_skips_a_combo_whose_dte_variants_are_all_already_done(monkeypatch, tmp_path):
    """Same check as the sized-list version above, for the lazy-generator
    branch (_stream_todo) - a combo with no bare-id row but all its "_dteN"
    variants already "ok" must be skipped, not re-run."""
    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(5)]
    done_cid = combo_id(combos[0])
    existing_statuses = {f"{done_cid}_dte{d}": "ok" for d in (0, 1)}

    stats = run_sweep_multiprocess(
        (c for c in combos),
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses=existing_statuses,
        parallelism=2,
        total_hint=5,
        already_done_hint=1,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
        dte_values=[0, 1],
        capture_dte_individually=True,
    )

    assert combos[0]["entry_time"] not in seen
    assert len(seen) == 4


def test_run_sweep_multiprocess_generator_reports_baseline_immediately_not_only_when_discovered(monkeypatch, tmp_path):
    """Regression test for the bug found live: resuming a sweep with thousands of
    already-done combos scattered randomly through the shuffled stream must report
    that whole baseline from the very first progress update, not wait for the random
    walk to happen to encounter each one - otherwise "current" badly understates real
    progress for a long time even though nothing is actually being re-run."""
    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        _drain(queue)
        worker_csv_path.write_text("combo_id,status,error\n")
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    first_reports: list[dict] = []
    combos = [_combo(i) for i in range(3)]  # none of these happen to be "already done"
    run_sweep_multiprocess(
        (c for c in combos),
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},  # empty - the baseline comes entirely from already_done_hint
        parallelism=2,
        total_hint=6831 + 3,
        already_done_hint=6831,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
        on_progress=lambda update: first_reports.append(update),
    )

    assert first_reports[0]["skipped"] == 6831
    assert first_reports[0]["current"] >= 6831


def test_run_sweep_multiprocess_splits_across_accounts_without_duplicating_combos(monkeypatch, tmp_path):
    """The user's explicit requirement: when spreading workers across two AlgoTest
    accounts, every combo must still go to exactly one worker (never both accounts
    running the same combo), and each worker must get its OWN account's
    email/password/profile_dir, not a mix."""
    seen: list[str] = []
    worker_accounts_used: dict[int, tuple] = {}

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        worker_accounts_used[worker_index] = (kwargs["email"], kwargs["password"], primary_profile_dir)
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(12)]
    accounts = [AccountConfig("a1@x.com", "pw1", tmp_path / "profile-1") for _ in range(3)] + [
        AccountConfig("a2@x.com", "pw2", tmp_path / "profile-2") for _ in range(3)
    ]

    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=99,  # must be ignored in favor of len(accounts) when accounts is given
        accounts=accounts,
        poll_interval_s=0.01,
    )

    # No duplication: every combo appears in exactly one worker's chunk.
    assert sorted(seen) == sorted(c["entry_time"] for c in combos)
    assert len(seen) == len(combos)
    assert stats["ok"] == 12

    # Exactly 6 workers ran (len(accounts)), not 99 (the ignored `parallelism`).
    assert len(worker_accounts_used) == 6
    # Workers 0-2 got account 1's credentials/profile, workers 3-5 got account 2's.
    for i in range(3):
        assert worker_accounts_used[i] == ("a1@x.com", "pw1", tmp_path / "profile-1")
    for i in range(3, 6):
        assert worker_accounts_used[i] == ("a2@x.com", "pw2", tmp_path / "profile-2")


def test_run_sweep_multiprocess_clears_stale_worker_files_from_prior_crash(monkeypatch, tmp_path):
    """Regression test: work_dir's name is derived only from csv_path's stem, so a
    prior crashed attempt at the same csv_path leaves worker-N.csv files behind at
    the exact same paths this run will use. Confirmed live: without clearing them,
    a resumed run silently re-merged that stale content on top of the fresh
    existing_snapshot, doubling every already-done row."""
    from src.store import combo_id as _combo_id

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        # mirrors real run_sweep()/append_row's append-don't-truncate behavior
        combos = _drain(queue)
        file_exists = worker_csv_path.exists()
        with worker_csv_path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            for combo in combos:
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("", encoding="utf-8") if not worker_log_path.exists() else None

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(4)]
    already_done_combo = combos[0]
    already_done_cid = _combo_id(already_done_combo)

    csv_path = tmp_path / "out.csv"
    fieldnames = ["combo_id", "status", "error"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": already_done_combo["entry_time"], "status": "ok", "error": ""})

    # simulate leftover worker-0.csv from an earlier crashed attempt at this same
    # csv_path, containing a row for the SAME already-done combo (as would happen if
    # that worker got to it before the earlier run crashed and its result later also
    # made it into csv_path's snapshot)
    work_dir = csv_path.parent / f".parallel-{csv_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    with (work_dir / "worker-0.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": already_done_combo["entry_time"], "status": "ok", "error": ""})

    run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=csv_path,
        log_path=tmp_path / "run.log",
        fieldnames=fieldnames,
        existing_statuses={already_done_cid: "ok"},
        parallelism=2,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )

    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    combo_ids = [r["combo_id"] for r in rows]
    assert combo_ids.count(already_done_combo["entry_time"]) == 1, (
        "the already-done combo must appear exactly once, not duplicated via stale worker files"
    )
    assert len(rows) == len(combos)


def test_run_sweep_multiprocess_preserves_preexisting_rows_when_resuming(monkeypatch, tmp_path):
    """Regression test: resuming a run whose csv_path already has rows from a prior
    session must not lose those rows once the periodic merge rewrites csv_path."""
    from src.store import combo_id as _combo_id

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(4)]
    already_done_combo = combos[0]
    already_done_cid = _combo_id(already_done_combo)

    csv_path = tmp_path / "out.csv"
    fieldnames = ["combo_id", "status", "error"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({"combo_id": already_done_combo["entry_time"], "status": "ok", "error": ""})

    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=csv_path,
        log_path=tmp_path / "run.log",
        fieldnames=fieldnames,
        existing_statuses={already_done_cid: "ok"},
        parallelism=2,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )

    assert stats["skipped"] == 1
    assert stats["ok"] == 3

    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    # the pre-existing row (from before resume) plus the 3 newly-run combos
    assert len(rows) == 4
    assert {r["combo_id"] for r in rows} == {c["entry_time"] for c in combos}


def test_run_sweep_multiprocess_respects_existing_statuses(monkeypatch, tmp_path):
    from src.store import combo_id as _combo_id

    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(5)]
    already_done = _combo_id(combos[0])

    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={already_done: "ok"},
        parallelism=2,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )

    assert stats["skipped"] == 1
    assert stats["ok"] == 4
    assert len(seen) == 4


def test_run_sweep_multiprocess_captures_known_duplicate_strategies_sized_list(monkeypatch, tmp_path):
    """A combo whose strategy_key already has a record under a DIFFERENT
    combo_id must never be dispatched for replay - captured into the queue
    file instead, exactly the "same strategy, trailing end_date" scenario this
    was built for."""
    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(5)]
    duplicate_key = strategy_key(combos[0])
    queue_path = tmp_path / "pending_duplicate_refresh.csv"

    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=2,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
        known_strategy_keys={duplicate_key: "existing_cid_from_a_previous_sweep"},
        duplicate_queue_path=queue_path,
    )

    assert stats["duplicate"] == 1
    assert stats["ok"] == 4
    assert len(seen) == 4  # the duplicate never reached a worker at all
    with queue_path.open(newline="") as f:
        queued = list(csv.DictReader(f))
    assert queued == [{"combo_id": "existing_cid_from_a_previous_sweep", "strategy_key": duplicate_key, "detected_at": queued[0]["detected_at"]}]


def test_run_sweep_multiprocess_captures_known_duplicate_strategies_generator(monkeypatch, tmp_path):
    """Same guard, but for the large-sweep lazy-generator path."""
    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(5)]
    duplicate_key = strategy_key(combos[0])
    queue_path = tmp_path / "pending_duplicate_refresh.csv"

    stats = run_sweep_multiprocess(
        (c for c in combos),
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=2,
        total_hint=5,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
        known_strategy_keys={duplicate_key: "existing_cid_from_a_previous_sweep"},
        duplicate_queue_path=queue_path,
    )

    assert stats["duplicate"] == 1
    assert stats["ok"] == 4
    assert len(seen) == 4
    with queue_path.open(newline="") as f:
        queued = list(csv.DictReader(f))
    assert len(queued) == 1
    assert queued[0]["combo_id"] == "existing_cid_from_a_previous_sweep"


def test_run_sweep_multiprocess_unaffected_when_no_known_strategy_keys_given(monkeypatch, tmp_path):
    """None (the default) must behave exactly as before this existed - no combo
    is ever treated as a duplicate."""
    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(5)]
    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=2,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )
    assert stats["duplicate"] == 0
    assert stats["ok"] == 5


def test_run_sweep_multiprocess_surfaces_login_crash_instead_of_silent_done(monkeypatch, tmp_path):
    """Regression test: confirmed live that a worker crashing on LoginNotConfigured
    left no trace other than a traceback in server stdout - run_sweep_multiprocess
    just saw no processes alive and returned a normal-looking stats dict, so the
    sweep silently reported "done" with 0 new results instead of an error."""

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        # simulates _worker_main's own except-and-mark behavior on a real crash
        (worker_csv_path.parent / f"{worker_csv_path.stem}.error").write_text(
            "Not logged in, and login.email_input/password_input/submit_button aren't configured."
        )

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(4)]
    with pytest.raises(LoginNotConfigured):
        run_sweep_multiprocess(
            combos,
            selectors=object(),
            csv_path=tmp_path / "out.csv",
            log_path=tmp_path / "run.log",
            fieldnames=["combo_id", "status", "error"],
            existing_statuses={},
            parallelism=2,
            primary_profile_dir=tmp_path / "primary-profile",
            poll_interval_s=0.01,
        )


def test_run_sweep_multiprocess_ignores_stale_error_markers_from_a_prior_run(monkeypatch, tmp_path):
    """A leftover worker-N.error file from an earlier crashed attempt at this same
    csv_path must not cause a fresh, successful run to be misreported as an error."""

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    csv_path = tmp_path / "out.csv"
    work_dir = csv_path.parent / f".parallel-{csv_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "worker-0.error").write_text("stale error from a previous crashed run")

    combos = [_combo(i) for i in range(4)]
    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=csv_path,
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses={},
        parallelism=2,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
    )

    assert stats["ok"] == 4
    assert stats["error"] == 0


def test_build_row_records_dte_as_sorted_comma_joined_string():
    row = _build_row({"a": 1}, "cid1", "ok", None, {}, dte_values=[2, 0, 1])
    assert row["dte"] == "0,1,2"


def test_build_row_dte_is_order_independent():
    """The same combination selected in a different order must produce the same
    stored value, so filtering by exact combination actually groups them together."""
    row_a = _build_row({"a": 1}, "cid1", "ok", None, {}, dte_values=[1, 0, 2])
    row_b = _build_row({"a": 1}, "cid2", "ok", None, {}, dte_values=[2, 1, 0])
    assert row_a["dte"] == row_b["dte"]


def test_build_row_dte_blank_when_not_provided():
    assert _build_row({"a": 1}, "cid1", "ok", None, {})["dte"] == ""
    assert _build_row({"a": 1}, "cid1", "ok", None, {}, dte_values=[])["dte"] == ""


def test_build_row_dte_single_value():
    row = _build_row({"a": 1}, "cid1", "ok", None, {}, dte_values=[0])
    assert row["dte"] == "0"


def test_known_duplicate_combo_id_finds_a_match_by_strategy_key():
    combo = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-16", "entry_time": "09:20"}
    known = {strategy_key(combo): "existing_cid_123"}
    assert known_duplicate_combo_id(combo, known) == "existing_cid_123"


def test_known_duplicate_combo_id_none_when_no_match():
    combo = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-16", "entry_time": "09:20"}
    assert known_duplicate_combo_id(combo, {"some_other_key": "cid"}) is None


def test_known_duplicate_combo_id_none_when_map_is_none_or_empty():
    combo = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-16", "entry_time": "09:20"}
    assert known_duplicate_combo_id(combo, None) is None
    assert known_duplicate_combo_id(combo, {}) is None


def test_queue_duplicate_for_refresh_appends_one_row(tmp_path):
    queue_path = tmp_path / "pending_duplicate_refresh.csv"
    queue_duplicate_for_refresh(queue_path, "existing_cid_123", "some_strategy_key")
    with queue_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["combo_id"] == "existing_cid_123"
    assert rows[0]["strategy_key"] == "some_strategy_key"
    assert rows[0]["detected_at"]


def test_queue_duplicate_for_refresh_appends_without_truncating(tmp_path):
    queue_path = tmp_path / "pending_duplicate_refresh.csv"
    queue_duplicate_for_refresh(queue_path, "cid_a", "key_a")
    queue_duplicate_for_refresh(queue_path, "cid_b", "key_b")
    with queue_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["combo_id"] for r in rows] == ["cid_a", "cid_b"]


def test_build_row_records_strategy_key_computed_from_the_live_combo_dict():
    """Computed HERE, from `combo` directly - never reconstructed later from a
    flattened CSV row (see src/store.py's strategy_key, and this session's
    finding that row_to_combo doesn't reliably reconstruct every historical
    combo shape)."""
    from src.store import strategy_key as compute_strategy_key

    combo = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09", "entry_time": "09:20"}
    row = _build_row(combo, "cid1", "ok", None, {})
    assert row["strategy_key"] == compute_strategy_key(combo)


def test_build_row_strategy_key_stable_across_dte_variants_of_the_same_combo():
    """A _dteN-suffixed variant row (capture_dte_individually) must carry the
    SAME strategy_key as its base row - both are the same underlying strategy,
    just captured per-DTE - confirmed dte is never part of the hashed combo
    dict at all (see strategy_key's own docstring)."""
    combo = {"instrument": "SENSEX", "start_date": "2025-01-01", "end_date": "2026-09-09", "entry_time": "09:20"}
    base_row = _build_row(combo, "cid1", "ok", None, {}, dte_values=[0, 1])
    variant_row = _build_row(combo, "cid1_dte0", "ok", None, {}, dte_values=[0])
    assert base_row["strategy_key"] == variant_row["strategy_key"]


def test_run_from_queue_never_picks_up_new_work_once_already_stopped(monkeypatch):
    """If stop_event is set before a worker even looks at the queue, it must not
    touch any combo sitting there - the queued item is left for a resume, not run."""
    processed: list[str] = []

    def fake_run_one_combo(page, combo, cid, selectors, csv_path, log_path, fieldnames, stats, **kwargs):
        processed.append(cid)

    monkeypatch.setattr(runner_mod, "_run_one_combo", fake_run_one_combo)

    queue: multiprocessing.Queue = multiprocessing.Queue()
    queue.put({"a": 1})
    stop_event = multiprocessing.Event()
    stop_event.set()

    stats = _run_from_queue(
        None, queue, None, Path("csv"), Path("log"), [], delay_s=0, stop_event=stop_event
    )
    assert processed == []
    assert stats == {"ok": 0, "error": 0, "skipped": 0}


def test_run_from_queue_finishes_in_flight_combo_before_stopping(monkeypatch):
    """The defining requirement: Stop must let whatever combo is already running
    finish and get recorded, and must never go on to start another one after that -
    not a mid-action kill (which used to crash AlgoTest's Node driver)."""
    processed: list[str] = []
    stop_event = multiprocessing.Event()

    def fake_run_one_combo(page, combo, cid, selectors, csv_path, log_path, fieldnames, stats, **kwargs):
        processed.append(cid)
        stats["ok"] += 1
        # Simulates the user clicking Stop while this exact combo is running.
        stop_event.set()

    monkeypatch.setattr(runner_mod, "_run_one_combo", fake_run_one_combo)

    queue: multiprocessing.Queue = multiprocessing.Queue()
    queue.put({"a": 1})
    queue.put({"a": 2})  # must never be picked up once stop_event is set

    stats = _run_from_queue(
        None, queue, None, Path("csv"), Path("log"), [], delay_s=0, stop_event=stop_event
    )
    assert len(processed) == 1  # the in-flight one finished ...
    assert stats["ok"] == 1  # ... and was recorded as a real result, not dropped
    # ... but nothing after it was ever started.


def test_combo_label_formats_instrument_and_times():
    combo = {"instrument": "SENSEX", "entry_time": "09:17", "exit_time": "13:15"}
    assert _combo_label(combo) == "SENSEX 09:17-13:15"


def test_combo_label_tolerates_missing_fields():
    assert _combo_label({}) == "? ?-?"


def test_run_from_queue_reports_started_and_finished_progress_events(monkeypatch):
    """The actual mechanism the Stopping message's per-combo visibility depends on
    (see run_sweep_multiprocess's own in_progress_labels) - "started" right before
    the (possibly long) replay, "finished" right after, in that order, carrying the
    real combo_id and a human label."""
    stop_event = multiprocessing.Event()

    def stop_after_one(page, combo, cid, selectors, csv_path, log_path, fieldnames, stats, **kwargs):
        stats["ok"] += 1
        stop_event.set()  # only one combo queued below - nothing left to pick up anyway

    monkeypatch.setattr(runner_mod, "_run_one_combo", stop_after_one)

    queue: multiprocessing.Queue = multiprocessing.Queue()
    combo = {"instrument": "SENSEX", "entry_time": "09:17", "exit_time": "13:15"}
    queue.put(combo)
    progress_queue: multiprocessing.Queue = multiprocessing.Queue()

    _run_from_queue(
        None, queue, None, Path("csv"), Path("log"), [], delay_s=0, stop_event=stop_event,
        progress_queue=progress_queue,
    )

    events = []
    while not progress_queue.empty():
        events.append(progress_queue.get())
    assert [e["event"] for e in events] == ["started", "finished"]
    assert events[0]["combo_id"] == events[1]["combo_id"] == combo_id(combo)
    assert events[0]["label"] == "SENSEX 09:17-13:15"


def test_run_from_queue_progress_reporting_is_best_effort(monkeypatch):
    """A progress_queue that can't accept the message (full, closed, whatever) must
    never turn an otherwise-successful combo into a failure - this is visibility
    only, never allowed to affect the real result."""
    def fake_run_one_combo(page, combo, cid, selectors, csv_path, log_path, fieldnames, stats, **kwargs):
        stats["ok"] += 1

    monkeypatch.setattr(runner_mod, "_run_one_combo", fake_run_one_combo)

    class _BrokenQueue:
        def put_nowait(self, item):
            raise RuntimeError("queue is broken")

    queue: multiprocessing.Queue = multiprocessing.Queue()
    queue.put({"instrument": "NIFTY", "entry_time": "09:20", "exit_time": "15:20"})
    queue.put(None)  # poison pill - stop after the one real combo, no stop_event needed

    stats = _run_from_queue(
        None, queue, None, Path("csv"), Path("log"), [], delay_s=0,
        progress_queue=_BrokenQueue(),
    )
    assert stats["ok"] == 1


# --- Auto-download: downloading a combo's trade report inline during the sweep,
# right after its metrics are scraped, if it already looks good enough - avoiding a
# full second replay later just to fetch the report for something Correlate would
# have wanted anyway. ---

def test_auto_download_eligible_requires_positive_pnl():
    row = {"total_pnl": -100.0, "return_max_dd": 5.0}
    assert not _auto_download_eligible(row, min_return_max_dd=1.5, min_trades=0)


def test_auto_download_eligible_requires_min_return_max_dd():
    row = {"total_pnl": 100.0, "return_max_dd": 1.0}
    assert not _auto_download_eligible(row, min_return_max_dd=1.5, min_trades=0)
    row["return_max_dd"] = 1.5
    assert _auto_download_eligible(row, min_return_max_dd=1.5, min_trades=0)


def test_auto_download_eligible_missing_metrics_are_not_eligible():
    assert not _auto_download_eligible({}, min_return_max_dd=1.5, min_trades=0)
    assert not _auto_download_eligible({"total_pnl": 100.0}, min_return_max_dd=1.5, min_trades=0)
    assert not _auto_download_eligible({"return_max_dd": 5.0}, min_return_max_dd=1.5, min_trades=0)


def test_auto_download_eligible_min_trades_gate_is_opt_in():
    row = {"total_pnl": 100.0, "return_max_dd": 5.0, "total_trades": 3}
    # min_trades=0 (the default) means no minimum at all - a tiny sample still qualifies.
    assert _auto_download_eligible(row, min_return_max_dd=1.5, min_trades=0)
    # Once set, it's enforced.
    assert not _auto_download_eligible(row, min_return_max_dd=1.5, min_trades=10)
    row["total_trades"] = 10
    assert _auto_download_eligible(row, min_return_max_dd=1.5, min_trades=10)


def _patch_run_one_combo_prereqs(monkeypatch, *, total_pnl, return_max_dd):
    monkeypatch.setattr(runner_mod, "is_logged_in", lambda page, selectors: True)
    monkeypatch.setattr(runner_mod, "apply_combination", lambda page, selectors, combo: None)
    monkeypatch.setattr(runner_mod, "wait_for_result", lambda page, selectors, timeout_s=180: ResultOutcome("ok"))
    monkeypatch.setattr(runner_mod, "apply_result_settings", lambda page, selectors, slippage_pct, dte_values: None)
    monkeypatch.setattr(
        runner_mod,
        "scrape_metrics",
        lambda page, selectors: {"total_pnl": str(total_pnl), "return_max_dd": str(return_max_dd)},
    )
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: None)
    monkeypatch.setattr(runner_mod, "_log", lambda *a, **k: None)


def test_run_one_combo_downloads_report_when_eligible_and_enabled(monkeypatch, tmp_path):
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")

    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
    )
    assert downloaded == ["combo1"]


def test_run_one_combo_skips_download_when_disabled(monkeypatch, tmp_path):
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")

    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0],
        auto_download_enabled=False,
    )
    assert downloaded == []


def test_run_one_combo_skips_download_when_below_threshold(monkeypatch, tmp_path):
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=0.5)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")

    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
    )
    assert downloaded == []


def test_run_one_combo_skips_download_when_already_cached(monkeypatch, tmp_path):
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    cached_target = tmp_path / "combo1.csv"
    cached_target.write_text("already here")
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: cached_target)

    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
    )
    assert downloaded == []  # never even attempted - the file already existed


def test_run_one_combo_download_failure_does_not_fail_the_combo(monkeypatch, tmp_path):
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")

    def boom(page, selectors, target, cid):
        raise RuntimeError("download exploded")

    monkeypatch.setattr(runner_mod, "download_current_report", boom)

    stats = {"ok": 0, "error": 0, "skipped": 0}
    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], stats,
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
    )
    assert stats["ok"] == 1  # the combo itself is still recorded as a success
    assert stats["error"] == 0


# --- capture_dte_individually: one row+report per DTE instead of one combined -
# see _capture_individual_dte_reports and its call site in _run_one_combo. ---

def test_capture_individual_dte_reports_writes_a_distinct_row_per_dte_with_downloads_off(monkeypatch):
    """Rows are the whole point of this feature and must always be written - even
    with auto_download_enabled left at its default False, which used to gate the
    ENTIRE split (see the bug this was rewritten to fix: a real sweep only ever
    split the ~6% of combos whose BLENDED row happened to clear the auto-download
    bar, silently leaving every other combo's individual DTEs uncaptured)."""
    settings_calls: list[list[int]] = []
    monkeypatch.setattr(
        runner_mod, "apply_result_settings",
        lambda page, selectors, slippage_pct, dte_values: settings_calls.append(list(dte_values)),
    )
    # A different scraped result per DTE, keyed by whichever dte_values was just
    # applied - proves each row reflects ITS OWN DTE's re-filtered view, not a copy
    # of the combined one.
    metrics_by_dte = {0: {"total_pnl": "100"}, 1: {"total_pnl": "200"}, 2: {"total_pnl": "300"}}
    monkeypatch.setattr(
        runner_mod, "scrape_metrics", lambda page, selectors: metrics_by_dte[settings_calls[-1][0]]
    )
    appended: list[dict] = []
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: appended.append(row))
    downloaded: list[str] = []
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: Path(f"{cid}.csv"))
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _capture_individual_dte_reports(
        None, None, {"instrument": "NIFTY"}, "base123", Path("csv"), [], slippage_pct=1.0, dte_values=[2, 0, 1],
        # auto_download_enabled intentionally omitted (defaults False)
    )

    # Applied in sorted order, one DTE at a time - never the combined list.
    assert settings_calls == [[0], [1], [2]]
    assert [r["combo_id"] for r in appended] == ["base123_dte0", "base123_dte1", "base123_dte2"]
    assert [r["dte"] for r in appended] == ["0", "1", "2"]
    assert [r["total_pnl"] for r in appended] == [100.0, 200.0, 300.0]
    # No downloads at all - auto_download_enabled is off, same as the combined path.
    assert downloaded == []


def test_capture_individual_dte_reports_downloads_only_the_variants_whose_own_metrics_qualify(monkeypatch):
    """Report downloads stay cost-gated same as before - but decided per-variant
    from THAT DTE's own scraped metrics, not the blended base row's. DTE 1 here is
    the one that clears the bar; 0 and 2 don't - proves a good single-DTE result
    isn't held hostage to how the OTHER DTEs (or the blend) happen to look, and
    isn't downloaded just because a sibling DTE happened to qualify either."""
    settings_calls: list[list[int]] = []
    monkeypatch.setattr(
        runner_mod, "apply_result_settings",
        lambda page, selectors, slippage_pct, dte_values: settings_calls.append(list(dte_values)),
    )
    metrics_by_dte = {
        0: {"total_pnl": "100", "return_max_dd": "0.5"},   # below threshold - no download
        1: {"total_pnl": "500", "return_max_dd": "3.0"},   # clears it - downloaded
        2: {"total_pnl": "-50", "return_max_dd": "5.0"},   # losing trade - no download
    }
    monkeypatch.setattr(
        runner_mod, "scrape_metrics", lambda page, selectors: metrics_by_dte[settings_calls[-1][0]]
    )
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: None)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: Path(f"{cid}.csv"))
    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _capture_individual_dte_reports(
        None, None, {"instrument": "NIFTY"}, "base123", Path("csv"), [], slippage_pct=1.0, dte_values=[0, 1, 2],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
    )

    assert downloaded == ["base123_dte1"]


def test_capture_individual_dte_reports_one_bad_dte_does_not_lose_the_others(monkeypatch):
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: Path(f"{cid}.csv"))

    def flaky_apply(page, selectors, slippage_pct, dte_values):
        if dte_values == [1]:
            raise RuntimeError("boom on DTE 1")

    monkeypatch.setattr(runner_mod, "apply_result_settings", flaky_apply)
    monkeypatch.setattr(runner_mod, "scrape_metrics", lambda page, selectors: {"total_pnl": "50"})
    appended: list[dict] = []
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: appended.append(row))
    monkeypatch.setattr(runner_mod, "download_current_report", lambda page, selectors, target, cid: None)

    _capture_individual_dte_reports(
        None, None, {"instrument": "NIFTY"}, "base123", Path("csv"), [], slippage_pct=1.0, dte_values=[0, 1, 2],
    )

    # DTE 1 failed and was skipped - 0 and 2 still got captured.
    assert [r["combo_id"] for r in appended] == ["base123_dte0", "base123_dte2"]


def test_capture_individual_dte_reports_id_is_a_composite_string_not_a_new_hash(monkeypatch):
    """The whole point of this design: variant ids are plain string composition
    from the base combo_id already computed elsewhere - combo_id() itself is never
    called again here, so no existing combo's identity is touched by this feature."""
    monkeypatch.setattr(runner_mod, "combo_id", lambda combo: (_ for _ in ()).throw(AssertionError("must not re-hash")))
    monkeypatch.setattr(runner_mod, "apply_result_settings", lambda page, selectors, slippage_pct, dte_values: None)
    monkeypatch.setattr(runner_mod, "scrape_metrics", lambda page, selectors: {"total_pnl": "1"})
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: Path(f"{cid}.csv"))
    monkeypatch.setattr(runner_mod, "download_current_report", lambda page, selectors, target, cid: None)
    appended: list[dict] = []
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: appended.append(row))

    _capture_individual_dte_reports(
        None, None, {"instrument": "NIFTY"}, "abc123def456", Path("csv"), [], slippage_pct=1.0, dte_values=[0, 5],
    )

    assert [r["combo_id"] for r in appended] == ["abc123def456_dte0", "abc123def456_dte5"]


def test_parse_dte_variant_suffix_extracts_n_from_composite_id():
    assert parse_dte_variant_suffix("abc123def456_dte0") == 0
    assert parse_dte_variant_suffix("abc123def456_dte12") == 12


def test_parse_dte_variant_suffix_none_for_a_bare_combo_id():
    assert parse_dte_variant_suffix("abc123def456") is None


def test_run_one_combo_captures_individual_dte_rows_when_enabled(monkeypatch, tmp_path):
    """End-to-end through _run_one_combo: with capture_dte_individually=True and
    more than one DTE selected, the combined single-report download must NOT
    happen - only the per-DTE variants."""
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")
    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )
    appended: list[dict] = []
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: appended.append(row))

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0, 1],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
        capture_dte_individually=True,
    )

    # No combined download for the base combo_id itself...
    assert "combo1" not in downloaded
    # ...only the two per-DTE variants.
    assert downloaded == ["combo1_dte0", "combo1_dte1"]
    # No combined ROW either - "instead of one combined" (the checkbox's own
    # label) means genuinely instead of, not "in addition to". Asserting the
    # exact set (not just filtering "combo1" out and checking what's left, as
    # this test used to) is what actually catches the real bug found live: a
    # sweep with this box checked wrote BOTH the blended row AND all per-DTE
    # rows for every combo (1,704 combos x 4 rows = 6,816), silently
    # contradicting the checkbox's own promise - the old, looser assertion
    # here would never have caught that.
    assert {r["combo_id"] for r in appended} == {"combo1_dte0", "combo1_dte1"}


def test_run_one_combo_falls_back_to_the_combined_row_when_every_dte_split_attempt_fails(monkeypatch, tmp_path):
    """A combo's own successful backtest must never end up with ZERO rows on
    disk just because the re-filter/scrape step happens to fail for every
    single DTE - the combined row is the safety net for that, not something
    that coexists with a successful split."""
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "apply_result_settings", lambda page, selectors, slippage_pct, dte_values: (_ for _ in ()).throw(RuntimeError("boom")))
    appended: list[dict] = []
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: appended.append(row))

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0, 1],
        auto_download_enabled=False, capture_dte_individually=True,
    )

    assert [r["combo_id"] for r in appended] == ["combo1"]


# --- _combo_already_done: the Resume/skip-check counterpart to the above - a
# combo captured via capture_dte_individually never gets a row under its own
# bare combo_id (see _run_one_combo), only its "_dteN" variants do, so the
# ordinary bare-id existing_statuses check would never recognize it as done. ---

def test_combo_already_done_checks_all_dte_variants_when_capturing_individually():
    existing = {"cid_dte0": "ok", "cid_dte1": "ok", "cid_dte2": "ok"}
    assert _combo_already_done("cid", [0, 1, 2], True, existing) is True


def test_combo_already_done_is_false_when_any_dte_variant_is_missing():
    existing = {"cid_dte0": "ok", "cid_dte1": "ok"}  # dte2 never completed
    assert _combo_already_done("cid", [0, 1, 2], True, existing) is False


def test_combo_already_done_ignores_a_leftover_bare_id_row_when_capturing_individually():
    """The exact bug this exists to close: a bare-id row existing (e.g. from an
    older sweep, before this fix, or the one-DTE fallback path) must NOT be
    mistaken for "done" once capture_dte_individually expects per-DTE rows -
    only the actual variant rows count."""
    existing = {"cid": "ok"}  # only the (stale/irrelevant) combined row
    assert _combo_already_done("cid", [0, 1, 2], True, existing) is False


def test_combo_already_done_falls_back_to_bare_id_check_with_one_dte():
    existing = {"cid": "ok"}
    assert _combo_already_done("cid", [0], True, existing) is True


def test_combo_already_done_falls_back_to_bare_id_check_when_flag_is_off():
    existing = {"cid": "ok"}
    assert _combo_already_done("cid", [0, 1, 2], False, existing) is True


def test_combo_already_done_handles_none_dte_values():
    assert _combo_already_done("cid", None, True, {"cid": "ok"}) is True
    assert _combo_already_done("cid", None, True, {}) is False


def test_run_sweep_multiprocess_sized_list_skips_a_combo_whose_dte_variants_are_all_already_done(monkeypatch, tmp_path):
    """Integration check: a combo with no bare-id row but all its "_dteN"
    variants already "ok" must be skipped on a sized-list Resume, not re-run."""
    seen: list[str] = []

    def fake_worker_main(worker_index, queue, selectors, primary_profile_dir, worker_csv_path, worker_log_path, fieldnames, **kwargs):
        combos = _drain(queue)
        with worker_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for combo in combos:
                seen.append(combo["entry_time"])
                writer.writerow({"combo_id": combo["entry_time"], "status": "ok", "error": ""})
        worker_log_path.write_text("")

    monkeypatch.setattr(runner_mod, "_worker_main", fake_worker_main)
    monkeypatch.setattr(runner_mod.multiprocessing, "Process", _FakeProcess)

    combos = [_combo(i) for i in range(3)]
    done_cid = combo_id(combos[0])
    existing_statuses = {f"{done_cid}_dte{d}": "ok" for d in (0, 1)}  # combo 0 fully done, no bare-id row

    stats = run_sweep_multiprocess(
        combos,
        selectors=object(),
        csv_path=tmp_path / "out.csv",
        log_path=tmp_path / "run.log",
        fieldnames=["combo_id", "status", "error"],
        existing_statuses=existing_statuses,
        parallelism=4,
        primary_profile_dir=tmp_path / "primary-profile",
        poll_interval_s=0.01,
        dte_values=[0, 1],
        capture_dte_individually=True,
    )

    assert stats["skipped"] == 1
    assert sorted(seen) == sorted(c["entry_time"] for c in combos[1:])  # only combos 1 and 2 dispatched


def test_run_one_combo_splits_dte_rows_even_when_the_blended_row_is_not_auto_download_eligible(monkeypatch, tmp_path):
    """The actual bug this session found live: capture_dte_individually used to be
    nested INSIDE the auto-download eligibility check on the BLENDED combo row, so
    a combo whose combined Return/MaxDD missed the bar never got split at all -
    confirmed against a real sweep where ~94% of combos silently stayed combined
    despite the checkbox being on. The blended row here is deliberately BELOW
    threshold (0.5 < 1.5) - the split must still happen; only the (also ineligible,
    since every DTE scrapes the same stub metrics here) report downloads stay off."""
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=0.5)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")
    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )
    appended: list[dict] = []
    monkeypatch.setattr(runner_mod, "append_row", lambda csv_path, fieldnames, row: appended.append(row))

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0, 1],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
        capture_dte_individually=True,
    )

    assert {r["combo_id"] for r in appended} == {"combo1_dte0", "combo1_dte1"}  # no combined row either
    assert downloaded == []  # every variant's own (stubbed, ineligible) metrics still miss the bar


def test_run_one_combo_capture_individually_with_one_dte_falls_back_to_combined(monkeypatch, tmp_path):
    """capture_dte_individually=True with only one DTE selected has nothing to
    split apart - must behave exactly like the combined path, not create a
    redundant "_dte0" duplicate of the same single-DTE result."""
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")
    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
        capture_dte_individually=True,
    )

    assert downloaded == ["combo1"]


def test_run_one_combo_capture_individually_default_off_is_a_no_op(monkeypatch, tmp_path):
    """capture_dte_individually defaults to False - every existing call site that
    never passes it must behave exactly as before this feature existed."""
    _patch_run_one_combo_prereqs(monkeypatch, total_pnl=1000.0, return_max_dd=5.0)
    monkeypatch.setattr(runner_mod, "trade_report_path", lambda reports_dir, instrument, cid: tmp_path / f"{cid}.csv")
    downloaded: list[str] = []
    monkeypatch.setattr(
        runner_mod, "download_current_report", lambda page, selectors, target, cid: downloaded.append(cid)
    )

    _run_one_combo(
        None, {"instrument": "NIFTY"}, "combo1", None, Path("csv"), Path("log"), [], {"ok": 0, "error": 0, "skipped": 0},
        result_timeout_s=1, max_retries=0, email=None, password=None, slippage_pct=1.0, dte_values=[0, 1, 2],
        auto_download_enabled=True, auto_download_min_return_max_dd=1.5, auto_download_min_trades=0,
        # capture_dte_individually intentionally omitted
    )

    assert downloaded == ["combo1"]
