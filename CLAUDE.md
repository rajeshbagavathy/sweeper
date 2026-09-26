# AlgoTest UI Sweeper - context for Claude

This file is the map for picking this project back up cold (a new machine, a new
Claude account/session, or just a long gap). The code itself is unusually
heavily commented - almost every non-obvious function has a "why", not just a
"what" - so read this for orientation and the *inventory* of what exists, then
go to the actual file for the reasoning behind any one piece.

**This project runs on both macOS and Windows from the same shared codebase -
neither setup is allowed to break the other.** The existing macOS/AlgoTest
workflow is the known-good baseline; anything Windows-only (starting with the
MTQuant integration - a Windows *desktop app*, not a website, so the
AlgoTest/Playwright pattern below does NOT apply to it) must be added as
strictly isolated, additive code: its own module (e.g. `src/mtquant/`), its
own optional dependency group, gated behind a `platform.system()` check at
its own entry point - never a change to a file the macOS setup already
depends on. See `docs/mtquant-integration.md` for the isolation checklist and
where to start, and `docs/windows-migration.md` for running the *existing*
sweep/AlgoTest side of this app on Windows too (already-audited: no source
changes needed there, just a workflow/command translation).

## What this is

AlgoTest (algotest.in) has no backtesting API. This tool drives its rendered UI
directly (Playwright) to sweep a parameter grid of options-strategy backtests and
collect results into CSV, then offers a second layer of analysis tools (Analyze
page) on top of accumulated results across many sweeps. Single user, local-only
- the web server binds to `127.0.0.1` and is never meant to be exposed.

Two entry points:
- `uv run python webapp.py` (or `.venv/bin/python webapp.py`) - the web UI
  (**this is the primary way the user works**; the CLI below is effectively
  legacy). Runs at `127.0.0.1:8765`.
- `python run.py` - an older CLI path, config-file driven (`config/sweep.yaml`).
  Still functional but not where new feature work goes.

## Running things

```bash
.venv/bin/python -m pytest -q          # full suite (~715 tests, ~25s)
.venv/bin/python webapp.py             # start the web server
```
Note: the bare `pytest`/`python` on PATH may resolve to a DIFFERENT Python
(a system install without `fastapi`/`playwright` installed) - always use
`.venv/bin/python` explicitly (`.venv\Scripts\python.exe` on Windows), or these
silently fail with `ModuleNotFoundError`.

**The webapp process must be restarted to pick up Python code changes** -
`webapp.py` runs with `reload=False` deliberately (a stray auto-reload
mid-sweep would be worse than requiring a manual restart). Static files
(`src/web/static/*.html`) ARE served fresh from disk on every request, no
restart needed for those. Config files (`config/*.yaml`) are also read fresh
per-call (`load_selectors`/`load_ui_config` have no caching) - only `.py`
changes need a restart. **Never restart while a sweep is `running`/`stopping`**
- check `GET /api/status` first; restarting mid-sweep loses that run's
in-memory progress (the CSV itself is safe - it's written incrementally -
but you'd need to Resume after restarting anyway, so just wait for it to stop
first).

## Two pages, two jobs

- **`index.html`** ("Sweeper") - configure and run a NEW sweep: instrument,
  date range, entry/exit time grids, legs, leg-level risk (target/SL/trail/
  momentum/re-entry), overall risk, exclude rules, DTE capture. Results stream
  live into a CSV as the sweep runs.
- **`analyze.html`** ("Analyze Results") - everything done AFTER sweeps exist:
  load any combination of past result CSVs, filter/sort/breakdown, and run
  higher-order analysis across them (uncorrelated-strategy baskets, multi-
  session portfolio construction, CAS regime analysis, coverage/timing charts).

## Data model - the load-bearing conventions

- **`combo_id(combo)`** (`src/store.py`) - SHA256 of the ENTIRE combo dict
  (canonicalized), truncated to 12 hex chars. Hashes `start_date`/`end_date`
  too, so the same strategy re-swept with a different date window gets a
  totally different id.
- **`strategy_key(combo)`** (`src/store.py`) - same hash, but with
  `start_date`/`end_date` stripped first. Identifies "the same underlying
  strategy" independent of which date window it was backtested over. Used to
  detect a sweep re-discovering something already in `combo_registry.csv`
  under a different combo_id/date window - see "Duplicate-strategy detection"
  below.
- **DTE variant ids** - `f"{base_cid}_dte{N}"`, a naming suffix applied AFTER
  hashing (DTE is never part of the hash itself). `capture_dte_individually`
  defaults True.
