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
  premium 40".
- CONFIRMED AND LOCATED (with the user's direct guidance, then verified
  live): EntryByPremium legs use the "Premium / Greek Legs" sub-form,
  opened by clicking the Strike column's dropdown ARROW specifically (not
  the text part) on a leg added while the "Premium / Greek Leg" checkbox
  (top-right of the dialog) is ticked. The sub-form's Value Type dropdown
  offers Premium/NearestPremium/Delta/IV/Theta/NearestDelta/
  NearestStraddlePremium; NearestPremium is the right choice for AlgoTest's
  EntryByPremium - it takes a single target Value (not a range), with Cond
  defaulting to "Any" and Max Depth/Side left at their defaults (15/BOTH),
  matching AlgoTest's single StrikeParameter number with no tolerance-range
  decision needed. Confirmed the value genuinely persists (reopening the
  popup shows it retained) even though the leg grid's own closed-state
  Strike text doesn't visually refresh to show it - a cosmetic quirk in
  what the dialog itself flags as a "recently added feature, use with
  care", not a sign the value didn't take.
- CONFIRMED: per-leg Stoploss/Target "type" choices are Premium (% or
  points off entry premium - this is what AlgoTest's LegStopLoss type
  "Percentage" maps to), AbsolutePremium (a literal price, no math),
  Underlying, Strike, and Delta/Theta (raw Greek thresholds).
- CONFIRMED: "Wait & Trade" delays order placement past the entry trigger
  until price moves a further signed amount ("-1%" = wait for a further 1%
  drop) - this is exactly AlgoTest's per-leg Momentum
  (PercentageDown/UnderlyingPointsUp/Down), not a guess anymore.
- CONFIRMED (user's own explanation): mtQuant's "End Time" is only needed
  when the portfolio's ENTRY itself is conditional (e.g. an underlying
  breakout) - End Time then bounds how long that condition is watched for.
  AlgoTest's entries here are all plain clock-time triggers (a single
  TimeIndicator, nothing conditional - see algtst_parser's `_extract_time`),
  so End Time is never needed for this file: ExitIndicators maps to SqOff
  Time alone, which force-exits every leg at that clock time. MTQuantPortfolioPlan
  intentionally has no separate `end_time` field for this reason.
