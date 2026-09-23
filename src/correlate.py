"""Correlate top N: replay+download each combo's per-trade report from AlgoTest
(cached by combo_id so repeat runs are cheap), then compute real pairwise correlation
between strategies from their aligned per-trade P&L series - something the aggregate
metrics CSV can't give, since two combos can have identical Return/MaxDD while moving
in lockstep on every single trading day.

Mirrors src/runner.py's queue/worker-pool pattern (run_sweep_multiprocess) but is
simpler in one respect: there's nothing to merge into a shared CSV - each worker just
writes its own downloaded file straight to its final cache path, and progress is
reported back through a small results queue instead.
"""
from __future__ import annotations

import csv
import multiprocessing
import re
import statistics
import time
import threading
from pathlib import Path
from queue import Empty, Full
from typing import Any, Callable

from playwright.sync_api import Page

from src.auth import LoginNotConfigured, ensure_logged_in, is_logged_in
from src.config import Selectors
from src.form import apply_combination
from src.results import (
    apply_result_settings,
    download_current_report,
    ensure_brokerage_rate,
    scrape_metrics,
    wait_for_result,
)

# Pairs with fewer overlapping trade-dates than this are reported as "insufficient
# data" (None) rather than a correlation computed from too few points to mean anything.
MIN_OVERLAP_DAYS = 5
DEFAULT_CORR_THRESHOLD = 0.5


def instrument_slug(instrument: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (instrument or "unknown").lower()).strip("_")
    return slug or "unknown"


def trade_report_path(reports_dir: Path, instrument: str, cid: str) -> Path:
    return reports_dir / instrument_slug(instrument) / f"{cid}.csv"


def parse_trade_report(path: Path) -> dict[str, float]:
    """Entry Date -> P/L, parent rows only (Index without a "." - AlgoTest's download
    nests each trade's individual legs under it as "1.1", "1.2", ... - the parent row
    "1" already holds that trade's combined P/L, so summing children too would double
    count). Same date appearing twice (shouldn't normally happen for one combo) sums."""
    pnl_by_date: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if "." in (row.get("Index") or ""):
                continue
            date = row.get("Entry Date")
            raw = row.get("P/L")
            if not date or raw in (None, ""):
                continue
            try:
                pnl = float(raw)
            except ValueError:
                continue
            pnl_by_date[date] = pnl_by_date.get(date, 0.0) + pnl
    return pnl_by_date


def correlation_matrix(
    series: dict[str, dict[str, float]], *, min_overlap: int = MIN_OVERLAP_DAYS
) -> dict[str, dict[str, float | None]]:
    """Pairwise correlation of each pair's *overlapping* trade-dates only. A pair
    sharing fewer than `min_overlap` dates gets None ("insufficient data") instead of
    a number computed from too few points to be meaningful - callers must not treat
    None as "uncorrelated", only as "unknown".

    ONE exception to "too few points -> unknown": if the two series are byte-
    identical (same trade-dates AND same P&L on every one of them), that's not
    "not enough data to tell" - it IS the answer, regardless of how few points
    there are. Confirmed live: a short backtest window (a handful of trading
    days) routinely produces several near-duplicate combos - identical entry/
    exit times, identical total_pnl/max_drawdown/reward_risk_ratio to the
    rupee - because whatever parameter actually differs between them (a leg SL%,
    a momentum threshold, a re-entry setting, ...) never once fires against that
    window's real price action, so every trade plays out exactly the same either
    way. With so few trading days, their overlap almost always falls below
    min_overlap, so without this check they'd get "unknown" instead of the
    obviously-correct "identical" - and pick_diversified_basket treats unknown
    as "keep it", letting every near-duplicate straight into the same basket.
    Guarded on a non-empty series so two candidates that both simply have ZERO
    trades in this window aren't misread as "the same strategy" - that's
    genuinely unknown, not identical."""
    ids = list(series)
    matrix: dict[str, dict[str, float | None]] = {i: {} for i in ids}
    for a_idx, a in enumerate(ids):
        for b in ids[a_idx:]:
            if a == b:
                matrix[a][b] = 1.0
                continue
            common = sorted(set(series[a]) & set(series[b]))
            corr: float | None = None
            if len(common) >= min_overlap:
                xs = [series[a][d] for d in common]
                ys = [series[b][d] for d in common]
                try:
                    corr = statistics.correlation(xs, ys)
                except statistics.StatisticsError:
                    corr = None  # e.g. one series is constant over the overlap
            elif series[a] and series[a] == series[b]:
                corr = 1.0
            matrix[a][b] = corr
            matrix[b][a] = corr
    return matrix


