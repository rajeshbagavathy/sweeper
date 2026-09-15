from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src import browser, store
from src.config import load_selectors
from src.correlate import (
    DEFAULT_CORR_THRESHOLD,
    compute_portfolio_metrics_weighted_with_charges,
    correlation_matrix,
    parse_trade_report,
    pick_diversified_basket,
    trade_report_path,
)
from src.runner import AccountConfig, parse_dte_variant_suffix
from src.web import registry
from src.web.combo_launcher import row_to_combo
from src.web.expand import load_ui_config
from src.web.narrow import combined_sort_key
from src.web.portfolio import charges_from_row, data_coverage_gaps, has_hard_stop_loss

SELECTORS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "selectors.yaml"
REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "output" / "trade_reports"
REFRESH_AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "output" / "refresh_audit.log"


def _log_refresh(cid: str, old_start: str, old_end: str, new_start: str, new_end: str) -> None:
    """Append-only history of every force-refresh's date-window change - the one
    piece of history output/run.log's own per-combo ok/error pings never capture
    (see runner._log: {combo_id, status, error, attempt, ts} only, no before/
    after values at all). roll_date_window overwrites combo["start_date"]/
    ["end_date"] in place with nothing recording what they used to be - once the
    trade report itself gets overwritten too (download_current_report, same
    file path), the previous window is gone with no way to answer "what did
    this used to cover before I refreshed it," which is exactly what caused
    real confusion this session ("the starting and ending times are not
    matching at all"). Best-effort: a logging failure must never block the
    actual refresh it's recording."""
    try:
        REFRESH_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "combo_id": cid,
            "old_start_date": old_start, "old_end_date": old_end,
            "new_start_date": new_start, "new_end_date": new_end,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        with REFRESH_AUDIT_LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass

# force=True still skips a combo whose report was downloaded within this window -
# lets a crashed/interrupted "Force re-download & update results" run be restarted
# without redoing everything: anything it already got to recently is left alone.
FORCE_SKIP_IF_NEWER_THAN_S = 24 * 60 * 60


def should_skip_download(target: Path, force: bool) -> bool:
    """Whether `target` (a combo's trade-report path) can be left alone rather than
    (re)downloaded. Without force: skip whenever it already exists at all. With
    force: skip only if it's also newer than FORCE_SKIP_IF_NEWER_THAN_S - old enough
    to need refreshing, but recent enough (almost certainly this same backfill,
    interrupted and restarted) that redoing it would just be wasted work."""
    if not target.exists():
        return False
    if not force:
        return True
    return (time.time() - target.stat().st_mtime) < FORCE_SKIP_IF_NEWER_THAN_S


def roll_date_window(combo: dict[str, Any], *, today: date | None = None) -> None:
    """Mutates combo's start_date/end_date in place, sliding both forward so the
    window ends today - same length, just rolled - instead of the fixed range the
    combo was originally discovered with. Every row's start_date/end_date is frozen
    at whatever it was when that combo was first swept (row_to_combo just copies the
    row's own columns), so without this, "Force re-download & update results" would
    keep replaying the exact same stale window forever, no matter how often it's
    re-run: confirmed live, a combo force-refreshed hours ago still had its original
    end_date, having silently missed a whole newer week of real trading data that
    AlgoTest's own live account had already rolled forward to include. A no-op if
    the window already ends today."""
    today = today or date.today()
    start = date.fromisoformat(combo["start_date"])
    end = date.fromisoformat(combo["end_date"])
    if end >= today:
        return
    window = end - start
    combo["end_date"] = today.isoformat()
    combo["start_date"] = (today - window).isoformat()


def row_dte_values(row: dict[str, Any], fallback: list[int]) -> list[int]:
    """A bare (non-"_dteN"-suffixed) combo's OWN recorded DTE(s), parsed from its
    "dte" column - a comma-separated string like "0" or "0,1,2,3,4" (see
    runner._build_row) - the DTE(s) this exact row was ACTUALLY discovered under,
    which can differ from whatever the CURRENTLY SAVED sweep config's own
    dte_values happens to be by the time someone clicks Force re-download. Using
    the saved config instead of the row's own value here is exactly the bug
    parse_dte_variant_suffix's docstring already warned about for "_dteN"
    variants, just for the bare-id case instead: confirmed live, a row recorded
    as dte="0" (isolated via the results table's own DTE filter) came back from a
    replay spread across every weekday - not just DTE 0's Tuesdays - because the
    saved config had since changed to dte_values=[0,1,2,3,4].

    `fallback` (the saved config's own dte_values) is used only when the row has
    no usable dte value at all - a genuinely older row from before DTE tracking
    existed, where there's nothing else to go on."""
    raw = (row.get("dte") or "").strip()
    if not raw:
        return fallback
    try:
        return [int(x) for x in raw.split(",") if x.strip() != ""]
    except ValueError:
        return fallback


