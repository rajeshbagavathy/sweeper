"""Builds a JSON-serializable preview from a parsed .algtst file - the "read
the file, show a table before touching mtQuant" step. No pywinauto, no OS
dependency: this is plain data transformation and works on any platform
(see docs/mtquant-integration.md's isolation rule - only the actual
automation needs the Windows gate, not this).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.mtquant.algtst_parser import parse_algtst_data
from src.mtquant.field_mapping import MTQuantPortfolioPlan, build_portfolio_plan


def _plan_to_dict(plan: MTQuantPortfolioPlan) -> dict[str, Any]:
    d = asdict(plan)
    d["has_notes"] = plan.has_notes
    d["all_notes"] = plan.all_notes()
    for leg in d["legs"] + d["idle_legs"]:
        leg["has_notes"] = bool(leg["notes"])
    return d


def build_preview(data: dict) -> dict[str, Any]:
    """`data` is an already-`json.load`ed .algtst file. Returns a dict ready
    to serialize straight into the API response - one entry per strategy,
    each already carrying its own notes/has_notes so the frontend never has
    to re-derive them."""
    portfolio = parse_algtst_data(data)
    plans = [build_portfolio_plan(s) for s in portfolio.strategies]
    return {
        "portfolio_name": portfolio.name,
        "strategy_count": len(plans),
        "flagged_count": sum(1 for p in plans if p.has_notes),
        "strategies": [_plan_to_dict(p) for p in plans],
    }
