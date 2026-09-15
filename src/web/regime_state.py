"""CAS regime analysis: a SEPARATE, isolated download+basket pathway for studying a
genuine market regime shift (e.g. CAS's introduction in August, after which decay
reportedly concentrates in the last 15 minutes before expiry) - deliberately not the
same thing as filtering an already-downloaded full-year report down to a shorter
window. A real, narrower backtest (start_date/end_date pinned to exactly the period
being studied) removes any assumption that AlgoTest's engine treats every day
independently of the window it was asked to replay - if there's any lookback/rolling
logic anchored to the backtest's own configured start date, only an actual replay of
that narrower window reflects it correctly.

Everything here WRITES only to its own folder (see regime_window_dir, one subfolder
per distinct [date_from, date_to] pair) and never mutates output/trade_reports/, the
results CSVs, or any of the existing CorrelateState/PortfolioSweepState machinery -
the basket-building math itself (src/web/portfolio.py) is reused completely
unchanged, just pointed at a different reports_dir. See src/web/app.py's /api/regime/*
endpoints for how the two attach.

It DOES read from output/trade_reports/ as a fast path (see
_reuse_regular_report_if_same_window): when a combo's own row already records
start_date/end_date EXACTLY matching the requested regime window, its regular
auto-downloaded report there IS a real backtest of that exact window already
(nothing to assume or filter down) - copied in instead of spending an AlgoTest
replay re-producing the identical thing. Confirmed live: a regime-pinned replay of
such a combo comes back byte-for-byte identical to its regular report. A combo
whose row was recorded under a DIFFERENT window still gets a genuine fresh replay,
pinned to the requested dates - the original assumption-avoidance this module
exists for is untouched for that case."""
from __future__ import annotations

import csv
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from src import browser
from src.config import load_selectors
from src.correlate import parse_trade_report, trade_report_path
from src.runner import AccountConfig, parse_dte_variant_suffix
from src.web import time_buckets
from src.web.combo_launcher import row_to_combo
from src.web.correlate_state import REPORTS_DIR, SELECTORS_PATH, label_for, row_dte_values
from src.web.expand import load_ui_config
from src.web.narrow import combined_sort_key, profitability_gated_key
from src.web.portfolio import BUCKET_ORDER, classify_bucket, has_hard_stop_loss
from src.web.portfolio_sweep_state import PortfolioSweepState

REGIME_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "output" / "trade_reports_regime"


def shortlist_for_download(
    rows: list[dict[str, Any]],
    top_n_per_bucket: int,
    bucket_order: list[str] | None = None,
    classify_fn: Callable[[dict[str, Any]], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Which rows to download a regime report FOR - one fair top_n_per_bucket slice
    from EACH session, instead of one flat "best N overall" sort. A flat sort lets
    whichever session scores best historically crowd out the other three almost
    entirely (confirmed live: a real download this way left 3 of 4 buckets too thin
    to build a basket from at all) - exactly the failure mode build_portfolio's own
    per-bucket _shortlist (src/web/portfolio.py) already exists to avoid for
    basket-building itself. This is the same idea applied one step earlier, at
    download time.

    `bucket_order`/`classify_fn` default to the regular 4 day-of-time sessions
    (BUCKET_ORDER/classify_bucket). CAS regime analysis passes cas_slot_buckets(...)
    instead: every CAS candidate's entry_time falls in the same narrow window (e.g.
    15:14-15:30), so classify_bucket would put ALL of them in the single
    "afternoon" bucket - the "fair per-session slice" this function exists to
    provide would then be a no-op, downloading only the historically-best-ranked
    handful of near-identical entry/exit times instead of spreading the download
    across the window (confirmed live: this was silently happening the whole time
    CAS regime downloads used the default buckets).

    Deliberately duplicated here rather than folded into _shortlist or added to
    portfolio.py itself: _shortlist requires a report to already be on disk (it
    picks candidates for an already-downloaded basket), which is exactly backwards
    for choosing what to download in the first place - and keeping this pathway's
    only new logic entirely out of portfolio.py means the regular, stable Portfolio
    sweep's own file is never touched by anything CAS-related."""
    order = bucket_order or BUCKET_ORDER
    classify = classify_fn or classify_bucket
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in order}
    for row in rows:
        b = classify(row)
        if b in grouped:
            grouped[b].append(row)

    picked: list[dict[str, Any]] = []
    for name in order:
        eligible = [r for r in grouped[name] if has_hard_stop_loss(r)]
        gated = profitability_gated_key(combined_sort_key(eligible))
        eligible.sort(key=gated, reverse=True)
        picked.extend(eligible[:top_n_per_bucket])
    return picked