def label_for(row: dict[str, Any]) -> str:
    """Short human-readable stand-in for a combo_id hash, for the matrix/basket/
    progress UI."""
    parts = [row.get("instrument", "?"), f"{row.get('entry_time', '?')}-{row.get('exit_time', '?')}"]
    dte = row.get("dte")
    if dte:
        parts.append(f"DTE {dte}")
    return " | ".join(parts)


def compute_correlation(rows: list[dict[str, Any]], *, threshold: float = DEFAULT_CORR_THRESHOLD) -> dict[str, Any]:
    """Pure/synchronous: correlate whatever's ALREADY on disk for `rows` right now -
    does no replaying or downloading itself (see CorrelateState for that, a separate
    step). Rows with no cached report yet are reported in `missing`, not silently
    dropped, so the UI can tell you to download first rather than just showing a
    smaller-than-expected matrix with no explanation. Same for a row with no hard
    Stop Loss at all (leg-level or overall) - reported in `excluded_no_stop_loss`
    rather than silently ranked, since Trail SL alone leaves the position with
    nothing capping its loss before the trail has locked anything in (see
    has_hard_stop_loss in src/web/portfolio.py, which this reuses directly)."""
    excluded_no_sl = [r for r in rows if not has_hard_stop_loss(r)]
    eligible_rows = [r for r in rows if has_hard_stop_loss(r)]

    row_by_id = {r.get("combo_id"): r for r in eligible_rows}
    series: dict[str, dict[str, float]] = {}
    missing: list[str] = []
    for row in eligible_rows:
        cid = row.get("combo_id")
        if not cid:
            continue
        path = trade_report_path(REPORTS_DIR, row.get("instrument", ""), cid)
        if path.exists():
            series[cid] = parse_trade_report(path)
        else:
            missing.append(cid)

    matrix = correlation_matrix(series)
    score_key = combined_sort_key(eligible_rows)
    ranked_ids = sorted(series.keys(), key=lambda cid: score_key(row_by_id[cid]), reverse=True)
    basket, skipped = pick_diversified_basket(ranked_ids, matrix, threshold=threshold)
    for entry in basket:
        row = row_by_id[entry["combo_id"]]
        entry["score"] = score_key(row)
        # So the basket table can show when each pick actually enters/exits without
        # a second lookup - useful in particular for spotting whether the "least
        # correlated" picks also happen to cluster at the same time of day.
        entry["entry_time"] = row.get("entry_time", "")
        entry["exit_time"] = row.get("exit_time", "")

    return {
        "matrix": matrix,
        "labels": {cid: label_for(row_by_id[cid]) for cid in series},
        "basket": basket,
        "skipped": skipped,
        "missing": [{"combo_id": cid, "label": label_for(row_by_id[cid])} for cid in missing],
        "excluded_no_stop_loss": [
            {"combo_id": r.get("combo_id"), "label": label_for(r)} for r in excluded_no_sl
        ],
        "data_gaps": data_coverage_gaps(eligible_rows, REPORTS_DIR),
        "threshold": threshold,
        # Each basket member's own "score" measures that strategy alone - this is the
        # basket run AS ONE COMBINED PORTFOLIO (daily P/L summed across all members
        # first, each also charged its own recorded brokerage/taxes - see
        # compute_portfolio_metrics_weighted_with_charges - a downloaded trade report
        # reflects neither on its own), directly comparable to an externally-reported
        # multi-strategy aggregate (Overall Profit / Win % / Return-MaxDD / Reward:Risk).
        "basket_portfolio": compute_portfolio_metrics_weighted_with_charges(
            {b["combo_id"]: 10.0 for b in basket},  # equal weight, all at the recorded base lot size
            series,
            {b["combo_id"]: charges_from_row(row_by_id[b["combo_id"]]) for b in basket},
            base_lots=10.0,
        ),
    }


