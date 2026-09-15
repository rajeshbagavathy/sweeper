from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def _canonical_for_hash(combo: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible view of a combo for hashing only - before the leg-level
    Stop Loss dual-basis feature (2026-08-28), leg_risk.stoploss_pct was a bare
    float. It's now {"kind": "percentage"|"underlying_percentage", "value": v} so a
    new "Underlying %" basis can coexist as a distinct dimension - but that shape
    change alone must not silently change the combo_id of every combo still using
    the ordinary percentage-of-premium basis, or resuming ANY older saved execution
    with leg-level Stop Loss enabled looks like starting from scratch (it isn't -
    the combo, and what actually gets submitted to AlgoTest, are identical).
    Collapse the percentage-basis dict back to its bare value for hashing only;
    underlying_percentage - a genuinely new dimension that never existed before -
    keeps its own distinct hash, unaffected."""
    if not isinstance(combo, dict):
        return combo
    leg_risk = combo.get("leg_risk")
    if not isinstance(leg_risk, dict):
        return combo
    stoploss = leg_risk.get("stoploss_pct")
    if not (isinstance(stoploss, dict) and stoploss.get("kind") == "percentage"):
        return combo
    out = dict(combo)
    out["leg_risk"] = {**leg_risk, "stoploss_pct": stoploss["value"]}
    return out


def combo_id(combo: dict[str, Any]) -> str:
    canonical = json.dumps(_canonical_for_hash(combo), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def strategy_key(combo: dict[str, Any]) -> str:
    """Same hash as combo_id(), but with start_date/end_date excluded first -
    identifies the underlying STRATEGY DEFINITION regardless of which calendar
    window it was swept over. Two combos differing only in start_date/end_date
    (e.g. the same strategy re-swept with a trailing "today" end_date) share
    the same strategy_key even though combo_id() gives them completely
    different ids (confirmed live: identical strategy, only end_date differing
    by a week, produced totally unrelated hashes) - this is what lets a sweep
    recognize "I already know this strategy, just under an older date range"
    instead of silently re-discovering (and re-downloading) it as brand new.

    Deliberately NOT DTE-aware, the same way combo_id() itself already isn't -
    dte_values is a sweep-level replay setting, never a key inside the combo
    dict being hashed (see src/web/expand.py's to_sweep_config/nest_combo - the
    combo dict never has a "dte" field at all) - so two DTE choices against the
    identical combo dict already produce the identical combo_id today. A
    "_dteN"-variant comparison, if ever needed, would layer the suffix on top
    of strategy_key the same way runner.py's _capture_individual_dte_reports
    already layers it on top of combo_id, rather than baking DTE into the hash
    here."""
    stripped = dict(_canonical_for_hash(combo))
    stripped.pop("start_date", None)
    stripped.pop("end_date", None)
    canonical = json.dumps(stripped, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def flatten(value: Any, parent_key: str = "") -> dict[str, Any]:
    """Flatten nested dicts/lists into dotted/indexed column names.

    None is kept as a plain scalar leaf (not skipped) so a top-level nullable field
    like target_pct always has a column; a field that's SOMETIMES a dict and
    sometimes None (like trail_sl) just won't contribute its nested sub-columns for
    the None rows - those columns still exist in the CSV (from rows where it wasn't
    None) and are left blank for this row, via append_row's fieldnames normalization.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = f"{parent_key}.{k}" if parent_key else str(k)
            out.update(flatten(v, key))
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            key = f"{parent_key}.{i}" if parent_key else str(i)
            out.update(flatten(v, key))
        return out
    return {parent_key: value}


def build_fieldnames(combos: list[dict[str, Any]], metric_names: list[str]) -> list[str]:
    param_keys: set[str] = set()
    for combo in combos:
        param_keys.update(flatten(combo).keys())
    return (
        # strategy_key sits alongside combo_id as a first-class identity column,
        # not mixed into the sorted flattened params - it's computed from the
        # combo dict (see strategy_key()) but isn't itself a param of it.
        ["combo_id", "run_at", "status", "error", "dte", "strategy_key"]
        + sorted(param_keys)
        + list(metric_names)
        + ["raw_metrics_json"]
    )


def load_existing(csv_path: Path) -> tuple[dict[str, str], list[str] | None]:
    """Returns ({combo_id: status}, existing header) - header is None if the file doesn't exist yet."""
    if not csv_path.exists():
        return {}, None
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        statuses = {row["combo_id"]: row.get("status", "") for row in reader if row.get("combo_id")}
    return statuses, fieldnames


def migrate_header_if_needed(csv_path: Path, required_fields: list[str]) -> list[str]:
    """Resuming an older CSV that predates a newly-added column (e.g. "dte", added
    once DTE-per-row tracking was built) would otherwise either silently drop that
    column (if the old header is reused as-is) or corrupt the file (if new rows are
    written with more columns than the header line on disk declares). Rewrite the
    file once with the expanded header instead - existing rows just get a blank
    value for the new column(s). Returns the fieldnames to use going forward
    (unchanged if nothing needed migrating)."""
    statuses, existing_header = load_existing(csv_path)
    if existing_header is None:
        return required_fields
    missing = [f for f in required_fields if f not in existing_header]
    if not missing:
        return existing_header

    new_fieldnames = existing_header + missing
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=new_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in new_fieldnames})
    return new_fieldnames


