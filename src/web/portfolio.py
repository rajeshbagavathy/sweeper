"""Multi-session capital-reuse portfolio: each bucket first shortlists its own top
candidates (guaranteeing every session gets a fair shot at being represented), then
ONE diversification pass runs across the merged shortlist together (same algorithm
as the single-session "Uncorrelated strategies" basket) so cross-bucket correlation
gets checked too - a pick from one bucket can still knock out a correlated pick from
a different bucket. The result is then split back out by bucket and lot-sized within
a realistic per-bucket margin budget.

The four buckets model reusing the morning's margin twice ("2.5x utilization"):
  - short_morning: a quick early exit that frees part of the morning's margin.
  - midday: a second strategy re-entered on that just-freed margin (never before
    the chosen short_morning picks' own latest exit time - the margin isn't
    actually free until then; any pick that would enter earlier than that is
    dropped, not reassigned, since there's nothing to reassign it TO).
  - long_morning: the rest of the morning's margin, held to early afternoon.
  - afternoon: a separate full-margin session, independent of the morning entirely.

Two things were tried and rejected before landing here:
  - Four fully siloed diversification passes (one per bucket, comparing candidates
    only against others in the same bucket) - lets two correlated strategies in
    DIFFERENT buckets both into the basket, since neither pass ever saw the other's
    candidates. The flat single-session basket would have rejected the second one
    outright; this let it through.
  - One flat global pass across every bucket's candidates with no per-bucket
    shortlist first - fixes the above, but since combined_sort_key ranks every
    candidate on the same scale regardless of session, whichever session happens to
    score best historically (long-morning, in this codebase's real data) crowds out
    every other session's candidates entirely before diversification even runs,
    leaving 3 of 4 buckets empty - useless for a plan whose whole point is spreading
    capital across sessions.

Shortlisting per bucket first, then diversifying the merged shortlist once, keeps
both properties: every bucket is guaranteed some real candidates in contention, and
a pick can still be rejected for correlating with a different bucket's pick.

Each pick is lot-sized inversely to its own max drawdown so a bigger loser gets a
smaller slice of its bucket's budget. The combined portfolio metrics use every
member's *recommended* lot size, not an equal split - and, being capital-realistic
(the flat basket implicitly assumes unlimited concurrent margin, i.e. every pick at
a full base_lots with no shared budget), this will still generally show a lower
Reward:Risk than the flat basket - that gap is real, not a bug: it's the cost of
actually fitting inside your margin."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.correlate import (
    compute_portfolio_metrics,
    compute_portfolio_metrics_weighted_with_charges,
    correlation_matrix,
    parse_trade_report,
    pick_diversified_basket,
    trade_report_path,
)
from src.web.models import MAX_SANE_UNDERLYING_SL_PCT
from src.web.narrow import combined_sort_key, profitability_gated_key

BUCKET_ORDER = ["short_morning", "long_morning", "midday", "afternoon"]
DEFAULT_BUDGETS: dict[str, float] = {
    "short_morning": 18.0,
    "long_morning": 27.0,
    "midday": 18.0,
    "afternoon": 45.0,
}
# Per-bucket shortlist size, BEFORE the merged pool gets diversified - not a global
# pool size (see module docstring for why a flat global top-N starves every bucket
# but the historically-best-scoring one).
DEFAULT_TOP_N = 20
BASE_LOTS = 10.0


def data_coverage_gaps(
    candidates: list[dict[str, Any]], reports_dir: Path, instrument: str | None = None
) -> list[dict[str, Any]]:
    """Which downloaded trade reports in this candidate pool are missing recent
    expiries relative to the most complete one - NOT a download-timestamp check
    (an old download of an already-settled expiry is fine and won't change; this
    catches an actual gap in what's covered, e.g. a report that stops short of the
    most recent expiry the rest of the pool already has). A combo flagged here has
    real missing data, not just an old file - replay it in AlgoTest and re-download
    to bring it level with the rest before trusting a basket that includes it.

    `instrument` is a fallback only - each candidate's own "instrument" field wins
    when present, so a mixed-instrument pool (e.g. an unfiltered Correlate call)
    still resolves each report's path correctly instead of assuming one instrument
    for everything."""
    date_sets: dict[str, set[str]] = {}
    for r in candidates:
        cid = r["combo_id"]
        row_instrument = r.get("instrument") or instrument or ""
        path = trade_report_path(reports_dir, row_instrument, cid)
        if not path.exists():
            continue
        series = parse_trade_report(path)
        if series:
            date_sets[cid] = set(series)

    if not date_sets:
        return []
    all_dates = set().union(*date_sets.values())
    expected_last_date = max(all_dates)

    gaps = []
    for r in candidates:
        cid = r["combo_id"]
        dates = date_sets.get(cid)
        if not dates:
            continue
        last_date = max(dates)
        if last_date < expected_last_date:
            missing_count = sum(1 for d in all_dates if d > last_date)
            gaps.append(
                {
                    "combo_id": cid,
                    "entry_time": r.get("entry_time", ""),
                    "exit_time": r.get("exit_time", ""),
                    "last_date": last_date,
                    "expected_last_date": expected_last_date,
                    "missing_count": missing_count,
                }
            )
    gaps.sort(key=lambda g: g["missing_count"], reverse=True)
    return gaps


def classify_bucket(row: dict[str, Any]) -> str | None:
    """Which of the four buckets a row's (entry_time, exit_time) belongs to, or None
    if it doesn't cleanly fit any of them (e.g. a full-day hold, or something in the
    gap between short/long morning that's ambiguous which pool it drew from)."""
    entry, exit_ = row.get("entry_time") or "", row.get("exit_time") or ""
    if not entry or not exit_:
        return None
    if entry < "11:00":
        if exit_ <= "11:45":
            return "short_morning"
        if exit_ >= "12:30":
            return "long_morning"
        return None
    if "11:00" <= entry < "13:00" and exit_ <= "14:15":
        return "midday"
    if entry >= "13:00":
        return "afternoon"
    return None


