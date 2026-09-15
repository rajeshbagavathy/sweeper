from __future__ import annotations

import json
import multiprocessing
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full
from typing import Any, Callable, Iterable

from playwright.sync_api import Page
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from src.auth import LoginNotConfigured, ensure_logged_in, is_logged_in
from src.config import Selectors
from src.correlate import trade_report_path
from src.form import apply_combination
from src.results import (
    apply_result_settings,
    download_current_report,
    ensure_brokerage_rate,
    parse_number,
    scrape_metrics,
    wait_for_result,
)
from src.store import append_row, combo_id, flatten, strategy_key

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
REPORTS_DIR = Path(__file__).resolve().parent.parent / "output" / "trade_reports"


def _combo_label(combo: dict[str, Any]) -> str:
    """Short human-readable stand-in for a combo_id hash - shown next to "N
    in-progress" while Stopping so it's WHICH combo(s), not just an opaque count
    that can look frozen for as long as the slowest one takes (see
    _run_from_queue's progress_queue "started" event). Same style as
    src.web.correlate_state.label_for, kept as its own copy here rather than an
    import - runner.py is the core sweep engine and doesn't otherwise depend on
    anything under src.web."""
    return f"{combo.get('instrument', '?')} {combo.get('entry_time', '?')}-{combo.get('exit_time', '?')}"


def _auto_download_eligible(row: dict[str, Any], *, min_return_max_dd: float, min_trades: int) -> bool:
    """Whether a just-completed combo's own scraped metrics qualify for an inline
    trade-report download - see SweepUIConfig.auto_download_* for the rationale.
    total_pnl > 0 is a hard, non-negotiable gate (never worth a report for a loser);
    Return/MaxDD - not reward:risk or win rate - is the quality bar on top of that,
    since those two were found to trade off against each other (high win-rate
    strategies cluster at low reward:risk and vice versa), so gating on either alone
    would systematically exclude a whole category of otherwise-good strategies."""
    pnl = row.get("total_pnl")
    rmdd = row.get("return_max_dd")
    if not isinstance(pnl, (int, float)) or not isinstance(rmdd, (int, float)):
        return False
    if pnl <= 0 or rmdd < min_return_max_dd:
        return False
    if min_trades > 0:
        trades = row.get("total_trades")
        if not isinstance(trades, (int, float)) or trades < min_trades:
            return False
    return True


def known_duplicate_combo_id(combo: dict[str, Any], known_strategy_keys: dict[str, str] | None) -> str | None:
    """The EXISTING combo_id this combo's underlying strategy already has a
    record under (same legs/times/stoploss/etc, just a different date range -
    see store.strategy_key), or None if it isn't a known duplicate. combo_id()
    hashes start_date/end_date too (confirmed live this session: an identical
    strategy differing only in end_date gets a completely different combo_id),
    so a sweep with a trailing end_date would otherwise re-discover the exact
    same strategies as brand new every time, fragmenting their history and
    wasting replay budget re-downloading what's already in the registry."""
    if not known_strategy_keys:
        return None
    return known_strategy_keys.get(strategy_key(combo))


