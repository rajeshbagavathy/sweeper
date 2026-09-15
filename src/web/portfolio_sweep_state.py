from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.web import portfolio


def frange_inclusive(start: float, stop: float, step: float) -> list[float]:
    """[start, start+step, ..., stop], inclusive of stop (up to float rounding) -
    unlike range(), which is exclusive of its stop and int-only. Rounded to 10
    decimal places so e.g. 0.25+0.05*3 comes back as 0.4, not 0.39999999999999997."""
    if step <= 0:
        raise ValueError("step must be positive")
    if stop < start:
        raise ValueError("stop must be >= start")
    n = round((stop - start) / step)
    return [round(start + i * step, 10) for i in range(n + 1)]


# Result entries are {**combo_params, **these} - fixed regardless of which axes
# the grid actually swept, so subtracting them back out of an entry recovers
# exactly the combo_params it was computed from (see _grid_key/_already_done).
_RESULT_METRIC_FIELDS = frozenset({
    "reward_risk_ratio", "overall_profit", "return_max_dd", "num_periods",
    "win_pct", "max_drawdown", "shortlist_pool_size", "total_picks",
    "unallocated_lots", "dropped_for_min_lots",
    # The data-fingerprint fields (see size_and_summarize) - NOT part of a grid
    # point's own identity, same as every other metric field here. Confirmed
    # live: leaving these out broke resume entirely - the reconstructed
    # "already done" key for every stored row picked up these 3 extra fields as
    # if they'd been part of the original combo_params, so it no longer matched
    # the real grid point and every previously-done row looked brand new again.
    "computed_at", "data_window_min", "data_window_max",
})

# A previous run left in one of these statuses has partial results worth keeping -
# "done" doesn't (nothing left to resume) and "idle"/never-run has none to keep.
_RESUMABLE_STATUSES = frozenset({"stopped", "error"})


def _grid_key(params: dict[str, Any]) -> tuple:
    """Order-independent identity for a grid point - two dicts with the same
    key/value pairs produce the same key regardless of what order they were built
    in (build_grid's axes ordering, vs. whatever order an entry's fields ended up
    in)."""
    return tuple(sorted(params.items()))


def build_grid(**axes: list[Any]) -> list[dict[str, Any]]:
    """Cartesian product of named axes, e.g. build_grid(threshold=[0.25, 0.3],
    top_n=[50, 100]) -> [{"threshold": 0.25, "top_n": 50}, {"threshold": 0.25,
    "top_n": 100}, {"threshold": 0.3, "top_n": 50}, {"threshold": 0.3, "top_n":
    100}] - one dict per combination, each passed straight through to
    build_portfolio as kwargs. An empty axis (e.g. min_lots=[] because the caller
    chose not to sweep it) makes the whole grid empty, same as any other empty
    range - the caller is expected to supply every axis it wants swept."""
    combos: list[dict[str, Any]] = [{}]
    for name, values in axes.items():
        combos = [{**c, name: v} for c in combos for v in values]
    return combos