- **CAS-subset synthetic ids** - `f"{base_cid}_cas{cutoff}"` (e.g.
  `..._cas20260801`), see "CAS subset tooling" below. A CAS-derived combo's own
  `start_date` is overwritten to the cutoff and its trade report is genuinely
  SLICED (data before the cutoff is gone) - it is not just relabeled, so it
  can never contribute pre-cutoff history to anything downstream, by design.
- **Union, not cross product** - the single most-repeated convention in this
  codebase. Any sweep dimension with multiple checkable sub-options (Stop Loss
  Percent vs Underlying %; Trail SL Points vs Percentage; Re-entry ASAP vs Cost
  vs Lazy Leg; Momentum up vs down) checks TWO boxes to mean "try either basis
  as a separate alternative", never "combine both at once in one combo". A
  combo lands on exactly one choice per dimension.
- **CSV schema is flat + dynamic** - nested combo dicts get dot-flattened
  (`leg_risk.stoploss_pct.kind`, `legs.0.strike.value`, ...). Column set is
  computed per-sweep from whatever fields actual combos use (`store.
  build_fieldnames`) - a large sweep uses `probe_combos` (varies one dimension
  at a time) rather than a full expansion to compute this cheaply. Its own
  docstring documents every known combined-dimension gap found so far (a
  field that only appears when TWO+ dimensions land on specific values
  together needs an explicit combined probe, or it silently gets dropped on
  write) - each one found so far (Lazy Leg's own attachment, and separately
  its derived strike's dual string-vs-dict shape) was a real, confirmed-live
  data-loss bug before the matching probe was added. If a new leg-risk
  dimension ever gets added, sanity-check whether it interacts with Lazy Leg's
  eligibility or shape the same way.
- **`output/combo_registry.csv`** - the consolidated, deduplicated store all
  older scattered `results_web_*.csv` files were migrated into
  (`scripts/migrate_to_registry.py`). Has both `combo_id` and `strategy_key`.
- **Profitability is a hard prerequisite for "best," never one more weighted
  ingredient** - `src/web/narrow.py`'s `profitability_gated_key` wraps any
  ranking key so a net-losing combo (`total_pnl <= 0`) can never outrank a
  profitable one, no matter how good its Return/MaxDD or Reward:Risk ratio
  looks in isolation (a negative P&L over a negative max_drawdown can read as
  a deceptively decent ratio). Confirmed live as a real gap once: Uncorrelated
  strategies' own basket-ranking was the one path in this codebase that didn't
  gate on this, and a strategy that lost >₹50,000 in its own backtest still
  got ranked into (and saved as part of) a "recommended diversified basket".
  Now hard-EXCLUDED there (not just demoted - `pick_diversified_basket` has no
  size cap, so a demoted loser could still slip in if it happened to be
  uncorrelated enough), surfaced in `excluded_not_profitable`. If you add
  another ranking/basket-building path, check whether it needs this too.

## Feature inventory (what exists, where)

**Lazy Leg re-entry** (`src/lazy_leg.py`, wired via `src/web/expand.py`) - a
third `reentry_sl_types` alternative alongside RE_ASAP/RE_COST (NOT a separate
toggle - it lives inside the same "Re-entry on SL" checkbox group, one choice
per combo, same union convention as everything else). Every lazy-leg parameter
(strike, SL%, trail, momentum) is *derived* from the same leg's own settings +
instrument (`derive_lazy_leg`), never independently swept. Only eligible when
that leg's own Stop Loss is percentage-of-premium (not Underlying %) and
between 25%-60% - outside that, or with no leg SL at all, the leg simply gets
no `lazy_leg` key for that combo (silent, not an error).

**Underlying % Stop Loss rules** (`src/web/models.py`/`src/web/expand.py`) -
three separate, independently-added rules, all enforced at combo-GENERATION
time in `src/web/expand.py`'s `_stoploss_choices()` (silently drops the
combo/value - visible as a smaller combo count in Preview, never an error),
**deliberately never as a Pydantic validator on the config model** - that was
tried and reverted once already: the model gets reconstructed from past data
everywhere (saved executions, the persisted `config/sweep_ui.yaml`, "save
combo to AlgoTest"), so rejecting *construction* broke loading anything saved
under an older, since-tightened rule. If you're ever tempted to add validation
back onto `LegRiskConfig`/`SweepUIConfig` for a new rule like these, put it at
generation/choice time instead of `__init__`.
1. **Sanity cap** - `MAX_SANE_UNDERLYING_SL_PCT = 1.0` (`src/web/models.py`). A
   move this large in the UNDERLYING (not the option premium) practically
   never happens intraday, so a bigger value isn't a real stop loss.
   Analysis-side, `src/web/portfolio.py`'s `has_hard_stop_loss()` applies the
   same cap to EXISTING rows (from before the cap existed) too - a stored
   combo whose only SL was an insane underlying % is treated as unprotected.
2. **Requires a real overall Stop Loss too** - an underlying-basis leg SL
   alone caps nothing about the strategy's actual P&L, only a move in the
   index; only ever generated alongside a real overall Stop Loss now.
3. **Never combined with Simple Momentum** - both key off a move in the
   underlying, so pairing them doesn't compose meaningfully.

**Duplicate-strategy detection** - when a sweep would re-discover a strategy
already in `combo_registry.csv` under a different combo_id (different date
window, same everything else via `strategy_key`), it's captured into
`output/pending_duplicate_refresh.csv` instead of being replayed as a "new"
combo - avoids wasting AlgoTest replay budget re-discovering what could just be
extended via Force re-download. `SweepUIConfig.skip_known_duplicate_strategies`
(default True) is the on/off switch. Full original design rationale is in
`docs/plans/duplicate-strategy-detection.md`.

**CAS subset tooling** (`scripts/build_cas_subset.py`, `POST
/api/analyze/cas-subset`) - slices existing trade reports to on/after a cutoff
(default `2026-08-01`), recomputes genuinely-derivable metrics net of prorated
brokerage/taxes, writes new `*_cas_all.csv` files with synthetic
`{combo_id}_cas{cutoff}` ids. Safe to re-run anytime on the full file set - it
tracks what it's already sliced (three separate dedup checks: own start_date,
a registry cross-reference, and a scan of prior `*_cas_all.csv` output) and
never produces duplicates, only genuinely new rows.

