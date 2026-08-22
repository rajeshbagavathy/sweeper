from __future__ import annotations

from src.web.expand import expand_ui_config, nest_combo, to_sweep_config
from src.web.models import LegUIConfig, NumericRange, SweepUIConfig, TimeRange


def test_numeric_range_single_value_when_min_equals_max():
    assert NumericRange(min=5, max=5, step=10).as_list() == [5]


def test_numeric_range_expands_with_step():
    assert NumericRange(min=20, max=50, step=10).as_list() == [20, 30, 40, 50]


def test_numeric_range_handles_non_divisible_span():
    assert NumericRange(min=30, max=55, step=10).as_list() == [30, 40, 50]


def test_time_range_expands_by_interval():
    assert TimeRange(start="10:00", end="11:00", interval_minutes=15).as_list() == [
        "10:00",
        "10:15",
        "10:30",
        "10:45",
        "11:00",
    ]


def test_time_range_single_value_when_end_before_start():
    assert TimeRange(start="10:00", end="09:00", interval_minutes=15).as_list() == ["10:00"]


def _base_cfg(**overrides) -> SweepUIConfig:
    base = dict(
        instrument="NIFTY",
        start_date="2025-01-01",
        end_date="2025-06-01",
        entry_time=TimeRange(start="09:20", end="09:20", interval_minutes=15),
        exit_time=TimeRange(start="15:10", end="15:10", interval_minutes=5),
        legs=[LegUIConfig(action="SELL", option_type="CE", strike_mode="offset", offsets=["ATM"])],
        stoploss_enabled=False,
        target_enabled=False,
        trail_sl_enabled=False,
    )
    base.update(overrides)
    return SweepUIConfig(**base)


def test_to_sweep_config_prefixes_per_leg_keys():
    cfg = _base_cfg(
        legs=[
            LegUIConfig(action="SELL", option_type="CE", strike_mode="offset", offsets=["ATM", "OTM1"]),
            LegUIConfig(action="SELL", option_type="PE", strike_mode="offset", offsets=["ATM"]),
        ]
    )
    sweep = to_sweep_config(cfg)
    assert sweep.vary["leg0_offset"] == ["ATM", "OTM1"]
    assert sweep.vary["leg1_offset"] == ["ATM"]
    assert sweep.vary["leg0_lots"] == [1]


def test_to_sweep_config_adds_premium_range_exclude_rule():
    cfg = _base_cfg(
        legs=[
            LegUIConfig(
                action="SELL",
                option_type="CE",
                strike_mode="premium_range",
                premium_lower=NumericRange(min=30, max=40, step=10),
                premium_upper=NumericRange(min=35, max=55, step=20),
            )
        ]
    )
    sweep = to_sweep_config(cfg)
    assert "leg0_premium_upper <= leg0_premium_lower" in sweep.exclude
    combos = expand_ui_config(cfg)
    for combo in combos:
        strike = combo["legs"][0]["strike"]
        assert strike["upper"] > strike["lower"]


def test_to_sweep_config_adds_target_vs_stoploss_exclude():
    cfg = _base_cfg(
        stoploss_enabled=True,
        stoploss_pct=NumericRange(min=20, max=20, step=10),
        target_enabled=True,
        target_pct=NumericRange(min=10, max=30, step=10),
    )
    combos = expand_ui_config(cfg)
    for combo in combos:
        assert combo["target_pct"] > combo["stoploss_pct"]


def test_nest_combo_reassembles_offset_mode_leg():
    cfg = _base_cfg()
    flat = {"instrument": "NIFTY", "entry_time": "09:20", "leg0_lots": 1, "leg0_offset": "ATM"}
    nested = nest_combo(flat, cfg)
    assert nested["legs"] == [{"action": "SELL", "option_type": "CE", "lots": 1, "strike": "ATM"}]
    assert nested["stoploss_pct"] is None
    assert nested["trail_sl"] is None


def test_nest_combo_reassembles_premium_range_leg():
    cfg = _base_cfg(
        legs=[
            LegUIConfig(
                action="BUY",
                option_type="PE",
                strike_mode="premium_range",
                premium_lower=NumericRange(min=30, max=30, step=5),
                premium_upper=NumericRange(min=55, max=55, step=5),
            )
        ]
    )
    flat = {"leg0_lots": 2, "leg0_premium_lower": 30, "leg0_premium_upper": 55}
    nested = nest_combo(flat, cfg)
    assert nested["legs"] == [
        {"action": "BUY", "option_type": "PE", "lots": 2, "strike": {"mode": "premium_range", "lower": 30, "upper": 55}}
    ]


def test_expand_ui_config_end_to_end_count():
    cfg = _base_cfg(
        entry_time=TimeRange(start="09:20", end="09:50", interval_minutes=15),  # 3 values
        legs=[LegUIConfig(action="SELL", option_type="CE", strike_mode="offset", offsets=["ATM", "OTM1"])],  # 2 values
    )
    combos = expand_ui_config(cfg)
    assert len(combos) == 3 * 2
