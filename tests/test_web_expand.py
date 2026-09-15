from __future__ import annotations

from src.store import build_fieldnames, flatten
from src.web.expand import expand_ui_config, nest_combo, probe_combos, to_sweep_config
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
    assert TimeRange(start="10:00", end="09:15", interval_minutes=15).as_list() == ["10:00"]


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
            stoploss_enabled=True,
            trail_points_enabled=True,
            trail_points_x=NumericRange(min=10, max=10, step=5),
            trail_points_y=NumericRange(min=5, max=5, step=5),
            trail_percentage_enabled=True,
            trail_percentage_x=NumericRange(min=1, max=2, step=1),  # 1, 2
            trail_percentage_y=NumericRange(min=0.5, max=0.5, step=0.5),
        )
    )
    combos = expand_ui_config(cfg)
    trails = [c["leg_risk"]["trail"] for c in combos]
    non_none = [t for t in trails if t is not None]
    # 1 Points combo (10,5) + 2 Percentage combos (1,0.5)/(2,0.5) = 3, unioned - plus a
    # None (no trailing) baseline that's always included alongside whatever's enabled.
    assert len(non_none) == 3
    assert {t["type"] for t in non_none} == {"Points", "Percentage"}
    assert None in trails


def test_leg_risk_trail_drops_x_less_than_y_pairs():
    """AlgoTest warns that X < Y trailing pairs cause backtest/live mismatch - those
    combinations should never be generated at all."""
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            stoploss_enabled=True,
            trail_points_enabled=True,
            trail_points_x=NumericRange(min=1, max=5, step=1),  # 1,2,3,4,5
            trail_points_y=NumericRange(min=1, max=5, step=1),  # 1,2,3,4,5
        )
    )
    combos = expand_ui_config(cfg)
    trails = [c["leg_risk"]["trail"] for c in combos]
    non_none = [t for t in trails if t is not None]
    assert all(t["x"] >= t["y"] for t in non_none)
    # valid (x, y) pairs with x >= y out of the 5x5 grid: 1+2+3+4+5 = 15
    assert len(non_none) == 15
    assert None in trails  # the no-trailing baseline is always included too


def test_leg_risk_stoploss_basis_union_not_cross_product():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=20, max=20, step=10),
            stoploss_underlying_enabled=True,
            stoploss_underlying_pct=NumericRange(min=0.1, max=0.2, step=0.1),  # 0.1, 0.2
        )
    )
    combos = expand_ui_config(cfg)
    stoplosses = [c["leg_risk"]["stoploss_pct"] for c in combos]
    non_none = [s for s in stoplosses if s is not None]
    # 1 percentage combo (20) + 2 underlying_percentage combos (0.1, 0.2) = 3, unioned.
    # No None baseline here: overall Stop Loss is never enabled in this config, so
    # a leg-level None would mean no hard Stop Loss at all - excluded outright.
    assert len(non_none) == 3
    assert {s["kind"] for s in non_none} == {"percentage", "underlying_percentage"}
    assert {s["value"] for s in non_none if s["kind"] == "percentage"} == {20}
    assert {s["value"] for s in non_none if s["kind"] == "underlying_percentage"} == {0.1, 0.2}
    assert None not in stoplosses


def test_leg_risk_underlying_stoploss_above_one_percent_silently_excluded():
    # Confirmed live: entering 15-20% instead of 0.15-0.20% for the underlying
    # basis (a move that large in the underlying practically never happens
    # intraday) used to produce thousands of combos that looked protected but
    # weren't. Filtered here at generation time - not raised as an error on the
    # config model - since the model is reconstructed from PAST data all over
    # this app and must tolerate a stale on-disk value; see
    # tests/test_web_models.py's own coverage of that distinction.
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            stoploss_underlying_enabled=True,
            stoploss_underlying_pct=NumericRange(min=15, max=20, step=5),  # 15, 20 - both insane
        )
    )
    combos = expand_ui_config(cfg)
    stoplosses = [c["leg_risk"]["stoploss_pct"] for c in combos]
    # Every value was filtered out, so the only surviving choice is "no leg SL at
    # all" - and since overall Stop Loss isn't enabled in this config either, the
    # existing "must have a hard SL somewhere" exclude removes that too, leaving
    # zero combos - loud (an empty sweep the user will notice in Preview), not a
    # silent bad backtest.
    assert combos == []