def has_hard_stop_loss(row: dict[str, Any]) -> bool:
    """True if this combo has a leg-level OR overall Stop Loss configured, on
    either basis - Trail SL alone (or no stop loss at all) leaves the position
    with no hard cap on loss before the trail has locked anything in, which on a
    bad enough day is exactly the "nightmare for the whole portfolio" scenario a
    diversified basket is supposed to guard against. A strategy failing this is
    never a candidate here, no matter how good its other numbers look - see
    _shortlist below, which excludes these before any ranking happens at all.

    An "Underlying %" leg SL above MAX_SANE_UNDERLYING_SL_PCT doesn't count -
    treated the same as having no leg SL at all (falls through to check overall
    Stop Loss instead), since a threshold that wide practically never trips."""
    leg_kind = row.get("leg_risk.stoploss_pct.kind")
    if leg_kind == "underlying_percentage":
        try:
            value = float(row.get("leg_risk.stoploss_pct.value"))
        except (TypeError, ValueError):
            value = None
        if value is not None and value <= MAX_SANE_UNDERLYING_SL_PCT:
            return True
    elif leg_kind:
        return True
    if (row.get("leg_risk.stoploss_pct") or "") not in ("", None):
        return True  # legacy flat column, from before the dual-basis leg SL feature
    return bool(row.get("stoploss.kind"))


def charges_from_row(row: dict[str, Any]) -> dict[str, float | None]:
    """{"brokerage": ..., "taxes_charges": ...} from a row's own recorded columns,
    or None for either that's missing/blank - rows recorded before brokerage_amount/
    taxes_charges_amount were tracked (see config/selectors.yaml) simply won't have
    them, and compute_portfolio_metrics_weighted_with_charges treats that as "no
    charge adjustment for this combo" rather than guessing."""

    def _num(key: str) -> float | None:
        raw = row.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return {"brokerage": _num("brokerage_amount"), "taxes_charges": _num("taxes_charges_amount")}