def cas_slot_buckets(entry_time_from: str, entry_time_to: str, slice_minutes: int) -> tuple[list[str], Callable[[dict[str, Any]], str | None]]:
    """Bucket definition for CAS regime diversification: instead of the regular
    day's short/long-morning/midday/afternoon sessions - meaningless once every
    candidate's entry_time falls in the same narrow CAS window, since they'd all
    classify into a single "afternoon" bucket - slice [entry_time_from,
    entry_time_to] into `slice_minutes`-wide bands and treat each band as its own
    session, reusing the SAME fixed-width grid src/web/time_buckets.py already
    uses for the results table's entry-time bucket filter (anchored to market open
    09:15, not midnight) so a row's classify() output always lands on a label this
    function actually generated - a separately-anchored grid here could silently
    produce a row whose slot never matches any bucket_order entry.

    Returned bucket_order is chronological and scoped tightly to the requested
    window (not every slot from market open to close) - build_portfolio's
    per-bucket loops only ever see the sessions that are actually relevant here."""
    aligned_start = time_buckets.bucket_label(entry_time_from, slice_minutes) or entry_time_from
    bucket_order = time_buckets.all_slot_labels(slice_minutes, start=aligned_start, end=entry_time_to)

    def classify(row: dict[str, Any]) -> str | None:
        return time_buckets.bucket_label(row.get("entry_time") or "", slice_minutes)

    return bucket_order, classify


def regime_window_dir(date_from: str, date_to: str) -> Path:
    """One dedicated subfolder per distinct [date_from, date_to] window - makes
    staleness impossible by construction (a different window can never collide with
    or silently overwrite another's data) and makes a report already sitting here
    safe to reuse as-is within the SAME window (same window -> same real backtest,
    unlike REPORTS_DIR's cache, which ages against a rolling "most recent expiry")."""
    return REGIME_REPORTS_DIR / f"{date_from}_to_{date_to}"


_WINDOW_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})$")


def list_downloaded_regime_windows() -> list[tuple[str, str, int]]:
    """Every distinct [date_from, date_to] window that already has at least one
    downloaded report on disk - (date_from, date_to, report_count), sorted by
    date_from then date_to. A basket/sweep request for a window that isn't an
    EXACT match against one of these fails with "no reports downloaded yet" (see
    /api/regime/basket, /api/regime/sweep/start) - without this, a date_to off by
    even a day or two from what was actually downloaded looks IDENTICAL to
    nothing ever having been downloaded at all, which is exactly what left a user
    guessing random date ranges and hitting the same wall repeatedly. Only
    windows with at least one real report count - an empty leftover folder from
    an interrupted/failed download isn't "available.\""""
    if not REGIME_REPORTS_DIR.exists():
        return []
    windows: list[tuple[str, str, int]] = []
    for child in REGIME_REPORTS_DIR.iterdir():
        if not child.is_dir():
            continue
        m = _WINDOW_DIR_RE.match(child.name)
        if not m:
            continue
        count = sum(1 for _ in child.rglob("*.csv"))
        if count == 0:
            continue
        windows.append((m.group(1), m.group(2), count))
    windows.sort()
    return windows


def _reuse_regular_report_if_same_window(
    row: dict[str, Any], instrument: str, cid: str, date_from: str, date_to: str, window_dir: Path
) -> bool:
    """True if a combo's regular auto-downloaded report (output/trade_reports/)
    was copied into `window_dir` in place of a fresh AlgoTest replay - only when
    the row's OWN recorded start_date/end_date are an EXACT match for the
    requested regime window. That's not "probably close enough": it's the literal
    same backtest configuration, so the regular report already on disk is exactly
    what a regime-pinned replay would produce (confirmed live - byte-for-byte
    identical). A row recorded under any other window returns False untouched -
    the caller then falls back to a genuine fresh pinned replay, same as before
    this reuse path existed."""
    if row.get("start_date") != date_from or row.get("end_date") != date_to:
        return False
    regular_path = trade_report_path(REPORTS_DIR, instrument, cid)
    if not regular_path.exists():
        return False
    target = trade_report_path(window_dir, instrument, cid)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(regular_path, target)
    return True


