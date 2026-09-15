"""Named, persisted (config, csv_path) pairs - lets you save a sweep's settings
together with the results file it produced, list them, and load any one back (both
the config *and* which CSV to resume) without losing track of others. Backed by a
single JSON file rather than a database since this is a single-user local tool."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.web.models import SweepUIConfig

EXECUTIONS_PATH = Path(__file__).resolve().parent.parent.parent / "output" / "executions.json"


def _load_all() -> dict[str, dict[str, Any]]:
    if not EXECUTIONS_PATH.exists():
        return {}
    return json.loads(EXECUTIONS_PATH.read_text()) or {}


def _save_all(data: dict[str, dict[str, Any]]) -> None:
    EXECUTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXECUTIONS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


def list_executions() -> list[dict[str, Any]]:
    """Newest-updated first - just the summary fields, not the full config (kept
    small and fast for a sidebar/dropdown listing)."""
    all_ex = _load_all()
    summaries = [
        {
            "name": name,
            "csv_path": entry.get("csv_path"),
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
        }
        for name, entry in all_ex.items()
    ]
    summaries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return summaries


def save_execution(name: str, cfg: SweepUIConfig, csv_path: str | None) -> None:
    if not name or not name.strip():
        raise ValueError("Execution name can't be empty.")
    name = name.strip()
    all_ex = _load_all()
    now = datetime.now(timezone.utc).isoformat()
    created_at = all_ex.get(name, {}).get("created_at", now)
    all_ex[name] = {
        "config": cfg.model_dump(),
        "csv_path": csv_path,
        "created_at": created_at,
        "updated_at": now,
    }
    _save_all(all_ex)


def load_execution(name: str) -> tuple[SweepUIConfig, str | None]:
    all_ex = _load_all()
    entry = all_ex.get(name)
    if entry is None:
        raise KeyError(f"No saved execution named {name!r}.")
    return SweepUIConfig(**entry["config"]), entry.get("csv_path")


def delete_execution(name: str) -> None:
    all_ex = _load_all()
    all_ex.pop(name, None)
    _save_all(all_ex)
