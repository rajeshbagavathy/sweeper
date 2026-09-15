from __future__ import annotations

import csv
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src import browser, store
from src.auth import LoginNotConfigured, is_logged_in
from src.config import load_selectors
from src.runner import AccountConfig, parse_dte_variant_suffix, run_sweep, run_sweep_multiprocess
from src.sweep import EXACT_COUNT_THRESHOLD, estimate_survival, raw_count
from src.web import registry
from src.web.expand import expand_ui_config, iter_shuffled_ui_combos, probe_combos, to_sweep_config
from src.web.models import SweepUIConfig

ROOT = Path(__file__).resolve().parent.parent.parent
SELECTORS_PATH = ROOT / "config" / "selectors.yaml"
OUTPUT_DIR = ROOT / "output"
LOG_PATH = OUTPUT_DIR / "run.log"


def _sync_registry_from_csv(csv_path: Path) -> None:
    """Every row this run's own file currently holds becomes visible in the one
    authoritative combo registry too - not just in this run's own timestamped
    file, which is exactly how the same combo_id ends up scattered across many
    results_web_*.csv files with nothing ever reconciling them (see
    src/web/registry.py's own module docstring). Called once at the end of a run
    (done or stopped early), not per-combo during it - reads the whole file in
    one pass and upserts it in one batched call, the same "don't pay a per-item
    I/O cost inside a sweep loop" lesson this session already learned once (see
    registry.upsert_rows' own docstring)."""
    if not csv_path.exists():
        return
    with csv_path.open(newline="") as f:
        rows = {row["combo_id"]: row for row in csv.DictReader(f) if row.get("combo_id")}
    registry.upsert_rows(rows)


def _known_strategy_keys() -> dict[str, str]:
    """strategy_key -> the registry's own existing combo_id for it - built once
    per sweep start (not looked up per-combo), so runner.py's skip-check is an
    O(1) dict lookup regardless of how large the registry or the sweep is. When
    the same strategy_key has both a base row and one or more "_dteN"-suffixed
    variant rows (DTE isn't part of the hash - see store.strategy_key), the
    BASE combo_id wins: Force re-download regenerates whichever DTE variants
    are needed from the base row alone, so that's the one future duplicate
    detection (and Force re-download) should target."""
    known: dict[str, str] = {}
    for cid, row in registry.read_registry().items():
        key = row.get("strategy_key")
        if not key:
            continue
        is_variant = parse_dte_variant_suffix(cid) is not None
        if key not in known or not is_variant:
            known[key] = cid
    return known


