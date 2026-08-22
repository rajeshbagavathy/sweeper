from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def combo_id(combo: dict[str, Any]) -> str:
    canonical = json.dumps(combo, sort_keys=True, default=str)
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
        ["combo_id", "run_at", "status", "error"]
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
