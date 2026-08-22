"""AlgoTest UI backtest sweeper - CLI entrypoint.

    python run.py --dry-run                 # print combination count + first 10, run nothing
    python run.py                            # full sweep, headed
    python run.py --headless --delay 3
    python run.py --limit 5                  # smoke test
    python run.py --resume output/results_20260822_1030.csv
    python run.py --only-failed <csv>        # retry just the error rows
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src import browser, store
from src.auth import LoginNotConfigured, is_logged_in
from src.config import load_selectors, load_sweep
from src.runner import run_sweep
from src.sweep import expand

ROOT = Path(__file__).resolve().parent
SELECTORS_PATH = ROOT / "config" / "selectors.yaml"
SWEEP_PATH = ROOT / "config" / "sweep.yaml"
OUTPUT_DIR = ROOT / "output"
LOG_PATH = OUTPUT_DIR / "run.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print combination count + first 10, run nothing")
    parser.add_argument("--headless", action="store_true", help="run Chromium headless (default: headed)")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds to wait between runs (default: 2)")
    parser.add_argument("--limit", type=int, default=None, help="cap total combinations (overrides sweep.yaml limit)")
    parser.add_argument("--resume", metavar="CSV", help="continue appending into an existing results CSV")
    parser.add_argument("--only-failed", metavar="CSV", help="retry only the combos marked status=error in CSV")
    parser.add_argument("--result-timeout", type=int, default=180, help="seconds to wait for one backtest result (default: 180)")
    parser.add_argument("--max-retries", type=int, default=2, help="retries per combo before giving up (default: 2)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv()

    sweep_config = load_sweep(SWEEP_PATH)
    if args.limit is not None:
        sweep_config.limit = args.limit
    combos = expand(sweep_config)

    if args.dry_run:
        print(f"{len(combos)} combination(s) after exclude/shuffle/limit.\n")
        for combo in combos[:10]:
            print(combo)
        return 0

    selectors = load_selectors(SELECTORS_PATH)

    if args.resume:
        csv_path = Path(args.resume)
    elif args.only_failed:
        csv_path = Path(args.only_failed)
    else:
        OUTPUT_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = OUTPUT_DIR / f"results_{timestamp}.csv"

    existing_statuses, existing_header = store.load_existing(csv_path)
    metric_names = list(selectors.results.metrics.keys())
    fieldnames = existing_header or store.build_fieldnames(combos, metric_names)

    if args.only_failed:
        failed_ids = {cid for cid, status in existing_statuses.items() if status == "error"}
        from src.store import combo_id as _combo_id

        combos = [c for c in combos if _combo_id(c) in failed_ids]
        print(f"Retrying {len(combos)} previously-failed combination(s).")

    email = os.environ.get("ALGOTEST_EMAIL")
    password = os.environ.get("ALGOTEST_PASSWORD")

    with browser.persistent_context(headless=args.headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(selectors.builder.url)

        try:
            is_logged_in(page, selectors)
        except LoginNotConfigured as exc:
            print(f"Cannot proceed: {exc}", file=sys.stderr)
            return 1

        stats = run_sweep(
            page,
            combos,
            selectors,
            csv_path,
            LOG_PATH,
            fieldnames,
            existing_statuses,
            delay_s=args.delay,
            result_timeout_s=args.result_timeout,
            max_retries=args.max_retries,
            email=email,
            password=password,
        )

    print(f"\nDone. ok={stats['ok']} error={stats['error']} skipped={stats['skipped']}")
    print(f"Results: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