def test_leg_risk_underlying_stoploss_partial_range_keeps_only_sane_values():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            stoploss_underlying_enabled=True,
            stoploss_underlying_pct=NumericRange(min=0.5, max=1.5, step=0.5),  # 0.5, 1.0, 1.5 - last one insane
        )
    )
    combos = expand_ui_config(cfg)
    stoplosses = [c["leg_risk"]["stoploss_pct"] for c in combos]
    non_none = [s for s in stoplosses if s is not None]
    assert {s["value"] for s in non_none} == {0.5, 1.0}


def test_leg_risk_disabled_dimensions_are_none():
    cfg = _base_cfg()
    combos = expand_ui_config(cfg)
    assert len(combos) == 1
    assert combos[0]["leg_risk"] == {
        "target_pct": None,
        "stoploss_pct": None,
        "trail": None,
        "momentum": None,
        "reentry_sl": None,
    }


def test_no_stop_loss_dimension_enabled_at_all_is_untouched():
    """When neither leg-level nor overall Stop Loss is enabled anywhere in the
    config, there's no Stop Loss dimension being swept at all - the "must have a
    hard Stop Loss" rule must not kick in and wipe out an unrelated sweep (e.g. one
    only exploring momentum or re-entry)."""
    cfg = _base_cfg(leg_risk=LegRiskConfig(momentum_up_enabled=True, momentum_up_pct=NumericRange(min=5, max=5, step=1)))
    combos = expand_ui_config(cfg)
    assert len(combos) == 2  # momentum on, or its None baseline - nothing excluded


def test_stop_loss_required_excludes_the_neither_baseline_when_leg_level_enabled():
    """Direct, minimal check of the actual rule: leg-level Stop Loss enabled, overall
    Stop Loss never enabled - every surviving combo must have picked the leg-level
    Stop Loss, since falling back to neither would mean no hard Stop Loss at all."""
    cfg = _base_cfg(leg_risk=LegRiskConfig(stoploss_enabled=True, stoploss_pct=NumericRange(min=20, max=20, step=10)))
    combos = expand_ui_config(cfg)
    assert len(combos) == 1
    assert combos[0]["leg_risk"]["stoploss_pct"] == {"kind": "percentage", "value": 20.0}


def test_stop_loss_required_excludes_the_neither_baseline_when_overall_enabled():
    """Same rule, mirrored for overall Stop Loss enabled with leg-level never on."""
    cfg = _base_cfg(overall_stoploss=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=20, max=20, step=10)))
    combos = expand_ui_config(cfg)
    assert len(combos) == 1
    assert combos[0]["stoploss"] == {"kind": "percentage", "value": 20.0}


def test_leg_risk_momentum_union_not_cross_product():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            momentum_up_enabled=True,
            momentum_up_pct=NumericRange(min=5, max=6, step=1),  # 5, 6
            momentum_down_enabled=True,
            momentum_down_pct=NumericRange(min=5, max=10, step=5),  # 5, 10
        )
    )
    combos = expand_ui_config(cfg)
    momenta = [c["leg_risk"]["momentum"] for c in combos]
    non_none = [m for m in momenta if m is not None]
    # 2 up combos + 2 down combos = 4, unioned (not 2*2=4 cross product coincidence -
    # use distinguishable ranges to prove it's a union) - plus a None (no momentum
    # entry criteria) baseline always included alongside whatever's enabled.
    assert len(non_none) == 4
    assert {m["direction"] for m in non_none} == {"UP", "DOWN"}
    assert {m["value"] for m in non_none if m["direction"] == "UP"} == {5, 6}
    assert {m["value"] for m in non_none if m["direction"] == "DOWN"} == {5, 10}
    assert None in momenta


def test_leg_risk_reentry_sl_one_choice_per_checked_type():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(reentry_sl_enabled=True, reentry_sl_types=["RE_ASAP", "RE_COST"], reentry_sl_count=2)
    )
    combos = expand_ui_config(cfg)
    reentries = [c["leg_risk"]["reentry_sl"] for c in combos]
    non_none = [r for r in reentries if r is not None]
    assert len(non_none) == 2
    assert {r["type"] for r in non_none} == {"RE_ASAP", "RE_COST"}
    assert all(r["count"] == 2 for r in non_none)
    assert None in reentries  # the no-re-entry baseline is always included too


def test_leg_risk_reentry_sl_disabled_is_none():
    cfg = _base_cfg(leg_risk=LegRiskConfig(reentry_sl_enabled=False))
    combos = expand_ui_config(cfg)
    assert combos[0]["leg_risk"]["reentry_sl"] is None


