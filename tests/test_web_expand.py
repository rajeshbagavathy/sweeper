from __future__ import annotations

from src.web.expand import expand_ui_config, nest_combo, to_sweep_config
from src.web.models import LegRiskConfig, LegUIConfig, NumericRange, OverallRiskConfig, StrikeConfig, SweepUIConfig, TimeRange

# These tests exercise the independent-legs path explicitly, so linked_ce_pe is off by
# default here; the linked (shared CE/PE) path has its own tests further down.


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


def test_time_range_fixed_ignores_end_and_interval():
    assert TimeRange(start="10:00", end="14:00", interval_minutes=15, fixed=True).as_list() == ["10:00"]


def _base_cfg(**overrides) -> SweepUIConfig:
    base = dict(
        instrument="NIFTY",
        start_date="2025-01-01",
        end_date="2025-06-01",
        entry_time=TimeRange(start="09:20", end="09:20", interval_minutes=15),
        exit_time=TimeRange(start="15:10", end="15:10", interval_minutes=5),
        linked_ce_pe=False,
        legs=[LegUIConfig(action="SELL", option_type="CE")],
        overall_stoploss=OverallRiskConfig(use_percentage=False),
        overall_target=OverallRiskConfig(use_percentage=False),
        trail_sl_enabled=False,
    )
    base.update(overrides)
    return SweepUIConfig(**base)


def test_to_sweep_config_prefixes_per_leg_keys():
    cfg = _base_cfg(
        legs=[
            LegUIConfig(
                action="SELL", option_type="CE",
                strike=StrikeConfig(use_offset=True, offsets=["ATM", "OTM1"]),
            ),
            LegUIConfig(action="SELL", option_type="PE", strike=StrikeConfig(use_offset=True, offsets=["ATM"])),
        ]
    )
    sweep = to_sweep_config(cfg)
    assert sweep.vary["leg0_strike"] == [{"kind": "offset", "value": "ATM"}, {"kind": "offset", "value": "OTM1"}]
    assert sweep.vary["leg1_strike"] == [{"kind": "offset", "value": "ATM"}]
    assert sweep.vary["leg0_lots"] == [1]


def test_strike_checkboxes_union_not_cross_product():
    """Checking both Strike Type and Closest Premium should ADD their choices
    together (2 offsets + 3 premiums = 5 total strike values), not multiply them."""
    cfg = _base_cfg(
        legs=[
            LegUIConfig(
                action="SELL",
                option_type="CE",
                strike=StrikeConfig(
                    use_offset=True,
                    offsets=["ATM", "OTM1"],
                    use_closest_premium=True,
                    premium_range=NumericRange(min=30, max=40, step=5),  # 30, 35, 40
                ),
            )
        ]
    )
    combos = expand_ui_config(cfg)
    assert len(combos) == 5  # 2 offset + 3 premium, unioned
    strikes = [c["legs"][0]["strike"] for c in combos]
    assert "ATM" in strikes and "OTM1" in strikes
    premium_strikes = [s for s in strikes if isinstance(s, dict)]
    assert {s["value"] for s in premium_strikes} == {30, 35, 40}
    assert all(s["mode"] == "premium_closest" for s in premium_strikes)


def test_leg_risk_trail_union_not_cross_product():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            trail_points_enabled=True,
            trail_points_x=NumericRange(min=10, max=10, step=5),
            trail_points_y=NumericRange(min=5, max=5, step=5),
            trail_percentage_enabled=True,
            trail_percentage_x=NumericRange(min=1, max=2, step=1),  # 1, 2
            trail_percentage_y=NumericRange(min=0.5, max=0.5, step=0.5),
        )
    )
    combos = expand_ui_config(cfg)
    # 1 Points combo (10,5) + 2 Percentage combos (1,0.5)/(2,0.5) = 3, unioned
    assert len(combos) == 3
    trails = [c["leg_risk"]["trail"] for c in combos]
    assert {t["type"] for t in trails} == {"Points", "Percentage"}


def test_leg_risk_disabled_dimensions_are_none():
    cfg = _base_cfg()
    combos = expand_ui_config(cfg)
    assert len(combos) == 1
    assert combos[0]["leg_risk"] == {"target_pct": None, "stoploss_pct": None, "trail": None}


def test_leg_risk_target_vs_stoploss_exclude():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            target_enabled=True,
            target_pct=NumericRange(min=10, max=30, step=10),
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=20, max=20, step=10),
        )
    )
    combos = expand_ui_config(cfg)
    for combo in combos:
        assert combo["leg_risk"]["target_pct"] > combo["leg_risk"]["stoploss_pct"]


def test_to_sweep_config_adds_target_vs_stoploss_exclude():
    cfg = _base_cfg(
        overall_stoploss=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=20, max=20, step=10)),
        overall_target=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=10, max=30, step=10)),
    )
    combos = expand_ui_config(cfg)
    for combo in combos:
        assert combo["target"]["value"] > combo["stoploss"]["value"]


