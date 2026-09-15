# Detect and capture known-duplicate strategies during a sweep

> Moved into the repo from `~/.claude/plans/enchanted-pondering-raccoon.md` (a
> Claude Code session's local plan file, outside version control) so this
> design rationale travels with the code instead of living only on one
> machine. The feature this describes is implemented - see `CLAUDE.md`'s
> "Duplicate-strategy detection" entry for the current-state summary; this
> file is the original design doc, kept for the full "why".

## Context

Confirmed live this session: `store.combo_id(combo)` hashes a combo's ENTIRE
dict, including `start_date`/`end_date` - an identical strategy differing only
in `end_date` (e.g. "today" vs "today + 1 week") produces a completely
different hash (`89eee228b375` vs `721e71ed77c2`, verified directly). The user
is about to run SENSEX sweeps with a fixed `start_date` (Jan 2025) and a
trailing `end_date` (always "today"), plus DTE 0/1 individually captured. Every
time that sweep re-runs, the SAME underlying strategies will silently get
BRAND NEW, unrelated combo_ids - fragmenting their own history instead of
extending it, and wasting AlgoTest replay budget re-discovering (and
re-downloading) things already sitting in `output/combo_registry.csv` from a
previous run.

The user's own requirement, stated directly: when a sweep encounters a combo
whose underlying strategy (everything except the date range) already exists in
the registry under a different combo_id, **do not replay it as a new
discovery**. Instead, capture it into a queue that feeds the *existing* Force
re-download mechanism (which already correctly extends the *same* combo_id's
data via `roll_date_window`, verified working earlier this session). Must be
stoppable/resumable independently of the main sweep, and once captured, that
strategy must never resurface as a "new" combo in any future sweep either.

Explored and confirmed (this session, via a dedicated Explore pass):
- Exactly **three** skip-decision sites share one identical predicate shape -
  `existing_statuses.get(combo_id(c)) == "ok"` (as an `if/continue`) or `!=
  "ok"` (as a `todo = [...]` filter/generator): `src/runner.py:130`
  (`run_sweep`), `src/runner.py:709` (`run_sweep_multiprocess`, sized list),
  `src/runner.py:730` (`run_sweep_multiprocess`, lazy generator). `src/web/
  state.py`'s two paths (eager list / large-sweep generator) both funnel into
  these same three sites with the same `existing_statuses` dict - one fix
  covers both.
- DTE is **not** part of what `combo_id()` hashes at all - confirmed via
  `src/web/expand.py`'s `to_sweep_config`/`nest_combo` (only `instrument`/
  `start_date`/`end_date` are `fixed`; `dte` never appears in the combo dict)
  and `src/runner.py:339`'s `_capture_individual_dte_reports` (`f"{base_cid}_
  dte{dte}"` - a naming suffix applied AFTER hashing, never a fresh hash). So a
  new `strategy_key()` (same hash, dates excluded) is automatically DTE-
  agnostic the same way `combo_id()` already is - no special DTE handling
  needed in the hash itself.
- `store.py:10-37` - `_canonical_for_hash()` is the existing normalization
  helper to reuse (handles the leg-risk stoploss_pct shape); `combo` dicts have
  literal top-level `start_date`/`end_date` keys ready to `.pop()`.
- `src/web/combo_launcher.py`'s `row_to_combo(row)` already reconstructs a
  nested combo dict from a flattened CSV/registry row - the existing tool
  needed to compute `strategy_key` for already-migrated registry rows.

## Design

### A. `strategy_key(combo)` - `src/store.py`

```python
def strategy_key(combo: dict[str, Any]) -> str:
    stripped = dict(_canonical_for_hash(combo))
    stripped.pop("start_date", None)
    stripped.pop("end_date", None)
    canonical = json.dumps(stripped, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]
```
Mirrors `combo_id()`'s own two-line body exactly, just with the date fields
excluded first. Deliberately not DTE-aware (see Context) - a per-DTE
comparison, if ever needed, would layer `_dteN` on top the same way
`combo_id()`'s own variants do.

### B. Registry gains a `strategy_key` column

- `src/web/state.py`'s and `src/web/correlate_state.py`'s existing
  `registry.upsert_rows(...)` call sites (added earlier this session) compute
  and include `strategy_key` on every row going forward - cheap, the combo dict
  is already in hand at both sites.
- One-off backfill script (`scripts/backfill_strategy_keys.py`, same dry-run/
  `--apply` convention as the other two scripts) for the 88,004 rows already
  migrated: for each registry row, `row_to_combo(row)` -> `strategy_key(combo)`
  -> `registry.upsert_rows({cid: {**row, "strategy_key": key}})`. Verify as a
  built-in sanity check: re-adding the row's own `start_date`/`end_date` back
  onto `row_to_combo`'s output and calling `combo_id()` should reproduce the
  row's own `combo_id` - if it doesn't for some row, `row_to_combo` dropped
  something hash-relevant and that row's `strategy_key` gets flagged rather
  than silently trusted.

### C. Sweep-time interception - capture instead of silent skip/replay

New `SweepUIConfig` field, `skip_known_duplicate_strategies: bool = True` (a
visible, default-ON checkbox on the Sweep Config page - the escape hatch if
anything about this ever needs to be turned off without a code change).