def pick_diversified_basket(
    ranked_ids: list[str],
    matrix: dict[str, dict[str, float | None]],
    *,
    threshold: float = DEFAULT_CORR_THRESHOLD,
    same_window: Callable[[str, str], bool] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedy diversification: walk `ranked_ids` (already best-first), keep a combo
    only if its correlation to every combo already in the basket stays below
    `threshold`. "Insufficient data" (None) pairs never block inclusion - unknown
    isn't evidence of correlation, so being conservative here means erring toward
    keeping a candidate, not excluding it.

    ONE exception: `same_window(a, b)` - True when two combos share the exact
    same (entry_time, exit_time). Confirmed live: a short backtest window (few
    trading days) makes real correlation "unknown" for almost every pair in a
    narrow-session bucket (e.g. a 14:55-15:38 afternoon slice with only 4
    overlapping days, one short of MIN_OVERLAP_DAYS) - "unknown, so keep it"
    then lets the SAME clock-time window get picked over and over, which isn't
    genuine diversification even though no individual pair is PROVEN
    correlated. Two candidates trading the identical entry/exit window are a
    much stronger prior of redundancy than "insufficient overlap" alone - when
    correlation itself can't settle it, same_window does: treated as maximally
    correlated (1.0) rather than falling back to "unknown, keep it". Left
    unset (the default), behavior is unchanged from before this existed."""
    basket: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for cid in ranked_ids:
        worst: float | None = None
        worst_with: str | None = None
        for picked in basket:
            corr = matrix.get(cid, {}).get(picked["combo_id"])
            if corr is None and same_window is not None and same_window(cid, picked["combo_id"]):
                corr = 1.0
            if corr is not None and (worst is None or corr > worst):
                worst, worst_with = corr, picked["combo_id"]
        if worst is not None and worst >= threshold:
            skipped.append({"combo_id": cid, "reason": f"correlation {worst:.2f} with {worst_with}"})
            continue
        basket.append({"combo_id": cid, "max_corr_to_basket": worst})
    return basket, skipped


def _metrics_from_daily(daily: dict[str, float]) -> dict[str, Any]:
    """Overall Profit / Win % / Max Drawdown / Return-MaxDD / Reward-to-Risk from one
    already-combined date->P/L series - the shared core behind both
    compute_portfolio_metrics (equal-weighted) and compute_portfolio_metrics_weighted
    (lot-scaled) below, so the drawdown-curve math only ever lives in one place."""
    if not daily:
        return {
            "num_periods": 0, "overall_profit": 0.0, "avg_profit_per_period": 0.0,
            "win_pct": 0.0, "loss_pct": 0.0, "avg_profit_winning": 0.0,
            "avg_loss_losing": 0.0, "max_profit_single_period": 0.0,
            "max_loss_single_period": 0.0, "max_drawdown": 0.0,
            "max_drawdown_start": None, "max_drawdown_end": None,
            "return_max_dd": None, "reward_risk_ratio": None,
        }

    dates = sorted(daily)
    values = [daily[d] for d in dates]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    overall_profit = sum(values)
    num_periods = len(values)

    equity = 0.0
    peak = 0.0
    peak_date = dates[0]
    max_dd = 0.0
    dd_start, dd_end = dates[0], dates[0]
    for d, v in zip(dates, values):
        equity += v
        if equity > peak:
            peak = equity
            peak_date = d
        drawdown = equity - peak
        if drawdown < max_dd:
            max_dd = drawdown
            dd_start, dd_end = peak_date, d

    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0

    return {
        "num_periods": num_periods,
        "overall_profit": overall_profit,
        "avg_profit_per_period": overall_profit / num_periods,
        "win_pct": 100.0 * len(wins) / num_periods,
        "loss_pct": 100.0 * len(losses) / num_periods,
        "avg_profit_winning": avg_win,
        "avg_loss_losing": avg_loss,
        "max_profit_single_period": max(values),
        "max_loss_single_period": min(values),
        "max_drawdown": max_dd,
        "max_drawdown_start": dd_start,
        "max_drawdown_end": dd_end,
        "return_max_dd": (overall_profit / abs(max_dd)) if max_dd != 0 else None,
        "reward_risk_ratio": (avg_win / abs(avg_loss)) if avg_loss != 0 else None,
    }


def compute_portfolio_metrics(cids: list[str], series: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Combined multi-strategy performance for a basket, exactly like AlgoTest's own
    "run these together" aggregate report - each combo's daily P/L summed date-by-date
    first, then Overall Profit / Win % / Max Drawdown / Return-MaxDD / Reward-to-Risk
    computed on that ONE combined series. This is not an average of the basket
    members' own individual scores (those measure each strategy alone, at its own
    entry/exit times, and stay high even when the strategies overlap heavily) - a
    basket can look great member-by-member yet be a weak combined portfolio if their
    bad days keep landing on the same dates, which is exactly what this exists to
    surface."""
    daily: dict[str, float] = {}
    for cid in cids:
        for date, pnl in series.get(cid, {}).items():
            daily[date] = daily.get(date, 0.0) + pnl
    return _metrics_from_daily(daily)


def compute_portfolio_metrics_weighted(
    lots_by_cid: dict[str, float], series: dict[str, dict[str, float]], *, base_lots: float = 10.0
) -> dict[str, Any]:
    """Same combined-portfolio treatment as compute_portfolio_metrics, but each
    combo's daily P/L is scaled by its own recommended lot size first (every
    downloaded trade report was backtested at `base_lots`, so a combo sized at half
    that contributes half its recorded P/L on each date) - for a basket where members
    deliberately don't all carry the same size, e.g. a bigger-drawdown strategy sized
    down to keep the combined portfolio's risk in check.

    Does NOT account for brokerage/taxes (see compute_portfolio_metrics_weighted_
    with_charges for that) - kept around for callers that don't have per-combo
    charges available (e.g. older rows recorded before brokerage_amount/
    taxes_charges_amount were tracked)."""
    daily: dict[str, float] = {}
    for cid, lots in lots_by_cid.items():
        scale = lots / base_lots
        for date, pnl in series.get(cid, {}).items():
            daily[date] = daily.get(date, 0.0) + pnl * scale
    return _metrics_from_daily(daily)


def compute_portfolio_metrics_weighted_with_charges(
    lots_by_cid: dict[str, float],
    series: dict[str, dict[str, float]],
    charges_by_cid: dict[str, dict[str, float | None]],
    *,
    base_lots: float = 10.0,
) -> dict[str, Any]:
    """Same as compute_portfolio_metrics_weighted, but also deducts each combo's own
    recorded brokerage and taxes & charges from its daily P/L before combining - a
    downloaded trade report's raw per-trade P/L reflects NEITHER (confirmed live:
    AlgoTest's "Include Brokerage"/"Taxes & charges" toggles and their computed
    amounts sit in the results settings panel, not in the downloadable report),
    which otherwise makes a recommended basket look more profitable - and its
    Reward:Risk ratio higher - than it would actually be. Validated against
    AlgoTest's own portfolio aggregate: applying this closed a 4.60-vs-3.57
    Reward:Risk gap down to 3.56, and Overall Profit to within ₹0.42 on a ~₹20L
    total.

    Brokerage is a flat per-order fee - unaffected by lot size, so it's NOT scaled.
    Taxes & charges scale with traded value (lot size), so they ARE scaled by the
    same lots/base_lots ratio as the P/L itself. Both were recorded at `base_lots`
    (same basis the raw per-trade series was backtested at), and are distributed
    evenly across however many periods that combo actually has - an approximation
    (brokerage is really per-order and taxes per-trade-value, not perfectly uniform
    per day), but far closer than not accounting for either at all.

    charges_by_cid: {combo_id: {"brokerage": float | None, "taxes_charges": float | None}}.
    A combo missing either value (older rows, before this was tracked) gets no
    charge adjustment at all rather than a partial/guessed one."""
    daily: dict[str, float] = {}
    for cid, lots in lots_by_cid.items():
        scale = lots / base_lots
        combo_series = series.get(cid, {})
        n_periods = len(combo_series)
        charges = charges_by_cid.get(cid) or {}
        brokerage = charges.get("brokerage")
        taxes = charges.get("taxes_charges")
        per_day_deduction = 0.0
        if brokerage is not None and taxes is not None and n_periods:
            per_day_deduction = (brokerage + taxes * scale) / n_periods
        for date, pnl in combo_series.items():
            daily[date] = daily.get(date, 0.0) + pnl * scale - per_day_deduction
    return _metrics_from_daily(daily)


def _download_one_combo(
    page: Page,
    combo: dict[str, Any],
    cid: str,
    instrument: str,
    selectors: Selectors,
    reports_dir: Path,
    *,
    result_timeout_s: int,
    max_retries: int,
    email: str | None,
    password: str | None,
    slippage_pct: float,
    dte_values: list[int],
    brokerage_rate: float | None,
    brokerage_configured: list[bool],
    update_results: bool = False,
) -> dict[str, Any]:
    """`update_results=True` ("Force re-download & update results" in the UI) skips
    the cached-file shortcut below (always replays, even for a combo that already
    has a report on disk) and, once the result is up on screen, also re-scrapes
    every metric - see the "row" key on the returned dict, merged back into this
    combo's original results row by the caller. Without it this is the original,
    cheaper "just get me the trade report" behavior Correlate itself uses."""
    target = trade_report_path(reports_dir, instrument, cid)
    if not update_results and target.exists():
        return {"combo_id": cid, "status": "cached"}

    attempt = 0
    backoff_s = 1.0
    while True:
        attempt += 1
        try:
            if not is_logged_in(page, selectors):
                ensure_logged_in(page, selectors, email, password)

            apply_combination(page, selectors, combo)
            outcome = wait_for_result(page, selectors, timeout_s=result_timeout_s)
            if outcome.status != "ok":
                raise RuntimeError(f"{outcome.status}: {outcome.error}")

            # Same post-result settings the main sweep applies before scraping metrics
            # - the downloaded per-trade P&L must reflect the same brokerage/slippage/
            # DTE filter as whatever ranked this combo into the top N, or correlating
            # against those metrics wouldn't be comparing like with like.
            if brokerage_rate is not None and not brokerage_configured[0]:
                ensure_brokerage_rate(page, selectors, brokerage_rate)
                brokerage_configured[0] = True
            apply_result_settings(page, selectors, slippage_pct, dte_values)

            result: dict[str, Any] = {"combo_id": cid, "status": "downloaded"}
            if update_results:
                # The result is already sitting on screen with brokerage/slippage/DTE
                # applied - re-scraping here is nearly free, and is what lets one
                # replay both refresh the trade report AND backfill this combo's
                # stored row (brokerage_amount/taxes_charges_amount included) instead
                # of needing a second pass. Local import: runner.py imports
                # trade_report_path from this module, so importing runner at module
                # level here would be circular.
                from src.runner import _build_row

                raw_metrics = scrape_metrics(page, selectors)
                result["row"] = _build_row(combo, cid, "ok", None, raw_metrics, dte_values)

            download_current_report(page, selectors, target, cid)
            return result

        except LoginNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad combo must not crash the job
            if attempt <= max_retries:
                time.sleep(backoff_s)
                backoff_s *= 2
                continue
            return {"combo_id": cid, "status": "failed", "error": str(exc)}


def _download_from_queue(
    page: Page,
    queue: "multiprocessing.Queue[tuple[dict[str, Any], str, str] | None]",
    selectors: Selectors,
    reports_dir: Path,
    progress_queue: "multiprocessing.Queue[dict[str, Any]]",
    *,
    delay_s: float,
    result_timeout_s: int,
    max_retries: int,
    email: str | None,
    password: str | None,
    slippage_pct: float,
    dte_values: list[int] | None,
    brokerage_rate: float | None,
    update_results: bool = False,
    stop_event: "multiprocessing.synchronize.Event | None" = None,
) -> None:
    """Same shared-queue/graceful-stop shape as runner._run_from_queue: stop_event is
    checked only between items, never mid-download, so an in-flight download always
    finishes and gets recorded rather than being killed mid-write."""
    brokerage_configured = [False]
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            item = queue.get(timeout=1)
        except Empty:
            continue
        if item is None:
            break
        combo, cid, instrument = item
        # Reported before the (possibly long) replay starts, not after - this is what
        # lets the UI show which specific combo(s) are in flight right now, not just a
        # count.
        progress_queue.put({"combo_id": cid, "status": "started"})
        result = _download_one_combo(
            page,
            combo,
            cid,
            instrument,
            selectors,
            reports_dir,
            result_timeout_s=result_timeout_s,
            max_retries=max_retries,
            email=email,
            password=password,
            slippage_pct=slippage_pct,
            dte_values=dte_values or [],
            brokerage_rate=brokerage_rate,
            brokerage_configured=brokerage_configured,
            update_results=update_results,
        )
        progress_queue.put(result)
        time.sleep(delay_s)


def _download_worker_main(
    worker_index: int,
    queue: "multiprocessing.Queue[tuple[dict[str, Any], str, str] | None]",
    selectors: Selectors,
    primary_profile_dir: Path,
    reports_dir: Path,
    progress_queue: "multiprocessing.Queue[dict[str, Any]]",
    *,
    headless: bool,
    delay_s: float,
    result_timeout_s: int,
    max_retries: int,
    email: str | None,
    password: str | None,
    slippage_pct: float,
    dte_values: list[int] | None,
    brokerage_rate: float | None,
    update_results: bool = False,
    stop_event: "multiprocessing.synchronize.Event | None" = None,
) -> None:
    from src import browser  # re-imported in the spawned child process
    from src.auth import LoginNotConfigured
    from src.runner import _ensure_worker_profile

    profile_dir = _ensure_worker_profile(worker_index, primary_profile_dir)
    try:
        with browser.persistent_context(headless=headless, profile_dir=profile_dir) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(selectors.builder.url)
            _download_from_queue(
                page,
                queue,
                selectors,
                reports_dir,
                progress_queue,
                delay_s=delay_s,
                result_timeout_s=result_timeout_s,
                max_retries=max_retries,
                email=email,
                password=password,
                slippage_pct=slippage_pct,
                dte_values=dte_values,
                brokerage_rate=brokerage_rate,
                update_results=update_results,
                stop_event=stop_event,
            )
    except LoginNotConfigured as exc:
        progress_queue.put({"combo_id": None, "status": "login_error", "error": str(exc)})


def run_correlate_multiprocess(
    work_items: list[tuple[dict[str, Any], str, str]],
    selectors: Selectors,
    reports_dir: Path,
    *,
    parallelism: int,
    headless: bool = True,
    delay_s: float = 2.0,
    result_timeout_s: int = 180,
    max_retries: int = 2,
    email: str | None = None,
    password: str | None = None,
    slippage_pct: float = 1.0,
    dte_values: list[int] | None = None,
    brokerage_rate: float | None = None,
    update_results: bool = False,
    primary_profile_dir: Path | None = None,
    accounts: list[Any] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event: "threading.Event | None" = None,
    poll_interval_s: float = 3.0,
) -> dict[str, Any]:
    """Only `work_items` NOT already cached should be passed in (see
    correlate_state.py) - this function always replays/downloads everything it's
    given. `accounts` (a list of AccountConfig) spreads workers across more than one
    AlgoTest account exactly like runner.run_sweep_multiprocess.

    `update_results=True` ("Force re-download & update results") also re-scrapes
    each combo's metrics while it's replayed anyway, returned under the
    "updated_rows" key below - the caller (CorrelateState) merges each one back
    into its original source CSV via store.update_row."""
    from src.browser import PROFILE_DIR

    if accounts:
        effective_parallelism = len(accounts)
    else:
        effective_parallelism = parallelism
        primary_profile_dir = primary_profile_dir or PROFILE_DIR

    total = len(work_items)
    if total == 0:
        if on_progress is not None:
            on_progress({"downloaded": 0, "failed": 0, "in_progress": 0, "in_progress_ids": [], "failures": []})
        return {"downloaded": 0, "failed": 0, "failures": [], "updated_rows": []}

    num_workers = min(effective_parallelism, total)
    if accounts:
        accounts = accounts[:num_workers]

    queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=200)
    progress_queue: multiprocessing.Queue = multiprocessing.Queue()
    worker_stop_event = multiprocessing.Event()

    def _feed_queue() -> None:
        for item in work_items:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    queue.put(item, timeout=1)
                    break
                except Full:
                    continue
        for _ in range(num_workers):
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    queue.put(None, timeout=1)
                    break
                except Full:
                    continue

    threading.Thread(target=_feed_queue, daemon=True).start()

    def _worker_account(i: int) -> tuple[str | None, str | None, Path]:
        if accounts:
            acct = accounts[i]
            return acct.email, acct.password, acct.profile_dir
        return email, password, primary_profile_dir

    processes = []
    for i in range(num_workers):
        w_email, w_password, w_profile_dir = _worker_account(i)
        processes.append(
            multiprocessing.Process(
                target=_download_worker_main,
                args=(i, queue, selectors, w_profile_dir, reports_dir, progress_queue),
                kwargs=dict(
                    headless=headless,
                    delay_s=delay_s,
                    result_timeout_s=result_timeout_s,
                    max_retries=max_retries,
                    email=w_email,
                    password=w_password,
                    slippage_pct=slippage_pct,
                    dte_values=dte_values,
                    brokerage_rate=brokerage_rate,
                    update_results=update_results,
                    stop_event=worker_stop_event,
                ),
            )
        )
    for p in processes:
        p.start()

    downloaded = 0
    failed = 0
    failures: list[dict[str, Any]] = []
    updated_rows: list[dict[str, Any]] = []
    in_progress_ids: set[str] = set()
    login_error: str | None = None

    def _drain_progress() -> None:
        nonlocal downloaded, failed, login_error
        while True:
            try:
                result = progress_queue.get_nowait()
            except Empty:
                return
            status = result.get("status")
            cid = result.get("combo_id")
            if status == "started":
                if cid is not None:
                    in_progress_ids.add(cid)
                continue
            if cid is not None:
                in_progress_ids.discard(cid)
            if status in ("downloaded", "cached"):
                downloaded += 1
                if "row" in result:
                    updated_rows.append(result["row"])
            elif status == "failed":
                failed += 1
                failures.append(result)
            elif status == "login_error":
                login_error = result.get("error")

    def _report(in_progress: int) -> None:
        if on_progress is not None:
            on_progress(
                {
                    "downloaded": downloaded,
                    "failed": failed,
                    "in_progress": in_progress,
                    "in_progress_ids": sorted(in_progress_ids),
                    "failures": list(failures),
                }
            )

    drain_deadline: float | None = None
    grace_period_s = max(60.0, result_timeout_s * (max_retries + 1) + 60.0)

    try:
        while any(p.is_alive() for p in processes):
            alive = [p for p in processes if p.is_alive()]
            _drain_progress()
            if stop_event is not None and stop_event.is_set():
                if drain_deadline is None:
                    worker_stop_event.set()
                    drain_deadline = time.monotonic() + grace_period_s
                elif time.monotonic() > drain_deadline:
                    for p in alive:
                        p.terminate()
                    break
            _report(in_progress=len(alive))
            time.sleep(poll_interval_s)
    finally:
        for p in processes:
            p.join(timeout=30)
        for p in processes:
            if p.is_alive():
                p.kill()
                p.join(timeout=10)

    _drain_progress()
    _report(in_progress=0)

    if not (stop_event is not None and stop_event.is_set()) and login_error:
        raise LoginNotConfigured(login_error)

    return {"downloaded": downloaded, "failed": failed, "failures": failures, "updated_rows": updated_rows}