@dataclass
class CorrelateState:
    """Background job for step 1 only - replay+download (or reuse a cached copy of)
    each of a set of already-filtered/top-N rows' per-trade report. Correlation math
    itself is a separate, synchronous step (see compute_correlation) triggered by its
    own button, not run automatically once downloads finish. Mirrors RunState's
    status/thread/stop_event shape."""

    status: str = "idle"  # idle | running | stopping | done | stopped | error
    total: int = 0
    downloaded: int = 0  # includes cache hits - both mean "the file is on disk now"
    failed: int = 0
    in_progress: int = 0
    in_progress_labels: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None
    started_at: str | None = None
    # Every combo whose stale backtest window got rolled forward this run (see
    # roll_date_window) - the actual mechanism that fragments a shared results file
    # into two different periods (confirmed live: 8418 rows at one period, 914 at
    # another, after exactly this happened). Populated at the same point
    # _log_refresh already writes to refresh_audit.log, so the UI can surface it as
    # a visible "why don't all my reports have the same period now" explanation the
    # moment it happens, not just leave it to be discovered later in the results
    # table's own mismatch banner.
    rolled_forward: list[dict[str, Any]] = field(default_factory=list)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _stop_event: threading.Event | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "total": self.total,
                "downloaded": self.downloaded,
                "failed": self.failed,
                "in_progress": self.in_progress,
                "in_progress_labels": list(self.in_progress_labels),
                "failures": list(self.failures),
                "message": self.message,
                "started_at": self.started_at,
                "rolled_forward": list(self.rolled_forward),
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status in ("running", "stopping")

    def start(
        self,
        rows: list[dict[str, Any]],
        csv_paths: list[Path] | None = None,
        force: bool = False,
        *,
        parallelism: int | None = None,
        parallelism_account2: int | None = None,
        parallelism_account3: int | None = None,
    ) -> None:
        """`force=True` ("Force re-download & update results" in the UI) replays
        every row regardless of caching - except one whose report was downloaded
        within FORCE_SKIP_IF_NEWER_THAN_S, which is left alone (see _run) so a
        backfill that got interrupted partway through can just be restarted instead
        of redoing everything - rolls each combo's start_date/end_date forward to
        end today instead of replaying the same stale window it was first
        discovered with (see roll_date_window) - and merges each fresh row back
        into its original source CSV. `csv_paths` (the exact files these rows were
        read from) is only needed for that merge step; pass it whenever `force`
        might be True.

        `parallelism`/`parallelism_account2`/`parallelism_account3` override the
        SAVED sweep config's own worker counts for this run only - never touching
        config/sweep_ui.yaml. This exists because this job's actual parallelism
        used to come exclusively from load_ui_config() (disk), completely
        independent of whatever the homepage's parallel-tabs fields showed in the
        browser at the time (typing a new value there and clicking something here
        did nothing; only an explicit config Save would have taken effect) - and
        because a page refresh restores whichever execution is currently active
        from its OWN saved config, silently overwriting an unsaved edit typed into
        those same shared fields. Passing an explicit override here sidesteps both
        problems entirely. Any left None uses the saved config's own value,
        unchanged from before this parameter existed."""
        if self.is_running():
            raise RuntimeError("A report download is already in progress.")
        if not rows:
            raise ValueError("No rows to download - adjust the filters/Top N or run a sweep first.")

        with self._lock:
            self.status = "running"
            self.total = len(rows)
            self.downloaded = 0
            self.failed = 0
            self.in_progress = 0
            self.in_progress_labels = []
            self.failures = []
            self.message = None
            self.started_at = datetime.now().isoformat()
            self.rolled_forward = []
            self._stop_event = threading.Event()

        stop_event = self._stop_event
        parallelism_overrides = {
            "parallelism": parallelism,
            "parallelism_account2": parallelism_account2,
            "parallelism_account3": parallelism_account3,
        }
        thread = threading.Thread(
            target=self._run, args=(rows, stop_event, csv_paths or [], force, parallelism_overrides), daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()
                if self.status == "running":
                    self.status = "stopping"

    def _on_download_progress(
        self, already_cached: int, row_by_id: dict[str, dict[str, Any]], update: dict[str, Any]
    ) -> None:
        with self._lock:
            self.downloaded = already_cached + update.get("downloaded", 0)
            self.failed = update.get("failed", 0)
            self.in_progress = update.get("in_progress", 0)
            self.in_progress_labels = [
                label_for(row_by_id[cid]) for cid in update.get("in_progress_ids", []) if cid in row_by_id
            ]
            self.failures = [
                {"combo_id": f["combo_id"], "label": label_for(row_by_id.get(f["combo_id"], {})), "error": f.get("error")}
                for f in update.get("failures", [])
            ]

    def _run(
        self,
        rows: list[dict[str, Any]],
        stop_event: threading.Event,
        csv_paths: list[Path],
        force: bool,
        parallelism_overrides: dict[str, int | None] | None = None,
    ) -> None:
        from src.correlate import run_correlate_multiprocess

        load_dotenv()
        try:
            cfg = load_ui_config()
            # Per-request overrides (see start()'s own docstring for why these
            # exist) win over whatever's saved on disk - applied by mutating the
            # loaded cfg copy in place, so every other field (dte_values, delay,
            # result_timeout, ...) still comes from the saved config unchanged.
            for field, value in (parallelism_overrides or {}).items():
                if value is not None:
                    setattr(cfg, field, value)
            selectors = load_selectors(SELECTORS_PATH)
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            row_by_id = {r.get("combo_id"): r for r in rows}

            # Grouped by the DTE(s) each combo must actually be replayed under -
            # a "_dte{N}" composite id (see runner.parse_dte_variant_suffix) MUST
            # be isolated to just [N], never the config's full multi-select
            # dte_values, or the re-downloaded report silently mixes in other
            # DTEs' trades and no longer matches what its own id promises
            # (confirmed live - see parse_dte_variant_suffix's docstring). A bare
            # combo_id uses ITS OWN recorded dte column (see row_dte_values) -
            # NOT the currently-saved config's dte_values, which can have since
            # changed to something broader. Confirmed live: a combo whose row
            # said dte="0" (isolated via the results table's own DTE=0 filter)
            # came back from a force-redownload spread across every weekday
            # instead of Tuesdays-only, because the saved config's dte_values had
            # since become [0,1,2,3,4] - every metric on it (P&L, drawdown,
            # period count) was silently wrong, and it kept winning baskets it
            # had no business winning because the extra volume of mixed-in data
            # inflated its ranking. `cfg.dte_values` is only the last-resort
            # fallback, for an older row with no usable dte column at all.
            groups: dict[tuple[int, ...], list[tuple[dict[str, Any], str, str]]] = {}
            already_cached = 0
            rolled: list[dict[str, Any]] = []
            for row in rows:
                cid = row.get("combo_id")
                instrument = row.get("instrument", "")
                if not cid:
                    continue
                # force=True still replays a combo whose report is already on disk -
                # that's the whole point of "update results": most of a top-N list
                # has usually already been auto-downloaded once, and skipping those
                # here would mean the backfill never actually reaches them - see
                # should_skip_download for the one exception (a very recent report).
                target = trade_report_path(REPORTS_DIR, instrument, cid)
                if should_skip_download(target, force):
                    already_cached += 1
                    continue
                combo = row_to_combo(row)
                if force:
                    # Otherwise this replays the exact same stale window the combo
                    # was first discovered with, forever - see roll_date_window.
                    old_start, old_end = combo.get("start_date"), combo.get("end_date")
                    roll_date_window(combo)
                    new_start, new_end = combo.get("start_date"), combo.get("end_date")
                    # roll_date_window is a no-op when the window already ends
                    # today - only log an actual change, not a redundant entry
                    # every time force-redownload happens to touch an
                    # already-current combo.
                    if (new_start, new_end) != (old_start, old_end):
                        _log_refresh(cid, old_start, old_end, new_start, new_end)
                        rolled.append({
                            "combo_id": cid, "old_start": old_start, "old_end": old_end,
                            "new_start": new_start, "new_end": new_end,
                        })
                variant_dte = parse_dte_variant_suffix(cid)
                key = (variant_dte,) if variant_dte is not None else tuple(row_dte_values(row, cfg.dte_values or []))
                groups.setdefault(key, []).append((combo, cid, instrument))

            with self._lock:
                self.downloaded = already_cached
                self.rolled_forward = rolled

            accounts = None
            if cfg.parallelism_account2 > 0 or cfg.parallelism_account3 > 0:
                # Same account-splitting convention as state.py's _run - one shared
                # queue regardless of how many accounts, so no combo ever replays on
                # more than one account.
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

            # One run_correlate_multiprocess call per distinct dte_values group -
            # it only accepts a single dte_values for the whole batch, so a mix of
            # bare combos and one or more "_dteN" variants can't share one call.
            # Progress accumulates across groups (done_downloaded/done_failed) so
            # the UI's running total only ever climbs, never resets partway
            # through when a later group's own callback starts back at 0.
            done_downloaded = 0
            done_failed = 0
            done_failures: list[dict[str, Any]] = []
            all_updated_rows: list[dict[str, Any]] = []
            for dte_key, items in groups.items():
                if stop_event.is_set():
                    break

                def _progress(update: dict[str, Any], _bd: int = done_downloaded, _bf: int = done_failed) -> None:
                    self._on_download_progress(
                        already_cached, row_by_id,
                        {
                            **update,
                            "downloaded": _bd + update.get("downloaded", 0),
                            "failed": _bf + update.get("failed", 0),
                            "failures": done_failures + update.get("failures", []),
                        },
                    )

                result = run_correlate_multiprocess(
                    items,
                    selectors,
                    REPORTS_DIR,
                    parallelism=cfg.parallelism,
                    headless=cfg.headless,
                    delay_s=cfg.delay,
                    result_timeout_s=cfg.result_timeout,
                    max_retries=cfg.max_retries,
                    email=os.environ.get("ALGOTEST_EMAIL"),
                    password=os.environ.get("ALGOTEST_PASSWORD"),
                    slippage_pct=cfg.slippage_pct,
                    dte_values=list(dte_key),
                    brokerage_rate=cfg.brokerage_rate,
                    update_results=force,
                    accounts=accounts,
                    on_progress=_progress,
                    stop_event=stop_event,
                )
                done_downloaded += result.get("downloaded", 0)
                done_failed += result.get("failed", 0)
                done_failures += result.get("failures", [])
                all_updated_rows += result.get("updated_rows", [])

            # A refreshed row's strategy_key must be the ORIGINAL row's own value,
            # never recomputed from row_to_combo(row)'s reconstruction (which
            # _build_row would otherwise do, via the same combo dict this force
            # replay used) - confirmed live this session that row_to_combo does
            # NOT reliably reconstruct every historical combo shape (e.g. an
            # "ATM" bare-string strike mode round-trips to the wrong dict
            # entirely). Force re-download changes a combo's DATA, never its
            # underlying strategy identity, so silently recomputing here would
            # risk corrupting an already-correct strategy_key (set once, live,
            # at original discovery time - see runner._build_row) on every
            # single refresh. Falls back to whatever was freshly computed only
            # for a genuinely pre-strategy_key row that has no value to preserve.
            for row in all_updated_rows:
                cid = row.get("combo_id")
                original = row_by_id.get(cid)
                if original and original.get("strategy_key"):
                    row["strategy_key"] = original["strategy_key"]

            # Merge every freshly re-scraped row back into whichever source CSV it
            # came from - one file at a time, sequential: store.update_rows'
            # read-modify-write-whole-file isn't safe to call concurrently against
            # the same CSV. Batched per file (ONE read + ONE write covering every
            # row that belongs to it) rather than store.update_row once per row -
            # for a large force-refresh (hundreds/thousands of rows against a
            # multi-thousand-row CSV) the old per-row approach meant a full-file
            # rewrite for EVERY SINGLE COMBO, taking a very long time with no
            # progress shown and no way to stop it (confirmed live - see
            # store.update_rows' own docstring for the exact incident). A combo
            # whose row isn't found in ANY given csv_path (nothing in this codebase
            # should cause that, since these rows came from csv_paths in the first
            # place) is just left alone rather than raising - one missing merge
            # must not lose everything else's updates.
            if force and csv_paths:
                pending = {row["combo_id"]: row for row in all_updated_rows if row.get("combo_id")}
                for path in csv_paths:
                    if not pending:
                        break
                    found = store.update_rows(path, pending)
                    for cid in found:
                        pending.pop(cid, None)

            # Also upsert into the one authoritative combo registry - not just
            # whichever csv_paths this refresh was told about. Without this, a
            # combo_id refreshed here stays looking stale/orphaned in every OTHER
            # results_web_*.csv file that happens to share it (nothing in this
            # codebase ever went looking for those "other files" before - see
            # src/web/registry.py's own module docstring for exactly this gap).
            if all_updated_rows:
                registry.upsert_rows({row["combo_id"]: row for row in all_updated_rows if row.get("combo_id")})

            with self._lock:
                self.status = "stopped" if stop_event.is_set() else "done"
                self.in_progress = 0
                self.in_progress_labels = []

        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI
            with self._lock:
                self.status = "error"
                self.message = str(exc)
                self.in_progress = 0
                self.in_progress_labels = []


correlate_state = CorrelateState()