def _slice_regular_report_into_window(
    row: dict[str, Any], instrument: str, cid: str, date_from: str, date_to: str, window_dir: Path
) -> bool:
    """The wider fast path _reuse_regular_report_if_same_window's exact-match
    check misses: when the combo's regular report's OWN recorded window fully
    CONTAINS [date_from, date_to] (not just equals it), slice its already-
    downloaded daily P/L down to just that sub-range and write that as this
    combo's regime-window report - no AlgoTest replay needed. This is exactly
    the math this session already empirically proved gives identical numbers to
    a real pinned replay (1,199/1,199 NIFTY and 2,098/2,099 SENSEX combos
    checked, exact match) - see portfolio.py's own _recompute_stats_for_window,
    which does the identical slice-and-recompute for Portfolio's "Only since/
    until" fields; this is that same idea, one step earlier, for CAS.

    Only the reconstructed date -> P/L view matters downstream - parse_trade_
    report is the only thing anything in this app ever reads a report file's
    contents through (confirmed: nothing reads individual leg/child rows) - so
    the file written here is a minimal, synthetic reconstruction (one parent
    row per date) rather than a byte-for-byte copy, and parse_trade_report reads
    it back identically to how it would read a genuine replay's own file.

    Returns False (nothing written) when the row has no recorded window, the
    window doesn't fully cover the request, the regular report doesn't exist, or
    the regular report happens to have no trade-dates actually inside the
    requested sub-range despite claiming to cover it - the caller then falls
    back to a genuine fresh pinned replay in every one of those cases, same as
    before this fast path existed."""
    recorded_start, recorded_end = row.get("start_date"), row.get("end_date")
    if not recorded_start or not recorded_end:
        return False
    if recorded_start > date_from or recorded_end < date_to:
        return False

    regular_path = trade_report_path(REPORTS_DIR, instrument, cid)
    if not regular_path.exists():
        return False

    full_series = parse_trade_report(regular_path)
    windowed = {d: pnl for d, pnl in full_series.items() if date_from <= d <= date_to}
    if not windowed:
        return False

    target = trade_report_path(window_dir, instrument, cid)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Index", "Entry Date", "P/L"])
        for i, d in enumerate(sorted(windowed)):
            writer.writerow([str(i), d, windowed[d]])
    return True


