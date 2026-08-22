"""Coarse-to-fine refinement: take a results CSV, find the best rows by Return/MaxDD,
and narrow the sweep ranges to center on whatever won - one round of the
coarse-grid-then-refine workflow instead of hand-editing every range."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.web.models import (
    LegRiskConfig,
    LegUIConfig,
    NumericRange,
    OverallRiskConfig,
    StrikeConfig,
    SweepUIConfig,
    TimeRange,
)


def rmdd_sort_key(row: dict) -> float:
    try:
        return float(row.get("return_max_dd", ""))
    except (TypeError, ValueError):
        return float("-inf")  # missing/error rows sink to the bottom, never crash the sort


def _numeric_values(rows: list[dict], column: str) -> list[float]:
    values = []
    for row in rows:
        raw = row.get(column)
        if raw not in (None, ""):
            try:
                values.append(float(raw))
            except ValueError:
                pass
    return values


def _narrow_range(old: NumericRange, values: list[float], floor: float | None = None) -> NumericRange:
    if not values:
        return old
    lo, hi = min(values), max(values)
    span = hi - lo
    pad = max(old.step, span * 0.25)
    new_min = lo - pad
    new_max = hi + pad
    if floor is not None:
        new_min = max(new_min, floor)
    new_step = old.step / 2
    if new_step < 1e-6:
        new_step = old.step
    return NumericRange(min=round(new_min, 4), max=round(new_max, 4), step=round(new_step, 4))


def _narrow_time(old: TimeRange, values: list[str]) -> TimeRange:
    if not values:
        return old
    times = sorted(set(values))
    if old.fixed or len(times) == 1:
        return TimeRange(start=times[0], end=times[0], interval_minutes=old.interval_minutes, fixed=True)

    lo = datetime.strptime(times[0], "%H:%M")
    hi = datetime.strptime(times[-1], "%H:%M")
    pad = timedelta(minutes=old.interval_minutes)
    day_start = lo.replace(hour=0, minute=0)
    new_start = max(lo - pad, day_start)
    new_end = hi + pad
    new_interval = max(old.interval_minutes // 2, 1)
    return TimeRange(start=new_start.strftime("%H:%M"), end=new_end.strftime("%H:%M"), interval_minutes=new_interval, fixed=False)


def _narrow_strike(old: StrikeConfig, rows: list[dict], prefix: str) -> StrikeConfig:
    offsets_seen: list[str] = []
    premiums_seen: list[float] = []
    for row in rows:
        plain = row.get(f"{prefix}.strike")
        if plain:
            offsets_seen.append(plain)
            continue
        if row.get(f"{prefix}.strike.mode") == "premium_closest":
            val = row.get(f"{prefix}.strike.value")
            if val not in (None, ""):
                premiums_seen.append(float(val))

    use_offset = bool(offsets_seen)
    use_premium = bool(premiums_seen)
    return StrikeConfig(
        use_offset=use_offset,
        offsets=sorted(set(offsets_seen)) if use_offset else old.offsets,
        use_closest_premium=use_premium,
        premium_range=_narrow_range(old.premium_range, premiums_seen, floor=0) if use_premium else old.premium_range,
    )


def _narrow_leg(old: LegUIConfig, rows: list[dict], prefix: str) -> LegUIConfig:
    return LegUIConfig(
        action=old.action,
        option_type=old.option_type,
        lots=_narrow_range(old.lots, _numeric_values(rows, f"{prefix}.lots"), floor=1),
        strike=_narrow_strike(old.strike, rows, prefix),
    )


def _narrow_leg_risk(old: LegRiskConfig, rows: list[dict]) -> LegRiskConfig:
    target_values = _numeric_values(rows, "leg_risk.target_pct")
    stoploss_values = _numeric_values(rows, "leg_risk.stoploss_pct")
    points_rows = [r for r in rows if r.get("leg_risk.trail.type") == "Points"]
    percentage_rows = [r for r in rows if r.get("leg_risk.trail.type") == "Percentage"]

    return LegRiskConfig(
        target_enabled=bool(target_values),
        target_pct=_narrow_range(old.target_pct, target_values, floor=0) if target_values else old.target_pct,
        stoploss_enabled=bool(stoploss_values),
        stoploss_pct=_narrow_range(old.stoploss_pct, stoploss_values, floor=0) if stoploss_values else old.stoploss_pct,
        trail_points_enabled=bool(points_rows),
        trail_points_x=_narrow_range(old.trail_points_x, _numeric_values(points_rows, "leg_risk.trail.x"), floor=0)
        if points_rows
        else old.trail_points_x,
        trail_points_y=_narrow_range(old.trail_points_y, _numeric_values(points_rows, "leg_risk.trail.y"), floor=0)
        if points_rows
        else old.trail_points_y,
        trail_percentage_enabled=bool(percentage_rows),
        trail_percentage_x=_narrow_range(old.trail_percentage_x, _numeric_values(percentage_rows, "leg_risk.trail.x"), floor=0)
        if percentage_rows
        else old.trail_percentage_x,
        trail_percentage_y=_narrow_range(old.trail_percentage_y, _numeric_values(percentage_rows, "leg_risk.trail.y"), floor=0)
        if percentage_rows
        else old.trail_percentage_y,
    )


def _narrow_overall_risk(old: OverallRiskConfig, rows: list[dict], column_prefix: str) -> OverallRiskConfig:
    pct_rows = [r for r in rows if r.get(f"{column_prefix}.kind") == "percentage"]
    amt_rows = [r for r in rows if r.get(f"{column_prefix}.kind") == "amount"]
    pct_values = _numeric_values(pct_rows, f"{column_prefix}.value")
    amt_values = _numeric_values(amt_rows, f"{column_prefix}.value")

    return OverallRiskConfig(
        use_percentage=bool(pct_values),
        percentage_range=_narrow_range(old.percentage_range, pct_values, floor=0) if pct_values else old.percentage_range,
        use_amount=bool(amt_values),
        amount_range=_narrow_range(old.amount_range, amt_values, floor=0) if amt_values else old.amount_range,
    )


def _narrow_trail_sl(cfg: SweepUIConfig, rows: list[dict]) -> dict[str, Any]:
    active_rows = [r for r in rows if r.get("trail_sl.x") not in (None, "")]
    none_rows = [r for r in rows if r.get("trail_sl.x") in (None, "")]

    if not active_rows:
        # Trailing never won among the top results - turn it off for the refined pass.
        return {
            "trail_sl_enabled": False,
            "trail_sl_include_none": cfg.trail_sl_include_none,
            "trail_sl_x": cfg.trail_sl_x,
            "trail_sl_y": cfg.trail_sl_y,
            "trail_sl_step": cfg.trail_sl_step,
            "trail_sl_trail_by": cfg.trail_sl_trail_by,
        }

    return {
        "trail_sl_enabled": True,
        "trail_sl_include_none": bool(none_rows),
        "trail_sl_x": _narrow_range(cfg.trail_sl_x, _numeric_values(active_rows, "trail_sl.x"), floor=0),
        "trail_sl_y": _narrow_range(cfg.trail_sl_y, _numeric_values(active_rows, "trail_sl.y"), floor=0),
        "trail_sl_step": _narrow_range(cfg.trail_sl_step, _numeric_values(active_rows, "trail_sl.step"), floor=0),
        "trail_sl_trail_by": _narrow_range(cfg.trail_sl_trail_by, _numeric_values(active_rows, "trail_sl.trail_by"), floor=0),
    }


def narrow_config(cfg: SweepUIConfig, rows: list[dict], top_n: int = 10) -> SweepUIConfig:
    """Build a new SweepUIConfig with every range narrowed around whatever won in the
    top `top_n` successful rows by Return/MaxDD. Doesn't mutate `cfg`."""
    successful = [r for r in rows if r.get("status") == "ok"]
    if not successful:
        raise ValueError("No successful ('ok') rows to narrow from yet - run the sweep first.")

    successful.sort(key=rmdd_sort_key, reverse=True)
    top_rows = successful[:top_n]

    new = cfg.model_copy(deep=True)

    new.entry_time = _narrow_time(cfg.entry_time, [r.get("entry_time") for r in top_rows if r.get("entry_time")])
    new.exit_time = _narrow_time(cfg.exit_time, [r.get("exit_time") for r in top_rows if r.get("exit_time")])

    if cfg.linked_ce_pe:
        new.shared_leg = _narrow_leg(cfg.shared_leg, top_rows, "legs.0")
    else:
        new.legs = [_narrow_leg(leg, top_rows, f"legs.{i}") for i, leg in enumerate(cfg.legs)]

    new.leg_risk = _narrow_leg_risk(cfg.leg_risk, top_rows)
    new.overall_stoploss = _narrow_overall_risk(cfg.overall_stoploss, top_rows, "stoploss")
    new.overall_target = _narrow_overall_risk(cfg.overall_target, top_rows, "target")

    for field, value in _narrow_trail_sl(cfg, top_rows).items():
        setattr(new, field, value)

    return new
