from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.web.time_buckets import MARKET_CLOSE, MARKET_OPEN

# An "Underlying %" leg SL above this is not a real stop loss - the underlying
# (index) essentially never moves this much intraday, so the SL practically never
# trips and the leg's loss is uncapped in exactly the scenario a hard SL exists to
# prevent. Confirmed live: a batch of sweeps meant to use 0.14%-0.25% underlying
# moves were mistakenly entered as whole percent (14-25), silently producing
# thousands of "protected" combos that were never actually protected. Shared with
# src/web/portfolio.py's has_hard_stop_loss, which applies the same cutoff to rows
# already sitting in a results CSV.
MAX_SANE_UNDERLYING_SL_PCT = 1.0


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

    @model_validator(mode="after")
    def _within_market_hours(self) -> "TimeRange":
        # Confirmed live (2026-08-29): a config with exit_time="23:20" (11:20pm,
        # meant as 11:20am - a 24-hour/AM-PM mixup) sailed straight through to a real
        # sweep and burned the full 180s timeout on every single combo, since
        # AlgoTest has no result to give for an exit time ~8 hours after market
        # close. Catching an out-of-hours value here, at config-save/validation time,
        # surfaces the mistake immediately instead of after minutes of automation.
        for label, value in (("start", self.start), ("end", self.end)):
            try:
                datetime.strptime(value, "%H:%M")
            except (ValueError, TypeError):
                raise ValueError(f'{label}={value!r} is not a valid "HH:MM" time.')
            if not (MARKET_OPEN <= value <= MARKET_CLOSE):
                raise ValueError(
                    f"{label}={value!r} is outside market hours ({MARKET_OPEN}-{MARKET_CLOSE}) - "
                    f"check for an AM/PM or 24-hour mixup (e.g. 23:20 instead of 11:20)."
                )
        return self

    def as_list(self) -> list[str]:
        from datetime import timedelta

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

    # A second, independent basis for the same leg-level Stop Loss - "Underlying %"
    # triggers off a % move in the underlying's price instead of the leg's own
    # premium. Confirmed live: AlgoTest's leg Stop Loss type dropdown offers both as
    # separate options reusing the same single value input. Checking both tries the
    # union of premium-basis combos and underlying-basis combos (not their cross
    # product) - same convention as Trail SL's Points/Percentage checkboxes below.
    stoploss_underlying_enabled: bool = False
    stoploss_underlying_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=0.2, max=0.2, step=0.05))

    # Each trailing type gets its own from/to/interval pair; checking both tries the
    # union of Points-mode combos and Percentage-mode combos (not their cross product).
    trail_points_enabled: bool = False
    trail_points_x: NumericRange = Field(default_factory=lambda: NumericRange(min=10, max=10, step=5))
    trail_points_y: NumericRange = Field(default_factory=lambda: NumericRange(min=5, max=5, step=5))

    trail_percentage_enabled: bool = False
    trail_percentage_x: NumericRange = Field(default_factory=lambda: NumericRange(min=1, max=1, step=0.5))
    trail_percentage_y: NumericRange = Field(default_factory=lambda: NumericRange(min=0.5, max=0.5, step=0.5))

    # AlgoTest's per-leg "Simple Momentum" entry criteria: only enter the leg once the
    # underlying premium has moved up/down by this percent from the reference price.
    # Checking both directions tries the union of up-move combos and down-move combos
    # (not their cross product), same convention as the Trail SL checkboxes above.
    momentum_up_enabled: bool = False
    momentum_up_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=5, max=10, step=1))

    momentum_down_enabled: bool = False
    momentum_down_pct: NumericRange = Field(default_factory=lambda: NumericRange(min=5, max=10, step=1))

    # AlgoTest's per-leg "Re-entry on SL": re-enter the leg after it's stopped out.
    # Phase 1 exposes 3 of AlgoTest's 7 re-entry types (RE ASAP, RE COST, Lazy Leg)
    # per the user's request; checking any combination tries each as a separate
    # alternative (union, not cross product with anything else, and never more
    # than one per combo - AlgoTest's own Re-entry on SL dropdown only ever holds
    # ONE type at a time, so a combo lands on exactly one of them, same as
    # RE_ASAP vs RE_COST already do today). Re-entry count (how many times) only
    # applies to RE_ASAP/RE_COST - a single fixed value for phase 1, not swept.
    #
    # "LAZY_LEG": instead of re-entering the SAME strike, introduce a NEW,
    # farther-OTM leg once the original is stopped out - smoothing the loss and
    # catching a reversal. Every value a lazy leg needs (strike/SL%/trail/
    # momentum) is derived, per leg, from that SAME leg's own already-chosen
    # settings plus the instrument, not configured separately here - see
    # src/lazy_leg.py's derive_lazy_leg for the exact rule (src/web/expand.py's
    # nest_combo calls it whenever a combo lands on this choice). Only takes
    # effect on a leg whose OWN Stop Loss is percentage-based (not "Underlying
    # %") and between 25-60% - outside that, or with no leg-level Stop Loss at
    # all, that leg simply gets no lazy leg for that combo.
    reentry_sl_enabled: bool = False
    reentry_sl_types: list[Literal["RE_ASAP", "RE_COST", "LAZY_LEG"]] = Field(default_factory=lambda: ["RE_ASAP"])
    reentry_sl_count: int = Field(default=1, ge=1, le=6)

    @model_validator(mode="after")
    def _trail_requires_stoploss(self) -> "LegRiskConfig":
        # Confirmed live on algotest.in: Trail SL trails a leg's Stop Loss, so the UI
        # silently rejects "Start Backtest" (a toast fades before you notice it) if
        # Trail SL is enabled on a leg without that leg's Stop Loss also being enabled
        # - either basis (premium % or underlying %) satisfies this.
        if (self.trail_points_enabled or self.trail_percentage_enabled) and not (
            self.stoploss_enabled or self.stoploss_underlying_enabled
        ):
            raise ValueError(
                "Leg-level Trail SL requires Leg-level Stop Loss to also be enabled "
                "(AlgoTest trails the Stop Loss - it can't run standalone). "
                "Enable Stop Loss (either basis) and set a value, then Trail SL will work."
            )
        return self


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
    # Brokerage rate (Rs per order) - a one-time setting on AlgoTest's side (confirmed
    # live: it persists across page loads within the same browser session), so this is
    # applied once at the start of a sweep run, not re-applied per combo.
    brokerage_rate: float = 20.0

    # Downloads a combo's trade report right after its backtest completes, IF it
    # looks good enough by the criteria below - avoiding a full second replay later
    # just to fetch the report for something Correlate would have wanted anyway
    # (the login+fill+180s-wait cost is what's expensive; the download itself is a
    # few extra seconds on a page that's already sitting there with the result on
    # screen). Uses the same combo_id-keyed cache Correlate's own download step
    # checks, so anything downloaded here is simply already-cached from its
    # perspective. Return/MaxDD (not reward:risk or win rate) is the quality gate -
    # confirmed this session that those two trade off against each other (high
    # win-rate strategies cluster at low reward:risk and vice versa), so gating on
    # either alone would systematically exclude a whole category of good strategies.
    auto_download_enabled: bool = True
    auto_download_min_return_max_dd: float = 1.5
    # 0 = no minimum. Set > 0 to also require this many trades before a combo
    # qualifies, guarding against a great-looking ratio that's really just 2-3 lucky
    # trades on a small sample.
    auto_download_min_trades: int = 0
    # When more than one DTE is selected above: capture one independent row +
    # report PER DTE instead of one report reflecting them combined - re-filtering
    # the SAME already-completed backtest to a single DTE at a time (no extra
    # "Start Backtest"). Each variant gets its own id (f"{combo_id}_dte{n}" - a
    # plain composite string, not a new hash) so it's a first-class, independently
    # rankable row wherever Correlate/Portfolio read the results CSV, with zero
    # change to how any OTHER combo's id is computed. Default True per explicit
    # user request - blended multi-DTE numbers hide which individual DTE is
    # actually good (confirmed live: this was OFF by inherited default the one
    # time it silently mattered, on top of the separate auto-download-gating bug
    # this session already fixed in src/runner.py). False = combined behavior,
    # for whenever a blended view is deliberately what's wanted.
    capture_dte_individually: bool = True

    # A combo's identity hash includes start_date/end_date (confirmed live: an
    # identical strategy differing only in end_date gets a completely different
    # combo_id) - so a sweep with a trailing end_date (e.g. always "today")
    # would otherwise re-discover the exact same strategies as brand new every
    # time, fragmenting their history and wasting replay budget re-downloading
    # what's already in output/combo_registry.csv. When on (the default): a
    # combo whose underlying strategy (see store.strategy_key) already has a
    # record under a different combo_id is NOT replayed - it's captured into
    # output/pending_duplicate_refresh.csv instead, for Force re-download to
    # bring current against its EXISTING combo_id. Off = today's unchanged
    # behavior (every combo always replayed, no registry lookup at all) - the
    # escape hatch for a deliberate full rediscovery pass.
    skip_known_duplicate_strategies: bool = True

    # Runs this many tabs concurrently within the same browser session (AlgoTest docs
    # cite a limit of 15 concurrent strategy runs per account). 1 = today's sequential
    # behavior. Each combo's cost is dominated by AlgoTest's own server-side backtest
    # computation, not our client automation, so this gives a close-to-linear speedup.
    parallelism: int = Field(default=1, ge=1, le=15)

    # Optional 2nd/3rd AlgoTest account (ALGOTEST_EMAIL_2/PASSWORD_2,
    # ALGOTEST_EMAIL_3/PASSWORD_3 in .env) - confirmed live that AlgoTest throttles
    # concurrency per account, not per machine/IP, so each extra account gets its own
    # independent budget rather than competing with the others for the same one. 0 =
    # disabled (today's single-account behavior); combos are still partitioned so no
    # combo ever runs on more than one account.
    parallelism_account2: int = Field(default=0, ge=0, le=15)
    parallelism_account3: int = Field(default=0, ge=0, le=15)

    delay: float = 2.0
    headless: bool = False
    result_timeout: int = 180
    max_retries: int = 2