def _recompute_stats_for_window(
    rows: list[dict[str, Any]],
    reports_dir: Path,
    instrument: str,
    *,
    date_from: str | None,
    date_to: str | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """When date_from/date_to narrow the analysis to part of a candidate's full
    downloaded history (e.g. "only since 2026-08-01" out of a year+ of downloaded
    reports), its own aggregate stats - total_pnl, max_drawdown, win_rate,
    return_max_dd, reward_risk_ratio - must reflect ONLY that window, not the
    whole-history numbers already sitting in results_web_*.csv (computed once,
    back when the combo was originally backtested over its full discovered
    range). combined_sort_key/_shortlist (ranking) and _size_lots (drawdown-
    weighted sizing) both read these same fields straight off the row - left
    unfiltered, a combo that was excellent historically but flat since date_from
    would still get shortlisted and sized ahead of one that's specifically
    strong in the window actually being asked about, which defeats the whole
    point of asking "what worked well since date_from."

    Every candidate with a downloaded trade report gets it parsed HERE, once,
    filtered to [date_from, date_to] (inclusive; either bound may be open) - the
    filtered series is returned alongside the rewritten rows so
    _diversify_merged_pool can reuse it instead of re-parsing the same files a
    second time. A candidate with no report yet is passed through unchanged -
    it's excluded downstream by _shortlist's own existence check regardless.

    Also returns the FULL, un-windowed series for the same candidates (parsed
    here anyway before being windowed down - free to keep, no extra I/O) so a
    caller that needs actual download freshness (staleness, not analysis scope -
    see _stale_picks) can reuse THIS instead of a third independent parse of the
    same files.

    Passed through completely unchanged (byte-for-byte the old behavior, no
    files touched at all) when neither date_from nor date_to is given - this is
    what every existing caller still does; both dicts come back empty in that
    case, since there's nothing to have parsed yet."""
    if date_from is None and date_to is None:
        return rows, {}, {}

    series_cache: dict[str, dict[str, float]] = {}
    full_series_cache: dict[str, dict[str, float]] = {}
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        cid = row["combo_id"]
        path = trade_report_path(reports_dir, row.get("instrument") or instrument, cid)
        if not path.exists():
            out_rows.append(row)
            continue
        full_series = parse_trade_report(path)
        full_series_cache[cid] = full_series
        windowed = {
            d: pnl for d, pnl in full_series.items()
            if (date_from is None or d >= date_from) and (date_to is None or d <= date_to)
        }
        series_cache[cid] = windowed
        metrics = compute_portfolio_metrics([cid], {cid: windowed})
        out_rows.append({
            **row,
            "total_pnl": metrics["overall_profit"],
            "max_drawdown": metrics["max_drawdown"],
            "win_rate": metrics["win_pct"],
            "return_max_dd": metrics["return_max_dd"],
            "reward_risk_ratio": metrics["reward_risk_ratio"],
        })
    return out_rows, series_cache, full_series_cache


def _shortlist(candidates: list[dict[str, Any]], reports_dir: Path, instrument: str, top_n: int) -> list[dict[str, Any]]:
    """This bucket's own candidates with a real downloaded trade report (only those
    can be correlated) AND a hard Stop Loss (leg-level or overall - see
    has_hard_stop_loss), gated by profitability then ranked by combined_sort_key,
    kept to the top_n - BEFORE any cross-bucket comparison, so this bucket always
    has a fair-sized pool in contention regardless of how its scores compare to
    other sessions'."""
    eligible = [
        r for r in candidates
        if trade_report_path(reports_dir, instrument, r["combo_id"]).exists() and has_hard_stop_loss(r)
    ]
    gated = profitability_gated_key(combined_sort_key(eligible))
    eligible.sort(key=gated, reverse=True)
    return eligible[:top_n]


def _diversify_merged_pool(
    shortlisted: list[dict[str, Any]],
    reports_dir: Path,
    instrument: str,
    *,
    threshold: float,
    series_cache: dict[str, dict[str, float]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, float]]]:
    """ONE diversification pass across every bucket's shortlist merged together -
    the same algorithm the flat single-session basket uses, so a pick is only kept
    if it's not correlated with any higher-ranked pick regardless of which bucket
    that pick eventually lands in. Returns (basket, row_by_id, series).

    `series_cache` reuses whatever _recompute_stats_for_window already parsed and
    date-filtered for a candidate (keyed by combo_id) instead of re-reading its
    trade report from disk a second time - checked with `in`, not truthiness,
    since a candidate can legitimately have zero trades inside the window (an
    empty-but-cached dict), which must NOT fall through to a fresh unfiltered
    parse. Left unset (the default), every series is parsed fresh - unchanged
    behavior for every caller that isn't date-windowing."""
    series_cache = series_cache or {}
    series = {}
    for r in shortlisted:
        cid = r["combo_id"]
        series[cid] = series_cache[cid] if cid in series_cache else parse_trade_report(trade_report_path(reports_dir, instrument, cid))
    matrix = correlation_matrix(series)
    row_by_id = {r["combo_id"]: r for r in shortlisted}
    score_key = combined_sort_key(shortlisted)
    ranked_ids = sorted(row_by_id, key=lambda cid: score_key(row_by_id[cid]), reverse=True)
    basket, _skipped = pick_diversified_basket(ranked_ids, matrix, threshold=threshold)
    return basket, row_by_id, series


DEFAULT_MAX_SHARE = 0.4  # no single pick may take more than this fraction of its bucket's budget
DEFAULT_MIN_LOTS = 2.0  # a pick that makes the basket is worth a real position, not a token 1-lot one
DEFAULT_MAX_LOTS: float | None = None  # optional absolute cap, tighter of this and max_share applies