def queue_duplicate_for_refresh(queue_path: Path, existing_combo_id: str, key: str) -> None:
    """Appends one entry to the pending-duplicate-refresh queue immediately (not
    buffered to sweep-end) - must survive Stop, so whatever's been captured so
    far is always immediately actionable via Force re-download, independent of
    whether the main sweep keeps running. Plain append, safe across concurrent
    worker processes (O_APPEND, same convention as correlate_state.py's
    refresh_audit.log)."""
    append_row(
        queue_path,
        ["combo_id", "strategy_key", "detected_at"],
        {
            "combo_id": existing_combo_id,
            "strategy_key": key,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _combo_already_done(
    cid: str, dte_values: list[int] | None, capture_dte_individually: bool, existing_statuses: dict[str, str]
) -> bool:
    """Whether this combo can be skipped as already-done on a Resume - aware of
    capture_dte_individually, since a combo captured that way never gets a row
    under its own bare combo_id at all (see _run_one_combo: the blended row is
    skipped entirely, not just supplemented, when the per-DTE split succeeds) -
    only its "_dteN" variant rows do. Checking just the bare id there would
    never find it "done" and re-run it on every single Resume, forever.

    Falls back to the ordinary bare-id check, completely unchanged, whenever
    there's nothing to split (a single DTE, or the flag off) - the only case
    this function's answer can ever differ from the old `existing_statuses.get
    (cid) == "ok"` check it replaces."""
    if capture_dte_individually and dte_values and len(dte_values) > 1:
        return all(existing_statuses.get(f"{cid}_dte{d}") == "ok" for d in dte_values)
    return existing_statuses.get(cid) == "ok"


def run_sweep(
    page: Page,
    combos: list[dict[str, Any]],
    selectors: Selectors,
    csv_path: Path,
    log_path: Path,
    fieldnames: list[str],
    existing_statuses: dict[str, str],
    *,
    delay_s: float = 2.0,
    result_timeout_s: int = 180,
    max_retries: int = 2,
    email: str | None = None,
    password: str | None = None,
    slippage_pct: float = 1.0,
    dte_values: list[int] | None = None,
    brokerage_rate: float | None = None,
    auto_download_enabled: bool = False,
    auto_download_min_return_max_dd: float = 1.5,
    auto_download_min_trades: int = 0,
    capture_dte_individually: bool = False,
    known_strategy_keys: dict[str, str] | None = None,
    duplicate_queue_path: Path | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event=None,
) -> dict[str, int]:
    """Sequential sweep over one page/session. This is the single source of truth for
    driving a backtest - run_sweep_multiprocess (for parallelism > 1) just calls this
    unchanged, once per worker process, each against its own combo share and browser
    profile, rather than reimplementing any of this logic a second time.

    `known_strategy_keys`/`duplicate_queue_path`: when a combo's underlying
    strategy (see store.strategy_key) already has a record in the registry
    under a DIFFERENT combo_id (a different date range - e.g. a trailing
    end_date sweep re-running), it is NOT replayed as a new discovery - it's
    captured into `duplicate_queue_path` instead, for Force re-download to pick
    up against its existing combo_id. Both None (the default) means every
    combo runs exactly as before this existed."""
    console = Console()
    stats = {"ok": 0, "error": 0, "skipped": 0, "duplicate": 0}
    # Set once, on whichever combo first reaches the results panel - never re-touched
    # after that (see ensure_brokerage_rate's docstring for why re-setting every combo
    # would be both wasted work and pointless, since the value already persists).
    brokerage_configured = [False]

    # in_progress/in_progress_labels default to "nothing in flight" - overridden only
    # by the explicit in-flight report fired right before _run_one_combo below, so a
    # concurrent /api/status read (this runs on a background thread - the web
    # server's own request handling isn't blocked by it) can show which combo Stop
    # is currently waiting on, instead of a bare, unmoving "Stopping...". Same shape
    # as run_sweep_multiprocess's own in_progress_labels, just always 0-or-1 here
    # (this path is strictly sequential - never more than one combo in flight).
    def _report(current_combo_index: int, *, in_progress: int = 0, in_progress_labels: list[str] | None = None) -> None:
        if on_progress is not None:
            on_progress(
                {
                    "current": current_combo_index,
                    "total": len(combos),
                    "in_progress": in_progress,
                    "in_progress_labels": in_progress_labels or [],
                    **stats,
                }
            )

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Sweeping (ok=0 error=0 skipped=0)", total=len(combos)
        )

        for i, combo in enumerate(combos):
            if stop_event is not None and stop_event.is_set():
                break

            cid = combo_id(combo)

            # Step 2: skip combos that already succeeded. Ones that previously errored
            # are retried on a plain re-run - max_retries governs retries *within* one
            # attempt, this is what lets a whole sweep eventually succeed across runs.
            if _combo_already_done(cid, dte_values, capture_dte_individually, existing_statuses):
                stats["skipped"] += 1
                progress.advance(task)
                progress.update(task, description=_status_line(stats))
                _report(i + 1)
                continue

            existing_cid = known_duplicate_combo_id(combo, known_strategy_keys)
            if existing_cid is not None:
                stats["duplicate"] += 1
                if duplicate_queue_path is not None:
                    queue_duplicate_for_refresh(duplicate_queue_path, existing_cid, strategy_key(combo))
                progress.advance(task)
                progress.update(task, description=_status_line(stats))
                _report(i + 1)
                continue

            # Reported BEFORE the (possibly long) replay starts, not just after - this
            # is what a concurrent /api/status poll sees while Stop is waiting on this
            # exact combo to finish, instead of stale pre-Stop numbers the whole time.
            _report(i, in_progress=1, in_progress_labels=[_combo_label(combo)])
            _run_one_combo(
                page,
                combo,
                cid,
                selectors,
                csv_path,
                log_path,
                fieldnames,
                stats,
                result_timeout_s=result_timeout_s,
                max_retries=max_retries,
                email=email,
                password=password,
                slippage_pct=slippage_pct,
                dte_values=dte_values or [],
                brokerage_rate=brokerage_rate,
                brokerage_configured=brokerage_configured,
                auto_download_enabled=auto_download_enabled,
                auto_download_min_return_max_dd=auto_download_min_return_max_dd,
                auto_download_min_trades=auto_download_min_trades,
                capture_dte_individually=capture_dte_individually,
            )

            progress.advance(task)
            progress.update(task, description=_status_line(stats))
            _report(i + 1)
            time.sleep(delay_s)

    return stats


def _status_line(stats: dict[str, int]) -> str:
    duplicate = stats.get("duplicate", 0)
    suffix = f" duplicate={duplicate}" if duplicate else ""
    return f"Sweeping (ok={stats['ok']} error={stats['error']} skipped={stats['skipped']}{suffix})"


def _run_one_combo(
    page: Page,
    combo: dict[str, Any],
    cid: str,
    selectors: Selectors,
    csv_path: Path,
    log_path: Path,
    fieldnames: list[str],
    stats: dict[str, int],
    *,
    result_timeout_s: int,
    max_retries: int,
    email: str | None,
    password: str | None,
    slippage_pct: float,
    dte_values: list[int],
    brokerage_rate: float | None = None,
    brokerage_configured: list[bool] | None = None,
    auto_download_enabled: bool = False,
    auto_download_min_return_max_dd: float = 1.5,
    auto_download_min_trades: int = 0,
    capture_dte_individually: bool = False,
) -> None:
    attempt = 0
    backoff_s = 1.0

    while True:
        attempt += 1
        try:
            # Step 3: ensure session alive, re-login if not.
            if not is_logged_in(page, selectors):
                ensure_logged_in(page, selectors, email, password)

            # Steps 4-6: fresh state, apply params, click run.
            apply_combination(page, selectors, combo)

            # Step 7: wait properly, racing error_marker.
            outcome = wait_for_result(page, selectors, timeout_s=result_timeout_s)
            if outcome.status != "ok":
                raise RuntimeError(f"{outcome.status}: {outcome.error}")

            # Brokerage/taxes/slippage + DTE filter change the scraped numbers, so
            # they must be applied before step 8, not after.
            if brokerage_rate is not None and brokerage_configured is not None and not brokerage_configured[0]:
                ensure_brokerage_rate(page, selectors, brokerage_rate)
                brokerage_configured[0] = True
            apply_result_settings(page, selectors, slippage_pct, dte_values)

            # Step 8: scrape + parse.
            raw_metrics = scrape_metrics(page, selectors)
            row = _build_row(combo, cid, "ok", None, raw_metrics, dte_values)

            _log(log_path, cid, "ok", None, attempt)
            stats["ok"] += 1

            # capture_dte_individually=True with only one DTE selected is exactly the
            # combined case (nothing to split apart), so it's not worth the extra
            # scrape - only branch into it when there's actually more than one DTE to
            # tell apart.
            #
            # Deliberately NOT gated on auto_download_enabled/_auto_download_eligible
            # the way the single combined-report download below still is - splitting
            # is just a re-filter + re-scrape of the SAME already-completed backtest
            # (no new "Start Backtest"), so it's cheap regardless of how the BLENDED
            # row looks. Gating it on the blended row's own eligibility (the original
            # shape of this code) silently hid the exact thing this feature exists to
            # surface: a combo whose blended Return/MaxDD misses the bar can easily
            # have one genuinely excellent single DTE diluted by a mediocre other one
            # - that combo would never get split, so its good DTE's story never made
            # it into a row at all. Confirmed live: a real sweep with this box checked
            # still wrote combined "0,1" rows for ~94% of its combos, and only the
            # ~6% whose BLENDED number already looked good ever got split - exactly
            # backwards from "capture individual DTE reports" for combos where it
            # matters most. See _auto_download_eligible's own docstring for the same
            # "gating on one number alone systematically excludes a whole category of
            # good strategies" principle, now applied per-DTE instead of per-metric.
            #
            # The blended row is genuinely never written here, not just supplemented -
            # "instead of one combined" (the checkbox's own label), not "in addition
            # to". Confirmed live this was STILL a real bug even after the fix above:
            # a real sweep with the box checked wrote BOTH the blended "0,1,2" row AND
            # all three per-DTE rows for every single combo (1,704 combos x 4 rows =
            # 6,816), silently contradicting exactly what the checkbox promises. Falls
            # back to writing the combined row anyway only if EVERY per-DTE attempt
            # failed (written_dtes empty) - a combo's own successful backtest must
            # never end up with zero rows recorded just because the re-filter/scrape
            # step happened to fail for every DTE (see known_duplicate_combo_id /
            # existing_statuses' skip-check, which now looks for these "_dteN" rows
            # specifically instead of the bare combo_id in this exact case - a combo
            # that never gets a bare-id row here must still be recognized as done on
            # a later Resume, not re-run forever).
            if capture_dte_individually and len(dte_values) > 1:
                written_dtes: list[int] = []
                try:
                    written_dtes = _capture_individual_dte_reports(
                        page, selectors, combo, cid, csv_path, fieldnames, slippage_pct, dte_values,
                        auto_download_enabled=auto_download_enabled,
                        auto_download_min_return_max_dd=auto_download_min_return_max_dd,
                        auto_download_min_trades=auto_download_min_trades,
                    )
                except Exception:  # noqa: BLE001 - best-effort, never fails the combo
                    pass
                if not written_dtes:
                    append_row(csv_path, fieldnames, row)
            else:
                # Step 9: append immediately.
                append_row(csv_path, fieldnames, row)
                # Optional: download this combo's trade report right now, while the
                # page already has the result on screen - if it turns out good,
                # that's the exact same report Correlate would otherwise pay a full
                # second replay for later. Best-effort only: a download failure here
                # must never turn an otherwise-successful combo into an error, so
                # it's swallowed, not raised.
                if auto_download_enabled and _auto_download_eligible(
                    row, min_return_max_dd=auto_download_min_return_max_dd, min_trades=auto_download_min_trades
                ):
                    try:
                        target = trade_report_path(REPORTS_DIR, combo.get("instrument", ""), cid)
                        if not target.exists():
                            download_current_report(page, selectors, target, cid)
                    except Exception:  # noqa: BLE001 - best-effort, never fails the combo
                        pass

            return

        except LoginNotConfigured:
            # Retrying per-combo can't fix a missing login config - stop the whole sweep.
            raise

        except Exception as exc:  # noqa: BLE001 - one bad combo must not crash the sweep
            if attempt <= max_retries:
                time.sleep(backoff_s)
                backoff_s *= 2
                continue

            # Step 10: give up on this combo - save artifacts, write an error row, move on.
            _save_failure_artifacts(page, cid)
            row = _build_row(combo, cid, "error", str(exc), {}, dte_values)
            append_row(csv_path, fieldnames, row)
            _log(log_path, cid, "error", str(exc), attempt)
            stats["error"] += 1
            return


def _build_row(
    combo: dict[str, Any],
    cid: str,
    status: str,
    error: str | None,
    raw_metrics: dict[str, str | None],
    dte_values: list[int] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "combo_id": cid,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "error": error or "",
        # Computed directly from `combo` HERE, at write-time - never reconstructed
        # from a flattened CSV row later (see src/web/combo_launcher.py's
        # row_to_combo, which was confirmed this session to NOT reliably
        # reconstruct every historical combo shape - e.g. an "ATM" bare-string
        # strike mode round-trips wrong). Computing it here instead guarantees
        # every row swept from this point on has a 100%-correct strategy_key,
        # with no reconstruction risk at all.
        "strategy_key": strategy_key(combo),
        # The DTE filter is applied once per sweep run (not a per-combo dimension),
        # so it was never actually recorded anywhere before this - meaning results
        # from two runs with different DTE settings merged into one CSV had no way
        # to be told apart. Stored as the exact combination used, e.g. "0" or
        # "0,1,2" - comma-joined and sorted so the same combination always produces
        # the same string regardless of the order it was selected in the UI.
        "dte": ",".join(str(v) for v in sorted(dte_values)) if dte_values else "",
    }
    row.update(flatten(combo))
    for name, raw in raw_metrics.items():
        row[name] = parse_number(raw)
    row["raw_metrics_json"] = json.dumps(raw_metrics)
    return row


def _capture_individual_dte_reports(
    page: Page,
    selectors: Selectors,
    combo: dict[str, Any],
    base_cid: str,
    csv_path: Path,
    fieldnames: list[str],
    slippage_pct: float,
    dte_values: list[int],
    *,
    auto_download_enabled: bool = False,
    auto_download_min_return_max_dd: float = 1.5,
    auto_download_min_trades: int = 0,
) -> list[int]:
    """SweepUIConfig.capture_dte_individually: instead of one report reflecting
    every selected DTE combined, capture one independent row PER DTE - re-filtering
    the SAME already-completed backtest result to a single DTE at a time
    (apply_result_settings again - no new backtest, no new "Start Backtest") and
    scraping each view separately. The ROW always gets written, unconditionally -
    that's the whole point of this feature, see the caller's own comment on why it
    must never be gated on how any one DTE (or the blended combo) happens to look.

    Returns the DTEs actually written - the caller (_run_one_combo) uses an empty
    return as a signal to fall back to writing the ordinary combined row instead,
    so a combo's backtest is never silently lost to disk entirely just because
    every one of its individual re-filter/scrape attempts happened to fail.

    The trade REPORT FILE for a variant is still optional and still cost-gated, same
    as the combined path's own report download - but decided from THAT VARIANT's own
    scraped metrics, not the blended base row's. A combo whose blended number misses
    auto_download's bar can easily have one individual DTE that clears it easily (or
    the reverse) - deciding per-variant is what actually lets a good single-DTE
    result get its report downloaded, instead of inheriting a verdict that was never
    about its own performance to begin with.

    Each variant's id is a deterministic composite of the base combo's own id -
    f"{base_cid}_dte{n}" - NOT a fresh hash of an enriched combo dict. combo_id()
    itself is never called with anything new here, so no existing combo's identity
    is touched by this feature; a variant is simply a new, distinct string that
    flows through trade_report_path/append_row exactly like any other combo_id
    would, with zero changes needed anywhere else in the system. Safe against
    collision with a real combo_id: combo_id() only ever produces bare 12-character
    hex hashes, never one with an "_dte" suffix.

    Best-effort PER DTE, deliberately more granular than the combined path's own
    best-effort wrapper: one DTE's scrape/download failing must not lose the
    others - a bad DTE is skipped, not fatal to the whole combo (which has already
    been recorded as "ok" by the time this runs regardless)."""
    instrument = combo.get("instrument", "")
    written: list[int] = []
    for dte in sorted(dte_values):
        try:
            apply_result_settings(page, selectors, slippage_pct, [dte])
            raw_metrics = scrape_metrics(page, selectors)
            variant_cid = f"{base_cid}_dte{dte}"
            row = _build_row(combo, variant_cid, "ok", None, raw_metrics, [dte])
            append_row(csv_path, fieldnames, row)
            written.append(dte)
            if auto_download_enabled and _auto_download_eligible(
                row, min_return_max_dd=auto_download_min_return_max_dd, min_trades=auto_download_min_trades
            ):
                target = trade_report_path(REPORTS_DIR, instrument, variant_cid)
                if not target.exists():
                    download_current_report(page, selectors, target, variant_cid)
        except Exception:  # noqa: BLE001 - one bad DTE must not lose the rest
            continue
    return written


_DTE_VARIANT_SUFFIX_RE = re.compile(r"_dte(\d+)$")


def parse_dte_variant_suffix(combo_id: str) -> int | None:
    """The N from a per-DTE composite id (f"{base}_dte{N}", see
    _capture_individual_dte_reports above) - None for a bare combo_id.

    Any code that REPLAYS a combo_id already on record - Correlate's Force
    re-download, CAS regime analysis's own download - must isolate the DTE
    filter back to just N when this returns non-None, not apply whatever DTE(s)
    the CURRENT config happens to have selected. Confirmed live: a "_dte0"
    variant re-downloaded with the config's full multi-select dte_values (e.g.
    [0, 1, 2]) came back with 3x the trade days of the original DTE-0-only
    report - a "_dte0" id no longer meant DTE 0 once replayed this way, and
    every metric on it (P&L, drawdown, win rate) was silently wrong."""
    m = _DTE_VARIANT_SUFFIX_RE.search(combo_id)
    return int(m.group(1)) if m else None


def _save_failure_artifacts(page: Page, cid: str) -> None:
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(SCREENSHOTS_DIR / f"{cid}.png"), full_page=True)
        (SCREENSHOTS_DIR / f"{cid}.html").write_text(page.content())
    except Exception:
        pass  # best-effort - artifact saving must never mask the original failure


