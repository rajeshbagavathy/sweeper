from __future__ import annotations

import pytest

from src.web.expand import expand_ui_config
from src.web.models import (
    LegRiskConfig,
    LegUIConfig,
    NumericRange,
    OverallRiskConfig,
    StrikeConfig,
    SweepUIConfig,
    TimeRange,
)
from src.web.narrow import narrow_config

# narrow_config reads csv.DictReader-style rows, i.e. everything is a *string* - use
# string values throughout so these tests exercise the real parsing path.


def _row(**overrides) -> dict:
    base = {
        "status": "ok",
        "return_max_dd": "1.0",
        "entry_time": "09:20",
        "exit_time": "15:10",
        "legs.0.lots": "1",
        "legs.0.strike": "ATM",
        "legs.0.strike.mode": "",
        "legs.0.strike.value": "",
        "leg_risk.target_pct": "",
        "leg_risk.stoploss_pct": "",
        "leg_risk.trail.type": "",
        "leg_risk.trail.x": "",
        "leg_risk.trail.y": "",
        "stoploss.kind": "percentage",
        "stoploss.value": "30",
        "target.kind": "percentage",
        "target.value": "50",
        "trail_sl.x": "",
        "trail_sl.y": "",
        "trail_sl.step": "",
        "trail_sl.trail_by": "",
    }
    base.update(overrides)
    return base


def _base_cfg(**overrides) -> SweepUIConfig:
    base = dict(
        entry_time=TimeRange(start="09:00", end="10:00", interval_minutes=10),
        exit_time=TimeRange(start="15:10", end="15:10", interval_minutes=5, fixed=True),
        linked_ce_pe=True,
        shared_leg=LegUIConfig(action="SELL", strike=StrikeConfig(use_offset=True, offsets=["ATM", "OTM1", "OTM2"])),
    )
    base.update(overrides)
    return SweepUIConfig(**base)


def test_raises_when_no_successful_rows():
    cfg = _base_cfg()
    with pytest.raises(ValueError):
        narrow_config(cfg, [_row(status="error")], top_n=10)


def test_narrows_entry_time_around_winners():
    cfg = _base_cfg()
    rows = [
        _row(return_max_dd="2.0", entry_time="09:30"),
        _row(return_max_dd="1.5", entry_time="09:40"),
        _row(return_max_dd="0.1", entry_time="09:00"),  # not in top 2
    ]
    narrowed = narrow_config(cfg, rows, top_n=2)
    assert narrowed.entry_time.start <= "09:30"
    assert narrowed.entry_time.end >= "09:40"
    assert narrowed.entry_time.interval_minutes == 5  # halved from 10


def test_entry_time_converges_to_fixed_when_one_winner():
    cfg = _base_cfg()
    rows = [_row(return_max_dd="2.0", entry_time="09:30")]
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.entry_time.start == "09:30"
    assert narrowed.entry_time.end == "09:30"
    assert narrowed.entry_time.fixed is True


def test_narrows_offsets_to_only_winning_values():
    cfg = _base_cfg()
    rows = [
        _row(return_max_dd="2.0", **{"legs.0.strike": "OTM1"}),
        _row(return_max_dd="1.5", **{"legs.0.strike": "OTM1"}),
        _row(return_max_dd="1.0", **{"legs.0.strike": "ATM"}),
    ]
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.shared_leg.strike.use_offset is True
    assert set(narrowed.shared_leg.strike.offsets) == {"ATM", "OTM1"}


def test_narrows_premium_closest_leg():
    cfg = _base_cfg(
        shared_leg=LegUIConfig(action="SELL", strike=StrikeConfig(use_offset=False, use_closest_premium=True, premium_range=NumericRange(min=20, max=40, step=5)))
    )
    rows = [
        _row(return_max_dd="2.0", **{"legs.0.strike": "", "legs.0.strike.mode": "premium_closest", "legs.0.strike.value": "30"}),
        _row(return_max_dd="1.5", **{"legs.0.strike": "", "legs.0.strike.mode": "premium_closest", "legs.0.strike.value": "35"}),
    ]
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.shared_leg.strike.use_closest_premium is True
    assert narrowed.shared_leg.strike.premium_range.min <= 30
    assert narrowed.shared_leg.strike.premium_range.max >= 35


def test_leg_risk_trail_type_disambiguation():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            trail_points_enabled=True,
            trail_points_x=NumericRange(min=1, max=20, step=1),
            trail_percentage_enabled=True,
        )
    )
    rows = [
        _row(return_max_dd="2.0", **{"leg_risk.trail.type": "Points", "leg_risk.trail.x": "10", "leg_risk.trail.y": "5"}),
        _row(return_max_dd="1.5", **{"leg_risk.trail.type": "Points", "leg_risk.trail.x": "12", "leg_risk.trail.y": "6"}),
    ]
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.leg_risk.trail_points_enabled is True
    assert narrowed.leg_risk.trail_percentage_enabled is False  # never won -> turned off
    assert narrowed.leg_risk.trail_points_x.min <= 10
    assert narrowed.leg_risk.trail_points_x.max >= 12


def test_overall_risk_basis_disambiguation():
    cfg = _base_cfg(
        overall_stoploss=OverallRiskConfig(use_percentage=True, use_amount=True, amount_range=NumericRange(min=1000, max=20000, step=1000))
    )
    rows = [
        _row(return_max_dd="2.0", **{"stoploss.kind": "amount", "stoploss.value": "8000"}),
        _row(return_max_dd="1.5", **{"stoploss.kind": "amount", "stoploss.value": "9000"}),
    ]
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.overall_stoploss.use_amount is True
    assert narrowed.overall_stoploss.use_percentage is False  # never won among winners
    assert narrowed.overall_stoploss.amount_range.min <= 8000
    assert narrowed.overall_stoploss.amount_range.max >= 9000


def test_overall_trail_sl_disabled_when_never_wins():
    cfg = _base_cfg(trail_sl_enabled=True, trail_sl_include_none=True)
    rows = [_row(return_max_dd="2.0")]  # trail_sl.x blank -> "no trailing" won
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.trail_sl_enabled is False


def test_overall_trail_sl_narrows_when_it_wins():
    cfg = _base_cfg(
        trail_sl_enabled=True,
        trail_sl_include_none=True,
        trail_sl_x=NumericRange(min=1000, max=20000, step=1000),
    )
    rows = [
        _row(return_max_dd="2.0", **{"trail_sl.x": "8000", "trail_sl.y": "1000", "trail_sl.step": "2000", "trail_sl.trail_by": "500"}),
        _row(return_max_dd="1.0", **{}),  # a "no trailing" row also made the top N
    ]
    narrowed = narrow_config(cfg, rows, top_n=10)
    assert narrowed.trail_sl_enabled is True
    assert narrowed.trail_sl_include_none is True  # "no trailing" was still competitive
    assert narrowed.trail_sl_x.min <= 8000


def test_narrow_config_output_is_still_expandable():
    """A narrowed config must remain a valid, expandable SweepUIConfig."""
    cfg = _base_cfg()
    rows = [_row(return_max_dd="2.0", entry_time="09:30")]
    narrowed = narrow_config(cfg, rows, top_n=10)
    combos = expand_ui_config(narrowed)
    assert len(combos) >= 1
