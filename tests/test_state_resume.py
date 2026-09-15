from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

import src.web.state as state_mod
from src import store
from src.web.models import SweepUIConfig


class _FakeResults:
    metrics: dict = {}


class _FakeSelectors:
    results = _FakeResults()


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _patch_common(monkeypatch, tmp_path, combos):
    monkeypatch.setattr(state_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "load_selectors", lambda path: _FakeSelectors())
    monkeypatch.setattr(state_mod, "expand_ui_config", lambda cfg: combos)


def test_resume_reuses_existing_csv_and_statuses(tmp_path, monkeypatch):
    combos = [{"a": 1}, {"a": 2}, {"a": 3}]
    _patch_common(monkeypatch, tmp_path, combos)

    existing_csv = tmp_path / "results_web_existing.csv"
    fieldnames = ["combo_id", "run_at", "status", "error", "a"]
    cid0 = store.combo_id(combos[0])
    _write_csv(existing_csv, fieldnames, [{"combo_id": cid0, "run_at": "", "status": "ok", "error": "", "a": 1}])

    recorded: dict = {}

    def fake_run(self, cfg, combos_, csv_path, fieldnames_, stop_event, existing_statuses):
        recorded["csv_path"] = csv_path
        recorded["existing_statuses"] = existing_statuses
        with self._lock:
            self.status = "done"

    monkeypatch.setattr(state_mod.RunState, "_run", fake_run)

    rs = state_mod.RunState()
    rs.csv_path = str(existing_csv)  # simulates "this is the last run's csv"

    rs.start(SweepUIConfig(), resume=True)

    # the synchronous part of start() (before the background thread runs _run)
    assert rs.csv_path == str(existing_csv)
    assert rs.ok == 1
    assert rs.skipped == 1
    assert rs.current == 1
    assert rs.total == 3

    for _ in range(100):
        if "csv_path" in recorded:
            break
        time.sleep(0.01)

    assert recorded["csv_path"] == existing_csv
    assert recorded["existing_statuses"] == {cid0: "ok"}


def test_start_captures_the_configured_period_and_dte_values_into_the_snapshot(tmp_path, monkeypatch):
    """`cfg` itself is discarded right after being handed to the background thread
    (never stored as self.cfg) - start_date/end_date/dte_values must be captured
    into RunState's own fields at start() time, or a "done" status has no way left
    to say which backtest period it actually ran. See the "backtest period
    visibility" plan this was added for."""
    combos = [{"a": 1}]
    _patch_common(monkeypatch, tmp_path, combos)

    def fake_run(self, cfg, combos_, csv_path, fieldnames_, stop_event, existing_statuses):
        with self._lock:
            self.status = "done"

    monkeypatch.setattr(state_mod.RunState, "_run", fake_run)

    rs = state_mod.RunState()
    rs.start(SweepUIConfig(start_date="2026-08-01", end_date="2026-09-03", dte_values=[0, 1]), resume=False)

    snap = rs.snapshot()
    assert snap["start_date"] == "2026-08-01"
    assert snap["end_date"] == "2026-09-03"
    assert snap["dte_values"] == [0, 1]


def test_known_strategy_keys_maps_to_the_base_combo_id_over_a_dte_variant(tmp_path, monkeypatch):
    """When the same strategy_key has both a base row and a "_dteN"-suffixed
    variant row, the base combo_id must win - Force re-download regenerates
    whichever DTE variants are needed from the base row alone."""
    from src.web import registry

    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    registry.upsert_rows({
        "base_cid": {"combo_id": "base_cid", "strategy_key": "shared_key"},
        "base_cid_dte0": {"combo_id": "base_cid_dte0", "strategy_key": "shared_key"},
        "other_cid": {"combo_id": "other_cid", "strategy_key": "different_key"},
    })

    known = state_mod._known_strategy_keys()
    assert known["shared_key"] == "base_cid"
    assert known["different_key"] == "other_cid"