@dataclass
class PortfolioSweepState:
    """Background job: re-run build_portfolio (the same computation "Recompute
    basket" already does, in src/web/portfolio.py) across a grid of parameter
    combinations - no browser automation, no new backtests, just reusing whatever
    trade reports are already downloaded, once per grid point. Lets you scan a
    range instead of hand-testing one combination at a time.

    The Portfolio section's own UI text already warns against chasing the highest
    Reward:Risk by trial and error across threshold/Top N, since with only a few
    dozen overlapping trading days behind it that number swings a lot between
    nearby settings. A grid search is the same risk, automated - this doesn't
    remove that warning, it makes the variance visible: every combination tried is
    shown, not just whichever one happened to score highest."""

    status: str = "idle"  # idle | running | stopping | done | stopped | error
    total: int = 0
    completed: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _stop_event: threading.Event | None = field(default=None, repr=False, compare=False)
    # Whatever identifies "the same underlying data/bucket-definition as last time"
    # (e.g. (reports_dir, bucket_order) for regime_sweep_state, which is reused
    # across different windows/CAS-slice settings) - a resume only reuses previous
    # results when this matches what they were actually computed against;
    # otherwise it starts clean even if the grid itself overlaps. Left at its
    # default () (never changes) for the regular Portfolio sweep, which is always
    # scoped to the one fixed REPORTS_DIR/BUCKET_ORDER - byte-for-byte the old
    # behavior there.
    _last_context: tuple[Any, ...] | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "total": self.total,
                "completed": self.completed,
                "results": list(self.results),
                "message": self.message,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status in ("running", "stopping")

    def start(
        self,
        rows: list[dict[str, Any]],
        reports_dir: Path,
        instrument: str,
        grid: list[dict[str, Any]],
        *,
        context: tuple[Any, ...] = (),
        **portfolio_kwargs: Any,
    ) -> None:
        """`grid` is a list of build_portfolio kwarg-dicts (see build_grid) - one
        entry per combination to try. `portfolio_kwargs` holds whatever's NOT being
        swept (e.g. budgets, max_share) and is passed to every combination
        unchanged.

        Resuming: if the previous run was stopped (or crashed) partway through,
        its results are kept rather than discarded, and any grid point that
        exactly matches one already computed is skipped - only what's actually
        left runs. This only helps when `grid` is the same (or overlaps) the
        previous call's - a Stop followed by Run sweep with the ranges unchanged
        picks up where it left off instead of redoing all 1200 from row one; a
        deliberately different grid just computes whatever in it isn't already
        there, same as it always would."""
        if self.is_running():
            raise RuntimeError("A portfolio sweep is already in progress.")
        if not rows:
            raise ValueError("No rows to sweep - adjust the filters or run a sweep first.")
        if not grid:
            raise ValueError("Empty parameter grid.")

        with self._lock:
            if self._last_context != context:
                # A different window/bucket-definition than whatever produced the
                # results currently sitting here - they're not comparable to this
                # run's grid at all, so start clean rather than risk resuming (or
                # displaying) numbers computed against a different context.
                self.results = []
            if self.status in _RESUMABLE_STATUSES and self.results:
                already_done = {_grid_key({k: v for k, v in r.items() if k not in _RESULT_METRIC_FIELDS}) for r in self.results}
                leftover = [g for g in grid if _grid_key(g) not in already_done]
            else:
                self.results = []
                leftover = list(grid)
            self._last_context = context

            self.status = "running"
            self.total = len(grid)
            self.completed = len(self.results)
            self.message = None
            self._stop_event = threading.Event()

        stop_event = self._stop_event
        thread = threading.Thread(
            target=self._run,
            args=(rows, reports_dir, instrument, leftover, portfolio_kwargs, stop_event),
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()
                if self.status == "running":
                    self.status = "stopping"

    def _run(
        self,
        rows: list[dict[str, Any]],
        reports_dir: Path,
        instrument: str,
        grid: list[dict[str, Any]],
        portfolio_kwargs: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        try:
            # bucket_order/classify_fn/date_from/date_to (see
            # portfolio.diversify_for_grid) only ever apply to the expensive half
            # below, not size_and_summarize (which reads the bucket order back out
            # of `diversified` itself and has no use for a date window) - split out
            # of portfolio_kwargs so they're never forwarded where they'd be an
            # unexpected kwarg. Absent (the regular Portfolio sweep's own case
            # before date-windowing existed), this is a no-op and diversify_for_grid
            # falls back to its defaults.
            _DIVERSIFY_ONLY_KEYS = ("bucket_order", "classify_fn", "date_from", "date_to")
            diversify_kwargs = {k: portfolio_kwargs[k] for k in _DIVERSIFY_ONLY_KEYS if k in portfolio_kwargs}
            size_kwargs_fixed = {k: v for k, v in portfolio_kwargs.items() if k not in _DIVERSIFY_ONLY_KEYS}

            # Grouped by (threshold, top_n): everything else in a grid point
            # (min_lots, max_lots) only affects the cheap lot-sizing half
            # (portfolio.size_and_summarize) - the expensive half
            # (portfolio.diversify_for_grid: parses every candidate's trade
            # report from disk, builds a full correlation matrix) only needs to
            # run once per distinct (threshold, top_n) actually in the grid, not
            # once per grid point. A 1200-point grid sweeping min_lots/max_lots
            # across, say, 32 distinct (threshold, top_n) pairs now does that
            # expensive work 32 times instead of 1200 - this is what was making
            # the sweep slow, not a lack of parallelism.
            by_threshold_top_n: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
            for combo_params in grid:
                key = (combo_params.get("threshold"), combo_params.get("top_n"))
                by_threshold_top_n.setdefault(key, []).append(combo_params)

            for (threshold, top_n), combos in by_threshold_top_n.items():
                if stop_event.is_set():
                    break
                diversified = portfolio.diversify_for_grid(
                    rows, reports_dir, instrument, threshold=threshold, top_n=top_n, **diversify_kwargs
                )
                # No stop_event check inside this inner loop, deliberately: the
                # expensive part (diversify_for_grid above) is already paid for
                # once we're here, and size_and_summarize is cheap - finishing
                # every combo in the group we've already committed to costs
                # little and means Stop never discards work just done. Stop
                # still takes effect promptly, just at the next GROUP boundary
                # (the check above), not mid-group.
                for combo_params in combos:
                    size_kwargs = {k: v for k, v in combo_params.items() if k not in ("threshold", "top_n")}
                    result = portfolio.size_and_summarize(
                        diversified, reports_dir, instrument, threshold=threshold, **size_kwargs, **size_kwargs_fixed,
                        # No sweep result row surfaces stale_picks (there's no
                        # per-row UI for it, unlike the single "Recompute basket"
                        # view) - skip the extra per-pick disk re-parse entirely
                        # rather than paying for it on every one of a sweep's grid
                        # points for nothing.
                        stale_after_days=None,
                    )
                    p = result["portfolio"]
                    entry = {
                        **combo_params,
                        "reward_risk_ratio": p.get("reward_risk_ratio"),
                        "overall_profit": p.get("overall_profit"),
                        "return_max_dd": p.get("return_max_dd"),
                        "num_periods": p.get("num_periods"),
                        "win_pct": p.get("win_pct"),
                        "max_drawdown": p.get("max_drawdown"),
                        "shortlist_pool_size": result.get("shortlist_pool_size"),
                        "total_picks": sum(len(b["members"]) for b in result["buckets"].values()),
                        # In overall_budget ("Overall lots") pooled mode, every bucket's
                        # OWN unallocated_lots/dropped_for_min_lots is hardcoded to 0 -
                        # the real, single, whole-session numbers live at
                        # result["overall_unallocated_lots"]/["overall_dropped_for_min_lots"]
                        # instead (see size_and_summarize) - summing the zeroed
                        # per-bucket values here would silently show "0 unused" even
                        # when a large chunk of the overall cap genuinely went
                        # undeployed (confirmed live: 2 picks capped at Max lots each,
                        # 14 of a 30-lot overall cap unaccounted for, reported as 0).
                        # Falls back to the regular per-bucket sum otherwise, unchanged.
                        "unallocated_lots": (
                            result["overall_unallocated_lots"] if result.get("overall_budget") is not None
                            else sum(b.get("unallocated_lots", 0.0) for b in result["buckets"].values())
                        ),
                        "dropped_for_min_lots": (
                            result["overall_dropped_for_min_lots"] if result.get("overall_budget") is not None
                            else sum(b.get("dropped_for_min_lots", 0) for b in result["buckets"].values())
                        ),
                        # A real fingerprint of the data this specific grid point was
                        # computed from - see size_and_summarize's own comment for why
                        # (reproducibility: a sweep row favorited today can't otherwise
                        # be told apart from one computed against since-refreshed data).
                        "computed_at": result.get("computed_at"),
                        "data_window_min": result.get("data_window_min"),
                        "data_window_max": result.get("data_window_max"),
                    }
                    with self._lock:
                        self.results.append(entry)
                        self.completed += 1

            with self._lock:
                self.status = "stopped" if stop_event.is_set() else "done"

        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI
            with self._lock:
                self.status = "error"
                self.message = str(exc)


portfolio_sweep_state = PortfolioSweepState()