def _log(log_path: Path, cid: str, status: str, error: str | None, attempt: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "combo_id": cid,
            "status": status,
            "error": error,
            "attempt": attempt,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    with log_path.open("a") as f:
        f.write(line + "\n")


# --- Parallel execution: separate OS processes, not threads --------------------
#
# Playwright's sync API is explicitly not thread-safe (a Page/BrowserContext is tied
# to the single greenlet/thread that created its connection) - confirmed live via a
# real "Cannot switch to a different thread" failure when driving 15 tabs from worker
# threads. Separate OS processes don't have this problem: each process gets its own
# Python interpreter and its own Playwright connection. Each worker below pulls its
# next combo from a queue SHARED across every worker process (see _run_from_queue)
# rather than being handed a fixed slice upfront - confirmed live that a fixed,
# equal-sized slice per worker leaves faster workers/accounts idling once they
# finish their share while a slower one is still grinding through its own. Each
# worker still gets its own persistent browser profile (Chromium locks a
# user-data-dir to one running instance, so N workers need N distinct profile
# directories).

WORKER_PROFILES_DIR = Path(__file__).resolve().parent.parent / ".browser-profiles"


def _worker_profile_dir(worker_index: int, primary_profile_dir: Path) -> Path:
    # Namespaced by which account's profile this worker copies from (not just its
    # flat index) - confirmed live this matters: worker index N can be assigned to
    # account 1 in one run and account 2 in a later run (multi-account splits aren't
    # fixed to specific indices), and a flat "worker-N" name would otherwise silently
    # reuse a stale copy of the WRONG account's session instead of refreshing it.
    account_ns = primary_profile_dir.name.lstrip(".")
    return WORKER_PROFILES_DIR / account_ns / f"worker-{worker_index}"


def _ensure_worker_profile(worker_index: int, primary_profile_dir: Path) -> Path:
    """Each worker needs its own already-logged-in profile. Rather than requiring
    config/selectors.yaml's login form selectors to be filled in (they aren't, as of
    this writing - see the NEEDS-CHECK notes there), copy the primary profile's
    cookies/local storage once per worker on first use. Confirmed acceptable live:
    AlgoTest tolerates the same account being logged in from several local browser
    processes at once (that's exactly what its documented 15-parallel-strategies
    support implies)."""
    import shutil

    target = _worker_profile_dir(worker_index, primary_profile_dir)
    if not target.exists() and primary_profile_dir.exists():
        shutil.copytree(primary_profile_dir, target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _run_from_queue(
    page: Page,
    queue: "multiprocessing.Queue[dict[str, Any] | None]",
    selectors: Selectors,
    csv_path: Path,
    log_path: Path,
    fieldnames: list[str],
    *,
    delay_s: float = 2.0,
    result_timeout_s: int = 180,
    max_retries: int = 2,
    email: str | None = None,
    password: str | None = None,
    slippage_pct: float = 1.0,
    dte_values: list[int] | None = None,
    brokerage_rate: float | None = None,
    auto_download_enabled: bool = False,
    auto_download_min_return_max_dd: float = 1.5,
    auto_download_min_trades: int = 0,
    capture_dte_individually: bool = False,
    stop_event: "multiprocessing.synchronize.Event | None" = None,
    progress_queue: "multiprocessing.Queue[dict[str, Any]] | None" = None,
) -> dict[str, int]:
    """Same per-combo execution as run_sweep, but pulls its next combo from a queue
    SHARED across every worker process instead of iterating a fixed slice handed to
    it upfront - this is what lets a faster worker/account keep picking up more work
    the moment it's free, instead of idling once an equal "fair share" runs out while
    a slower worker/account is still going.

    A `None` pulled from the queue is a poison pill telling this worker there's no
    more work. Exactly one pill is queued per worker (see run_sweep_multiprocess), so
    every worker is guaranteed to eventually see one and stop, however the real
    combos happened to be split up between them.

    `stop_event` (a multiprocessing.Event, NOT the caller's own threading.Event -
    see run_sweep_multiprocess) is checked only *between* combos, never during one -
    Stop must let whatever combo is already running finish and record its result
    rather than killing the browser mid-action, which is what used to leave AlgoTest's
    Node driver mid-write and crash it with an EPIPE, sometimes wedging that worker
    process so badly even SIGTERM couldn't kill it. The queue.get() itself is
    poll-with-timeout rather than a plain blocking get so a worker sitting idle
    (nothing left to pull) still notices Stop within about a second instead of only
    when its poison pill happens to arrive.

    `progress_queue`, if given, gets one {"combo_id", "label", "event": "started"}
    message right before a combo starts and {"combo_id", "event": "finished"} right
    after - this is what lets the parent process's monitor loop (see
    run_sweep_multiprocess) show WHICH specific combo(s) are still in flight during
    Stop, instead of only a bare worker-process-alive count that can look frozen for
    however long the current combo's retries happen to take. Best-effort only (a
    full queue must never block or fail an otherwise-successful combo) - the
    fallback if this is ever dropped is exactly today's behavior (a bare count)."""
    stats = {"ok": 0, "error": 0, "skipped": 0}
    brokerage_configured = [False]

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            combo = queue.get(timeout=1)
        except Empty:
            continue
        if combo is None:
            break

        cid = combo_id(combo)
        if progress_queue is not None:
            try:
                progress_queue.put_nowait({"combo_id": cid, "label": _combo_label(combo), "event": "started"})
            except Exception:  # noqa: BLE001 - visibility only, never worth failing a combo over
                pass
        _run_one_combo(
            page,
            combo,
            cid,
            selectors,
            csv_path,
            log_path,
            fieldnames,
            stats,
            result_timeout_s=result_timeout_s,
            max_retries=max_retries,
            email=email,
            password=password,
            slippage_pct=slippage_pct,
            dte_values=dte_values or [],
            brokerage_rate=brokerage_rate,
            brokerage_configured=brokerage_configured,
            auto_download_enabled=auto_download_enabled,
            auto_download_min_return_max_dd=auto_download_min_return_max_dd,
            auto_download_min_trades=auto_download_min_trades,
            capture_dte_individually=capture_dte_individually,
        )
        if progress_queue is not None:
            try:
                progress_queue.put_nowait({"combo_id": cid, "event": "finished"})
            except Exception:  # noqa: BLE001 - visibility only, never worth failing a combo over
                pass
        time.sleep(delay_s)

    return stats


def _worker_main(
    worker_index: int,
    queue: "multiprocessing.Queue[dict[str, Any] | None]",
    selectors: Selectors,
    primary_profile_dir: Path,
    worker_csv_path: Path,
    worker_log_path: Path,
    fieldnames: list[str],
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
    auto_download_enabled: bool = False,
    auto_download_min_return_max_dd: float = 1.5,
    auto_download_min_trades: int = 0,
    capture_dte_individually: bool = False,
    stop_event: "multiprocessing.synchronize.Event | None" = None,
    progress_queue: "multiprocessing.Queue[dict[str, Any]] | None" = None,
) -> None:
    from src import browser  # re-imported in the spawned child process
    from src.auth import LoginNotConfigured

    profile_dir = _ensure_worker_profile(worker_index, primary_profile_dir)
    try:
        with browser.persistent_context(headless=headless, profile_dir=profile_dir) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(selectors.builder.url)
            _run_from_queue(
                page,
                queue,
                selectors,
                worker_csv_path,
                worker_log_path,
                fieldnames,
                delay_s=delay_s,
                result_timeout_s=result_timeout_s,
                max_retries=max_retries,
                email=email,
                password=password,
                slippage_pct=slippage_pct,
                dte_values=dte_values,
                brokerage_rate=brokerage_rate,
                auto_download_enabled=auto_download_enabled,
                auto_download_min_return_max_dd=auto_download_min_return_max_dd,
                auto_download_min_trades=auto_download_min_trades,
                capture_dte_individually=capture_dte_individually,
                stop_event=stop_event,
                progress_queue=progress_queue,
            )
    except LoginNotConfigured as exc:
        # Confirmed live: a worker whose session expired crashes silently here - the
        # only trace was a traceback in the server's own stdout, while run_sweep_multiprocess
        # just saw "no processes alive" and reported a normal "done". Write a marker so
        # the orchestrator can tell a crash apart from genuine completion and surface it.
        (worker_csv_path.parent / f"{worker_csv_path.stem}.error").write_text(str(exc))


def _merge_csvs(worker_paths: list[Path], into: Path, fieldnames: list[str]) -> None:
    import csv

    # Keyed by combo_id (last write wins) as a defensive backstop against duplicate
    # rows - confirmed live that stale worker files left over from an earlier crashed
    # attempt at the same csv_path can otherwise get silently double-counted (see
    # run_sweep_multiprocess, which now also clears those files before each run).
    by_combo_id: dict[str, dict[str, Any]] = {}
    for p in worker_paths:
        if not p.exists():
            continue
        with p.open(newline="") as f:
            for row in csv.DictReader(f):
                cid = row.get("combo_id")
                if cid:
                    by_combo_id[cid] = row
    into.parent.mkdir(parents=True, exist_ok=True)
    with into.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in by_combo_id.values():
            writer.writerow(row)


def _merge_logs(worker_paths: list[Path], into: Path) -> None:
    into.parent.mkdir(parents=True, exist_ok=True)
    with into.open("a") as out:
        for p in worker_paths:
            if not p.exists():
                continue
            out.write(p.read_text())


@dataclass
class AccountConfig:
    """One worker slot's login credentials + the base profile to copy from - lets
    run_sweep_multiprocess spread workers across more than one AlgoTest account
    (confirmed live: AlgoTest throttles concurrency per account, not per machine/IP,
    so a second account gets its own independent budget rather than competing with
    the first for the same one)."""

    email: str | None
    password: str | None
    profile_dir: Path


def run_sweep_multiprocess(
    combos: Iterable[dict[str, Any]],
    selectors: Selectors,
    csv_path: Path,
    log_path: Path,
    fieldnames: list[str],
    existing_statuses: dict[str, str],
    *,
    parallelism: int,
    total_hint: int | None = None,
    already_done_hint: int = 0,
    headless: bool = True,
    delay_s: float = 2.0,
    result_timeout_s: int = 180,
    max_retries: int = 2,
    email: str | None = None,
    password: str | None = None,
    slippage_pct: float = 1.0,
    dte_values: list[int] | None = None,
    brokerage_rate: float | None = None,
    auto_download_enabled: bool = False,
    auto_download_min_return_max_dd: float = 1.5,
    auto_download_min_trades: int = 0,
    capture_dte_individually: bool = False,
    known_strategy_keys: dict[str, str] | None = None,
    duplicate_queue_path: Path | None = None,
    primary_profile_dir: Path | None = None,
    accounts: list[AccountConfig] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_event=None,
    poll_interval_s: float = 3.0,
) -> dict[str, int]:
    """Run independent worker processes, each on its own browser profile, pulling
    combos one at a time from a single work queue SHARED across all of them, merging
    their per-worker CSVs/logs into `csv_path`/`log_path` as they go so progress stays
    live.

    Work is dispatched dynamically, not pre-split: every combo is dropped into one
    shared multiprocessing.Queue up front (plus one "no more work" pill per worker),
    and each worker just keeps pulling the next combo whenever it's free. This is
    deliberate - confirmed live that a fixed, equal-sized slice per worker leaves a
    faster worker/account idling once its share is done while a slower one is still
    grinding through its own; a shared queue means whichever worker/account is faster
    naturally ends up doing more of the total work, with no idle time on either side.
    The queue's own locking guarantees each combo is handed to exactly one worker,
    never more than one, regardless of how many workers or accounts are involved.

    Pass `accounts` (a list of one AccountConfig per worker slot) to spread workers
    across more than one AlgoTest account instead of the single email/password/
    primary_profile_dir - `parallelism` is then derived from len(accounts) and the
    email/password/primary_profile_dir arguments are ignored.

    `combos` may be a plain list (every existing caller - exact counts, and an early
    exit when there's nothing left to do) or, for a sweep too large to fully
    materialize up front, a lazy iterable such as
    src.web.expand.iter_shuffled_ui_combos - already-done combos are then filtered out
    of dispatch as the queue-feeder thread below consumes it, one at a time, instead
    of in one eager pass. `total_hint` (an estimate is fine - see
    src.sweep.estimate_survival) stands in for len(combos) in that case, since
    computing the real number would mean the same eager pass this exists to avoid.
    `already_done_hint` likewise stands in for the exact "skipped" count a sized list
    would compute directly - already-done combos are scattered at random positions
    across the whole shuffled stream, not gathered at the front, so counting them only
    as the stream happens to walk past them would make "current" progress badly
    understate a large resume (confirmed live: a run resuming 6,831 already-done
    combos reported "current: 23" after several minutes - nothing was actually being
    re-run, but it looked broken).

    `capture_dte_individually` (see _capture_individual_dte_reports): when an
    eligible combo's report would be auto-downloaded and more than one DTE is
    selected, capture one independent row + report per DTE instead of one combined
    one - reusing the same already-completed backtest result (no extra "Start
    Backtest"), just re-filtered per DTE. Off by default - every sweep that doesn't
    set this behaves exactly as before."""
    from src.browser import PROFILE_DIR

    if accounts:
        effective_parallelism = len(accounts)
    else:
        effective_parallelism = parallelism
        primary_profile_dir = primary_profile_dir or PROFILE_DIR

    combos_are_sized = hasattr(combos, "__len__")
    # Mutable holder (not a plain int) so both branches below and the generator
    # closure can increment it as duplicates are discovered - unlike `skipped`,
    # this can't be known upfront even as an estimate, since it depends on the
    # registry lookup happening per-combo as the stream is actually consumed.
    duplicates_count = [0]

    def _check_duplicate(c: dict[str, Any]) -> bool:
        """True if `c` was captured as a known duplicate (and must NOT be
        dispatched for replay) - see known_duplicate_combo_id's own docstring."""
        existing_cid = known_duplicate_combo_id(c, known_strategy_keys)
        if existing_cid is None:
            return False
        duplicates_count[0] += 1
        if duplicate_queue_path is not None:
            queue_duplicate_for_refresh(duplicate_queue_path, existing_cid, strategy_key(c))
        return True

    if combos_are_sized:
        pre_filtered = [
            c for c in combos
            if not _combo_already_done(combo_id(c), dte_values, capture_dte_individually, existing_statuses)
        ]
        skipped = len(combos) - len(pre_filtered)
        combos_total = len(combos)
        todo = [c for c in pre_filtered if not _check_duplicate(c)]
    else:
        if total_hint is None:
            raise ValueError("total_hint is required when combos is not a sized sequence (e.g. a generator)")
        combos_total = total_hint
        # Already-done combos are scattered at random positions across the whole
        # shuffled stream, not gathered at the front - counting them only as
        # _stream_todo below happens to walk past them would mean "current" barely
        # moves for a long time despite thousands of rows already being safely on
        # disk (confirmed live: a 6,831-row resume showed "current: 23" after several
        # minutes - technically nothing was being re-run, but it looked broken).
        # already_done_hint is the fixed, known-upfront count instead (same value
        # start() already computes as RunState.skipped) - _stream_todo below still
        # filters these out of dispatch, it just no longer needs to also COUNT them,
        # since they're already represented here.
        skipped = already_done_hint

        def _stream_todo() -> Any:
            for c in combos:
                cid = combo_id(c)
                if not _combo_already_done(cid, dte_values, capture_dte_individually, existing_statuses) and not _check_duplicate(c):
                    yield c

        todo = _stream_todo()

    def _report(
        completed: int, ok: int, error: int, in_progress: int = 0, in_progress_labels: list[str] | None = None
    ) -> None:
        if on_progress is not None:
            on_progress(
                {
                    "current": completed + skipped,
                    "total": combos_total,
                    "ok": ok,
                    "error": error,
                    "skipped": skipped,
                    "duplicate": duplicates_count[0],
                    "in_progress": in_progress,
                    "in_progress_labels": in_progress_labels or [],
                }
            )

    if combos_are_sized and not todo:
        _report(0, 0, 0)
        return {"ok": 0, "error": 0, "skipped": skipped, "duplicate": duplicates_count[0]}

    # The generator case can't cheaply check "is there any work at all" without
    # consuming it (see docstring) - worst case a couple of workers spin up, open a
    # browser, and immediately get the queue's poison pill with nothing to do. Wasted
    # seconds, not a correctness problem; the list case above still exits early.
    num_workers = min(effective_parallelism, len(todo)) if combos_are_sized else effective_parallelism
    if accounts:
        accounts = accounts[:num_workers]  # fewer combos than worker slots - drop the extra slots
    work_dir = csv_path.parent / f".parallel-{csv_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    worker_csvs = [work_dir / f"worker-{i}.csv" for i in range(num_workers)]
    worker_logs = [work_dir / f"worker-{i}.log" for i in range(num_workers)]
    worker_errors = [work_dir / f"worker-{i}.error" for i in range(num_workers)]

    # work_dir's name is deterministic (derived from csv_path's stem alone, not this
    # invocation), so a prior run against this same csv_path - e.g. one that crashed
    # mid-sweep, exactly the resume scenario this function exists for - can leave
    # worker-N.csv/.log files behind. run_sweep() appends rather than truncating, so
    # without clearing these first, a resumed run would silently inherit and re-merge
    # stale rows from the crashed attempt on top of the fresh existing_snapshot below
    # (confirmed live: this doubled every already-done row before this fix).
    for stale in worker_csvs + worker_logs + worker_errors:
        stale.unlink(missing_ok=True)

    # Resuming: csv_path may already hold rows from a previous run (the combos those
    # rows cover are exactly what `existing_statuses` filtered out of `todo` above).
    # _merge_csvs rebuilds csv_path from scratch every cycle, so without this those
    # already-done rows would be silently dropped the moment the first merge runs -
    # snapshot them once up front and always include the snapshot in every merge.
    existing_snapshot = work_dir / "existing.csv"
    if csv_path.exists():
        existing_snapshot.write_text(csv_path.read_text())
    merge_sources = ([existing_snapshot] if existing_snapshot.exists() else []) + worker_csvs

    def _worker_account(i: int) -> tuple[str | None, str | None, Path]:
        if accounts:
            acct = accounts[i]
            return acct.email, acct.password, acct.profile_dir
        return email, password, primary_profile_dir

    # One shared queue instead of a pre-split chunk per worker - every real combo is
    # enqueued, followed by one None poison pill per worker, so whichever
    # worker/account is faster just keeps pulling more work instead of running dry
    # while a slower one is still going.
    #
    # Deliberately BOUNDED and fed from a background thread instead of dumping every
    # combo in with one synchronous loop before any worker exists to consume them -
    # confirmed live this is not just tidiness. On macOS, multiprocessing.Queue's
    # internal semaphore caps out around 32767 regardless of not passing maxsize
    # (unlike Linux, where "unbounded" really is unbounded). Feeding a ~170k-combo
    # queue in one shot, before any worker process had even started, hit that cap and
    # deadlocked the whole run: the feeding loop blocked forever inside queue.put()
    # waiting for capacity that only a running worker's queue.get() could free - and
    # since nothing had started consuming yet, nothing ever would. That same thread
    # never even reached the stop_event check in the poll loop below, so clicking
    # Stop had nothing to interrupt; the run looked hung with no way out short of
    # restarting the server. A small bounded queue, fed incrementally with a
    # stop_event-aware timeout, can't hit that cap and always stays responsive to
    # Stop within about a second.
    queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=200)

    # Separate from `queue` above (that's real WORK; this is just "combo X started/
    # finished" visibility) - see _run_from_queue's own progress_queue docstring.
    # Unbounded is safe here unlike `queue`: at most ~2 x num_workers messages are
    # ever pending between drains (the monitor loop below drains it every tick), so
    # it can never approach the macOS semaphore cap that mandated a bounded, fed-
    # incrementally queue for the (potentially hundreds-of-thousands-deep) work queue.
    progress_queue: multiprocessing.Queue = multiprocessing.Queue()

    # A SEPARATE Event from the caller's own `stop_event` (a threading.Event, which
    # can't cross a process boundary) - this is what each worker actually checks,
    # between combos only, to decide whether to keep pulling work. See
    # _run_from_queue's docstring for why Stop works this way: it lets whatever
    # combo is already running finish and get recorded, rather than killing the
    # browser mid-action (which used to crash AlgoTest's Node driver with EPIPE and
    # could leave the worker process wedged badly enough that even SIGTERM failed).
    worker_stop_event = multiprocessing.Event()

    def _feed_queue() -> None:
        for c in todo:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    queue.put(c, timeout=1)
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

    processes = []
    for i in range(num_workers):
        w_email, w_password, w_profile_dir = _worker_account(i)
        processes.append(
            multiprocessing.Process(
                target=_worker_main,
                args=(i, queue, selectors, w_profile_dir, worker_csvs[i], worker_logs[i], fieldnames),
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
                    auto_download_enabled=auto_download_enabled,
                    auto_download_min_return_max_dd=auto_download_min_return_max_dd,
                    auto_download_min_trades=auto_download_min_trades,
                    capture_dte_individually=capture_dte_individually,
                    stop_event=worker_stop_event,
                    progress_queue=progress_queue,
                ),
            )
        )
    for p in processes:
        p.start()

    # Set once we've told workers to drain (worker_stop_event.set()) - bounds how
    # long we'll wait for in-flight combos to finish on their own before treating a
    # still-alive process as genuinely stuck rather than just slow, and escalating.
    # Sized generously off the worst case a single combo can legitimately take: a
    # full result_timeout_s wait, repeated across every retry.
    drain_deadline: float | None = None
    grace_period_s = max(60.0, result_timeout_s * (max_retries + 1) + 60.0)

    # Keyed by combo_id -> label, kept current from _run_from_queue's "started"/
    # "finished" progress_queue events (see there) - this is what lets the Stopping
    # message name WHICH combo(s) are still in flight instead of only a bare
    # worker-process-alive count, which can look frozen for however long the
    # current combo's own retries happen to take.
    in_progress_ids: dict[str, str] = {}

    def _drain_worker_progress() -> None:
        while True:
            try:
                msg = progress_queue.get_nowait()
            except Empty:
                return
            cid = msg.get("combo_id")
            if not cid:
                continue
            if msg.get("event") == "started":
                in_progress_ids[cid] = msg.get("label", cid)
            else:
                in_progress_ids.pop(cid, None)

    try:
        while any(p.is_alive() for p in processes):
            alive = [p for p in processes if p.is_alive()]
            if stop_event is not None and stop_event.is_set():
                if drain_deadline is None:
                    worker_stop_event.set()
                    drain_deadline = time.monotonic() + grace_period_s
                elif time.monotonic() > drain_deadline:
                    # Safety net only, not the normal path: a worker still alive this
                    # long after being told to stop isn't "finishing up," it's stuck.
                    for p in alive:
                        p.terminate()
                    break
            _drain_worker_progress()
            _merge_csvs(merge_sources, csv_path, fieldnames)
            completed, ok, error = _count_rows(worker_csvs)
            _report(completed, ok, error, in_progress=len(alive), in_progress_labels=list(in_progress_ids.values()))
            time.sleep(poll_interval_s)
    finally:
        for p in processes:
            p.join(timeout=30)
        # Last-resort hard kill - confirmed live that a worker whose Node/Playwright
        # driver crashed (e.g. an EPIPE from the scenario above) can occasionally end
        # up wedged in a state SIGTERM alone doesn't clear; never leave Stop hanging
        # on a single stuck process indefinitely.
        for p in processes:
            if p.is_alive():
                p.kill()
                p.join(timeout=10)

    _merge_csvs(merge_sources, csv_path, fieldnames)
    _merge_logs(worker_logs, log_path)
    completed, ok, error = _count_rows(worker_csvs)
    _report(completed, ok, error)

    # A worker crashing on LoginNotConfigured looks identical to "all done" from here
    # (no processes left alive) unless we check for the marker it leaves behind -
    # confirmed live: without this, a session expiring mid-sweep silently reported
    # "done" with 0 new results instead of surfacing as an error the UI can show.
    if not (stop_event is not None and stop_event.is_set()):
        login_errors = [m.read_text() for m in worker_errors if m.exists()]
        if login_errors:
            from src.auth import LoginNotConfigured

            raise LoginNotConfigured(login_errors[0])

    return {"ok": ok, "error": error, "skipped": skipped, "duplicate": duplicates_count[0]}