- EXPLAINED (not a gap, just no explicit field needed): SquareOffAllLegs/
  ReentryTimeRestriction/MaxPositionInADay/SkipInitialCandles have no named
  mtQuant field because mtQuant's own default behavior already matches what
  each one describes at the value it holds across all 14 real strategies:
  SquareOffAllLegs=False already matches mtQuant's default of exiting each
  leg independently on its own SL/Target rather than forcing them together
  (SqOff Time is the only "square off everything together" trigger, and
  that's handled separately via the Exit Settings tab's own "Exit Sell Legs
  First" ordering, not a per-portfolio all-or-nothing toggle);
  ReentryTimeRestriction=None needs no restriction to configure;
  MaxPositionInADay=1 is just describing "enter once", which is the
  unconfigured default; SkipInitialCandles=0 needs no delay-before-entry
  setting. None of these would change what gets built.
- LOCATED BUT NOT YET EXERCISED LIVE: the leg grid's "On Stoploss" dropdown
  and "SL Portfolio Name/Count" field (visible in a screenshot the user
  shared of the live grid) are almost certainly where per-leg AtCost/
  NextLeg re-entry actually gets wired (mirroring "On Target"/"Tgt
  Portfolio Name/Count" for the target-side case) - not yet clicked
  through, needed before automation.py can build strategy #10 specifically
  (the one real strategy using NextLeg/idle legs - unrelated to leg COUNT,
  it's just strategy id "10"; nothing in this file has anywhere near 10
  actual legs). Also confirmed via that screenshot: the grid's unused
  columns (Hedge Req $, Trail TGT, SL Wait, per-leg Day From Start/Start
  Time, Spread Limit) have no counterpart in any of the 14 real strategies
  (AlgoTest's own LegTarget is always type "None" there, so Trail TGT is
  moot; entries are never staggered per-leg) - nothing is silently missing
  by leaving those blank.
- STILL OPEN: the leg grid turned out to be a UI-Automation-invisible
  control (not enumerable via the standard tree walk the rest of this
  dialog was mapped with), so automation.py will need coordinate/
  image-based interaction for the leg grid specifically, unlike the rest
  of this dialog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.mtquant.algtst_parser import AlgtstLeg, AlgtstStrategy

_WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Index strike spacing. NIFTY's 50 was confirmed on the live dialog (the Strike
# Step field reads 50 when the symbol is NIFTY). The others are the exchange's
# standard option strike intervals, used only to turn OTM1/ITM2 into ATM±N.
STRIKE_STEPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
    "BANKEX": 100,
}

# Typed into the Stoploss Settings "SL wait" field on every portfolio.
SL_WAIT_SECONDS = 10


def _strike_name_token(leg: LegPlan) -> str | None:
    """The first piece of the saved portfolio name.

    ATM stays "ATM". A premium of 70 becomes "PRM70".
    """
    if leg.strike_mode == "PREMIUM":
        try:
            value = f"{float(leg.strike_value):g}"
        except (TypeError, ValueError):
            value = str(leg.strike_value)
        return f"PRM{value}"
    if leg.strike_mode and leg.strike_mode != "UNKNOWN":
        return leg.strike_mode
    return None


def portfolio_save_name(legs: list[LegPlan], entry_time: tuple[int, int] | None) -> str:
    """Name typed into Option Portfolio Name when the portfolio is saved.

    `<Strike or premium>_<SL%>_<start HH.MM>`, using the original legs and
    the strategy's own start clock (09:17 stays 09.17, not the minus-one-second
    value typed into Start Time). Example: ATM_20%_09.17, PRM70_25%_09.30.
    """
    strike_tokens: list[str] = []
    for leg in legs:
        token = _strike_name_token(leg)
        if token and token not in strike_tokens:
            strike_tokens.append(token)
    sl_tokens: list[str] = []
    for leg in legs:
        if leg.stoploss_pct is None:
            continue
        token = f"{leg.stoploss_pct:g}%"
        if token not in sl_tokens:
            sl_tokens.append(token)
    if entry_time is None:
        clock = "??.??"
    else:
        hour, minute = entry_time
        clock = f"{hour:02d}.{minute:02d}"
    parts = ["-".join(strike_tokens) if strike_tokens else "STRIKE"]
    if sl_tokens:
        parts.append("-".join(sl_tokens))
    parts.append(clock)
    return "_".join(parts)


def _format_time(hm: tuple[int, int] | None) -> str | None:
    """mtQuant Start/SqOff are the AlgoTest clock time minus one second.

    A 09:17 entry is typed as 09:16:59, and a 14:45 exit as 14:44:59. AlgoTest
    only stores hour and minute, so this is always HH:MM:00 minus one second.
    """
    if hm is None:
        return None
    hour, minute = hm
    total = hour * 3600 + minute * 60
    if total <= 0:
        return None
    total -= 1
    hour, rem = divmod(total, 3600)
    minute, second = divmod(rem, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


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
    # Percent-up is an unsigned percent. Confirmed by the user: 5% up is "5%",
    # 5% down is "-5%". The plus sign is not typed.
    if kind == "PercentageUp":
        return f"{value}%", None
    if kind == "UnderlyingPointsDown":
        return f"-{value}", None
    if kind == "UnderlyingPointsUp":
        return f"+{value}", "UnderlyingPointsUp sign convention inferred by symmetry with the doc's PercentageDown example, not independently confirmed"
    return None, f"unrecognized Momentum type {kind!r} - not mapped to Wait & Trade"


def strike_label(ce_pe: str, strike_mode: str, symbol: str) -> tuple[str | None, str | None]:
    """AlgoTest OTM/ITM offsets as mtQuant's ATM±points label.

    CE OTM moves up (NIFTY OTM1 = ATM+50, OTM2 = ATM+100) and CE ITM moves
    down. PE is the opposite: PE OTM1 = ATM-50, PE ITM1 = ATM+50.
    """
    if strike_mode == "PREMIUM":
        return None, None
    if strike_mode == "ATM":
        return "ATM", None
    kind = strike_mode[:3] if strike_mode[:3] in ("OTM", "ITM") else ""
    distance = strike_mode[3:]
    if kind not in ("OTM", "ITM") or not distance.isdigit() or int(distance) < 1:
        return None, f"unrecognized strike {strike_mode!r} - not converted to an ATM± offset"
    step = STRIKE_STEPS.get(symbol)
    if step is None:
        return None, f"no strike step known for symbol {symbol!r} - can't convert {strike_mode} to ATM±"
    points = int(distance) * step
    # CE: OTM is above ATM, ITM is below. PE swaps those directions.
    positive = (ce_pe == "CE" and kind == "OTM") or (ce_pe == "PE" and kind == "ITM")
    sign = "+" if positive else "-"
    return f"ATM{sign}{points}", None


def build_premium_selection(target_premium: float) -> dict:
    """The "Premium / Greek Legs" sub-form values for an EntryByPremium leg -
    opened via the leg grid's Strike column dropdown ARROW (not the text
    part) on a leg added while "Premium / Greek Leg" is ticked. NearestPremium
    takes a single target value directly (Cond defaulting to "Any"), so
    there's no tolerance-range decision to make - confirmed live, including
    that the value genuinely persists across reopening the popup even though
    the leg grid's own closed-state Strike text doesn't visually refresh.
    """
    return {
        "value_type": "NearestPremium",
        "value": target_premium,
        "cond": "Any",
        "max_depth": 15,  # dialog's own default - left as-is per live guidance
        "side": "BOTH",  # dialog's own default - left as-is per live guidance
    }


@dataclass
class LegPlan:
    leg_id: str
    buy_sell: str  # "Buy" | "Sell"
    ce_pe: str  # "CE" | "PE"
    lots: int | None
    expiry: str
    strike_mode: str  # "ATM" | "OTM<n>" | "ITM<n>" | "PREMIUM"
    strike_value: Any  # the ATM/OTM/ITM label itself, or the numeric premium for "PREMIUM"
    strike_label: str | None  # "ATM" / "ATM+50" / "ATM-100" once the symbol's strike step is known
    premium_selection: dict | None  # set iff strike_mode == "PREMIUM" - see build_premium_selection()
    stoploss_pct: float | None
    stoploss_text: str | None  # what gets typed into SL Value, e.g. "15%"
    target_value: float | None
    trail_sl: dict | None  # {"instrument_move": ..., "stoploss_move": ...} | None
    wait_trade: str | None  # mtQuant's signed "Wait & Trade" value (e.g. "-5%"), translated from Momentum
    idle: bool  # True => add via the leg grid's "Idle" checkbox, not a live leg
    reentry_kind: str | None  # "AtCost" | "NextLeg" | None
    reentry_count: int | None  # for AtCost
    reentry_target_leg_id: str | None  # for NextLeg - the idle leg id it promotes
    on_sl_action: str | None = None  # "Execute_Leg3" once the grid row of the lazy partner is known
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_algtst_leg(cls, leg: AlgtstLeg, *, idle: bool) -> LegPlan:
        notes: list[str] = []

        premium_selection = None
        if leg.entry_type == "EntryByStrikeType":
            strike_mode = str(leg.strike_parameter)  # "ATM" / "OTM2" / "ITM1" / ...
            strike_value = leg.strike_parameter
        elif leg.entry_type == "EntryByPremium":
            strike_mode = "PREMIUM"
            strike_value = leg.strike_parameter
            premium_selection = build_premium_selection(leg.strike_parameter)
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
            strike_label=None,  # filled in build_portfolio_plan, which knows the symbol
            premium_selection=premium_selection,
            stoploss_pct=stoploss_pct,
            stoploss_text=f"{stoploss_pct:g}%" if stoploss_pct is not None else None,
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
    sqoff_time: str | None  # AlgoTest's ExitIndicators - confirmed maps to mtQuant's SqOff Time, not End Time (see module docstring)
    entry_path: str  # "premium" (Add Leg) | "predefined" (Short Straddle / Short Strangle)
    predefined_strategy: str | None  # "ShortStraddle" | "ShortStrangle" | None
    sl_wait_seconds: int
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


def grid_rows(plan: MTQuantPortfolioPlan) -> list[LegPlan]:
    """Leg-grid order: the live legs first, then the lazy (idle) legs.

    mtQuant numbers that grid from 1, so the third row is Execute_Leg3.
    """
    return [*plan.legs, *plan.idle_legs]


def execute_leg_name(rows: list[LegPlan], leg: LegPlan) -> str:
    """The On Stoploss action that runs this leg's lazy partner.

    CE is tied to the CE lazy leg and PE to the PE lazy leg, by the id
    already stored on the NextLeg re-entry. The number is that partner's
    1-based row in `rows` (live legs, then lazy legs).
    """
    if leg.reentry_kind != "NextLeg" or not leg.reentry_target_leg_id:
        raise ValueError(f"leg {leg.leg_id} has no lazy-leg reference")
    for index, candidate in enumerate(rows, start=1):
        if candidate.leg_id != leg.reentry_target_leg_id:
            continue
        if candidate.ce_pe != leg.ce_pe:
            raise ValueError(
                f"leg {leg.leg_id} is {leg.ce_pe} but its lazy leg {candidate.leg_id} is {candidate.ce_pe}"
            )
        return f"Execute_Leg{index}"
    raise ValueError(f"leg {leg.leg_id} points at lazy leg {leg.reentry_target_leg_id!r}, which is not in the grid")


def build_portfolio_plan(strategy: AlgtstStrategy) -> MTQuantPortfolioPlan:
    notes: list[str] = list(strategy.unmapped_notes)  # carry the parser's own flags forward

    if strategy.strategy_type != "IntradaySameDay":
        notes.append(f"StrategyType {strategy.strategy_type!r} isn't IntradaySameDay - not exercised live yet")

    if strategy.exit_time is None and strategy.entry_time is not None:
        notes.append("sqoff time missing/unmapped but entry time present - see parser notes above")

    live_legs = [LegPlan.from_algtst_leg(leg, idle=False) for leg in strategy.legs]
    idle_legs = [LegPlan.from_algtst_leg(leg, idle=True) for leg in strategy.idle_legs.values()]
    for leg in (*live_legs, *idle_legs):
        label, strike_note = strike_label(leg.ce_pe, leg.strike_mode, strategy.ticker)
        leg.strike_label = label
        if strike_note:
            leg.notes.append(strike_note)
    rows = [*live_legs, *idle_legs]
    for leg in rows:
        if leg.reentry_kind != "NextLeg":
            continue
        try:
            leg.on_sl_action = execute_leg_name(rows, leg)
        except ValueError as exc:
            leg.notes.append(str(exc))

    premium_legs = [leg for leg in live_legs if leg.strike_mode == "PREMIUM"]
    strike_legs = [leg for leg in live_legs if leg.strike_mode not in ("PREMIUM", "UNKNOWN")]
    if premium_legs and strike_legs:
        notes.append("mixes premium-based and ATM/OTM legs - mtQuant needs one entry path per portfolio")
        entry_path = "premium"
        predefined = None
    elif premium_legs or not strike_legs:
        entry_path = "premium"
        predefined = None
    elif all(leg.strike_mode == "ATM" for leg in strike_legs):
        entry_path = "predefined"
        predefined = "ShortStraddle"
    else:
        entry_path = "predefined"
        predefined = "ShortStrangle"

    if strategy.entry_time is not None and _format_time(strategy.entry_time) is None:
        notes.append("entry time is 00:00 - can't subtract one second into the previous day")
    if strategy.exit_time is not None and _format_time(strategy.exit_time) is None:
        notes.append("exit time is 00:00 - can't subtract one second into the previous day")

    # These four have no named mtQuant field (confirmed via its own doc) because mtQuant's
    # default behavior already matches them AT THEIR DEFAULT VALUE (see module docstring) -
    # only flagged here when a strategy actually asks for the non-default behavior, which
    # would need real UI investigation this session never did.
    if strategy.max_positions_per_day not in (None, 1):
        notes.append(f"MaxPositionInADay={strategy.max_positions_per_day} (not 1): mtQuant equivalent not investigated - defaults to unmapped")
    if strategy.skip_initial_candles:
        notes.append(f"SkipInitialCandles={strategy.skip_initial_candles}: mtQuant equivalent not investigated - defaults to unmapped")
    if strategy.square_off_all_legs:
        notes.append("SquareOffAllLegs=True: mtQuant equivalent not investigated (Exit Settings tab's per-leg-independent default is what's confirmed, not this)")
    if strategy.reentry_time_restriction:
        notes.append(f"ReentryTimeRestriction={strategy.reentry_time_restriction!r}: mtQuant equivalent not investigated - defaults to unmapped")

    default_lots = strategy.multiplier
    if default_lots is None:
        notes.append("no portfolio.items multiplier for this strategy - Default Lots left unset")

    return MTQuantPortfolioPlan(
        source_strategy_id=strategy.strategy_id,
        portfolio_name=portfolio_save_name(live_legs, strategy.entry_time),
        symbol=strategy.ticker,
        expiry=strategy.legs[0].expiry_kind if strategy.legs else "",
        underlying="Spot" if strategy.take_underlying_from_cash else "Future",
        default_lots=default_lots,
        dte=strategy.dte,
        run_on_days=_run_on_days(strategy.weekdays),
        start_time=_format_time(strategy.entry_time),
        sqoff_time=_format_time(strategy.exit_time),
        entry_path=entry_path,
        predefined_strategy=predefined,
        sl_wait_seconds=SL_WAIT_SECONDS,
        legs=live_legs,
        idle_legs=idle_legs,
        overall_stoploss=strategy.overall_sl,
        overall_target=strategy.overall_target,
        overall_trail=strategy.overall_trail_sl,
        lock_and_trail=strategy.lock_and_trail,
        move_sl_to_cost=strategy.trail_sl_to_breakeven,
        notes=notes,
    )