@dataclass
class RegimeDownloadState:
    """Background job: replay+download each of a set of rows' per-trade report with
    start_date/end_date PINNED to an explicit [date_from, date_to] - not each
    combo's originally-discovered window, and not CorrelateState's "same length,
    rolled to today" - a genuinely different, shorter-period backtest. Mirrors
    CorrelateState's status/thread/stop_event shape, but simpler: there's no CSV to
    merge results back into (update_results is always False here - this pathway
    never mutates the stable results CSV), and no age-based cache-skip (see
    regime_window_dir - within one window, "already on disk" is always safe to
    reuse; force=True still allows an explicit redo)."""

    status: str = "idle"  # idle | running | stopping | done | stopped | error
    total: int = 0
    downloaded: int = 0  # includes cache hits - both mean "the file is on disk now"
    failed: int = 0
    in_progress: int = 0
    in_progress_labels: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None
    started_at: str | None = None
    date_from: str | None = None
    date_to: str | None = None

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
                "date_from": self.date_from,
                "date_to": self.date_to,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status in ("running", "stopping")

    def start(self, rows: list[dict[str, Any]], *, date_from: str, date_to: str, force: bool = False) -> None:
        if self.is_running():
            raise RuntimeError("A regime report download is already in progress.")
        if not rows:
            raise ValueError("No rows to download - adjust the filters/Top N or run a sweep first.")
        if not date_from:
            raise ValueError("A start date is required.")
        if date_to < date_from:
            raise ValueError("End date can't be before start date.")

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
            self.date_from = date_from
            self.date_to = date_to
            self._stop_event = threading.Event()

        stop_event = self._stop_event
        thread = threading.Thread(target=self._run, args=(rows, date_from, date_to, stop_event, force), daemon=True)
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
        self, rows: list[dict[str, Any]], date_from: str, date_to: str, stop_event: threading.Event, force: bool
    ) -> None:
        from src.correlate import run_correlate_multiprocess

        load_dotenv()
        try:
            cfg = load_ui_config()
            selectors = load_selectors(SELECTORS_PATH)
            window_dir = regime_window_dir(date_from, date_to)
            window_dir.mkdir(parents=True, exist_ok=True)
            row_by_id = {r.get("combo_id"): r for r in rows}

            # Grouped by the DTE(s) each combo must actually be replayed under -
            # a "_dte{N}" composite id (see runner.parse_dte_variant_suffix) MUST
            # be isolated to just [N], never the config's full multi-select
            # dte_values, or the report it produces silently mixes in other
            # DTEs' trades and no longer matches what its own id promises
            # (confirmed live - see parse_dte_variant_suffix's docstring). A bare
            # combo_id uses ITS OWN recorded dte column (see correlate_state's
            # row_dte_values) - NOT the currently-saved config's dte_values,
            # which can have since changed by the time someone runs a regime
            # replay (the identical bug CorrelateState's Force re-download had,
            # fixed there first - see row_dte_values's own docstring).
            groups: dict[tuple[int, ...], list[tuple[dict[str, Any], str, str]]] = {}
            already_cached = 0
            for row in rows:
                cid = row.get("combo_id")
                instrument = row.get("instrument", "")
                if not cid:
                    continue
                target = trade_report_path(window_dir, instrument, cid)
                if target.exists() and not force:
                    already_cached += 1
                    continue
                # force=True means "genuinely redo this replay" - the reuse fast
                # path is skipped so it never stands in for the fresh replay the
                # user explicitly asked for (same convention as the exists-check
                # just above).
                if not force and _reuse_regular_report_if_same_window(row, instrument, cid, date_from, date_to, window_dir):
                    already_cached += 1
                    continue
                # Wider fast path than the exact-match check just above: the
                # regular report doesn't have to equal the requested window, only
                # to fully CONTAIN it - see _slice_regular_report_into_window's
                # own docstring for why slicing gives identical numbers to a real
                # replay, empirically proven this session.
                if not force and _slice_regular_report_into_window(row, instrument, cid, date_from, date_to, window_dir):
                    already_cached += 1
                    continue
                combo = row_to_combo(row)
                # Pinned to the requested regime window explicitly - NOT
                # roll_date_window's "same length, rolled to today" (CorrelateState's
                # Force re-download) - that preserves the ORIGINAL discovery window's
                # length, which is exactly what this exists to replace with a real,
                # independently-configured shorter period.
                combo["start_date"] = date_from
                combo["end_date"] = date_to
                variant_dte = parse_dte_variant_suffix(cid)
                key = (variant_dte,) if variant_dte is not None else tuple(row_dte_values(row, cfg.dte_values or []))
                groups.setdefault(key, []).append((combo, cid, instrument))

            with self._lock:
                self.downloaded = already_cached

            accounts = None
            if cfg.parallelism_account2 > 0 or cfg.parallelism_account3 > 0:
                # Same account-splitting convention as state.py/correlate_state.py -
                # one shared queue regardless of how many accounts, so no combo ever
                # replays on more than one account.
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
            # Progress is accumulated across groups (done_downloaded/done_failed)
            # so the UI's running total only ever climbs, never resets partway
            # through when a later group's own callback starts back at 0.
            done_downloaded = 0
            done_failed = 0
            done_failures: list[dict[str, Any]] = []
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
                    window_dir,
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
                    update_results=False,  # never mutate the stable results CSV from this pathway
                    accounts=accounts,
                    on_progress=_progress,
                    stop_event=stop_event,
                )
                done_downloaded += result.get("downloaded", 0)
                done_failed += result.get("failed", 0)
                done_failures += result.get("failures", [])

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


regime_download_state = RegimeDownloadState()

# A second, fully independent instance of the SAME sweep-job class the regular
# Portfolio "Parameter sweep" uses (src/web/portfolio_sweep_state.py) - not the
# shared singleton. Reusing the class means zero new sweep/resume/grid logic;
# instantiating separately means this job's is_running()/results/resume-dedup state
# never cross-contaminates with a regular Portfolio sweep - critical, since a stray
# shared "already done" grid-point match between a regime run and a stable run would
# silently serve one window's numbers as if they were the other's.
regime_sweep_state = PortfolioSweepState()