**Portfolio: multi-session capital reuse** (`src/web/portfolio.py`) - builds a
diversified (correlation-filtered), lot-sized basket per time-of-day bucket,
combined into one lot-sized day, modeling reusing the morning's margin twice.
Two bucket schemes, selectable per-request via `bucket_order`/`classify_fn`
(both `build_portfolio`/`diversify_for_grid` args, plumbed through the
Parameter sweep grid too, not just the single-basket endpoint):
- **Regular 4-way** (`BUCKET_ORDER`/`classify_bucket`) -
  short_morning/long_morning/midday/afternoon, each with its own narrow
  entry/exit cutoff.
- **"Very long morning"** (`VERY_LONG_MORNING_BUCKET_ORDER`/
  `classify_very_long_morning_bucket`) - collapses short/long-morning/midday
  into ONE session (any entry before noon held into the afternoon, exit
  >= 2pm). Added because the regular scheme has a real gap: an 11:00-11:59am
  entry held past a 2:15pm exit lands in no bucket at all, and one held to an
  earlier exit lands in "midday" instead of "long_morning" - confirmed live
  against real data (~21% of a stated 9:17am-noon/2-3pm window silently
  unrepresented). UI toggle disables/relabels the regular fields accordingly
  (reuses `long_morning_budget`'s own value/field as this bucket's budget, no
  new field needed).

**Uncorrelated strategies** (single-session, `src/web/correlate_state.py`) /
**CAS regime analysis** (`src/web/regime_state.py`, scoped to a downloaded CAS
time-window) - same correlation + diversification core as Portfolio.

**Correlation/diversification hardening** (`src/correlate.py`) - two rules
`pick_diversified_basket` applies on top of a real correlation coefficient,
both confirmed-live fixes for baskets that looked diversified but weren't:
1. **Exact-duplicate detection** - `correlation_matrix()`: two candidates with
   BYTE-IDENTICAL daily P&L (same trade-dates, same values) are treated as
   correlation 1.0 even when there are too few overlapping days to compute a
   real coefficient (below `MIN_OVERLAP_DAYS = 5`) - "too few points to know"
   is not the same as "we know they're identical."