def update_row(csv_path: Path, combo_id: str, new_row: dict[str, Any]) -> bool:
    """Overwrites the one existing row matching combo_id with new_row entirely (not
    merged - a fresh replay already produces a complete row, params included, via
    the same _build_row a normal sweep uses, so there's nothing of the old row worth
    keeping). Expands the file's header first if new_row introduces columns it
    doesn't have yet (e.g. brokerage_amount/taxes_charges_amount, added after this
    file was first written) - reuses migrate_header_if_needed, the same convention
    resume() already relies on. Returns False (file left untouched) if combo_id
    isn't actually in this file - the caller decides what that means (e.g. "try the
    next CSV in the list" for a multi-file refresh).

    Single-process, read-modify-write-whole-file - safe to call repeatedly from one
    sequential loop, NOT safe to call concurrently from multiple processes/threads
    against the same csv_path (last writer wins, earlier updates silently lost)."""
    if not csv_path.exists():
        return False
    fieldnames = migrate_header_if_needed(csv_path, list(new_row.keys()))
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    found = False
    for i, row in enumerate(rows):
        if row.get("combo_id") == combo_id:
            rows[i] = {col: ("" if new_row.get(col) is None else new_row.get(col)) for col in fieldnames}
            found = True
            break
    if not found:
        return False
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return True


def update_rows(csv_path: Path, updates: dict[str, dict[str, Any]]) -> set[str]:
    """Batched counterpart to update_row: applies every combo_id -> new_row update
    in `updates` that actually belongs to this file in ONE read + ONE write,
    instead of a full read-modify-write pass PER ROW. update_row's per-row cost (a
    full DictReader parse, plus migrate_header_if_needed's own full read, plus a
    full DictWriter rewrite - three-plus full passes over the file, called once per
    updated combo) is fine for a handful of rows but becomes the actual bottleneck
    once hundreds/thousands need merging back after a large "Force re-download &
    update results" run. Confirmed live: 2000 combos merging into a ~14,000-row CSV
    made the whole thing look hung well after every combo had already finished
    downloading - it hadn't hung, it was still inside this exact merge step, one
    full-file rewrite at a time, with no progress shown and no way to interrupt it.

    Returns the subset of `updates`' combo_ids actually found (and overwritten) in
    this file - same "not every combo_id necessarily belongs to this file" contract
    find_row_with_path/update_row already had, just resolved for the whole batch at
    once so a caller merging across several files knows what's left to look for in
    the next one."""
    if not csv_path.exists() or not updates:
        return set()
    # Every update is a complete row (see update_row's own docstring) sharing the
    # same field set - any one of them is representative for the header-migration
    # check below.
    sample_fields = list(next(iter(updates.values())).keys())
    fieldnames = migrate_header_if_needed(csv_path, sample_fields)
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    found: set[str] = set()
    for i, row in enumerate(rows):
        cid = row.get("combo_id")
        new_row = updates.get(cid) if cid else None
        if new_row is not None:
            rows[i] = {col: ("" if new_row.get(col) is None else new_row.get(col)) for col in fieldnames}
            found.add(cid)
    if not found:
        return found
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return found


def append_row(csv_path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    file_exists = csv_path.exists()
    normalized = {col: ("" if row.get(col) is None else row.get(col)) for col in fieldnames}
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(normalized)
        f.flush()