def test_lazy_leg_is_one_alternative_alongside_re_asap_re_cost():
    # A combo always lands on exactly ONE reentry_sl choice - checking LAZY_LEG
    # alongside RE_ASAP/RE_COST tries each as a separate alternative (union),
    # same convention as RE_ASAP vs RE_COST already had - never combined on one
    # combo, matching AlgoTest's own Re-entry on SL dropdown holding one type.
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["RE_ASAP", "RE_COST", "LAZY_LEG"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=35, max=35, step=10),
        )
    )
    combos = expand_ui_config(cfg)
    reentries = [c["leg_risk"]["reentry_sl"] for c in combos]
    non_none_types = {r["type"] for r in reentries if r is not None}
    assert non_none_types == {"RE_ASAP", "RE_COST", "LAZY_LEG"}
    assert None in reentries
    # LAZY_LEG's own choice carries no "count" (unlike RE_ASAP/RE_COST) - it's
    # not a meaningful concept for it (see _reentry_sl_choices).
    lazy_choice = next(r for r in reentries if r is not None and r["type"] == "LAZY_LEG")
    assert "count" not in lazy_choice


def test_lazy_leg_attached_per_leg_when_eligible():
    cfg = _base_cfg(
        legs=[LegUIConfig(action="SELL", option_type="CE"), LegUIConfig(action="SELL", option_type="PE")],
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["LAZY_LEG"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=35, max=35, step=10),
        ),
    )
    combos = expand_ui_config(cfg)
    # stoploss_pct also unions in a "None" baseline (see _stoploss_choices), and
    # reentry_sl unions in a "None" (no re-entry) baseline too - pick out the one
    # combo that actually landed on both the enabled 35% SL and LAZY_LEG.
    eligible_combo = next(
        c for c in combos
        if c["leg_risk"]["stoploss_pct"] is not None
        and c["leg_risk"]["reentry_sl"] is not None
        and c["leg_risk"]["reentry_sl"]["type"] == "LAZY_LEG"
    )
    ce_leg = next(leg for leg in eligible_combo["legs"] if leg["option_type"] == "CE")
    pe_leg = next(leg for leg in eligible_combo["legs"] if leg["option_type"] == "PE")
    # The CE leg's own lazy leg must itself be CE (never PE, and vice versa) -
    # the actual "no mix and match" guarantee, not just the momentum direction.
    assert ce_leg["lazy_leg"]["option_type"] == "CE"
    assert pe_leg["lazy_leg"]["option_type"] == "PE"
    assert ce_leg["lazy_leg"]["momentum"] == {"direction": "UNDERLYING_DOWN", "value": 20}
    assert pe_leg["lazy_leg"]["momentum"] == {"direction": "UNDERLYING_UP", "value": 20}
    assert ce_leg["lazy_leg"]["strike"] == "OTM2"  # both legs default to ATM strike


def test_lazy_leg_excluded_entirely_when_sl_outside_eligible_range():
    """Confirmed live: this combination used to be silently GENERATED (and
    actually run) with reentry_sl labeled "LAZY_LEG" even though "Re-entry on
    SL" was never toggled on in AlgoTest for that leg - producing a row
    byte-identical to the None sibling combo already generated for the same
    other parameters, just under a separate combo_id that falsely claimed
    Lazy Leg was used. Half of one real sweep's own "LAZY_LEG" rows turned
    out to be exactly this. Excluded at the combo-generation level now,
    instead of silently no-opping at apply time."""
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["LAZY_LEG"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=10, max=10, step=10),  # below the 25-60 range
        )
    )
    combos = expand_ui_config(cfg)
    assert all(
        c["leg_risk"]["reentry_sl"] is None or c["leg_risk"]["reentry_sl"]["type"] != "LAZY_LEG"
        for c in combos
    )


def test_lazy_leg_kept_when_sl_inside_eligible_range_alongside_ineligible_baseline():
    """The exclude only removes the specific (LAZY_LEG, out-of-range SL%)
    combination - a sweep trying BOTH an eligible and an ineligible SL% value
    still keeps the eligible one's LAZY_LEG combo, only dropping the
    ineligible one's."""
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["LAZY_LEG"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=10, max=35, step=25),  # 10 (ineligible), 35 (eligible)
        )
    )
    combos = expand_ui_config(cfg)
    lazy_combos = [
        c for c in combos
        if c["leg_risk"]["reentry_sl"] is not None and c["leg_risk"]["reentry_sl"]["type"] == "LAZY_LEG"
    ]
    assert len(lazy_combos) == 1
    assert lazy_combos[0]["leg_risk"]["stoploss_pct"]["value"] == 35