def test_overall_risk_percentage_and_amount_union_not_cross_product():
    cfg = _base_cfg(
        overall_stoploss=OverallRiskConfig(
            use_percentage=True,
            percentage_range=NumericRange(min=20, max=30, step=10),  # 20, 30
            use_amount=True,
            amount_range=NumericRange(min=5000, max=5000, step=1000),  # 5000
        ),
    )
    combos = expand_ui_config(cfg)
    assert len(combos) == 3  # 2 percentage + 1 amount, unioned
    kinds = {c["stoploss"]["kind"] for c in combos}
    assert kinds == {"percentage", "amount"}


def test_overall_risk_mismatched_basis_is_not_excluded():
    """A percentage target and an amount stoploss aren't comparable, so the auto
    exclude rule shouldn't touch that combination even if the raw numbers look wrong."""
    cfg = _base_cfg(
        overall_stoploss=OverallRiskConfig(use_percentage=False, use_amount=True, amount_range=NumericRange(min=100, max=100, step=10)),
        overall_target=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=10, max=10, step=10), use_amount=False),
    )
    combos = expand_ui_config(cfg)
    assert len(combos) == 1
    assert combos[0]["stoploss"] == {"kind": "amount", "value": 100}
    assert combos[0]["target"] == {"kind": "percentage", "value": 10}


def test_overall_trailing_four_way_cartesian():
    cfg = _base_cfg(
        trail_sl_enabled=True,
        trail_sl_include_none=False,
        trail_sl_x=NumericRange(min=20, max=20, step=5),
        trail_sl_y=NumericRange(min=10, max=10, step=5),
        trail_sl_step=NumericRange(min=5, max=10, step=5),  # 5, 10
        trail_sl_trail_by=NumericRange(min=2, max=4, step=2),  # 2, 4
    )
    combos = expand_ui_config(cfg)
    assert len(combos) == 1 * 1 * 2 * 2
    for combo in combos:
        assert combo["trail_sl"]["x"] == 20
        assert combo["trail_sl"]["y"] == 10


def test_nest_combo_reassembles_offset_mode_leg():
    cfg = _base_cfg()
    flat = {
        "instrument": "NIFTY",
        "entry_time": "09:20",
        "leg0_lots": 1,
        "leg0_strike": {"kind": "offset", "value": "ATM"},
        "legrisk_target_pct": None,
        "legrisk_stoploss_pct": None,
        "legrisk_trail": None,
        "overall_stoploss": None,
        "overall_target": None,
    }
    nested = nest_combo(flat, cfg)
    assert nested["legs"] == [{"action": "SELL", "option_type": "CE", "lots": 1, "strike": "ATM"}]
    assert nested["stoploss"] is None
    assert nested["target"] is None
    assert nested["trail_sl"] is None
    assert nested["leg_risk"] == {"target_pct": None, "stoploss_pct": None, "trail": None}


def test_nest_combo_reassembles_premium_closest_leg():
    cfg = _base_cfg(
        legs=[
            LegUIConfig(action="BUY", option_type="PE", strike=StrikeConfig(use_offset=False, use_closest_premium=True))
        ]
    )
    flat = {
        "leg0_lots": 2,
        "leg0_strike": {"kind": "premium_closest", "value": 35},
        "legrisk_target_pct": None,
        "legrisk_stoploss_pct": None,
        "legrisk_trail": None,
        "overall_stoploss": None,
        "overall_target": None,
    }
    nested = nest_combo(flat, cfg)
    assert nested["legs"] == [
        {"action": "BUY", "option_type": "PE", "lots": 2, "strike": {"mode": "premium_closest", "value": 35}}
    ]


def test_linked_ce_pe_uses_one_shared_vary_dimension_not_two():
    """The whole point of linking: a shared 2-value strike dimension should contribute
    2 combinations total, not 2*2=4 (which is what two independently-varying CE/PE legs
    with the same 2 offsets would produce)."""
    cfg = _base_cfg(
        linked_ce_pe=True,
        shared_leg=LegUIConfig(action="SELL", strike=StrikeConfig(use_offset=True, offsets=["ATM", "OTM1"])),
    )
    sweep = to_sweep_config(cfg)
    assert "shared_strike" in sweep.vary
    assert "leg0_strike" not in sweep.vary and "leg1_strike" not in sweep.vary

    combos = expand_ui_config(cfg)
    assert len(combos) == 2


def test_linked_ce_pe_mirrors_strike_and_lots_across_both_legs():
    cfg = _base_cfg(
        linked_ce_pe=True,
        shared_leg=LegUIConfig(
            action="SELL",
            strike=StrikeConfig(use_offset=False, use_closest_premium=True, premium_range=NumericRange(min=30, max=30, step=5)),
        ),
    )
    combos = expand_ui_config(cfg)
    assert len(combos) == 1
    ce_leg, pe_leg = combos[0]["legs"]
    assert ce_leg["option_type"] == "CE" and pe_leg["option_type"] == "PE"
    assert ce_leg["action"] == pe_leg["action"] == "SELL"
    assert ce_leg["lots"] == pe_leg["lots"]
    assert ce_leg["strike"] == pe_leg["strike"] == {"mode": "premium_closest", "value": 30}


def test_linked_ce_pe_is_the_default():
    cfg = SweepUIConfig()
    assert cfg.linked_ce_pe is True
    combos = expand_ui_config(cfg)
    assert all(len(c["legs"]) == 2 for c in combos)
    assert all(c["legs"][0]["strike"] == c["legs"][1]["strike"] for c in combos)
