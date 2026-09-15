# AlgoTest UI Sweeper - context for Claude

This file is the map for picking this project back up cold (a new machine, a new
Claude account/session, or just a long gap). The code itself is unusually
heavily commented - almost every non-obvious function has a "why", not just a
"what" - so read this for orientation and the *inventory* of what exists, then
go to the actual file for the reasoning behind any one piece.

## What this is

AlgoTest (algotest.in) has no backtesting API. This tool drives its rendered UI
directly (Playwright) to sweep a parameter grid of options-strategy backtests and
collect results into CSV, then offers a second layer of analysis tools (Analyze
page) on top of accumulated results across many sweeps. Single user, local-only
- the web server binds to `127.0.0.1` and is never meant to be exposed.

Two entry points:
- `uv run python webapp.py` - the web UI (**this is the primary way the user
  works**; the CLI below is effectively legacy). Runs at `127.0.0.1:8765`.
- `python run.py` - an older CLI path, config-file driven (`config/sweep.yaml`).
  Still functional but not where new feature work goes.

## Running things

```bash
.venv/bin/python -m pytest -q          # full suite (~690 tests, ~20s)
.venv/bin/python webapp.py             # start the web server
```
Note: the bare `pytest`/`python` on PATH may resolve to a DIFFERENT Python
(system 3.12 without `fastapi` installed) - always use `.venv/bin/python`
explicitly, or these silently fail with `ModuleNotFoundError`.

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
  `..._cas20260801`), see "CAS subset tooling" below.
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
  at a time) rather than a full expansion to compute this cheaply; see its own
  docstring for the one known structural gap (a field that only appears when
  TWO dimensions land on specific values together needs an explicit combined
  probe, or it silently gets dropped on write).
- **`output/combo_registry.csv`** - the consolidated, deduplicated store all
  older scattered `results_web_*.csv` files were migrated into
  (`scripts/migrate_to_registry.py`). Has both `combo_id` and `strategy_key`.

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

**Underlying % Stop Loss sanity cap** - `MAX_SANE_UNDERLYING_SL_PCT = 1.0` in
`src/web/models.py`. A move THIS LARGE IN THE UNDERLYING (not the option
premium) practically never happens intraday, so a bigger value isn't a real
stop loss. Enforced in exactly one place: `src/web/expand.py`'s
`_stoploss_choices()` silently drops any underlying-basis value above the cap
at combo-GENERATION time (so bad values never get backtested, and the sweep
just doesn't include them - visible as a smaller/zero combo count in Preview,
not an error). Analysis-side, `src/web/portfolio.py`'s `has_hard_stop_loss()`
applies the same cap when deciding whether an EXISTING row (from before this
cap existed) counts as protected - a stored combo whose only SL was an insane
underlying % is treated as having no SL at all, same as any unprotected combo.
**Deliberately NOT a Pydantic validator on the config model** - that was tried
and reverted; the model gets reconstructed from past data everywhere (saved
executions, the persisted `config/sweep_ui.yaml`, "save combo to AlgoTest"),
so rejecting construction broke loading anything saved before the cap existed.
If you're ever tempted to add validation back onto `LegRiskConfig` for
something like this, put it at generation/choice time instead, not on the
model's `__init__`.

**Duplicate-strategy detection** - when a sweep would re-discover a strategy
already in `combo_registry.csv` under a different combo_id (different date
window, same everything else via `strategy_key`), it's captured into
`output/pending_duplicate_refresh.csv` instead of being replayed as a "new"
combo - avoids wasting AlgoTest replay budget re-discovering what could just be
extended via Force re-download. `SweepUIConfig.skip_known_duplicate_strategies`
(default True) is the on/off switch. Full original design rationale is in
`docs/plans/duplicate-strategy-detection.md` (moved into the repo from a
Claude Code session's local plan file, which lived outside version control).

**CAS subset tooling** (`scripts/build_cas_subset.py`, `POST
/api/analyze/cas-subset`) - slices existing trade reports to on/after a cutoff
(default `2026-08-01`), recomputes genuinely-derivable metrics net of prorated
brokerage/taxes, writes new `*_cas_all.csv` files with synthetic
`{combo_id}_cas{cutoff}` ids. Safe to re-run anytime on the full file set - it
tracks what it's already sliced (three separate dedup checks: own start_date,
a registry cross-reference, and a scan of prior `*_cas_all.csv` output) and
never produces duplicates, only genuinely new rows.

