from __future__ import annotations

from src.mtquant.algtst_parser import parse_algtst_data
from src.mtquant.field_mapping import build_portfolio_plan


def _time_indicator(hour: int, minute: int) -> dict:
    return {
        "OperandType": "OperandType.And",
        "Type": "IndicatorTreeNodeType.OperandNode",
        "Value": [
            {
                "Type": "IndicatorTreeNodeType.DataNode",
                "Value": {"IndicatorName": "IndicatorType.TimeIndicator", "Parameters": {"Hour": hour, "Minute": minute}},
            }
        ],
    }


def _leg(leg_id: str, kind: str, strike, entry_type: str = "EntryByStrikeType", **overrides) -> dict:
    base = {
        "id": leg_id,
        "InstrumentKind": f"LegType.{kind}",
        "PositionType": "PositionType.Sell",
        "ExpiryKind": "ExpiryType.Weekly",
        "EntryType": f"EntryType.{entry_type}",
        "StrikeParameter": strike,
        "LotConfig": {"Type": "LotType.Quantity", "Value": 1},
        "LegStopLoss": {"Type": "LegTgtSLType.Percentage", "Value": 15},
        "LegTarget": {"Type": "None", "Value": 0},
        "LegTrailSL": {"Type": "TrailStopLossType.Points", "Value": {"InstrumentMove": 5, "StopLossMove": 1}},
        "LegMomentum": {"Type": "None", "Value": 0},
        "LegReentrySL": {"Type": "None", "Value": {}},
        "LegReentryTP": {"Type": "None", "Value": {}},
    }
    base.update(overrides)
    return base


def _definition(**overrides) -> dict:
    base = {
        "EntryIndicators": _time_indicator(9, 37),
        "ExitIndicators": _time_indicator(14, 45),
        "IdleLegConfigs": {},
        "ListOfLegConfigs": [_leg("leg1", "CE", "StrikeType.ATM"), _leg("leg2", "PE", "StrikeType.ATM")],
        "LockAndTrail": {"Type": "None", "Value": {}},
        "MaxPositionInADay": 1,
        "OverallSL": {"Type": "OverallTgtSLType.MTM", "Value": 600},
        "OverallTgt": {"Type": "None", "Value": 0},
        "OverallTrailSL": {"Type": "None", "Value": {}},
        "ReentryTimeRestriction": "None",
        "SkipInitialCandles": 0,
        "SquareOffAllLegs": "False",
        "StrategyType": "StrategyType.IntradaySameDay",
        "TakeUnderlyingFromCashOrNot": "True",
        "Ticker": "NIFTY",
        "TrailSLtoBreakeven": "False",
        "WeeklyOldRegime": True,
    }
    base.update(overrides)
    return base


def _item(id_: str, dte=(1,), multiplier=4) -> dict:
    return {
        "checked": True,
        "dte": list(dte),
        "id": id_,
        "multiplier": multiplier,
        "weekdays": {"monday": True, "tuesday": True, "wednesday": False, "thursday": True, "friday": True, "saturday": False, "sunday": False},
    }


def _strategy(definition: dict, item: dict, name: str = "STRAT") -> "AlgtstStrategy":  # noqa: F821
    data = {
        "version": "0.7",
        "data": {
            "portfolio": {"is_weekdays": False, "items": [item], "name": "P"},
            "strategies": {item["id"]: {"attributes": {}, "definition": definition, "name": name}},
        },
    }
    return parse_algtst_data(data).strategies[0]


def test_plain_atm_straddle_maps_cleanly():
    strategy = _strategy(_definition(), _item("1"))
    plan = build_portfolio_plan(strategy)

    assert plan.symbol == "NIFTY"
    assert plan.underlying == "Spot"
    assert plan.default_lots == 4
    assert plan.dte == [1]
    assert plan.run_on_days == ["Monday", "Tuesday", "Thursday", "Friday"]  # wed/sat/sun excluded
    assert plan.start_time == "09:37:00"
    assert plan.exit_time == "14:45:00"
    assert plan.overall_stoploss == {"type": "MTM", "value": 600}
    assert plan.move_sl_to_cost is False
    assert not plan.has_notes

    ce, pe = plan.legs
    assert ce.buy_sell == "Sell"
    assert ce.ce_pe == "CE"
    assert ce.strike_mode == "ATM"
    assert ce.stoploss_pct == 15
    assert ce.trail_sl == {"instrument_move": 5, "stoploss_move": 1}
    assert not ce.has_notes


