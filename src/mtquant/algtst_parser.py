"""Parses an AlgoTest `.algtst` portfolio export into normalized dataclasses.

Terminology trap, confirmed against the live mtQuant app (see
docs/mtquant-integration.md): AlgoTest's own "portfolio" is a GROUP of many
strategies. mtQuant's "Portfolio" is ONE individual strategy. Don't conflate
the two - `AlgtstPortfolio` here mirrors AlgoTest's naming (a group), and
each `AlgtstStrategy` in it is what becomes one mtQuant Portfolio later, in
automation.py.

This module only parses and normalizes - it knows nothing about mtQuant's UI
or field names. See field_mapping.py for the AlgoTest -> mtQuant translation.

`.algtst` fields consistently use a `{"Type": "<EnumClass>.<Member>", "Value":
...}` shape for anything optional (Type == "None" means "not set"). `_type_value`
below normalizes that into `{"type": "<Member>", "value": ...} | None` so
downstream code never has to strip the enum-class prefix or special-case the
"None" sentinel itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _type_value(d: dict | None) -> dict | None:
    """Normalizes the `{"Type": "EnumClass.Member", "Value": ...}` shape used
    throughout .algtst for optional fields. Returns None when unset (Type is
    the literal string "None", or the dict itself is missing)."""
    if not d:
        return None
    type_str = d.get("Type")
    if type_str is None or type_str == "None":
        return None
    # "LegTgtSLType.Percentage" -> "Percentage"; tolerate a bare member with
    # no "." (seen nowhere in practice yet, but cheap to not crash on).
    member = type_str.rsplit(".", 1)[-1]
    return {"type": member, "value": d.get("Value")}


def _extract_time(indicator_tree: dict, notes: list[str], which: str) -> tuple[int, int] | None:
    """Pulls a single (Hour, Minute) out of an Entry/ExitIndicators tree.

    Every strategy seen so far uses exactly one AND'd TimeIndicator node on
    each side. Anything richer (multiple conditions, a non-time indicator) is
    real signal we can't safely collapse into a single clock time - rather
    than silently picking the first node (this codebase's own CLAUDE.md is
    explicit that AlgoTest-shaped data has burned this project before when a
    fix "looked obviously correct" without checking it against real data), we
    record a note and return None so the caller must decide what to do
    rather than mtQuant silently getting the wrong time.
    """
    values = indicator_tree.get("Value") or []
    if len(values) != 1:
        notes.append(f"{which}Indicators has {len(values)} conditions, not 1 - not auto-mapped, needs manual review")
        return None
    node = values[0]
    indicator = node.get("Value", {})
    if indicator.get("IndicatorName") != "IndicatorType.TimeIndicator":
        notes.append(f"{which}Indicators' single condition isn't a TimeIndicator ({indicator.get('IndicatorName')!r}) - not auto-mapped")
        return None
    params = indicator.get("Parameters", {})
    hour, minute = params.get("Hour"), params.get("Minute")
    if hour is None or minute is None:
        notes.append(f"{which}Indicators TimeIndicator is missing Hour/Minute")
        return None
    return (hour, minute)


@dataclass
class AlgtstLeg:
    """One leg - either a real entry in `ListOfLegConfigs`, or an entry from
    `IdleLegConfigs` (same shape; stays dormant until a NextLeg re-entry on
    some other leg promotes it - see `reentry_sl`'s "NextLeg" case)."""

    leg_id: str
    instrument_kind: str  # "CE" | "PE" (stripped of the "LegType." prefix)
    position_type: str  # "Buy" | "Sell" (stripped of "PositionType.")
    expiry_kind: str  # e.g. "Weekly" (stripped of "ExpiryType.")
    entry_type: str  # "EntryByPremium" | "EntryByStrikeType" (stripped of "EntryType.")
    strike_parameter: Any  # numeric premium (EntryByPremium) or "ATM"/"OTM1"/... (EntryByStrikeType, stripped of "StrikeType.")
    lot_quantity: int
    stop_loss: dict | None
    target: dict | None
    trail_sl: dict | None
    momentum: dict | None
    reentry_sl: dict | None
    reentry_tp: dict | None

    @classmethod
    def from_raw(cls, leg_id: str, raw: dict) -> AlgtstLeg:
        lot_cfg = raw.get("LotConfig") or {}
        strike = raw.get("StrikeParameter")
        if isinstance(strike, str) and "." in strike:
            strike = strike.rsplit(".", 1)[-1]
        return cls(
            leg_id=leg_id,
            instrument_kind=(raw.get("InstrumentKind") or "").rsplit(".", 1)[-1],
            position_type=(raw.get("PositionType") or "").rsplit(".", 1)[-1],
            expiry_kind=(raw.get("ExpiryKind") or "").rsplit(".", 1)[-1],
            entry_type=(raw.get("EntryType") or "").rsplit(".", 1)[-1],
            strike_parameter=strike,
            lot_quantity=lot_cfg.get("Value"),
            stop_loss=_type_value(raw.get("LegStopLoss")),
            target=_type_value(raw.get("LegTarget")),
            trail_sl=_type_value(raw.get("LegTrailSL")),
            momentum=_type_value(raw.get("LegMomentum")),
            reentry_sl=_type_value(raw.get("LegReentrySL")),
            reentry_tp=_type_value(raw.get("LegReentryTP")),
        )


@dataclass
class AlgtstStrategy:
    """One AlgoTest strategy - this is what becomes ONE mtQuant Portfolio."""

    strategy_id: str
    name: str
    ticker: str
    strategy_type: str
    entry_time: tuple[int, int] | None
    exit_time: tuple[int, int] | None
    legs: list[AlgtstLeg]
    idle_legs: dict[str, AlgtstLeg]
    max_positions_per_day: int | None
    overall_sl: dict | None
    overall_target: dict | None
    overall_trail_sl: dict | None
    lock_and_trail: dict | None
    skip_initial_candles: int | None
    square_off_all_legs: bool
    reentry_time_restriction: str | None
    trail_sl_to_breakeven: bool
    take_underlying_from_cash: bool
    # From the enclosing portfolio.items entry with the same id:
    checked: bool
    dte: list[int]
    multiplier: int | None
    weekdays: dict[str, bool]
    # Anything the parser couldn't confidently normalize - surfaced by
    # preview.py, never silently dropped.
    unmapped_notes: list[str] = field(default_factory=list)

    @property
    def has_unmapped(self) -> bool:
        return bool(self.unmapped_notes)


@dataclass
class AlgtstPortfolio:
    """The whole parsed file. Named after AlgoTest's own "portfolio" concept
    (a GROUP of strategies) - do not confuse with mtQuant's "Portfolio"."""

    name: str
    is_weekdays: bool
    strategies: list[AlgtstStrategy]


def _parse_strategy(strategy_id: str, strategy_raw: dict, item_raw: dict | None) -> AlgtstStrategy:
    definition = strategy_raw.get("definition", {})
    notes: list[str] = []

    entry_time = _extract_time(definition.get("EntryIndicators", {}), notes, "Entry")
    exit_time = _extract_time(definition.get("ExitIndicators", {}), notes, "Exit")

    legs = [AlgtstLeg.from_raw(leg.get("id", f"{strategy_id}-leg{i}"), leg) for i, leg in enumerate(definition.get("ListOfLegConfigs", []))]
    idle_raw = definition.get("IdleLegConfigs") or {}
    idle_legs = {leg_id: AlgtstLeg.from_raw(leg_id, leg) for leg_id, leg in idle_raw.items()}

    # Cross-check NextLeg re-entry references actually resolve - a dangling
    # reference would otherwise fail silently much later, inside the live
    # mtQuant automation, where it's far more expensive to diagnose.
    for leg in legs:
        if leg.reentry_sl and leg.reentry_sl["type"] == "NextLeg":
            ref = (leg.reentry_sl["value"] or {}).get("NextLegRef")
            if ref not in idle_legs:
                notes.append(f"leg {leg.leg_id}: LegReentrySL.NextLegRef {ref!r} not found in IdleLegConfigs")

    if item_raw is None:
        notes.append("no matching portfolio.items entry for this strategy id - checked/dte/multiplier/weekdays unknown")

    def _bool(raw_val: str | bool | None) -> bool:
        return raw_val is True or raw_val == "True"

    return AlgtstStrategy(
        strategy_id=strategy_id,
        name=strategy_raw.get("name", ""),
        ticker=definition.get("Ticker", ""),
        strategy_type=(definition.get("StrategyType") or "").rsplit(".", 1)[-1],
        entry_time=entry_time,
        exit_time=exit_time,
        legs=legs,
        idle_legs=idle_legs,
        max_positions_per_day=definition.get("MaxPositionInADay"),
        overall_sl=_type_value(definition.get("OverallSL")),
        overall_target=_type_value(definition.get("OverallTgt")),
        overall_trail_sl=_type_value(definition.get("OverallTrailSL")),
        lock_and_trail=_type_value(definition.get("LockAndTrail")),
        skip_initial_candles=definition.get("SkipInitialCandles"),
        square_off_all_legs=_bool(definition.get("SquareOffAllLegs")),
        reentry_time_restriction=(definition.get("ReentryTimeRestriction") or None) if definition.get("ReentryTimeRestriction") != "None" else None,
        trail_sl_to_breakeven=_bool(definition.get("TrailSLtoBreakeven")),
        take_underlying_from_cash=_bool(definition.get("TakeUnderlyingFromCashOrNot")),
        checked=bool(item_raw.get("checked")) if item_raw else False,
        dte=list(item_raw.get("dte", [])) if item_raw else [],
        multiplier=item_raw.get("multiplier") if item_raw else None,
        weekdays=dict(item_raw.get("weekdays", {})) if item_raw else {},
        unmapped_notes=notes,
    )


def parse_algtst_data(data: dict) -> AlgtstPortfolio:
    """Parses an already-loaded .algtst JSON dict (see `parse_algtst_file`
    for parsing straight from a path)."""
    portfolio_raw = data.get("data", {}).get("portfolio", {})
    strategies_raw = data.get("data", {}).get("strategies", {})

    items_by_id = {str(item.get("id")): item for item in portfolio_raw.get("items", [])}

    strategies = [_parse_strategy(sid, sraw, items_by_id.get(str(sid))) for sid, sraw in strategies_raw.items()]

    return AlgtstPortfolio(
        name=portfolio_raw.get("name", ""),
        is_weekdays=bool(portfolio_raw.get("is_weekdays")),
        strategies=strategies,
    )


def parse_algtst_file(path: str | Path) -> AlgtstPortfolio:
    """Parses a .algtst file straight from disk."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return parse_algtst_data(data)