def _size_lots(
    basket: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    budget: float,
    *,
    max_share: float = DEFAULT_MAX_SHARE,
    min_lots: float = DEFAULT_MIN_LOTS,
    max_lots: float | None = DEFAULT_MAX_LOTS,
) -> tuple[list[float], float, int]:
    """Inverse-|max_drawdown| weighting, water-filled against bounds so the result is
    genuinely spread out rather than nominally diversified:
      - no single pick may exceed max_share of the bucket's budget, OR max_lots in
        absolute terms if given (whichever is tighter) - otherwise one strategy
        quietly absorbs the whole bucket (or, without an absolute cap, a small
        bucket's 40% can itself still be a big number - e.g. 40% of 45 is 18).
      - no pick may be sized below min_lots (otherwise a "diversified" pick that
        only gets 1 lot isn't contributing anything real - just noise).
    If max_lots is tight enough that even every pick sitting AT the cap can't reach
    the bucket's full budget, the shortfall is reported back as unallocated rather
    than silently forced onto whichever pick has room - that's a real signal ("add
    more diversified picks to this session, or raise the cap") not something to
    paper over.

    The opposite squeeze - too many picks for the budget to give each one a real
    min_lots (e.g. 5 picks, an 18-lot budget, min_lots=4: 18/5=3.6 lots each) - used
    to be handled by silently shrinking the floor to fit everyone (confirmed live: a
    pick could come back below the configured min_lots with no signal that it had
    happened, worse the tighter the budget got relative to the pick count). Now the
    weakest pick(s) (basket is already ranked best-first) are dropped instead, one
    at a time, until the ones that remain can each genuinely get min_lots - fewer,
    real positions rather than more, token ones. Returns (lots, unallocated_budget,
    dropped_for_min_lots) - the third only counts picks cut for this reason, not any
    other exclusion.

    budget<=0 is a deliberate "deploy nothing in this bucket" (e.g. Short-morning
    lots set to 0 to hand that session's whole margin to another bucket instead) -
    returns no picks at all rather than falling through to the general water-fill
    logic below, which forces at least 1 lot onto every surviving pick regardless
    of budget (see the two `max(1.0, ...)`/`max(1, ...)` floors further down) -
    confirmed live: a bucket explicitly zeroed out still showed lots assigned to
    it, because that floor doesn't check whether there was ever any real budget
    to begin with."""
    if not basket or budget <= 0:
        return [], max(0.0, budget), 0

    dropped_for_min_lots = 0
    while len(basket) > 1 and budget / len(basket) < min_lots:
        basket = basket[:-1]
        dropped_for_min_lots += 1
    n = len(basket)

    share_cap = max_share * budget
    cap = share_cap if max_lots is None else min(share_cap, max_lots)
    floor = min(min_lots, budget / n)
    cap = max(cap, floor)  # a cap tighter than the floor would be self-contradictory

    if n == 1:
        lots0 = min(budget, cap)
        return [max(1.0, round(lots0))], budget - lots0, dropped_for_min_lots

    weights = []
    for b in basket:
        row = row_by_id[b["combo_id"]]
        try:
            dd = abs(float(row.get("max_drawdown") or 1.0)) or 1.0
        except (TypeError, ValueError):
            dd = 1.0
        weights.append(1.0 / dd)

    lots: list[float | None] = [None] * n
    fixed_total = 0.0
    free_idx = list(range(n))
    for _ in range(n):
        if not free_idx:
            break
        free_w = sum(weights[i] for i in free_idx) or 1.0
        remaining = budget - fixed_total
        clamped = False
        for i in list(free_idx):
            share = remaining * weights[i] / free_w
            bound = cap if share > cap else (floor if share < floor else None)
            if bound is not None:
                lots[i] = bound
                fixed_total += bound
                free_idx.remove(i)
                clamped = True
        if not clamped:
            free_w = sum(weights[i] for i in free_idx) or 1.0
            remaining = budget - fixed_total
            for i in free_idx:
                lots[i] = remaining * weights[i] / free_w
            break
    else:
        # Every pick got clamped (typically all at cap) with picks still left over
        # in free_idx on the final pass, or budget was never fully spoken for -
        # whatever's unassigned stays that way rather than being forced past a bound.
        for i in free_idx:
            lots[i] = 0.0

    rounded = [round(x or 0.0) for x in lots]

    # Absorb whatever's left (usually just rounding drift; occasionally a genuine
    # cap-driven shortfall) into whoever has headroom against the bound it would
    # otherwise breach - dumping it onto the single largest pick would silently
    # re-inflate a just-capped pick right back toward the dominance the cap exists
    # to prevent. Whatever can't be placed within bounds stays unallocated.
    diff = int(round(budget - sum(rounded)))
    step = 1 if diff > 0 else -1
    remaining_diff = abs(diff)
    guard = 0
    while remaining_diff > 0 and guard < 10 * n + 10:
        guard += 1
        eligible = [i for i in range(n) if (rounded[i] < cap if step > 0 else rounded[i] > floor)]
        if not eligible:
            break  # genuinely nowhere left to put it within bounds - leave as unallocated
        idx = max(eligible, key=lambda i: weights[i]) if step > 0 else min(eligible, key=lambda i: weights[i])
        rounded[idx] += step
        remaining_diff -= 1
    unallocated = max(0.0, budget - sum(rounded))
    return [max(1, x) for x in rounded], unallocated, dropped_for_min_lots