def _count_rows(worker_csvs: list[Path]) -> tuple[int, int, int]:
    """Counts unique COMBOS, not raw rows - capture_dte_individually writes up
    to len(dte_values) separate rows for the ONE combo it came from (see
    _capture_individual_dte_reports), all "ok". Counting every row naively
    over-reports "ok" by that same multiple, desynchronizing this live
    progress readout from combos_total (always per-combo, len(combos)) and
    from run_sweep_multiprocess's own upfront `skipped` figure (also
    per-combo, via _combo_already_done) - confirmed live: a resumed
    9,040-combo sweep showed "current: 1835" (current = completed + skipped)
    after only 1,747 real combos were actually done, and the gap only grows
    the longer a run with this flag on keeps going. A combo's own successful
    backtest always writes its first row (bare or "_dteN") before any
    variant-specific work begins, so counting each distinct base id once -
    the instant its first row appears, not waiting for every variant - mirrors
    exactly when the sequential run_sweep path's own `stats["ok"] += 1`
    already fires (before DTE-splitting even starts)."""
    import csv

    ok_ids: set[str] = set()
    error_ids: set[str] = set()
    for p in worker_csvs:
        if not p.exists():
            continue
        with p.open(newline="") as f:
            for row in csv.DictReader(f):
                cid = row.get("combo_id", "")
                base_cid = _DTE_VARIANT_SUFFIX_RE.sub("", cid)
                if row.get("status") == "ok":
                    ok_ids.add(base_cid)
                elif row.get("status") == "error":
                    error_ids.add(base_cid)
    ok, error = len(ok_ids), len(error_ids)
    return ok + error, ok, error