def test_known_strategy_keys_falls_back_to_variant_when_no_base_row_exists(tmp_path, monkeypatch):
    from src.web import registry

    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    registry.upsert_rows({"base_cid_dte0": {"combo_id": "base_cid_dte0", "strategy_key": "shared_key"}})

    known = state_mod._known_strategy_keys()
    assert known["shared_key"] == "base_cid_dte0"


def test_known_strategy_keys_skips_rows_with_no_strategy_key(tmp_path, monkeypatch):
    from src.web import registry

    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    registry.upsert_rows({"a": {"combo_id": "a", "strategy_key": ""}})

    assert state_mod._known_strategy_keys() == {}


def test_run_captures_a_known_duplicate_strategy_instead_of_replaying_it(tmp_path, monkeypatch):
    """End-to-end: a combo whose strategy already has a registry record under a
    different combo_id must be captured into the pending-refresh queue, never
    replayed - the concrete throughput/history-fragmentation fix this session's
    plan was built for."""
    from src.store import strategy_key
    from src.web import registry
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(state_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    monkeypatch.setattr(registry, "PENDING_DUPLICATE_REFRESH_PATH", tmp_path / "pending_duplicate_refresh.csv")
    monkeypatch.setattr(state_mod, "load_selectors", lambda path: _FakeSelectors())

    combos = [{"a": 1}, {"a": 2}]
    monkeypatch.setattr(state_mod, "expand_ui_config", lambda cfg: combos)
    registry.upsert_rows({"existing_cid": {"combo_id": "existing_cid", "strategy_key": strategy_key(combos[0])}})

    def fake_run_sweep_multiprocess(combos_, selectors, csv_path, log_path, fieldnames, **kwargs):
        assert kwargs["known_strategy_keys"] == {strategy_key(combos[0]): "existing_cid"}
        # Simulate what run_sweep_multiprocess itself would do: the duplicate
        # combo never reaches a replay, only combos[1] does.
        row_fieldnames = ["combo_id", "run_at", "status", "error", "dte", "a"]
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=row_fieldnames)
            writer.writeheader()
            writer.writerow({"combo_id": "new_cid", "run_at": "", "status": "ok", "error": "", "dte": "", "a": "2"})
        kwargs["on_progress"]({"current": 2, "total": 2, "ok": 1, "error": 0, "skipped": 0, "duplicate": 1})

    monkeypatch.setattr(state_mod, "run_sweep_multiprocess", fake_run_sweep_multiprocess)

    rs = state_mod.RunState()
    rs.start(SweepUIConfig(parallelism=2), resume=False)
    rs._thread.join(timeout=5)

    assert rs.snapshot()["status"] == "done"
    assert rs.snapshot()["duplicate"] == 1


def test_run_skips_duplicate_detection_entirely_when_flag_is_off(tmp_path, monkeypatch):
    from src.web import registry
    from src.web.models import SweepUIConfig

    monkeypatch.setattr(state_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    monkeypatch.setattr(state_mod, "load_selectors", lambda path: _FakeSelectors())
    combos = [{"a": 1}]
    monkeypatch.setattr(state_mod, "expand_ui_config", lambda cfg: combos)

    def fake_run_sweep_multiprocess(combos_, selectors, csv_path, log_path, fieldnames, **kwargs):
        assert kwargs["known_strategy_keys"] is None
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["combo_id", "run_at", "status", "error", "dte", "a"])
            writer.writeheader()
        kwargs["on_progress"]({"current": 1, "total": 1, "ok": 1, "error": 0, "skipped": 0})

    monkeypatch.setattr(state_mod, "run_sweep_multiprocess", fake_run_sweep_multiprocess)

    rs = state_mod.RunState()
    rs.start(SweepUIConfig(parallelism=2, skip_known_duplicate_strategies=False), resume=False)
    rs._thread.join(timeout=5)

    assert rs.snapshot()["status"] == "done"
    assert rs.snapshot()["duplicate"] == 0


