# AlgoTest UI Backtest Sweeper

Local Python + Playwright tool that automates options-strategy backtesting on
algotest.in through the visible UI, sweeps a parameter grid, and writes results to
CSV. AlgoTest has no backtesting API, so this drives the rendered UI directly.

## Status: Phase 0 (selector discovery)

Only the selector-discovery tool exists so far. The sweeper app itself (Phase 1) is
built next, once `config/selectors.yaml` below is filled in.

## Setup

```bash
uv venv
uv sync
uv run playwright install chromium
cp .env.example .env   # then fill in your AlgoTest email/password
```

## Run the recorder

```bash
uv run python tools/record.py
```

This opens a headed Chromium window (with a persistent profile in
`.browser-profile/`, so your login sticks between runs) and makes a best-effort
attempt to log in from `.env`. Then it pauses in the Playwright Inspector.

From there:

1. Navigate to the backtest / strategy-builder page.
2. Build one representative strategy and run one backtest.
3. Use the Inspector's "Pick locator" element picker to copy the selector for each
   field into `config/selectors.yaml`.
4. Close the Inspector to end the script.
5. Paste the filled-in `config/selectors.yaml` back to continue to Phase 1.
