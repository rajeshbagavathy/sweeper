"""Translates a parsed `AlgtstStrategy` into the concrete values mtQuant's
"Create Options Portfolio" (Add Portfolio V2) dialog needs - organized by
which part of that dialog each value belongs to, confirmed live against the
running app on 2026-09-26 (see the field-mapping table worked out in this
session). automation.py (not built yet) is the only thing that should know
how to actually click/type these into the app; this module only decides
*what* the values are.

Mapping confidence - live UI exploration (2026-09-26) plus mtQuant's own
help doc (opened via the dialog's "Help" link -> a Google Doc; fetched and
cross-checked, not just eyeballed):

- CONFIRMED: Symbol, Expiry, Underlying (Spot/Future), per-leg CE/PE, B/S,
  Lots, Expiry, ATM/OTM/ITM strike notation, per-leg Trail SL (Points; the
  doc confirms the two values are "profit increase threshold" / "trail
  amount", matching InstrumentMove/StopLossMove exactly), Run On Days, DTE,
  Start Time, Overall SL/Target (MTM), Overall Trailing Target ("For Every
  Increase In Profit By"/"Trail Profit By"), Lock-and-trail ("If Profit
  Reaches"/"Lock Minimum Profit At"), Move SL to Cost, re-entry EXISTS as
  ReEntry (AtCost: "re-enter when price re-crosses the original avg traded
  price") vs ReExecuteLeg (NextLeg: "exact copy, executed per the original
  leg's own settings") on the ReExecute tab.
- CONFIRMED BUT CORRECTED from an earlier wrong live-test conclusion this
  same session: a bare number typed into the leg grid's Strike column is an
  ABSOLUTE STRIKE PRICE (mtQuant's own doc: "typing '17500' selects the
  17500 strike... interpreted as the literal strike level"), NOT a premium
  value. An earlier test that typed "40" into that field and saw it accepted
  without error was misleading - it silently set strike=40, not "enter at
  premium 40". EntryByPremium must instead go through the dedicated
  "Premium / Greek Legs" sub-form (Value Type=Premium/NearestPremium, a
  "Between" range, Max Depth, Side=ITM/OTM/Both) - see the still-open item
  below.
- CONFIRMED: per-leg Stoploss/Target "type" choices are Premium (% or
  points off entry premium - this is what AlgoTest's LegStopLoss type
  "Percentage" maps to), AbsolutePremium (a literal price, no math),
  Underlying, Strike, and Delta/Theta (raw Greek thresholds).
- CONFIRMED: "Wait & Trade" delays order placement past the entry trigger
  until price moves a further signed amount ("-1%" = wait for a further 1%
  drop) - this is exactly AlgoTest's per-leg Momentum
  (PercentageDown/UnderlyingPointsUp/Down), not a guess anymore.
- STILL OPEN (flagged in `notes`/`LegPlan.notes`, not guessed): the exact
  live UI location of the "Premium / Greek Legs" sub-form (Value
  Type/Between/Max Depth/Side) - ticking the checkbox and clicking Add Leg
  produced an ordinary leg row in this session's live test, not the
  documented sub-form, and the leg grid turned out to be a separately
  UI-Automation-invisible control (not enumerable via the standard tree
  walk the rest of this dialog was mapped with), so this needs a fresh
  targeted pass, not a repeat of the same steps; whether AlgoTest's
  ExitIndicators time maps to mtQuant's "End Time" or "SqOff Time" (or
  both); the exact per-leg wiring of ReExecute's "OnSL/OnTarget ReExecute
  Count" and "SL Portfolio Name/Count" fields for multi-leg NextLeg cases;
  and where SquareOffAllLegs/ReentryTimeRestriction/MaxPositionInADay/
  SkipInitialCandles live, if anywhere - mtQuant's own doc doesn't name an
  explicit equivalent for any of these. Not a blocker for the user's actual
  14-strategy file though: every one of those four fields sits at its
  trivial/default value (0/False/1/None) across all 14 real strategies, so
  nothing there is actually lost by leaving them unmapped for now.
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


def _momentum_to_wait_trade(momentum: dict) -> tuple[str | None, str | None]:
    """Translates AlgoTest's LegMomentum into mtQuant's "Wait & Trade" value
    (a signed percentage or points - see mtQuant's own doc: "-1%" means wait
    for a further 1% adverse move past the trigger before actually entering).
    Only the PercentageDown case is confirmed against a worked example in
    that doc; the "Up" sign convention is inferred by symmetry, not
    independently confirmed, so it's flagged rather than trusted silently.
    """
    kind, value = momentum["type"], momentum["value"]
    if kind == "PercentageDown":
        return f"-{value}%", None
    if kind == "PercentageUp":
        return f"+{value}%", "PercentageUp sign convention inferred by symmetry with the doc's PercentageDown example, not independently confirmed"
    if kind == "UnderlyingPointsDown":
        return f"-{value}", None
    if kind == "UnderlyingPointsUp":
        return f"+{value}", "UnderlyingPointsUp sign convention inferred by symmetry with the doc's PercentageDown example, not independently confirmed"
    return None, f"unrecognized Momentum type {kind!r} - not mapped to Wait & Trade"


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
    wait_trade: str | None  # mtQuant's signed "Wait & Trade" value (e.g. "-5%"), translated from Momentum
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
            notes.append(
                f"EntryByPremium (target premium {leg.strike_parameter}): confirmed this does NOT go in the plain "
                "Strike column (that's an absolute strike price in mtQuant, per its own docs) - it needs the "
                "'Premium / Greek Legs' sub-form (Value Type=Premium, a Between range, Max Depth, Side). Live UI "
                "location of that sub-form not yet found; a Between-range tolerance policy also needs deciding."
            )
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

        wait_trade = None
        if leg.momentum:
            wait_trade, momentum_note = _momentum_to_wait_trade(leg.momentum)
            if momentum_note:
                notes.append(momentum_note)

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
            wait_trade=wait_trade,
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
