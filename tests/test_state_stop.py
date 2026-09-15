from __future__ import annotations

import threading

from src.web.state import RunState


def test_stop_flips_status_to_stopping_immediately():
    """The UI shouldn't have to wait for the run loop's own poll cycle (up to a few
    seconds) to learn Stop was clicked - stop() itself must reflect it right away."""
    rs = RunState()
    rs.status = "running"
    rs._stop_event = threading.Event()

    rs.stop()

    assert rs.status == "stopping"
    assert rs._stop_event.is_set()


def test_stop_is_a_noop_when_nothing_is_running():
    rs = RunState()
    assert rs.status == "idle"
    rs.stop()
    assert rs.status == "idle"  # unchanged - no _stop_event to set, nothing to flip


def test_is_running_true_while_stopping_so_a_new_run_cannot_start_mid_drain():
    rs = RunState()
    rs.status = "stopping"
    assert rs.is_running() is True


def test_on_progress_tracks_in_progress_count():
    rs = RunState()
    rs._on_progress({"current": 5, "total": 10, "ok": 4, "error": 1, "skipped": 0, "in_progress": 3})
    assert rs.in_progress == 3
    snap = rs.snapshot()
    assert snap["in_progress"] == 3


def test_on_progress_defaults_in_progress_to_zero_when_absent():
    """The sequential (non-multiprocess) run path's on_progress payload has no
    in_progress key at all - must not KeyError, must read as 0."""
    rs = RunState()
    rs.in_progress = 7  # stale from a previous multiprocess run
    rs._on_progress({"current": 1, "total": 1, "ok": 1, "error": 0, "skipped": 0})
    assert rs.in_progress == 0