def test_run_syncs_the_registry_after_completion(tmp_path, monkeypatch):
    """The new hook this session added: after a sweep's own file is written,
    every row in it should also land in the shared combo registry - not just its
    own timestamped file - so freshness is visible in one authoritative place
    immediately, closing the "same combo scattered across many files" gap (see
    src/web/registry.py's own module docstring)."""
    from src.web import registry

    monkeypatch.setattr(state_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "combo_registry.csv")
    monkeypatch.setattr(state_mod, "load_selectors", lambda path: _FakeSelectors())
    combos = [{"a": 1}]
    monkeypatch.setattr(state_mod, "expand_ui_config", lambda cfg: combos)

    def fake_run_sweep_multiprocess(combos_, selectors, csv_path, log_path, fieldnames, **kwargs):
        row_fieldnames = ["combo_id", "run_at", "status", "error", "dte", "a", "total_pnl"]
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=row_fieldnames)
            writer.writeheader()
            writer.writerow({
                "combo_id": "abc123", "run_at": "", "status": "ok", "error": "",
                "dte": "", "a": "1", "total_pnl": "500",
            })
        kwargs["on_progress"]({"current": 1, "total": 1, "ok": 1, "error": 0, "skipped": 0})

    monkeypatch.setattr(state_mod, "run_sweep_multiprocess", fake_run_sweep_multiprocess)

    rs = state_mod.RunState()
    rs.start(SweepUIConfig(parallelism=2), resume=False)
    rs._thread.join(timeout=5)

    assert rs.snapshot()["status"] == "done"
    reg = registry.read_registry()
    assert "abc123" in reg
    assert reg["abc123"]["total_pnl"] == "500"


def test_resume_with_no_explicit_target_and_no_prior_csv_raises_instead_of_guessing(tmp_path, monkeypatch):
    """Covers the case where the server itself restarted (RunState.csv_path reset to
    None) and no saved execution was explicitly loaded (no resume_csv). Must raise a
    clear error, NOT silently guess "the most recently modified CSV on disk" - that
    guess has nothing to do with which execution the user actually meant to resume,
    and was confirmed live to resume into a totally unrelated file (re-running
    thousands of already-completed combos as if they were new) the one time it had
    more than one plausible CSV to choose from."""
    combos = [{"a": 1}, {"a": 2}]
    _patch_common(monkeypatch, tmp_path, combos)

    # An unrelated CSV sitting on disk, newer than nothing else - must NOT be picked.
    on_disk_csv = tmp_path / "results_web_20260101_000000.csv"
    fieldnames = ["combo_id", "run_at", "status", "error", "a"]
    cid0 = store.combo_id(combos[0])
    _write_csv(on_disk_csv, fieldnames, [{"combo_id": cid0, "run_at": "", "status": "ok", "error": "", "a": 1}])

    rs = state_mod.RunState()  # csv_path is None, as if freshly (re)started
    with pytest.raises(RuntimeError, match="load the saved execution"):
        rs.start(SweepUIConfig(), resume=True)


def test_resume_csv_targets_an_explicit_path_not_just_most_recent(tmp_path, monkeypatch):
    """Covers "load a saved execution, then resume it" - resume_csv must win over
    both RunState.csv_path (a different, more recent run) and the on-disk fallback."""
    combos = [{"a": 1}, {"a": 2}]
    _patch_common(monkeypatch, tmp_path, combos)

    older_named_csv = tmp_path / "results_web_20260101_000000.csv"
    newer_unrelated_csv = tmp_path / "results_web_20260202_000000.csv"
    fieldnames = ["combo_id", "run_at", "status", "error", "a"]
    cid0 = store.combo_id(combos[0])
    _write_csv(older_named_csv, fieldnames, [{"combo_id": cid0, "run_at": "", "status": "ok", "error": "", "a": 1}])
    _write_csv(newer_unrelated_csv, fieldnames, [])

    recorded: dict = {}

    def fake_run(self, cfg, combos_, csv_path, fieldnames_, stop_event, existing_statuses):
        recorded["csv_path"] = csv_path
        with self._lock:
            self.status = "done"

    monkeypatch.setattr(state_mod.RunState, "_run", fake_run)

    rs = state_mod.RunState()
    rs.csv_path = str(newer_unrelated_csv)  # simulates a different run being "the last one"

    rs.start(SweepUIConfig(), resume=True, resume_csv=str(older_named_csv))

    assert rs.csv_path == str(older_named_csv)
    assert rs.ok == 1