def test_lazy_leg_not_attached_when_type_not_checked():
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["RE_ASAP"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=35, max=35, step=10),
        )
    )
    combos = expand_ui_config(cfg)
    assert all("lazy_leg" not in leg for c in combos for leg in c["legs"])


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
        t, s = combo["leg_risk"]["target_pct"], combo["leg_risk"]["stoploss_pct"]
        if t is not None and s is not None:
            assert t > s["value"]
    # target's own baseline (unset) must still be present - the exclude only rules
    # out the invalid *combination* of both being set wrong.
    assert any(c["leg_risk"]["target_pct"] is None for c in combos)
    # stoploss's own None baseline is gone: overall Stop Loss is never enabled here,
    # so a leg-level None would mean no hard Stop Loss at all - excluded outright.
    assert all(c["leg_risk"]["stoploss_pct"] is not None for c in combos)


def test_to_sweep_config_adds_target_vs_stoploss_exclude():
    cfg = _base_cfg(
        overall_stoploss=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=20, max=20, step=10)),
        overall_target=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=10, max=30, step=10)),
    )
    combos = expand_ui_config(cfg)
    for combo in combos:
        target, stoploss = combo["target"], combo["stoploss"]
        if target is not None and stoploss is not None:
            assert target["value"] > stoploss["value"]
    assert any(c["target"] is None for c in combos)
    # overall stoploss's own None baseline is gone: leg-level Stop Loss is never
    # enabled here, so an overall None would mean no hard Stop Loss at all.
    assert all(c["stoploss"] is not None for c in combos)


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
    stoplosses = [c["stoploss"] for c in combos]
    non_none = [s for s in stoplosses if s is not None]
    assert len(non_none) == 3  # 2 percentage + 1 amount, unioned
    assert {s["kind"] for s in non_none} == {"percentage", "amount"}
    # No None baseline here: leg-level Stop Loss is never enabled in this config, so
    # an overall None would mean no hard Stop Loss at all - excluded outright.
    assert None not in stoplosses


def test_overall_risk_mismatched_basis_is_not_excluded():
    """A percentage target and an amount stoploss aren't comparable, so the auto
    exclude rule shouldn't touch that combination even if the raw numbers look wrong."""
    cfg = _base_cfg(
        overall_stoploss=OverallRiskConfig(use_percentage=False, use_amount=True, amount_range=NumericRange(min=100, max=100, step=10)),
        overall_target=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=10, max=10, step=10), use_amount=False),
    )
    combos = expand_ui_config(cfg)
    pairs = [(c["stoploss"], c["target"]) for c in combos]
    # the fully-set combo (mismatched basis) must survive - that's the point of this test
    assert ({"kind": "amount", "value": 100}, {"kind": "percentage", "value": 10}) in pairs
    # target's own baseline (unset) must still be present alongside the set stoploss
    assert ({"kind": "amount", "value": 100}, None) in pairs
    # but a stoploss-unset baseline is gone: leg-level Stop Loss is never enabled
    # here, so an overall None would mean no hard Stop Loss at all.
    assert (None, None) not in pairs
    assert (None, {"kind": "percentage", "value": 10}) not in pairs
    assert len(combos) == 2


def test_at_least_one_combo_has_nothing_set_when_several_features_enabled():
    """Enabling several optional risk/entry features together (leg SL, Momentum,
    Re-entry on SL, Overall SL) must still let Momentum and Re-entry independently
    sit at their own "not set" baseline - not just combos where various subsets are
    simultaneously active. Stop Loss itself is the one exception: with both leg-level
    and overall Stop Loss enabled here, at least one of the two must always be set
    (excluding "neither" - a hard risk-management rule, not one more free dimension),
    so the true "nothing at all set" baseline no longer exists by design."""
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            stoploss_enabled=True,
            momentum_down_enabled=True,
            reentry_sl_enabled=True,
        ),
        overall_stoploss=OverallRiskConfig(use_percentage=True, percentage_range=NumericRange(min=20, max=20, step=10)),
    )
    combos = expand_ui_config(cfg)
    baseline = [
        c
        for c in combos
        if c["leg_risk"]["momentum"] is None
        and c["leg_risk"]["reentry_sl"] is None
    ]
    assert len(baseline) >= 1
    # but every combo (including that baseline) still has a hard Stop Loss from
    # somewhere - never both None simultaneously.
    assert all(c["leg_risk"]["stoploss_pct"] is not None or c["stoploss"] is not None for c in combos)


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
        "legrisk_momentum": None,
        "legrisk_reentry_sl": None,
        "overall_stoploss": None,
        "overall_target": None,
    }
    nested = nest_combo(flat, cfg)
    assert nested["legs"] == [{"action": "SELL", "option_type": "CE", "lots": 1, "strike": "ATM"}]
    assert nested["stoploss"] is None
    assert nested["target"] is None
    assert nested["trail_sl"] is None
    assert nested["leg_risk"] == {
        "target_pct": None,
        "stoploss_pct": None,
        "trail": None,
        "momentum": None,
        "reentry_sl": None,
    }


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
        "legrisk_momentum": None,
        "legrisk_reentry_sl": None,
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


