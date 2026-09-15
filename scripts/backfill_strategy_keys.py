"""One-time backfill: compute strategy_key for every combo_registry.csv row that
predates it - see the "detect and capture known-duplicate strategies" plan
(/Users/rajeshkumarbagavathy/.claude/plans/enchanted-pondering-raccoon.md) for
the full design this belongs to.

Every row swept AFTER src/runner.py's _build_row fix already carries a correct
strategy_key, computed directly from the live combo dict at write-time - no
reconstruction, no risk. For rows swept BEFORE that fix, the only way to get a
strategy_key is to reconstruct the nested combo dict from the row via
src/web/combo_launcher.py's row_to_combo() and hash that - but row_to_combo was
confirmed live this session to NOT reliably reconstruct every historical combo
shape (e.g. an "ATM" bare-string strike mode round-trips to a different dict
entirely; measured 90.4% overall reliable, 98.7% for SENSEX specifically, 80.1%
for NIFTY, 100% for BANKNIFTY/MIDCPNIFTY across the real registry).

So every backfilled row is SELF-VERIFIED before being trusted: reconstruct the
combo, recompute combo_id() from it, and check it matches the row's own
already-known combo_id (stripping any "_dteN" suffix first, since DTE is a
naming layer applied after hashing, never part of the hash itself). Only a
verified row gets its strategy_key written; an unverified one is left blank
rather than silently trusted - it simply won't participate in duplicate
detection (fails safely open: worst case, occasionally re-downloads something
already known, never corrupts anything).

Usage:
    uv run python scripts/backfill_strategy_keys.py              # dry run - report only, nothing written
    uv run python scripts/backfill_strategy_keys.py --apply      # write verified strategy_keys into the registry
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from src.store import combo_id, strategy_key
from src.web import registry
from src.web.combo_launcher import row_to_combo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "output"


def _expected_base_combo_id(row: dict) -> str:
    """A "_dteN"-suffixed combo_id's base hash never included the suffix (see
    runner._capture_individual_dte_reports - the suffix is string-appended
    AFTER combo_id() is computed, never a fresh hash) - strip it before
    comparing against a freshly recomputed combo_id()."""
    return row["combo_id"].split("_dte")[0]


def compute_verified_keys(rows: list[dict]) -> tuple[dict[str, str], Counter]:
    """combo_id -> strategy_key for every row that round-trips correctly, plus a
    per-instrument Counter of (verified, total) pairs for reporting."""
    verified: dict[str, str] = {}
    totals: Counter = Counter()
    oks: Counter = Counter()
    for row in rows:
        cid = row.get("combo_id")
        if not cid:
            continue
        # Already has one (swept after the live-computation fix) - trust it
        # as-is, no reconstruction needed, nothing to verify.
        if row.get("strategy_key"):
            continue
        instrument = row.get("instrument") or "unknown"
        totals[instrument] += 1
        try:
            combo = row_to_combo(row)
        except Exception:
            continue
        if combo_id(combo) != _expected_base_combo_id(row):
            continue
        oks[instrument] += 1
        verified[cid] = strategy_key(combo)
    return verified, Counter({inst: (oks[inst], totals[inst]) for inst in totals})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually write verified strategy_keys (default: dry run, report only)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory (default: ./output)")
    args = parser.parse_args()

    registry.REGISTRY_PATH = args.output_dir / "combo_registry.csv"
    if not registry.REGISTRY_PATH.exists():
        print(f"No registry found at {registry.REGISTRY_PATH} - run scripts/migrate_to_registry.py first.")
        return 1

    rows = list(registry.read_registry().values())
    print(f"{len(rows)} row(s) in the registry.")

    already_has_key = sum(1 for r in rows if r.get("strategy_key"))
    print(f"{already_has_key} already have a strategy_key (swept after the live-computation fix) - left as-is.")

    verified, per_instrument = compute_verified_keys(rows)
    needing = sum(1 for r in rows if not r.get("strategy_key"))
    print(f"{needing} row(s) need backfilling.")
    print(f"{len(verified)} round-trip-verified and will get a strategy_key "
          f"({needing - len(verified)} could not be reliably reconstructed and will be left blank).")
    print()
    print("Per-instrument verification rate:")
    for instrument, (ok, total) in sorted(per_instrument.items(), key=lambda kv: -kv[1][1]):
        pct = 100 * ok / total if total else 0
        print(f"  {instrument:12s} {ok:6d}/{total:6d} ({pct:.1f}%)")

    if not args.apply:
        print("\nDry run only - nothing written. Re-run with --apply to write the verified strategy_keys.")
        return 0

    # One dict, not a linear re-scan of `rows` per verified combo_id (an O(N x M)
    # cost this session already found and fixed once elsewhere - see
    # src/web/app.py's redownload_correlate_gaps).
    rows_by_id = {r["combo_id"]: r for r in rows}
    updates = {cid: {**rows_by_id[cid], "strategy_key": key} for cid, key in verified.items()}
    registry.upsert_rows(updates)
    print(f"\nWrote strategy_key for {len(updates)} row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