2. **Same-window tie-break** - `same_window(a, b)` callback (built from
   `(entry_time, exit_time)` equality in both `portfolio.py`'s and
   `correlate_state.py`'s own callers): when correlation is STILL unknown
   (too few overlapping days, and the two series aren't byte-identical, just
   similar), two candidates sharing the exact same entry+exit clock window are
   treated as maximally correlated too, rather than defaulting to "unknown, so
   keep it." A short backtest window can leave correlation unmeasurable for
   almost every pair in a narrow session - without this, the same clock-time
   window kept getting picked into the basket repeatedly.
Both `None` (genuinely unknown, different windows) and a real low coefficient
still behave as before - these only ever narrow the "unknown -> keep it"
default, never override an actual computed number.

**Page-level filters -> analysis endpoints** - `date_from`/`date_to` (the
"Backtest date from" range filter, distinct from the exact-match "Backtest
period" pill `date_range`) is wired into `_scope_ok_rows` and from there into
every analyze endpoint that scopes candidate ROWS, **including both
`/api/correlate/download` and `/api/correlate/compute`** (Uncorrelated
strategies) as of the same fix that added the profitability gate above -
confirmed live this was silently dropped there too (neither endpoint even
declared the param, so FastAPI discarded it despite the frontend always
sending it) - a basket built "since a filtered date" was actually built from
the ENTIRE loaded file. Watch for endpoints that reuse the `date_from`/
`date_to` NAME for something else entirely (Portfolio's own
`trade_date_from`/`trade_date_to` restrict the trade-level window inside an
already-selected candidate's report - a different concept; don't re-merge
them). **If you add a new analyze endpoint that builds a candidate pool from
loaded rows, check it accepts and forwards `date_range`/`date_from`/
`date_to` explicitly - this has now been the actual root cause of two
separate "my filter isn't working" reports.**

**Save basket in AlgoTest** (`src/web/combo_launcher.py`'s `BasketSaveState`,
`POST /api/basket-save/start`) - one shared browser session saves every combo
in the currently-computed basket as a named AlgoTest strategy
(`prefix_comboid_starttime`), live per-row status via polling. Available on
BOTH the Portfolio basket panel and the Uncorrelated strategies basket panel -
literally the same feature/state machine/status-polling, not two
implementations; `lastPortfolioComboIds` (the frontend pointer to "whichever
basket was most recently computed anywhere on the page") gets set by whichever
panel you last computed. This is a web-automation pattern specific to
AlgoTest being a website - it does NOT extend to MTQuant (a Windows desktop
app); see `docs/mtquant-integration.md` for that boundary instead.

## Known stale/open items

- `config/sweep_ui.yaml` (the persisted sweep form) has carried a couple of
  now-invalid leftover ranges across different fixes (an underlying-% SL range
  predating the 1% cap, at one point). It loads fine regardless (see the
  "never a validator" note above) but silently contributes zero combos to any
  sweep until re-entered sane through the UI - if a sweep's Preview count looks
  suspiciously low for a config that "used to work," check this.
- `output/results_web_20260909_122639.csv` has ~7,522 old rows from Sep 9-10
  combining DTE 0+1 into one row (pre-existing, predates the per-DTE capture
  fix) - not cleaned up, `_combo_already_done`-style resume logic won't
  recognize them as already-done for a DTE-split re-sweep.
- No git remote configured - this local repo is the only copy of the commit
  history. Worth adding one now that this project runs from both a MacBook and
  a Windows machine - see `docs/windows-migration.md`.
- Portfolio's correlation is computed on daily P/L overlap only, still no
  explicit entry-time-spacing constraint - two combos minutes apart can both
  make the basket if they're structurally different enough to decorrelate
  day-to-day. Discussed, not yet built.
- `iter_shuffled_combos` (`src/sweep.py`) exists for a "coarse diversified
  sample across a too-large combo space" workflow (full-space random order,
  unbiased for any prefix) but isn't wired into the UI as its own mode yet.

## Working conventions established across sessions

- **Verify against real data, not assumption** - this project's history
  includes several real bugs (lazy-leg metadata mislabeling, a silently
  dropped date filter, a missing profitability gate, a stale login-page
  selector) that would have shipped, or did ship, because a fix looked
  obviously correct without checking it against production data or a live
  replay. The standard pattern: back up before any destructive edit
  (`.pre-<description>-backup.csv` suffix, deliberately excluded from the
  `results_web_*.csv` glob so it never gets picked up as a data file), verify
  scope precisely before mutating, verify again after, and where possible
  confirm via a live `/api/launch-combo` replay against the user's real
  AlgoTest account rather than trusting derived math alone.
- **AlgoTest UI behavior gets confirmed live, not assumed - and re-confirmed
  when something that "just worked" suddenly stops.** Site text/markup
  changes silently under you: `logged_in_marker` was `"text=Credits Available"`
  for a long time, then AlgoTest quietly relabeled it to `"Credits:<amount>"`
  and every login check started returning a false negative - every sweep
  worker across every configured account began failing identically at the
  SAME step (trying to fill a phone/password form that only appears when
  you're actually logged out), and it took a live headless screenshot of the
  actual rendered page (not just re-reading the selector) to see AlgoTest's
  own wording had changed. When a working automation suddenly fails
  uniformly, suspect the SITE changed before suspecting login/credentials/
  environment - screenshot the real page first.
- Standard 10-lot sizing is the default assumption for any combo design/testing
  unless told otherwise; scale absolute-rupee SL/Target to match.
- "Auto-download trade report" defaults ON in the sweep UI.