def test_probe_combos_covers_every_fieldname_a_full_expansion_would():
    """probe_combos exists so RunState.start doesn't have to fully expand a huge
    sweep just to build the CSV header - it must produce the exact same set of
    fieldnames a real (small enough to fully expand) equivalent config would."""
    cfg = _base_cfg(
        legs=[
            LegUIConfig(
                action="SELL",
                option_type="CE",
                strike=StrikeConfig(use_offset=True, offsets=["ATM", "OTM1"]),
            )
        ],
        leg_risk=LegRiskConfig(
            target_enabled=True,
            stoploss_enabled=True,
            stoploss_underlying_enabled=True,
            trail_points_enabled=True,
            trail_percentage_enabled=True,
            momentum_up_enabled=True,
            momentum_down_enabled=True,
            reentry_sl_enabled=True,
            reentry_sl_types=["RE_ASAP", "RE_COST"],
        ),
        overall_stoploss=OverallRiskConfig(use_percentage=True, use_amount=True),
        overall_target=OverallRiskConfig(use_percentage=True, use_amount=True),
        trail_sl_enabled=True,
        trail_sl_include_none=True,
    )

    full = expand_ui_config(cfg)
    probes = probe_combos(cfg)

    full_fields = set(build_fieldnames(full, metric_names=[]))
    probe_fields = set(build_fieldnames(probes, metric_names=[]))
    assert probe_fields == full_fields


def test_probe_combos_covers_lazy_leg_fieldnames_too():
    """The one exception to "vary one key at a time still covers every shape" -
    see probe_combos' own docstring. Confirmed live this was a real, active bug:
    a large sweep's own CSV header never gained the "legs.N.lazy_leg.*" columns
    on any resume, so every genuinely-eligible combo's lazy leg data was
    silently dropped on write, forever - even though the exact same fields
    show up fine for a small enough sweep that fully expands instead."""
    cfg = _base_cfg(
        legs=[LegUIConfig(action="SELL", option_type="CE"), LegUIConfig(action="SELL", option_type="PE")],
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["LAZY_LEG"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=30, max=30, step=10),
        ),
        # An enabled overall_stoploss keeps the existing "must have a hard SL
        # somewhere" exclude (see to_sweep_config) from stripping every
        # stoploss_pct=None combo out of the full expansion entirely - probe_
        # combos doesn't apply excludes at all (a separate, pre-existing,
        # harmless-in-practice gap unrelated to this test's own lazy_leg
        # focus), so without this the two field-sets would differ over a
        # spurious extra "leg_risk.stoploss_pct" placeholder instead.
        overall_stoploss=OverallRiskConfig(use_amount=True),
    )

    full = expand_ui_config(cfg)
    probes = probe_combos(cfg)

    full_fields = set(build_fieldnames(full, metric_names=[]))
    probe_fields = set(build_fieldnames(probes, metric_names=[]))
    assert any(f.startswith("legs.0.lazy_leg.") for f in full_fields)  # sanity: the config really does produce this shape
    assert probe_fields == full_fields


def test_probe_combos_lazy_leg_probe_omitted_when_nothing_eligible_exists():
    """No eligible stoploss_pct anywhere in the sweep - the extra combined
    probe would be pure noise (nest_combo could never actually attach a
    lazy_leg for any real combo either), so it's correctly skipped rather
    than added unconditionally."""
    cfg = _base_cfg(
        leg_risk=LegRiskConfig(
            reentry_sl_enabled=True,
            reentry_sl_types=["LAZY_LEG"],
            stoploss_enabled=True,
            stoploss_pct=NumericRange(min=10, max=10, step=10),  # outside 25-60
        ),
    )
    probes = probe_combos(cfg)
    assert not any("lazy_leg" in leg for p in probes for leg in p["legs"])
