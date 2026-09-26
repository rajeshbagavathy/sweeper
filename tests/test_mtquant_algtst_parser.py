from __future__ import annotations

from src.mtquant.algtst_parser import parse_algtst_data


def _time_indicator(hour: int, minute: int) -> dict:
    return {
        "OperandType": "OperandType.And",
        "Type": "IndicatorTreeNodeType.OperandNode",
        "Value": [
            {
                "Type": "IndicatorTreeNodeType.DataNode",
                "Value": {
                    "IndicatorName": "IndicatorType.TimeIndicator",
                    "Parameters": {"Hour": hour, "Minute": minute},
                },
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
        "ListOfLegConfigs": [
            _leg("leg1", "CE", "StrikeType.ATM"),
            _leg("leg2", "PE", "StrikeType.ATM"),
        ],
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


def _file(strategies: dict, items: list[dict]) -> dict:
    return {
        "version": "0.7",
        "data": {
            "portfolio": {"is_weekdays": False, "items": items, "name": "TEST_PORT"},
            "strategies": strategies,
        },
    }


def _item(id_: str, dte=(1,), multiplier=4, checked=True) -> dict:
    return {
        "checked": checked,
        "dte": list(dte),
        "id": id_,
        "multiplier": multiplier,
        "weekdays": {d: True for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
    }


def test_basic_strategy_parses_straddle_legs():
    data = _file(
        {"1": {"attributes": {}, "definition": _definition(), "name": "STRAT_1"}},
        [_item("1")],
    )
    portfolio = parse_algtst_data(data)
    assert portfolio.name == "TEST_PORT"
    assert len(portfolio.strategies) == 1

    s = portfolio.strategies[0]
    assert s.name == "STRAT_1"
    assert s.ticker == "NIFTY"
    assert s.entry_time == (9, 37)
    assert s.exit_time == (14, 45)
    assert s.overall_sl == {"type": "MTM", "value": 600}
    assert s.overall_target is None
    assert s.multiplier == 4
    assert s.dte == [1]
    assert s.checked is True
    assert not s.has_unmapped

    ce_leg, pe_leg = s.legs
    assert ce_leg.instrument_kind == "CE"
    assert ce_leg.position_type == "Sell"
    assert ce_leg.strike_parameter == "ATM"
    assert ce_leg.stop_loss == {"type": "Percentage", "value": 15}
    assert ce_leg.trail_sl == {"type": "Points", "value": {"InstrumentMove": 5, "StopLossMove": 1}}
    assert pe_leg.instrument_kind == "PE"


def test_premium_based_entry_keeps_numeric_strike():
    data = _file(
        {
            "1": {
                "attributes": {},
                "definition": _definition(
                    ListOfLegConfigs=[
                        _leg("leg1", "CE", 40, entry_type="EntryByPremium"),
                        _leg("leg2", "PE", 40, entry_type="EntryByPremium"),
                    ]
                ),
                "name": "STRAT_PREM",
            }
        },
        [_item("1")],
    )
    s = parse_algtst_data(data).strategies[0]
    assert s.legs[0].entry_type == "EntryByPremium"
    assert s.legs[0].strike_parameter == 40


def test_nextleg_reentry_resolves_against_idle_legs():
    idle_leg = _leg("lazy1", "CE", 35, entry_type="EntryByPremium")
    main_leg = _leg(
        "leg1",
        "CE",
        70,
        entry_type="EntryByPremium",
        LegReentrySL={"Type": "ReentryType.NextLeg", "Value": {"NextLegRef": "lazy1"}},
    )
    data = _file(
        {
            "1": {
                "attributes": {},
                "definition": _definition(IdleLegConfigs={"lazy1": idle_leg}, ListOfLegConfigs=[main_leg]),
                "name": "STRAT_LAZY",
            }
        },
        [_item("1")],
    )
    s = parse_algtst_data(data).strategies[0]
    assert "lazy1" in s.idle_legs
    assert s.legs[0].reentry_sl == {"type": "NextLeg", "value": {"NextLegRef": "lazy1"}}
    assert not s.has_unmapped  # reference resolves cleanly


def test_dangling_nextleg_reference_is_flagged_not_silently_dropped():
    main_leg = _leg(
        "leg1",
        "CE",
        70,
        entry_type="EntryByPremium",
        LegReentrySL={"Type": "ReentryType.NextLeg", "Value": {"NextLegRef": "does_not_exist"}},
    )
    data = _file(
        {"1": {"attributes": {}, "definition": _definition(ListOfLegConfigs=[main_leg]), "name": "STRAT_BAD"}},
        [_item("1")],
    )
    s = parse_algtst_data(data).strategies[0]
    assert s.has_unmapped
    assert any("does_not_exist" in note for note in s.unmapped_notes)


def test_multi_condition_entry_indicators_not_auto_mapped():
    definition = _definition()
    definition["EntryIndicators"]["Value"].append(definition["EntryIndicators"]["Value"][0])
    data = _file(
        {"1": {"attributes": {}, "definition": definition, "name": "STRAT_MULTI"}},
        [_item("1")],
    )
    s = parse_algtst_data(data).strategies[0]
    assert s.entry_time is None
    assert s.has_unmapped
    assert any("2 conditions" in note for note in s.unmapped_notes)


def test_missing_portfolio_item_is_flagged():
    data = _file(
        {"1": {"attributes": {}, "definition": _definition(), "name": "ORPHAN"}},
        [],  # no matching portfolio.items entry for id "1"
    )
    s = parse_algtst_data(data).strategies[0]
    assert s.has_unmapped
    assert s.checked is False
    assert s.multiplier is None


def test_multiple_strategies_all_parsed():
    data = _file(
        {
            "1": {"attributes": {}, "definition": _definition(), "name": "S1"},
            "2": {"attributes": {}, "definition": _definition(), "name": "S2"},
        },
        [_item("1"), _item("2", multiplier=6)],
    )
    portfolio = parse_algtst_data(data)
    assert len(portfolio.strategies) == 2
    by_id = {s.strategy_id: s for s in portfolio.strategies}
    assert by_id["1"].multiplier == 4
    assert by_id["2"].multiplier == 6
