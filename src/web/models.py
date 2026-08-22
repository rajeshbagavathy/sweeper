from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NumericRange(BaseModel):
    min: float
    max: float
    step: float = 1.0

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

    def as_list(self) -> list[str]:
        from datetime import datetime, timedelta

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


StrikeMode = Literal["offset", "premium_range", "premium_closest"]


class LegUIConfig(BaseModel):
    action: Literal["BUY", "SELL"] = "SELL"
    option_type: Literal["CE", "PE"] = "CE"
    lots: NumericRange = Field(default_factory=lambda: NumericRange(min=1, max=1, step=1))
    strike_mode: StrikeMode = "offset"
    offsets: list[str] = Field(default_factory=lambda: ["ATM"])
    premium_lower: NumericRange = Field(default_factory=lambda: NumericRange(min=30, max=30, step=5))
    premium_upper: NumericRange = Field(default_factory=lambda: NumericRange(min=55, max=55, step=5))
    premium_value: NumericRange = Field(default_factory=lambda: NumericRange(min=30, max=30, step=5))


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

    stoploss_enabled: bool = True
    stoploss_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=20, max=50, step=10))

    target_enabled: bool = True
    target_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=30, max=80, step=25))

    trail_sl_enabled: bool = False
    trail_sl_include_none: bool = True
    trail_sl_x: NumericRange = Field(default_factory=lambda: NumericRange(min=20, max=20, step=5))
    trail_sl_y: NumericRange = Field(default_factory=lambda: NumericRange(min=10, max=10, step=5))

    exclude: list[str] = Field(default_factory=list)
    limit: int | None = None
    shuffle: bool = False

    delay: float = 2.0
    headless: bool = False
    result_timeout: int = 180
    max_retries: int = 2