@dataclass
class RunState:
    status: str = "idle"  # idle | running | stopping | done | stopped | error
    current: int = 0
    total: int = 0
    ok: int = 0
    error: int = 0
    skipped: int = 0
    # How many combos were captured into output/pending_duplicate_refresh.csv
    # instead of replayed, because their underlying strategy already has a
    # record under a different combo_id (see SweepUIConfig.
    # skip_known_duplicate_strategies) - always 0 when that flag is off.
    duplicate: int = 0
    # How many workers currently have a combo in flight - only ever non-zero while
    # multiprocess workers are active. Most useful during "stopping": Stop no longer
    # kills mid-combo (see run_sweep_multiprocess), so this is what lets the UI say
    # "finishing N in-progress combo(s), picking up no new work" instead of the
    # click appearing to do nothing while everything quietly finishes up.
    in_progress: int = 0
    # Which specific combo(s) in_progress' count refers to (e.g. "SENSEX 09:17-13:15")
    # - see runner._combo_label/_run_from_queue's progress_queue. Empty whenever
    # nothing's actually mid-replay right now (e.g. workers are alive but pausing
    # between combos) even if `in_progress` itself is non-zero - that distinction is
    # real and worth keeping, not a bug: it tells you workers are about to re-check
    # Stop very soon rather than being stuck deep in a slow combo.
    in_progress_labels: list[str] = field(default_factory=list)
    csv_path: str | None = None
    message: str | None = None
    started_at: str | None = None
    # True while `total` is a fast sampled estimate rather than an exact count - only
    # happens for a sweep too large to fully expand up front (see EXACT_COUNT_THRESHOLD
    # in src.sweep). Flips to False once the run completes naturally and the true
    # total is known (see _run's multiprocess branch) - stays True for the life of a
    # run that's still in progress or was stopped early.
    total_estimated: bool = False
    # What this run was actually CONFIGURED with, captured once at start() - `cfg`
    # itself is otherwise discarded right after being handed to the background
    # thread (never stored as self.cfg), so this is the only trace of it left once
    # the run is underway. Exists so the status panel can tell you which backtest
    # period/DTE(s) a "done" sweep actually used, instead of leaving that only
    # discoverable by opening a row's own start_date/end_date column - see the
    # "backtest period visibility" plan this was added for.
    start_date: str | None = None
    end_date: str | None = None
    dte_values: list[int] | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _stop_event: threading.Event | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "total_estimated": self.total_estimated,
                "ok": self.ok,
                "error": self.error,
                "skipped": self.skipped,
                "duplicate": self.duplicate,
                "in_progress": self.in_progress,
                "in_progress_labels": list(self.in_progress_labels),
                "csv_path": self.csv_path,
                "message": self.message,
                "started_at": self.started_at,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "dte_values": list(self.dte_values) if self.dte_values is not None else None,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status in ("running", "stopping")

    def start(self, cfg: SweepUIConfig, resume: bool = False, resume_csv: str | None = None) -> None:
        if self.is_running():
            raise RuntimeError("A sweep is already running - stop it first.")

        # A sweep small enough to fully expand up front (the common case) keeps the
        # exact, well-tested path unchanged - `combos` is a plain list, `total` is the
        # precise post-exclude count. Above EXACT_COUNT_THRESHOLD, expand_ui_config's
        # eager pass is itself the performance problem (tens of seconds of eval()
        # filtering across a huge raw product before Start even returns, plus the
        # whole result sitting in memory) - `combos` becomes a lazy generator
        # (iter_shuffled_ui_combos) that's fed to the worker queue incrementally, in
        # random order, and `total` is a fast sampled estimate instead of exact (see
        # estimate_survival) until the run completes and the real number is known.
        sweep = to_sweep_config(cfg)
        n = raw_count([sweep.vary[k] for k in sweep.vary])
        large_sweep = n > EXACT_COUNT_THRESHOLD
        if large_sweep:
            estimated_total, _sample = estimate_survival(sweep)
            combos: Any = None
            total = estimated_total
            fieldnames_source = probe_combos(cfg)
        else:
            combos = expand_ui_config(cfg)
            total = len(combos)
            fieldnames_source = combos

        OUTPUT_DIR.mkdir(exist_ok=True)
        metric_names = list(load_selectors(SELECTORS_PATH).results.metrics.keys())

        if resume:
            if resume_csv:
                # explicit target (e.g. from "load a saved execution, then resume it")
                # takes priority over guessing which CSV was "last" - the whole point
                # is being able to jump back to *any* saved execution, not just the
                # most recent one.
                resume_path: Path | None = Path(resume_csv)
            else:
                with self._lock:
                    prior_csv = self.csv_path
                # Deliberately no "guess the most recently modified CSV on disk"
                # fallback here (there used to be one) - with more than one CSV in
                # output/, "most recent" has nothing to do with "the execution the
                # user meant to resume." Confirmed live: it silently resumed into an
                # unrelated file after a server restart cleared prior_csv, re-running
                # thousands of already-completed combos as if they were new. Resuming
                # without an explicit target is only safe within the SAME server
                # process that ran it (prior_csv, still in memory) - across a restart,
                # the user must explicitly re-load the saved execution first so the
                # frontend can pass its exact csv_path back as resume_csv.
                resume_path = Path(prior_csv) if prior_csv else None
            if resume_path is None or not resume_path.exists():
                raise RuntimeError(
                    "Nothing to resume - load the saved execution you want to resume first "
                    "(its exact CSV can't be guessed), or start a fresh run instead."
                )
            csv_path = resume_path
            # Rewrites the file in place (once) if it predates a column that's since
            # become standard (e.g. "dte") - existing rows just get a blank value for
            # it rather than the new column being silently dropped or the file ending
            # up with more values per row than its header declares.
            fieldnames = store.migrate_header_if_needed(
                csv_path, store.build_fieldnames(fieldnames_source, metric_names)
            )
            existing_statuses, _ = store.load_existing(csv_path)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = OUTPUT_DIR / f"results_web_{timestamp}.csv"
            existing_statuses = {}
            fieldnames = store.build_fieldnames(fieldnames_source, metric_names)

        if large_sweep:
            # Checking every one of a huge sweep's combos against existing_statuses
            # would mean walking the whole space again (see EXACT_COUNT_THRESHOLD) -
            # existing_statuses is already keyed by combo_id from a resume of this
            # exact csv_path, so just counting its "ok" entries is equivalent unless
            # the config changed between runs, in which case this is only ever the
            # STARTING number shown before any new work happens - _on_progress
            # corrects it for real as run_sweep_multiprocess counts skips itself.
            already_done = sum(1 for v in existing_statuses.values() if v == "ok")
        else:
            already_done = sum(1 for c in combos if existing_statuses.get(store.combo_id(c)) == "ok")

        with self._lock:
            self.status = "running"
            self.current = already_done
            self.total = total
            self.total_estimated = large_sweep
            self.ok = already_done
            self.error = 0
            self.skipped = already_done
            self.csv_path = str(csv_path)
            self.message = None
            self.started_at = datetime.now().isoformat()
            self.start_date = cfg.start_date
            self.end_date = cfg.end_date
            self.dte_values = list(cfg.dte_values) if cfg.dte_values else None
            self._stop_event = threading.Event()

        stop_event = self._stop_event
        # Built here, right before handing off to the background thread, rather than
        # reused from the `combos = None` placeholder above - a fresh seed each Start
        # means a fresh shuffle each time (fine: already-done combos are still tracked
        # by combo_id regardless of what order they're encountered in, see
        # run_sweep_multiprocess's generator branch).
        combos_for_run: Any = iter_shuffled_ui_combos(cfg, seed=random.randrange(2**31)) if large_sweep else combos
        thread = threading.Thread(
            target=self._run,
            args=(cfg, combos_for_run, csv_path, fieldnames, stop_event, existing_statuses),
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()
                # Immediate UI feedback - don't wait for the run loop's own poll
                # cycle (up to a few seconds) to notice and flip this itself.
                if self.status == "running":
                    self.status = "stopping"

    def _on_progress(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.current = update["current"]
            self.ok = update["ok"]
            self.error = update["error"]
            self.skipped = update["skipped"]
            self.duplicate = update.get("duplicate", 0)
            self.in_progress = update.get("in_progress", 0)
            self.in_progress_labels = update.get("in_progress_labels", [])

    def _run(
        self,
        cfg: SweepUIConfig,
        combos: Any,  # list[dict] (small sweep) or a lazy Iterable[dict] (large sweep - see start())
        csv_path: Path,
        fieldnames: list[str],
        stop_event: threading.Event,
        existing_statuses: dict[str, str],
    ) -> None:
        load_dotenv()
        try:
            selectors = load_selectors(SELECTORS_PATH)
            known_strategy_keys = _known_strategy_keys() if cfg.skip_known_duplicate_strategies else None

            if cfg.parallelism > 1 or cfg.parallelism_account2 > 0 or cfg.parallelism_account3 > 0:
                # Each worker opens its own browser profile/process (see
                # runner.run_sweep_multiprocess) - this thread never opens a
                # browser context itself, it only dispatches and merges results.
                accounts = None
                if cfg.parallelism_account2 > 0 or cfg.parallelism_account3 > 0:
                    # Confirmed live: AlgoTest throttles concurrency per account, not
                    # per machine/IP - splitting workers across multiple accounts gives
                    # each its own independent budget. Combos are dispatched from one
                    # shared queue (see run_sweep_multiprocess) regardless of how many
                    # accounts are involved, so no combo ever runs on more than one
                    # account, and whichever account is faster just picks up more work.
                    account_slots = [
                        (cfg.parallelism, "ALGOTEST_EMAIL", "ALGOTEST_PASSWORD", browser.PROFILE_DIR),
                        (cfg.parallelism_account2, "ALGOTEST_EMAIL_2", "ALGOTEST_PASSWORD_2", browser.PROFILE_DIR_2),
                        (cfg.parallelism_account3, "ALGOTEST_EMAIL_3", "ALGOTEST_PASSWORD_3", browser.PROFILE_DIR_3),
                    ]
                    accounts = [
                        AccountConfig(os.environ.get(email_env), os.environ.get(password_env), profile_dir)
                        for count, email_env, password_env, profile_dir in account_slots
                        for _ in range(count)
                    ]
                run_sweep_multiprocess(
                    combos,
                    selectors,
                    csv_path,
                    LOG_PATH,
                    fieldnames,
                    existing_statuses=existing_statuses,
                    parallelism=cfg.parallelism,
                    # Only meaningful when combos is the large-sweep generator (see
                    # start()) - run_sweep_multiprocess ignores both otherwise, since a
                    # plain list already knows its own exact length and skip count.
                    # self.skipped was seeded in start() with the same already-done
                    # count already_done_hint needs - read here before any progress
                    # update can change it.
                    total_hint=self.total,
                    already_done_hint=self.skipped,
                    headless=cfg.headless,
                    delay_s=cfg.delay,
                    result_timeout_s=cfg.result_timeout,
                    max_retries=cfg.max_retries,
                    email=os.environ.get("ALGOTEST_EMAIL"),
                    password=os.environ.get("ALGOTEST_PASSWORD"),
                    slippage_pct=cfg.slippage_pct,
                    dte_values=cfg.dte_values,
                    brokerage_rate=cfg.brokerage_rate,
                    auto_download_enabled=cfg.auto_download_enabled,
                    auto_download_min_return_max_dd=cfg.auto_download_min_return_max_dd,
                    auto_download_min_trades=cfg.auto_download_min_trades,
                    capture_dte_individually=cfg.capture_dte_individually,
                    known_strategy_keys=known_strategy_keys,
                    duplicate_queue_path=registry.PENDING_DUPLICATE_REFRESH_PATH,
                    accounts=accounts,
                    on_progress=self._on_progress,
                    stop_event=stop_event,
                )
                with self._lock:
                    self.status = "stopped" if stop_event.is_set() else "done"
                    self.in_progress = 0
                    self.in_progress_labels = []
                    # The generator has now been fully consumed (unless stopped
                    # early, in which case `current` is only a partial count) - the
                    # true final total is knowable for the first time, so stop
                    # showing the pre-run estimate.
                    if self.total_estimated and not stop_event.is_set():
                        self.total = self.current
                        self.total_estimated = False
                _sync_registry_from_csv(csv_path)
                return

            # The single-browser path (no parallelism) - never used for a sweep large
            # enough to need the lazy generator in practice, but run_sweep needs a
            # real list (it calls len(combos) for its own progress total), so fall
            # back to materializing one here rather than erroring on a generator.
            if not hasattr(combos, "__len__"):
                combos = list(combos)

            with browser.persistent_context(headless=cfg.headless) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(selectors.builder.url)

                try:
                    is_logged_in(page, selectors)
                except LoginNotConfigured as exc:
                    with self._lock:
                        self.status = "error"
                        self.message = str(exc)
                    return

                run_sweep(
                    page,
                    combos,
                    selectors,
                    csv_path,
                    LOG_PATH,
                    fieldnames,
                    existing_statuses=existing_statuses,
                    delay_s=cfg.delay,
                    result_timeout_s=cfg.result_timeout,
                    max_retries=cfg.max_retries,
                    email=os.environ.get("ALGOTEST_EMAIL"),
                    password=os.environ.get("ALGOTEST_PASSWORD"),
                    slippage_pct=cfg.slippage_pct,
                    dte_values=cfg.dte_values,
                    brokerage_rate=cfg.brokerage_rate,
                    auto_download_enabled=cfg.auto_download_enabled,
                    auto_download_min_return_max_dd=cfg.auto_download_min_return_max_dd,
                    auto_download_min_trades=cfg.auto_download_min_trades,
                    capture_dte_individually=cfg.capture_dte_individually,
                    known_strategy_keys=known_strategy_keys,
                    duplicate_queue_path=registry.PENDING_DUPLICATE_REFRESH_PATH,
                    on_progress=self._on_progress,
                    stop_event=stop_event,
                )

            with self._lock:
                self.status = "stopped" if stop_event.is_set() else "done"
                self.in_progress = 0
                self.in_progress_labels = []
            _sync_registry_from_csv(csv_path)

        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI instead of dying silently
            with self._lock:
                self.status = "error"
                self.message = str(exc)
                self.in_progress = 0
                self.in_progress_labels = []


run_state = RunState()
