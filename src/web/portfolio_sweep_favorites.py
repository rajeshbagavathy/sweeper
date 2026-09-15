"""Named, persisted portfolio-sweep parameter combinations - lets you keep a
combination (threshold, top_n, min_lots, max_lots, plus whatever budgets/max_share
were active when it was favorited) so it can be re-applied and recomputed anytime,
without re-running the sweep that found it. Backed by a single JSON file, same
convention as executions.py (this is a single-user local tool, not a database)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FAVORITES_PATH = Path(__file__).resolve().parent.parent.parent / "output" / "portfolio_sweep_favorites.json"


def _load_all() -> dict[str, dict[str, Any]]:
    if not FAVORITES_PATH.exists():
        return {}
    return json.loads(FAVORITES_PATH.read_text()) or {}


def _save_all(data: dict[str, dict[str, Any]]) -> None:
    FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAVORITES_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


def list_favorites() -> list[dict[str, Any]]:
    """Newest-saved first."""
    all_fav = _load_all()
    out = [{"name": name, **entry} for name, entry in all_fav.items()]
    out.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return out


def save_favorite(name: str, params: dict[str, Any], last_result: dict[str, Any] | None = None) -> None:
    """`params` is whatever a later /api/analyze/portfolio-basket call needs to
    reproduce this exact combination - threshold/top_n/min_lots/max_lots plus the
    budgets/max_share active when it was favorited. `last_result` is just the
    sweep row's own summary metrics at save time, kept for display in the
    favorites list - always re-fetched fresh (not read back from here) when
    actually applying a favorite, since reports may have changed since."""
    if not name or not name.strip():
        raise ValueError("Favorite name can't be empty.")
    name = name.strip()
    all_fav = _load_all()
    now = datetime.now(timezone.utc).isoformat()
    created_at = all_fav.get(name, {}).get("created_at", now)
    all_fav[name] = {"params": params, "last_result": last_result, "created_at": created_at, "updated_at": now}
    _save_all(all_fav)


def delete_favorite(name: str) -> None:
    all_fav = _load_all()
    all_fav.pop(name, None)
    _save_all(all_fav)
