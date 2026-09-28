from __future__ import annotations

import json

from src.mtquant.preview import build_preview


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


def _leg(leg_id: str, kind: str, strike, entry_type: str = "EntryByStrikeType") -> dict:
    return {
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


def _sample_file() -> dict:
    definition = {
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
    item = {
        "checked": True,
        "dte": [1],
        "id": "1",
        "multiplier": 4,
        "weekdays": {d: True for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
    }
    return {
        "version": "0.7",
        "data": {
            "portfolio": {"is_weekdays": False, "items": [item], "name": "TEST_PORT"},
            "strategies": {"1": {"attributes": {}, "definition": definition, "name": "STRAT_1"}},
        },
    }


def test_build_preview_shape():
    preview = build_preview(_sample_file())
    assert preview["portfolio_name"] == "TEST_PORT"
    assert preview["strategy_count"] == 1
    assert preview["flagged_count"] == 0

    s = preview["strategies"][0]
    assert s["symbol"] == "NIFTY"
    assert s["start_time"] == "09:36:59"
    assert s["sqoff_time"] == "14:44:59"
    assert s["entry_path"] == "predefined"
    assert s["predefined_strategy"] == "ShortStraddle"
    assert s["has_notes"] is False
    assert s["all_notes"] == []
    assert len(s["legs"]) == 2
    assert s["legs"][0]["ce_pe"] == "CE"
    assert s["legs"][0]["has_notes"] is False


def test_build_state_rejects_a_tag_with_spaces_before_touching_mtquant():
    from src.mtquant.build_state import MTQuantBuildState

    state = MTQuantBuildState()
    try:
        state.start([object()], "NIFTY 1DTE")
    except ValueError as exc:
        assert "NIFTY_1DTE" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert state.status == "idle"


def test_build_preview_is_json_serializable():
    preview = build_preview(_sample_file())
    # Must round-trip cleanly - this is exactly what the API endpoint returns.
    json.dumps(preview)


def test_build_preview_surfaces_flagged_count():
    data = _sample_file()
    # Two-condition entry indicator -> parser can't auto-map it -> a note.
    data["data"]["strategies"]["1"]["definition"]["EntryIndicators"]["Value"].append(
        data["data"]["strategies"]["1"]["definition"]["EntryIndicators"]["Value"][0]
    )
    preview = build_preview(data)
    assert preview["flagged_count"] == 1
    assert preview["strategies"][0]["has_notes"] is True
    assert len(preview["strategies"][0]["all_notes"]) >= 1


def test_build_preview_real_file():
    from pathlib import Path

    path = Path(r"C:\Users\rajes\Downloads\export_nt_1dte_cas_reg_v2.algtst")
    if not path.exists():
        import pytest

        pytest.skip("real .algtst fixture not present on this machine")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    preview = build_preview(data)
    assert preview["strategy_count"] == 14
    assert preview["flagged_count"] == 1  # strategy 10's one honestly-flagged note
    json.dumps(preview)  # still must be JSON-serializable on the real file