def test_resume_csv_with_nonexistent_path_raises(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, [{"a": 1}])
    rs = state_mod.RunState()
    with pytest.raises(RuntimeError):
        rs.start(SweepUIConfig(), resume=True, resume_csv=str(tmp_path / "does-not-exist.csv"))


def test_resume_without_any_previous_csv_raises(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path, [{"a": 1}])
    rs = state_mod.RunState()
    with pytest.raises(RuntimeError):
        rs.start(SweepUIConfig(), resume=True)


def test_large_sweep_uses_a_lazy_generator_and_an_estimated_total(tmp_path, monkeypatch):
    """Above EXACT_COUNT_THRESHOLD, start() must not fall back to expand_ui_config's
    eager, exact pass - that eager pass (and its eval()-heavy exclude filtering) is
    exactly the performance problem this path exists to avoid. Force the threshold
    down instead of building a genuinely huge fixture config."""
    monkeypatch.setattr(state_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "load_selectors", lambda path: _FakeSelectors())
    monkeypatch.setattr(state_mod, "EXACT_COUNT_THRESHOLD", 2)  # default SweepUIConfig() has 160 raw combos

    def blow_up(cfg):
        raise AssertionError("expand_ui_config must not be called on the large-sweep path")

    monkeypatch.setattr(state_mod, "expand_ui_config", blow_up)

    recorded: dict = {}

    def fake_run(self, cfg, combos_, csv_path, fieldnames_, stop_event, existing_statuses):
        recorded["combos_has_len"] = hasattr(combos_, "__len__")
        recorded["first_five"] = [next(combos_) for _ in range(5)]
        with self._lock:
            self.status = "done"

    monkeypatch.setattr(state_mod.RunState, "_run", fake_run)

    rs = state_mod.RunState()
    rs.start(SweepUIConfig(), resume=False)

    assert rs.total_estimated is True
    # sample_size (5000) covers the whole 160-combo raw space, so the "estimate" is
    # exact here: 104 of 160 raw combos survive the default config's built-in "must
    # have a hard Stop Loss" exclude (see to_sweep_config).
    assert rs.total == 104
    assert rs.current == 0
    assert rs.skipped == 0

    for _ in range(100):
        if "combos_has_len" in recorded:
            break
        time.sleep(0.01)

    assert recorded["combos_has_len"] is False
    assert len(recorded["first_five"]) == 5


def test_fresh_start_ignores_any_previous_csv(tmp_path, monkeypatch):
    """resume=False (the default / plain Start button) must not accidentally pick up
    an old CSV - it should always create a brand-new timestamped file."""
    combos = [{"a": 1}]
    _patch_common(monkeypatch, tmp_path, combos)

    stale_csv = tmp_path / "results_web_stale.csv"
    stale_csv.write_text("combo_id,status\nabc,ok\n")

    recorded: dict = {}

    def fake_run(self, cfg, combos_, csv_path, fieldnames_, stop_event, existing_statuses):
        recorded["csv_path"] = csv_path
        recorded["existing_statuses"] = existing_statuses
        with self._lock:
            self.status = "done"

    monkeypatch.setattr(state_mod.RunState, "_run", fake_run)

    rs = state_mod.RunState()
    rs.start(SweepUIConfig(), resume=False)

    assert rs.csv_path != str(stale_csv)
    assert rs.skipped == 0

    for _ in range(100):
        if "existing_statuses" in recorded:
            break
        time.sleep(0.01)
    assert recorded["existing_statuses"] == {}
