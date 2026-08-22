# AlgoTest UI Backtest Sweeper

Local Python + Playwright tool that automates options-strategy backtesting on
algotest.in through the visible UI, sweeps a parameter grid, and writes results to
CSV. AlgoTest has no backtesting API, so this drives the rendered UI directly.

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
mode as well as the newer premium-range and closest-premium modes. Bound to
`127.0.0.1` only; never expose this externally.

Your config is saved to `config/sweep_ui.yaml`. This is separate from
`config/sweep.yaml` below - the CLI and the web UI don't share a config file, since the
web UI's per-leg range sweeping doesn't map onto the CLI's simpler fixed-legs shape.

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
