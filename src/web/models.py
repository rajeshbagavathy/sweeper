from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NumericRange(BaseModel):
    min: float
    max: float
    step: float = 1.0  # the interval between consecutive values, e.g. min=10 max=120 step=5 -> 10, 15, 20, ... 120

    def as_list(self) -> list[float]:
        if self.max <= self.min:
            return [self.min]
        values = []
        v = self.min
        # Guard against a zero/negative step turning this into an infinite loop.
        step = self.step if self.step > 0 else 1.0
        while v <= self.max + 1e-9:
            values.append(round(v, 6))
            v += step
        return values


class TimeRange(BaseModel):
    start: str  # "HH:MM"
    end: str
    interval_minutes: int = 15
    fixed: bool = False  # UI convenience: when true, the frontend mirrors start into end

    def as_list(self) -> list[str]:
        from datetime import datetime, timedelta

        if self.fixed:
            return [self.start]

        start = datetime.strptime(self.start, "%H:%M")
        end = datetime.strptime(self.end, "%H:%M")
        if end <= start or self.interval_minutes <= 0:
            return [self.start]
        values = []
        t = start
        while t <= end:
            values.append(t.strftime("%H:%M"))
            t += timedelta(minutes=self.interval_minutes)
        return values


class StrikeConfig(BaseModel):
    """Either or both of these can be on - the sweep tries the union of both sets of
    strike choices, not their cross product (see src/web/expand.py)."""

    use_offset: bool = True
    offsets: list[str] = Field(default_factory=lambda: ["ATM"])
    use_closest_premium: bool = False
    premium_range: NumericRange = Field(default_factory=lambda: NumericRange(min=30, max=30, step=5))


class LegUIConfig(BaseModel):
    action: Literal["BUY", "SELL"] = "SELL"
    option_type: Literal["CE", "PE"] = "CE"
    lots: NumericRange = Field(default_factory=lambda: NumericRange(min=1, max=1, step=1))
    strike: StrikeConfig = Field(default_factory=StrikeConfig)


class OverallRiskConfig(BaseModel):
    """Overall Stop Loss / Target: AlgoTest offers a percentage-of-premium basis
    ("Total Premium %") and an absolute-amount basis ("Max Loss" / "Max Profit").
    Either or both can be checked - unioned into one sweep dimension, not crossed."""

    use_percentage: bool = True
    percentage_range: NumericRange = Field(default_factory=lambda: NumericRange(min=20, max=50, step=10))
    use_amount: bool = False
    amount_range: NumericRange = Field(default_factory=lambda: NumericRange(min=5000, max=5000, step=1000))


class LegRiskConfig(BaseModel):
    """Per-leg Target Profit / Stop Loss / Trail SL - shared identically across every
    leg (CE and PE both), per the user's simplification request."""

    target_enabled: bool = False
    target_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=20, max=20, step=10))

    stoploss_enabled: bool = False
    stoploss_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=20, max=20, step=10))

    # Each trailing type gets its own from/to/interval pair; checking both tries the
    # union of Points-mode combos and Percentage-mode combos (not their cross product).
    trail_points_enabled: bool = False
    trail_points_x: NumericRange = Field(default_factory=lambda: NumericRange(min=10, max=10, step=5))
    trail_points_y: NumericRange = Field(default_factory=lambda: NumericRange(min=5, max=5, step=5))

    trail_percentage_enabled: bool = False
    trail_percentage_x: NumericRange = Field(default_factory=lambda: NumericRange(min=1, max=1, step=0.5))
    trail_percentage_y: NumericRange = Field(default_factory=lambda: NumericRange(min=0.5, max=0.5, step=0.5))


class SweepUIConfig(BaseModel):
    instrument: str = "NIFTY"
    start_date: str = "2025-08-22"
    end_date: str = "2026-08-22"

    entry_time: TimeRange = Field(default_factory=lambda: TimeRange(start="09:20", end="10:15", interval_minutes=15))
    exit_time: TimeRange = Field(default_factory=lambda: TimeRange(start="15:10", end="15:15", interval_minutes=5))

    # When linked_ce_pe is True (the common short-strangle/straddle case), shared_leg's
    # action/lots/strike config is applied identically to a CE leg and a PE leg, and -
    # importantly - that shared dimension only varies *once* in the combination math
    # (see src/web/expand.py) rather than being cross-multiplied between two
    # independently-varying legs. Uncheck to configure CE and PE independently via `legs`.
    linked_ce_pe: bool = True
    shared_leg: LegUIConfig = Field(default_factory=lambda: LegUIConfig(action="SELL"))
    legs: list[LegUIConfig] = Field(
        default_factory=lambda: [
            LegUIConfig(action="SELL", option_type="CE"),
            LegUIConfig(action="SELL", option_type="PE"),
        ]
    )

    # Leg-level risk management - always one shared config applied to every leg,
    # regardless of linked_ce_pe (separate AlgoTest feature from the overall
    # stoploss/target/trailing below, which apply once to the whole combined position).
    leg_risk: LegRiskConfig = Field(default_factory=LegRiskConfig)

    overall_stoploss: OverallRiskConfig = Field(default_factory=OverallRiskConfig)
    overall_target: OverallRiskConfig = Field(
        default_factory=lambda: OverallRiskConfig(percentage_range=NumericRange(min=30, max=80, step=25))
    )

    # Overall trailing always uses AlgoTest's "Lock and Trail" mode when enabled - it's
    # the only mode exposing all 4 of these fields (confirmed live).
    trail_sl_enabled: bool = False
    trail_sl_include_none: bool = True
    trail_sl_x: NumericRange = Field(default_factory=lambda: NumericRange(min=20, max=20, step=5))  # "If profit reaches"
    trail_sl_y: NumericRange = Field(default_factory=lambda: NumericRange(min=10, max=10, step=5))  # "Lock profit"
    trail_sl_step: NumericRange = Field(default_factory=lambda: NumericRange(min=10, max=10, step=5))  # "For every increase in profit by"
    trail_sl_trail_by: NumericRange = Field(default_factory=lambda: NumericRange(min=5, max=5, step=5))  # "Trail profit by"

    exclude: list[str] = Field(default_factory=list)
    limit: int | None = None
    shuffle: bool = False

    # Results-panel settings applied once results are ready, before scraping - not
    # sweep dimensions, the same setting is used for every combination. Brokerage and
    # taxes & charges are always turned on (not exposed as a toggle) per the request
    # that prompted this.
    slippage_pct: float = 1.0
    dte_values: list[int] = Field(default_factory=lambda: [0])

    delay: float = 2.0
    headless: bool = False
    result_timeout: int = 180
    max_retries: int = 2
