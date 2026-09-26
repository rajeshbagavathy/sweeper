"""Translates a parsed `AlgtstStrategy` into the concrete values mtQuant's
"Create Options Portfolio" (Add Portfolio V2) dialog needs - organized by
which part of that dialog each value belongs to, confirmed live against the
running app on 2026-09-26 (see the field-mapping table worked out in this
session). automation.py (not built yet) is the only thing that should know
how to actually click/type these into the app; this module only decides
*what* the values are.

Mapping confidence, worked out by hand against the live app:
- CONFIRMED: Symbol, Expiry, Underlying (Spot/Future), per-leg CE/PE, B/S,
  Lots, Expiry, ATM/OTM/ITM strike notation, per-leg Stoploss %, per-leg
  Trail SL (Points), Run On Days, DTE, Start Time, Overall SL/Target (MTM),
  Overall Trailing Target ("For Every Increase In Profit By"/"Trail Profit
  By"), Lock-and-trail ("If Profit Reaches"/"Lock Minimum Profit At"),
  Move SL to Cost, re-entry EXISTS as ReEntry (AtCost) vs ReExecute
  (NextLeg) on the ReExecute tab.
- NOT YET CONFIRMED (flagged in `notes`/`LegPlan.notes` instead of guessed):
  the exact Strike Selection UI path for EntryByPremium legs (numeric
  premium, not ATM/OTM/ITM); whether AlgoTest's ExitIndicators time maps to
  mtQuant's "End Time" or "SqOff Time" (or both); the exact per-leg wiring
  of ReExecute's "OnSL/OnTarget ReExecute Count" and "SL Portfolio
  Name/Count" fields for multi-leg NextLeg cases; where
  SquareOffAllLegs/ReentryTimeRestriction/MaxPositionInADay/
  SkipInitialCandles live in the dialog (Exit Settings / Other Settings /
  Monitoring tabs were seen to exist but not opened yet); and whether
  "Wait & Trade" is really the right field for AlgoTest's per-leg Momentum.
  None of these are guessed at - automating past a flagged note without
  resolving it first would risk silently building a wrong strategy, which
  is worse than not building it at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.mtquant.algtst_parser import AlgtstLeg, AlgtstStrategy

_WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _format_time(hm: tuple[int, int] | None) -> str | None:
    if hm is None:
        return None
    hour, minute = hm
    return f"{hour:02d}:{minute:02d}:00"


def _run_on_days(weekdays: dict[str, bool]) -> list[str]:
    return [day.capitalize() for day in _WEEKDAY_ORDER if weekdays.get(day)]


@dataclass
class LegPlan:
    leg_id: str
    buy_sell: str  # "Buy" | "Sell"
    ce_pe: str  # "CE" | "PE"
    lots: int | None
    expiry: str
    strike_mode: str  # "ATM" | "OTM<n>" | "ITM<n>" | "PREMIUM"
    strike_value: Any  # the ATM/OTM/ITM label itself, or the numeric premium for "PREMIUM"
    stoploss_pct: float | None
    target_value: float | None
    trail_sl: dict | None  # {"instrument_move": ..., "stoploss_move": ...} | None
    momentum: dict | None  # raw normalized momentum, if any - see module docstring caveat
    idle: bool  # True => add via the leg grid's "Idle" checkbox, not a live leg
    reentry_kind: str | None  # "AtCost" | "NextLeg" | None
    reentry_count: int | None  # for AtCost
    reentry_target_leg_id: str | None  # for NextLeg - the idle leg id it promotes
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_algtst_leg(cls, leg: AlgtstLeg, *, idle: bool) -> LegPlan:
        notes: list[str] = []

        if leg.entry_type == "EntryByStrikeType":
            strike_mode = str(leg.strike_parameter)  # "ATM" / "OTM2" / "ITM1" / ...
            strike_value = leg.strike_parameter
        elif leg.entry_type == "EntryByPremium":
            strike_mode = "PREMIUM"
            strike_value = leg.strike_parameter
            notes.append("EntryByPremium: exact mtQuant Strike Selection UI path for a numeric premium not yet confirmed live")
        else:
            strike_mode = "UNKNOWN"
            strike_value = leg.strike_parameter
            notes.append(f"unrecognized EntryType {leg.entry_type!r}")

        stoploss_pct = None
        if leg.stop_loss:
            if leg.stop_loss["type"] == "Percentage":
                stoploss_pct = leg.stop_loss["value"]
            else:
                notes.append(f"leg stop loss type {leg.stop_loss['type']!r} isn't Percentage - not auto-mapped")

        target_value = None
        if leg.target:
            notes.append(f"leg has a Target ({leg.target}) - per-leg Target column mapping not yet exercised live")
            target_value = leg.target.get("value")

        trail_sl = None
        if leg.trail_sl:
            if leg.trail_sl["type"] == "Points":
                trail_sl = {
                    "instrument_move": leg.trail_sl["value"].get("InstrumentMove"),
                    "stoploss_move": leg.trail_sl["value"].get("StopLossMove"),
                }
            else:
                notes.append(f"leg trail SL type {leg.trail_sl['type']!r} isn't Points - not auto-mapped")

        momentum = None
        if leg.momentum:
            momentum = leg.momentum
            notes.append("has Momentum - mapping to mtQuant's 'Wait & Trade' column is a strong candidate but not yet confirmed live")

        reentry_kind = reentry_count = reentry_target_leg_id = None
        if leg.reentry_sl:
            reentry_kind = leg.reentry_sl["type"]
            if reentry_kind == "AtCost":
                reentry_count = (leg.reentry_sl["value"] or {}).get("ReentryCount")
            elif reentry_kind == "NextLeg":
                reentry_target_leg_id = (leg.reentry_sl["value"] or {}).get("NextLegRef")
            else:
                notes.append(f"unrecognized LegReentrySL type {reentry_kind!r}")

        if leg.reentry_tp:
            notes.append(f"has LegReentryTP ({leg.reentry_tp}) - target-side re-entry not yet exercised live")

        return cls(
            leg_id=leg.leg_id,
            buy_sell=leg.position_type,
            ce_pe=leg.instrument_kind,
            lots=leg.lot_quantity,
            expiry=leg.expiry_kind,
            strike_mode=strike_mode,
            strike_value=strike_value,
            stoploss_pct=stoploss_pct,
            target_value=target_value,
            trail_sl=trail_sl,
            momentum=momentum,
            idle=idle,
            reentry_kind=reentry_kind,
            reentry_count=reentry_count,
            reentry_target_leg_id=reentry_target_leg_id,
            notes=notes,
        )

    @property
    def has_notes(self) -> bool:
        return bool(self.notes)


@dataclass
class MTQuantPortfolioPlan:
    """One mtQuant "Portfolio" worth of build instructions - corresponds to
    exactly one AlgoTest "strategy" (see algtst_parser's terminology note)."""

    source_strategy_id: str
    portfolio_name: str
    symbol: str
    expiry: str
    underlying: str  # "Spot" | "Future"
    default_lots: int | None
    dte: list[int]
    run_on_days: list[str]
    start_time: str | None
    exit_time: str | None  # see module docstring: End Time vs SqOff Time still unconfirmed
    legs: list[LegPlan]  # live legs, in ListOfLegConfigs order
    idle_legs: list[LegPlan]  # promoted only via a NextLeg re-entry
    overall_stoploss: dict | None
    overall_target: dict | None
    overall_trail: dict | None
    lock_and_trail: dict | None
    move_sl_to_cost: bool
    notes: list[str] = field(default_factory=list)

    @property
    def has_notes(self) -> bool:
        return bool(self.notes) or any(leg.has_notes for leg in (*self.legs, *self.idle_legs))

    def all_notes(self) -> list[str]:
        """Every note, portfolio-level and per-leg, each prefixed with where
        it came from - what preview.py shows the user before anything is
        built live."""
        out = [f"portfolio: {n}" for n in self.notes]
        for leg in self.legs:
            out += [f"leg {leg.leg_id} ({leg.ce_pe}): {n}" for n in leg.notes]
        for leg in self.idle_legs:
            out += [f"idle leg {leg.leg_id} ({leg.ce_pe}): {n}" for n in leg.notes]
        return out


def build_portfolio_plan(strategy: AlgtstStrategy) -> MTQuantPortfolioPlan:
    notes: list[str] = list(strategy.unmapped_notes)  # carry the parser's own flags forward

    if strategy.strategy_type != "IntradaySameDay":
        notes.append(f"StrategyType {strategy.strategy_type!r} isn't IntradaySameDay - not exercised live yet")

    if strategy.exit_time is None and strategy.entry_time is not None:
        notes.append("exit time missing/unmapped but entry time present - see parser notes above")

    live_legs = [LegPlan.from_algtst_leg(leg, idle=False) for leg in strategy.legs]
    idle_legs = [LegPlan.from_algtst_leg(leg, idle=True) for leg in strategy.idle_legs.values()]

    if strategy.max_positions_per_day not in (None, 1):
        notes.append(f"MaxPositionInADay={strategy.max_positions_per_day} (not 1) - mtQuant field location not yet confirmed")
    if strategy.skip_initial_candles:
        notes.append(f"SkipInitialCandles={strategy.skip_initial_candles} - mtQuant field location not yet confirmed")
    if strategy.square_off_all_legs:
        notes.append("SquareOffAllLegs=True - mtQuant field location not yet confirmed (Exit Settings tab, unopened)")
    if strategy.reentry_time_restriction:
        notes.append(f"ReentryTimeRestriction={strategy.reentry_time_restriction!r} - mtQuant field location not yet confirmed")

    default_lots = strategy.multiplier
    if default_lots is None:
        notes.append("no portfolio.items multiplier for this strategy - Default Lots left unset")

    return MTQuantPortfolioPlan(
        source_strategy_id=strategy.strategy_id,
        portfolio_name=strategy.name,
        symbol=strategy.ticker,
        expiry=strategy.legs[0].expiry_kind if strategy.legs else "",
        underlying="Spot" if strategy.take_underlying_from_cash else "Future",
        default_lots=default_lots,
        dte=strategy.dte,
        run_on_days=_run_on_days(strategy.weekdays),
        start_time=_format_time(strategy.entry_time),
        exit_time=_format_time(strategy.exit_time),
        legs=live_legs,
        idle_legs=idle_legs,
        overall_stoploss=strategy.overall_sl,
        overall_target=strategy.overall_target,
        overall_trail=strategy.overall_trail_sl,
        lock_and_trail=strategy.lock_and_trail,
        move_sl_to_cost=strategy.trail_sl_to_breakeven,
        notes=notes,
    )