def diversify_for_grid(
    rows: list[dict[str, Any]],
    reports_dir: Path,
    instrument: str,
    *,
    threshold: float,
    top_n: int,
    bucket_order: list[str] | None = None,
    classify_fn: Callable[[dict[str, Any]], str | None] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """The threshold/top_n-dependent half of build_portfolio - shortlisting,
    correlation, diversification, and the midday-margin-timing filter - split out
    from the budgets/min_lots/max_lots-dependent half (size_and_summarize) below
    so a parameter sweep across many (min_lots, max_lots, budget) combinations
    that all share the same (threshold, top_n) only pays this part - by far the
    expensive one, since it parses every candidate's trade report from disk and
    builds a full correlation matrix - once per (threshold, top_n), not once per
    grid point. See PortfolioSweepState._run, which groups its grid this way.

    `bucket_order`/`classify_fn` default to the regular day-session buckets
    (BUCKET_ORDER/classify_bucket) - passed unset, behavior is byte-for-byte
    identical to before. CAS regime analysis passes its own time-slice buckets
    instead (see src/web/regime_state.py's cas_slot_buckets), since every CAS
    candidate's entry_time falls in the same narrow window and would otherwise
    all classify into one "afternoon" bucket, defeating the whole point of
    per-bucket shortlisting.

    `date_from`/`date_to` (ISO "YYYY-MM-DD", either bound optional) restrict the
    ENTIRE analysis - ranking, correlation, and the final combined metrics - to
    trades inside that window, e.g. "only since 2026-08-01" out of a much longer
    downloaded history. See _recompute_stats_for_window for why this has to
    happen before shortlisting, not just on the final combined series. Left
    unset (the default), behavior is unchanged - no report is parsed here at
    all, the whole-history stats already on each row are used as before."""
    order = bucket_order or BUCKET_ORDER
    classify = classify_fn or classify_bucket
    rows, series_cache, full_series_cache = _recompute_stats_for_window(
        rows, reports_dir, instrument, date_from=date_from, date_to=date_to
    )
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in order}
    for row in rows:
        b = classify(row)
        if b in grouped:
            grouped[b].append(row)

    # Shortlist each bucket on its own first (guarantees every session a fair-sized
    # pool in contention), THEN one diversification pass across the merged shortlist
    # so cross-bucket correlation still gets checked - see the module docstring.
    shortlisted = [r for name in order for r in _shortlist(grouped[name], reports_dir, instrument, top_n)]
    gaps = data_coverage_gaps(shortlisted, reports_dir, instrument)
    global_basket, row_by_id, series = _diversify_merged_pool(
        shortlisted, reports_dir, instrument, threshold=threshold, series_cache=series_cache
    )

    picks_by_bucket: dict[str, list[dict[str, Any]]] = {name: [] for name in order}
    for b in global_basket:
        row = row_by_id[b["combo_id"]]
        name = classify(row)
        if name in picks_by_bucket:
            picks_by_bucket[name].append(b)

    # Can't reuse margin that isn't free yet - drop any midday pick entering before
    # the chosen short_morning picks' own latest exit (nothing to reassign it to;
    # it just doesn't fit this plan, so it's dropped rather than swapped in for
    # something else that might now be more correlated with what's already picked).
    # Only meaningful for the regular day-session buckets - CAS time-slice buckets
    # (or any other custom bucket_order) simply don't have these names, so this
    # step is a no-op for them rather than a KeyError.
    dropped_for_timing = 0
    if "short_morning" in picks_by_bucket and "midday" in picks_by_bucket:
        sm_exits = [row_by_id[b["combo_id"]]["exit_time"] for b in picks_by_bucket["short_morning"] if row_by_id[b["combo_id"]].get("exit_time")]
        midday_min_entry = max(sm_exits) if sm_exits else None
        if midday_min_entry:
            kept = []
            for b in picks_by_bucket["midday"]:
                if (row_by_id[b["combo_id"]].get("entry_time") or "") >= midday_min_entry:
                    kept.append(b)
                else:
                    dropped_for_timing += 1
            picks_by_bucket["midday"] = kept

    return {
        "bucket_order": order,
        "grouped": grouped,
        "picks_by_bucket": picks_by_bucket,
        # The single globally-ranked (best-first), already-diversified list BEFORE
        # it gets split out by bucket - needed by size_and_summarize's
        # overall_budget mode, which pools every bucket's picks back into one list
        # and sizes them together against one shared cap instead of per-bucket
        # budgets. Kept in original rank order deliberately: _size_lots's own
        # weakest-dropped-first logic assumes whatever it's given is already
        # ranked best-first.
        "global_basket": global_basket,
        "row_by_id": row_by_id,
        "series": series,
        # Empty unless date_from/date_to actually windowed `series` down - in
        # that case, this is the FULL (un-windowed) history for the same
        # candidates, parsed once already by _recompute_stats_for_window and
        # kept for free - see size_and_summarize's own staleness checks, which
        # need real download freshness, not the analysis window's scope.
        "full_series": full_series_cache,
        "gaps": gaps,
        "shortlist_pool_size": len(shortlisted),
        "dropped_for_timing": dropped_for_timing,
    }


DEFAULT_STALE_AFTER_DAYS = 10