**Portfolio: multi-session capital reuse** (`src/web/portfolio.py`) - builds a
diversified (correlation-filtered), lot-sized basket per time-of-day bucket
(short_morning/long_morning/midday/afternoon), modeling reusing the morning's
margin twice. Correlation is computed on DAILY P/L overlap, not entry-time
proximity - two combos minutes apart can both make the basket if they're
structurally different enough to decorrelate day-to-day, which is a known,
not-yet-built-out optimization gap (no explicit entry-time-spacing constraint
today). `iter_shuffled_combos` (`src/sweep.py`) exists for the "coarse
diversified sample across a too-large combo space" use case discussed but not
yet built into the UI - full-space random order, unbiased for any prefix.

**Uncorrelated strategies / CAS regime analysis** - same correlation +
diversification core as Portfolio, single-session (Uncorrelated) or scoped to
a downloaded CAS time-window (`src/web/regime_state.py`).

**Page-level filters -> analysis endpoints** - `date_from`/`date_to` (the
"Backtest date from" range filter, distinct from the exact-match "Backtest
period" pill `date_range`) is wired into `_scope_ok_rows` and from there into
every analyze endpoint that scopes candidate ROWS. Watch for endpoints that
reuse the `date_from`/`date_to` NAME for something else entirely (Portfolio's
own `trade_date_from`/`trade_date_to` restrict the trade-level window inside
an already-selected candidate's report - a different concept that used to
collide with the page filter under the same param name; now split apart on
purpose, don't re-merge them).

## Known stale/open items

- `config/sweep_ui.yaml` (the persisted sweep form) still has an underlying-%
  SL range of 15-20 left over from before the sanity cap existed. It loads
  fine now (see above), but silently contributes zero combos to any sweep
  until someone re-enters a sane value (e.g. 0.14-0.25) through the UI.
- `output/results_web_20260909_122639.csv` has ~7,522 old rows from Sep 9-10
  combining DTE 0+1 into one row (pre-existing, predates the per-DTE capture
  fix) - not cleaned up, `_combo_already_done`-style resume logic won't
  recognize them as already-done for a DTE-split re-sweep.
- No git remote configured - this local repo is the only copy of the commit
  history. Worth adding one if continuity across machines/accounts matters.
- README.md historically undersold this project (described only the bare
  sweep CLI/web-UI, nothing about the Analyze page) - update it alongside this
  file if the feature set moves again.

## Working conventions established across sessions

- **Verify against real data, not assumption** - this project's history
  includes at least one serious bug (lazy-leg metadata mislabeling) that
  shipped because a fix was believed correct without checking production CSVs.
  The now-standard pattern: back up before any destructive edit
  (`.pre-<description>-backup.csv` suffix, deliberately excluded from the
  `results_web_*.csv` glob so it never gets picked up as a data file), verify
  scope precisely before mutating, verify again after, and where possible
  confirm via a live `/api/launch-combo` replay against the user's real
  AlgoTest account rather than trusting derived math alone.
- **AlgoTest UI behavior gets confirmed live, not assumed** - e.g. Trail SL
  silently rejecting "Start Backtest" without its own Stop Loss enabled first,
  or the Re-entry SL dropdown only ever holding one type at a time. Comments
  citing "confirmed live on algotest.in" reflect real observed behavior, not
  documentation AlgoTest publishes anywhere.
- Standard 10-lot sizing is the default assumption for any combo design/testing
  unless told otherwise; scale absolute-rupee SL/Target to match.
- "Auto-download trade report" defaults ON in the sweep UI.