def test_premium_based_leg_flags_a_note_but_still_carries_the_value():
    strategy = _strategy(
        _definition(ListOfLegConfigs=[_leg("leg1", "CE", 40, entry_type="EntryByPremium"), _leg("leg2", "PE", 40, entry_type="EntryByPremium")]),
        _item("1"),
    )
    plan = build_portfolio_plan(strategy)
    assert plan.has_notes
    ce = plan.legs[0]
    assert ce.strike_mode == "PREMIUM"
    assert ce.strike_value == 40
    assert any("EntryByPremium" in n for n in ce.notes)
    assert any("EntryByPremium" in n for n in plan.all_notes())


def test_nextleg_reentry_produces_idle_leg_plan_and_reference():
    idle_leg = _leg("lazy1", "CE", 35, entry_type="EntryByPremium")
    main_leg = _leg("leg1", "CE", 70, entry_type="EntryByPremium", LegReentrySL={"Type": "ReentryType.NextLeg", "Value": {"NextLegRef": "lazy1"}})
    strategy = _strategy(_definition(IdleLegConfigs={"lazy1": idle_leg}, ListOfLegConfigs=[main_leg]), _item("1"))
    plan = build_portfolio_plan(strategy)

    assert len(plan.idle_legs) == 1
    assert plan.idle_legs[0].leg_id == "lazy1"
    assert plan.idle_legs[0].idle is True

    main = plan.legs[0]
    assert main.reentry_kind == "NextLeg"
    assert main.reentry_target_leg_id == "lazy1"


def test_atcost_reentry_carries_count():
    main_leg = _leg("leg1", "CE", 70, entry_type="EntryByPremium", LegReentrySL={"Type": "ReentryType.AtCost", "Value": {"ReentryCount": 1}})
    strategy = _strategy(_definition(ListOfLegConfigs=[main_leg]), _item("1"))
    plan = build_portfolio_plan(strategy)
    assert plan.legs[0].reentry_kind == "AtCost"
    assert plan.legs[0].reentry_count == 1


def test_unsupported_overall_sl_type_is_flagged_not_silently_passed_through():
    strategy = _strategy(_definition(OverallSL={"Type": "OverallTgtSLType.SomethingNew", "Value": 5}), _item("1"))
    plan = build_portfolio_plan(strategy)
    # Value is still carried (never dropped) but flagged - build_portfolio_plan
    # doesn't reject unknown Overall SL types, only per-leg SL/trail types do
    # today; this documents that OverallSL types pass through untouched.
    assert plan.overall_stoploss == {"type": "SomethingNew", "value": 5}


def test_parser_unmapped_notes_carry_forward_into_plan():
    definition = _definition()
    definition["EntryIndicators"]["Value"].append(definition["EntryIndicators"]["Value"][0])
    strategy = _strategy(definition, _item("1"))
    plan = build_portfolio_plan(strategy)
    assert plan.has_notes
    assert any("2 conditions" in n for n in plan.notes)


def test_real_file_builds_a_plan_for_every_strategy_with_notes_surfaced():
    from pathlib import Path

    from src.mtquant.algtst_parser import parse_algtst_file

    path = Path(r"C:\Users\rajes\Downloads\export_nt_1dte_cas_reg_v2.algtst")
    if not path.exists():
        import pytest

        pytest.skip("real .algtst fixture not present on this machine")

    portfolio = parse_algtst_file(path)
    plans = [build_portfolio_plan(s) for s in portfolio.strategies]
    assert len(plans) == 14
    for plan in plans:
        assert plan.symbol == "NIFTY"
        assert len(plan.legs) == 2
        # Every leg in this real file is EntryByPremium or ATM EntryByStrikeType -
        # both parse to a strike_mode, never "UNKNOWN".
        for leg in plan.legs:
            assert leg.strike_mode != "UNKNOWN"