def _stale_picks(
    combo_ids: list[str],
    row_by_id: dict[str, dict[str, Any]],
    reports_dir: Path,
    instrument: str,
    *,
    max_age_days: int,
    today: date | None = None,
    full_series_cache: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Which of the FINAL basket picks (not the whole shortlist pool - see
    data_coverage_gaps for that one) have no trade data at all within the last
    `max_age_days`, measured against today's real calendar date - an ABSOLUTE
    freshness check, unlike data_coverage_gaps' own RELATIVE one (which only
    flags a candidate that's behind the rest of its own pool; confirmed live that
    when the entire pool shares the same old ceiling - e.g. everything capped at
    an old end_date from when these combos were first swept - nothing looks
    behind ANYTHING else, so that check reports zero gaps even though the whole
    basket is weeks out of date). This is exactly the blind spot that check has:
    it can't tell "the whole neighborhood is old" from "current," only "which
    house is behind its neighbors."

    Checks each pick's FULL downloaded history, never a date-windowed subset -
    staleness is about how current the DOWNLOAD itself is, which is unrelated to
    any "Only since"/"Only until" scope applied for analysis purposes; a report
    correctly windowed down to last month for analysis is not thereby "stale."
    `full_series_cache`, when given, is checked FIRST (by `in`, not truthiness -
    a candidate can legitimately have an empty series) before falling back to a
    fresh disk parse - lets a caller that already parsed these same reports for
    something else (correlation, date-window recompute) avoid a second full pass
    over potentially thousands of files just for this check. Left unset (the
    default), every report is read fresh, unchanged from before this existed.

    `max_age_days` is intentionally NOT auto-detected from each combo's own
    trading cadence (e.g. a genuinely monthly-expiry strategy might go 25+ real
    days between trades without being behind at all) - that's a hard problem to
    get right in general, so this is a plain, honest, user-set threshold instead:
    "flag anything with no data in the last N days," left for the caller (the
    Portfolio UI) to tune based on what they know about their own strategies'
    frequency, rather than silently guessing and being wrong either direction."""
    today = today or date.today()
    full_series_cache = full_series_cache or {}
    stale: list[dict[str, Any]] = []
    for cid in combo_ids:
        row = row_by_id.get(cid) or {}
        if cid in full_series_cache:
            dates = set(full_series_cache[cid])
        else:
            row_instrument = row.get("instrument") or instrument
            path = trade_report_path(reports_dir, row_instrument, cid)
            if not path.exists():
                continue
            dates = set(parse_trade_report(path))
        if not dates:
            continue
        last_date = max(dates)
        age_days = (today - date.fromisoformat(last_date)).days
        if age_days > max_age_days:
            stale.append({"combo_id": cid, "last_date": last_date, "age_days": age_days})
    stale.sort(key=lambda s: -s["age_days"])
    return stale


def size_and_summarize(
    diversified: dict[str, Any],
    reports_dir: Path,
    instrument: str,
    *,
    threshold: float,
    budgets: dict[str, float] | None = None,
    base_lots: float = BASE_LOTS,
    max_share: float = DEFAULT_MAX_SHARE,
    min_lots: float = DEFAULT_MIN_LOTS,
    max_lots: float | None = DEFAULT_MAX_LOTS,
    overall_budget: float | None = None,
    stale_after_days: int | None = DEFAULT_STALE_AFTER_DAYS,
    check_stale_pool: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """The budgets/min_lots/max_lots-dependent (cheap - no disk I/O, no
    correlation math) half of build_portfolio: lot-sizes whatever
    diversify_for_grid already picked. `threshold` is only echoed into the
    returned dict here, not recomputed against.

    `overall_budget` (left unset, the default - identical to every existing
    caller's behavior) switches from per-bucket budgets to ONE shared pool: every
    bucket's picks are combined back into diversify_for_grid's own global_basket
    (still ranked best-first) and sized ONCE against this single cap, each pick
    still bounded individually by min_lots/max_lots - CAS regime analysis's own
    "Overall lots" mode, for a window where every time-slice draws from the same
    real margin rather than day-sessions' sequential reuse. `budgets` is ignored
    entirely in this mode.

    `stale_after_days` (default 10; None disables the check entirely) flags any
    FINAL pick with no trade data in that many days - see _stale_picks for why
    this exists as a separate, absolute check alongside data_coverage_gaps'
    existing relative one. `check_stale_pool` (default False - opt-in, since it's
    meaningfully more disk I/O) additionally runs the SAME absolute check across
    the ENTIRE shortlisted candidate pool (row_by_id's full key set, hundreds to
    low thousands of combos), not just the handful of final picks - refreshing
    only the final picks fixes THIS basket, but the next "Recompute basket" can
    just surface a different set of stale runners-up once rankings shift after
    that refresh (confirmed live - a real, repetitive whack-a-mole otherwise).
    Refreshing the whole flagged pool once means whoever ends up winning next is
    already current, regardless of how the ranking moves. `today` is injectable
    for tests; real callers leave it unset."""
    bucket_order = diversified.get("bucket_order", BUCKET_ORDER)
    grouped = diversified["grouped"]
    picks_by_bucket = diversified["picks_by_bucket"]
    row_by_id = diversified["row_by_id"]
    series = diversified["series"]

    lots_by_cid: dict[str, float] = {}
    if overall_budget is not None:
        global_basket = diversified.get("global_basket", [])
        lots, pooled_unallocated, pooled_dropped = _size_lots(
            global_basket, row_by_id, overall_budget, max_share=max_share, min_lots=min_lots, max_lots=max_lots
        )
        # zip stops at the shorter of the two - _size_lots may have trimmed the
        # weakest picks off the tail of global_basket (its own already-ranked
        # order), so this naturally keeps only the survivors, same convention as
        # the per-bucket branch below.
        lots_by_cid = {b["combo_id"]: lot for b, lot in zip(global_basket, lots)}
    else:
        pooled_unallocated = pooled_dropped = None

    out_buckets: dict[str, Any] = {}
    all_series: dict[str, dict[str, float]] = {}
    charges_by_cid: dict[str, dict[str, float | None]] = {}
    resolved_budgets = {} if overall_budget is not None else {**DEFAULT_BUDGETS, **(budgets or {})}
    for name in bucket_order:
        basket = picks_by_bucket[name]
        if overall_budget is not None:
            # Already sized above, pooled across every bucket - just pick out
            # this bucket's own survivors (in their original rank order) rather
            # than sizing them again against a per-bucket budget.
            basket = [b for b in basket if b["combo_id"] in lots_by_cid]
            lots = [lots_by_cid[b["combo_id"]] for b in basket]
            unallocated = dropped_for_min_lots = 0.0
        else:
            lots, unallocated, dropped_for_min_lots = _size_lots(
                basket, row_by_id, resolved_budgets.get(name, 0.0), max_share=max_share, min_lots=min_lots, max_lots=max_lots
            )
        members = []
        for b, lot in zip(basket, lots):
            row = row_by_id[b["combo_id"]]
            cid = b["combo_id"]
            lots_by_cid[cid] = lot
            all_series[cid] = series[cid]
            charges_by_cid[cid] = charges_from_row(row)
            members.append(
                {
                    "combo_id": cid,
                    "entry_time": row.get("entry_time", ""),
                    "exit_time": row.get("exit_time", ""),
                    "lots": lot,
                    "max_corr_to_basket": b["max_corr_to_basket"],
                    "return_max_dd": row.get("return_max_dd"),
                    "total_pnl": row.get("total_pnl"),
                    "max_drawdown": row.get("max_drawdown"),
                    "win_rate": row.get("win_rate"),
                }
            )
        with_report_rows = [r for r in grouped[name] if trade_report_path(reports_dir, instrument, r["combo_id"]).exists()]
        out_buckets[name] = {
            # None in pooled mode - there's no single-bucket budget to show, only
            # the one shared "overall_budget" at the top level below.
            "budget": None if overall_budget is not None else resolved_budgets.get(name, 0.0),
            "total_candidates": len(grouped[name]),
            "with_report": len(with_report_rows),
            # Downloaded but excluded outright for having no hard Stop Loss at all
            # (leg-level or overall) - never ranked, regardless of how good their
            # other numbers look. See has_hard_stop_loss.
            "excluded_no_stop_loss": sum(1 for r in with_report_rows if not has_hard_stop_loss(r)),
            "members": members,
            # >0 only when max_lots/max_share made the budget genuinely impossible to
            # fully deploy across the picks available (e.g. a 7-lot cap across only 4
            # picks can't reach a 45-lot budget) - a real "add more diversified picks
            # to this session, or raise the cap" signal, not silently absorbed. Always
            # 0 per-bucket in pooled mode - see overall_unallocated_lots below instead.
            "unallocated_lots": unallocated,
            # >0 only when there were more diversified picks than this budget could
            # give a genuine min_lots each - the weakest ones were dropped rather
            # than everyone being quietly sized below the configured floor. Always 0
            # per-bucket in pooled mode - see overall_dropped_for_min_lots below.
            "dropped_for_min_lots": dropped_for_min_lots,
        }

    portfolio = compute_portfolio_metrics_weighted_with_charges(lots_by_cid, all_series, charges_by_cid, base_lots=base_lots)
    # A real fingerprint of the data this result was actually computed from - not
    # new I/O, just a min/max scan over the SAME per-date series already parsed
    # for the portfolio metrics above. Confirmed live this session: a basket
    # looked at today can't be reproduced later once trade reports get force-
    # refreshed (same combo_id, same file path, silently different date
    # coverage) - this at least lets a later look tell "was this the same data"
    # instead of having to trust nothing changed in between.
    all_result_dates = {d for combo_series in all_series.values() for d in combo_series}
    data_window_min = min(all_result_dates) if all_result_dates else None
    data_window_max = max(all_result_dates) if all_result_dates else None
    computed_at = datetime.now(timezone.utc).isoformat()
    # `series` is already the FULL (un-windowed) history for every candidate in
    # `row_by_id` UNLESS a date window was applied, in which case it's been
    # truncated for analysis and diversified["full_series"] (parsed once already
    # by _recompute_stats_for_window, never re-read here) is the real thing -
    # `or` correctly picks `series` back up when full_series is the empty dict
    # (no window was applied, so series was never truncated in the first place).
    # Reusing either one instead of a fresh per-candidate disk parse is what
    # makes stale_pool_picks below viable at pool sizes in the thousands -
    # confirmed live, the naive re-parse-everything version timed out entirely
    # on a ~2,000-candidate pool that had grown to include much larger report
    # files than usual.
    staleness_series = diversified.get("full_series") or series
    stale_picks = (
        _stale_picks(
            list(lots_by_cid), row_by_id, reports_dir, instrument,
            max_age_days=stale_after_days, today=today, full_series_cache=staleness_series,
        )
        if stale_after_days is not None
        else []
    )
    # Opt-in, whole-pool version of the same check - see this function's own
    # docstring for why the final-picks-only version above can't fully replace
    # it. row_by_id's full key set IS the shortlisted pool (every candidate that
    # survived per-bucket shortlisting, before diversification/sizing) - no
    # extra data needed, just a bigger combo_ids list through the same helper.
    stale_pool_picks = (
        _stale_picks(
            list(row_by_id), row_by_id, reports_dir, instrument,
            max_age_days=stale_after_days, today=today, full_series_cache=staleness_series,
        )
        if check_stale_pool and stale_after_days is not None
        else []
    )
    return {
        "buckets": out_buckets,
        "portfolio": portfolio,
        "overall_budget": overall_budget,
        "overall_unallocated_lots": pooled_unallocated,
        "overall_dropped_for_min_lots": pooled_dropped,
        # Only the buckets actually in play, not every key resolved_budgets happens
        # to carry - {**DEFAULT_BUDGETS, **(budgets or {})} keeps DEFAULT_BUDGETS'
        # 4 session names even when bucket_order is a completely different key set
        # (e.g. CAS time-slices), so summing resolved_budgets.values() directly
        # would silently add in 4 unused, never-allocated-to budgets on top of the
        # real total. In pooled mode, overall_budget itself already IS the total.
        "total_lots": overall_budget if overall_budget is not None else sum(resolved_budgets.get(name, 0.0) for name in bucket_order),
        "threshold": threshold,
        "shortlist_pool_size": diversified["shortlist_pool_size"],
        "dropped_for_timing": diversified["dropped_for_timing"],
        # Relative staleness - a candidate behind the REST OF ITS OWN SHORTLIST
        # POOL (thousands of candidates, before diversification). Misses the case
        # where the whole pool shares one old ceiling - see stale_picks below for
        # that.
        "data_gaps": diversified["gaps"],
        # Absolute staleness - just the FINAL picks (a handful), checked against
        # today's real date regardless of how the rest of the pool looks. This is
        # the one that actually answers "is what I'm about to trade current."
        "stale_picks": stale_picks,
        "stale_after_days": stale_after_days,
        # Empty whenever check_stale_pool wasn't requested - not "the pool has no
        # stale candidates," just "nobody asked." See size_and_summarize's own
        # docstring for what this catches that stale_picks above can't.
        "stale_pool_picks": stale_pool_picks,
        # See the comment above where these are computed - a real record of what
        # this result was actually built from, not just when it was requested.
        "computed_at": computed_at,
        "data_window_min": data_window_min,
        "data_window_max": data_window_max,
    }


def build_portfolio(
    rows: list[dict[str, Any]],
    reports_dir: Path,
    instrument: str,
    *,
    threshold: float = 0.5,
    top_n: int = DEFAULT_TOP_N,
    budgets: dict[str, float] | None = None,
    base_lots: float = BASE_LOTS,
    max_share: float = DEFAULT_MAX_SHARE,
    min_lots: float = DEFAULT_MIN_LOTS,
    max_lots: float | None = DEFAULT_MAX_LOTS,
    bucket_order: list[str] | None = None,
    classify_fn: Callable[[dict[str, Any]], str | None] | None = None,
    overall_budget: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    stale_after_days: int | None = DEFAULT_STALE_AFTER_DAYS,
    check_stale_pool: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """One session's basket, split into four time-of-day buckets and lot-sized
    within a real margin budget - see the module docstring for the full algorithm.
    Just chains diversify_for_grid (threshold/top_n) into size_and_summarize
    (budgets/min_lots/max_lots) - kept as one call for every existing caller (the
    single "Recompute basket" endpoint); a parameter sweep calls the two halves
    separately instead so it can reuse the expensive half across grid points that
    share a (threshold, top_n) - see PortfolioSweepState._run.

    `bucket_order`/`classify_fn` - see diversify_for_grid; left unset, behavior is
    unchanged from before either param existed. `overall_budget` - see
    size_and_summarize; also unset by default. `date_from`/`date_to` - see
    diversify_for_grid; also unset by default. `stale_after_days`/
    `check_stale_pool`/`today` - see size_and_summarize/_stale_picks."""
    diversified = diversify_for_grid(
        rows, reports_dir, instrument, threshold=threshold, top_n=top_n,
        bucket_order=bucket_order, classify_fn=classify_fn,
        date_from=date_from, date_to=date_to,
    )
    return size_and_summarize(
        diversified, reports_dir, instrument, threshold=threshold, budgets=budgets,
        base_lots=base_lots, max_share=max_share, min_lots=min_lots, max_lots=max_lots,
        overall_budget=overall_budget, stale_after_days=stale_after_days,
        check_stale_pool=check_stale_pool, today=today,
    )