At sweep start (`state.py`'s `start()`, both the eager and large-sweep paths),
when this flag is on: load the registry ONCE, build an in-memory `dict[str,
str]` mapping every known `strategy_key` -> its existing `combo_id` (O(registry
size), once - not a per-combo file read, given sweeps can touch tens of
thousands of candidates). Pass this map into `run_sweep`/`run_sweep_
multiprocess`.

Extend the exact three predicate sites found above with one more condition:
`strategy_key(c) not in known_strategy_keys` (alongside the existing `!= "ok"`
check). A combo whose strategy_key IS found:
- Is **not** replayed (no AlgoTest session spent) and does **not** get a fresh
  combo_id/row in this sweep's own results file at all.
- Is appended, immediately (not buffered to sweep-end - must survive Stop),
  to a new small queue file, `output/pending_duplicate_refresh.csv` (columns:
  `combo_id` [the EXISTING one from the registry], `strategy_key`,
  `detected_at`) - reusing `store.append_row`'s existing single-append
  convention, same as `correlate_state.py`'s `_log_refresh` pattern.
- Counts toward a new `duplicate` stat (alongside `ok`/`error`/`skipped`) so
  the live progress panel shows it distinctly, in real time.

The existing Preview flow gets the identical check (found and wired during
implementation - not yet pinpointed to an exact line, needs locating) so the
count is visible before committing to a run: "4,800 combination(s)... 1,200
already known under a different date range - will be captured for Force
re-download instead. 3,600 will actually be replayed."

### D. Acting on the captured queue

Small new endpoints (`src/web/app.py`), reusing `CorrelateState` wholesale (no
new job-runner class needed):
- `GET /api/duplicate-refresh/pending` - reads `pending_duplicate_refresh.csv`,
  returns the list + count.
- `POST /api/duplicate-refresh/start` - resolves each pending combo_id's
  current row from the registry, calls `correlate_state.start(rows,
  csv_paths=[registry.REGISTRY_PATH], force=True)` (the registry is a valid
  `store.update_rows` target like any other CSV - already proven this session,
  since `correlate_state.py`'s own merge-back already upserts into it). On
  successful *start* (not completion - the job runs in the background exactly
  like today's Force re-download), clears the consumed entries from the
  pending file so they aren't re-queued.

Small UI addition near the existing Force re-download controls: "N
strategies pending force-refresh (found during sweeps) - [Force re-download
these now]" - reuses the existing CorrelateState progress/polling UI already on
the page, just pointed at this new endpoint instead of the regular top-N flow.
Usable regardless of whether the main sweep is running, stopping, or stopped -
the only existing constraint is `CorrelateState` itself not already being busy
(unchanged, already true today).

### E. "Never appears again" is a natural consequence, not a separate feature

Once a duplicate is captured and later force-refreshed, its EXISTING combo_id's
registry row gets its window extended (via `roll_date_window`, unchanged) but
keeps the SAME `strategy_key`. A future sweep's registry lookup (section C)
will keep finding that `strategy_key` match indefinitely - no separate
permanent-exclusion list is needed; the registry itself, re-checked fresh at
the start of every sweep, already guarantees this strategy can never resurface
as a "new" discovery again, in this run or any future one.

## Files touched
- `src/store.py` - `strategy_key()`.
- `src/web/registry.py` or its callers - include `strategy_key` when upserting.
- `scripts/backfill_strategy_keys.py` (new) - one-off backfill for already-
  migrated rows, dry-run/`--apply` like the other two scripts.
- `src/web/models.py` - `SweepUIConfig.skip_known_duplicate_strategies: bool = True`.
- `src/runner.py` - extend the three identified skip-predicate sites; add the
  `duplicate` stat bucket; append to `pending_duplicate_refresh.csv`.
- `src/web/state.py` - build the in-memory strategy_key map once at `start()`,
  thread it through to `run_sweep`/`run_sweep_multiprocess`.
- The existing Preview endpoint (to be located precisely during
  implementation) - same registry check, reported as a count before running.
- `src/web/app.py` - `/api/duplicate-refresh/pending`, `/api/duplicate-refresh/start`.
- `src/web/static/index.html` - the new checkbox, the pending-duplicates panel
  + its "Force re-download these now" button, reusing existing Correlate
  progress-polling UI.
- Tests: `strategy_key()` unit tests (same combo/different dates -> same key;
  different combo/same dates -> different key); the three runner.py skip-site
  extensions (a combo whose strategy_key is pre-seeded as known must not reach
  `run_correlate_multiprocess`/the single-browser replay, and must appear in
  the pending-queue file); the backfill script (mirroring the other two
  scripts' own test style); the new endpoints (pending list + start wiring
  `correlate_state.start` with the registry as `csv_paths`).

## Verification
- `uv run pytest` - all new/updated tests, plus the full existing suite.
- Live browser pass on a disposable temp server instance (never the user's own
  running one): load a small fixture registry with one known strategy_key,
  configure a sweep whose combos include that same strategy (different
  end_date) plus a genuinely new one, confirm Preview reports the duplicate
  count, confirm Start captures it into the pending file without replaying it,
  confirm the new panel shows it and "Force re-download these now" correctly
  starts `CorrelateState` against the registry.
