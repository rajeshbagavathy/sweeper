# AlgoTest UI Backtest Sweeper

Local Python + Playwright tool that automates options-strategy backtesting on
algotest.in through the visible UI, sweeps a parameter grid, and writes results to
CSV. AlgoTest has no backtesting API, so this drives the rendered UI directly.
A second page (Analyze Results) then works across any set of accumulated result
CSVs: filtering/breakdown, uncorrelated-strategy basket building, multi-session
portfolio construction, and CAS regime analysis. See `CLAUDE.md` for the fuller
architecture/feature map and established conventions.

## Setup

```bash
uv venv
uv sync
uv run playwright install chromium
cp .env.example .env   # then fill in your AlgoTest email/password
```

## Option A: Web UI (recommended)

```bash
uv run python webapp.py            # http://127.0.0.1:8765
```

Set each parameter as a range (min/max/step, or a time range + interval), click
**Preview combinations** to see the resulting grid size, then **Start** to run it -
progress and results appear live in the page. Supports the ATM/OTM/ITM offset strike
mode as well as the newer premium-range and closest-premium modes, per-leg risk
(target/Stop Loss on either a premium-% or Underlying-% basis/Trail SL/momentum
entry/re-entry-on-SL including Lazy Leg), and overall Stop Loss/Target/Trailing.
Bound to `127.0.0.1` only; never expose this externally.

Your config is saved to `config/sweep_ui.yaml`. This is separate from
`config/sweep.yaml` below - the CLI and the web UI don't share a config file, since the
web UI's per-leg range sweeping doesn't map onto the CLI's simpler fixed-legs shape.

### Analyze Results page (`/analyze.html`)

Load any combination of past `results_web_*.csv` files (or `output/combo_registry.csv`)
and work across them:

- **Results table** - sort/filter (instrument, DTE, entry/exit time, backtest-date
  range, combo search), coverage heatmaps, timing coverage, and per-parameter
  performance breakdowns.
- **Uncorrelated strategies** - download trade reports for the current top-N and
  build a single correlation-filtered, diversified basket.
- **Portfolio: multi-session capital reuse** - the same diversification, split
  across four time-of-day buckets (short-morning/long-morning/midday/afternoon) and
  lot-sized within a real per-bucket margin budget, modeling reusing the morning's
  margin twice. Includes a parameter sweep (threshold/top_n/min_lots/max_lots grid)
  and saved favorites.
- **CAS regime analysis** - the same portfolio machinery scoped to a downloaded
  CAS (post-cutoff) time window.
- **Create CAS subset** - slice existing results to on/after a cutoff date
  (default 2026-08-01), safe to re-run anytime without producing duplicates.

Both pages support saving/resuming named executions and "Save basket in AlgoTest",
which replays a chosen combo (or basket) back through the live AlgoTest UI.

## Option B: CLI

```bash
python run.py --dry-run                 # print combination count + first 10, run nothing
python run.py                            # full sweep, headed
python run.py --headless --delay 3
python run.py --limit 5                  # smoke test
python run.py --resume output/results_20260822_1030.csv
python run.py --only-failed <csv>        # retry just the error rows
```

Edit `config/sweep.yaml` directly to set the parameter grid (fixed values + explicit
`vary` lists). `--dry-run` first is the recommended way to see the combination count
before committing to a multi-hour run.

## Selector discovery (`tools/record.py`)

`config/selectors.yaml` is already filled in from a live DOM inspection of
algotest.in. If AlgoTest changes its UI and something breaks:

```bash
uv run python tools/record.py
```

This opens a headed Chromium window (persistent profile in `.browser-profile/`, so
your login sticks between runs), makes a best-effort login attempt from `.env`, then
pauses in the Playwright Inspector so you can use "Pick locator" to find the new
selector and update `config/selectors.yaml` by hand.

## Known follow-ups

- `login.email_input`/`password_input`/`submit_button` are still blank - fine as long
  as `.browser-profile`'s session stays alive, but there's no auto re-login if it
  expires mid-sweep.
- `results.error_marker` has one confirmed false-positive fixed (Next.js's route
  announcer) but hasn't been checked against a genuine AlgoTest error yet.
- `builder.reentry_count`/`reentry_type` are unexercised - not wired into the web UI or
  the default `sweep.yaml`.
- `config/sweep_ui.yaml`'s saved Underlying % Stop Loss range (15-20) predates the
  1% sanity cap and now silently contributes zero combos to any sweep - re-enter a
  sane value (e.g. 0.14-0.25) through the UI.
- See `CLAUDE.md`'s "Known stale/open items" for the rest (an old un-cleaned
  DTE-combining data issue).
