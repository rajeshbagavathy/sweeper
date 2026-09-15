from __future__ import annotations

from src.lazy_leg import derive_lazy_leg


def _leg(option_type: str, strike) -> dict:
    return {"action": "SELL", "option_type": option_type, "lots": 1, "strike": strike}


def _leg_risk(sl_value: float, kind: str = "percentage", trail=None) -> dict:
    return {"stoploss_pct": {"kind": kind, "value": sl_value}, "trail": trail}


def test_atm_shifts_two_steps_otm():
    result = derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(35), "NIFTY")
    assert result["strike"] == "OTM2"


def test_otm2_shifts_to_otm4():
    result = derive_lazy_leg(_leg("CE", "OTM2"), _leg_risk(35), "NIFTY")
    assert result["strike"] == "OTM4"


def test_itm1_shifts_to_otm1():
    # -1 (ITM1) + 2 = +1 (OTM1) - crosses ATM, per the user's own worked rule.
    result = derive_lazy_leg(_leg("CE", "ITM1"), _leg_risk(35), "NIFTY")
    assert result["strike"] == "OTM1"


def test_itm3_shifts_to_itm1():
    result = derive_lazy_leg(_leg("CE", "ITM3"), _leg_risk(35), "NIFTY")
    assert result["strike"] == "ITM1"


def test_deep_otm_clips_at_otm20():
    result = derive_lazy_leg(_leg("CE", "OTM19"), _leg_risk(35), "NIFTY")
    assert result["strike"] == "OTM20"  # 19 + 2 = 21, clipped to 20


def test_premium_based_strike_is_halved():
    leg = _leg("CE", {"mode": "premium_closest", "value": 100})
    result = derive_lazy_leg(leg, _leg_risk(35), "NIFTY")
    assert result["strike"] == {"mode": "premium_closest", "value": 50}


def test_sl_below_eligible_range_excluded():
    assert derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(24.9), "NIFTY") is None


def test_sl_above_eligible_range_excluded():
    assert derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(60.1), "NIFTY") is None


def test_sl_at_range_boundaries_included():
    assert derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(25), "NIFTY") is not None
    assert derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(60), "NIFTY") is not None


def test_no_leg_stoploss_excluded():
    leg_risk = {"stoploss_pct": None, "trail": None}
    assert derive_lazy_leg(_leg("CE", "ATM"), leg_risk, "NIFTY") is None


def test_underlying_based_stoploss_excluded():
    leg_risk = _leg_risk(35, kind="underlying_percentage")
    assert derive_lazy_leg(_leg("CE", "ATM"), leg_risk, "NIFTY") is None


def test_ce_momentum_is_underlying_down():
    result = derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(35), "NIFTY")
    assert result["momentum"] == {"direction": "UNDERLYING_DOWN", "value": 20}


def test_lazy_leg_option_type_always_matches_its_own_parent_leg():
    # The actual "never mix CE/PE" guarantee: this value is what src/form.py's
    # _apply_lazy_leg later force-sets in the popup (see its own docstring) -
    # never left to whatever AlgoTest's popup happens to default to.
    assert derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(35), "NIFTY")["option_type"] == "CE"
    assert derive_lazy_leg(_leg("PE", "ATM"), _leg_risk(35), "NIFTY")["option_type"] == "PE"


def test_pe_momentum_is_underlying_up():
    result = derive_lazy_leg(_leg("PE", "ATM"), _leg_risk(35), "NIFTY")
    assert result["momentum"] == {"direction": "UNDERLYING_UP", "value": 20}


def test_sensex_momentum_points_is_50():
    result = derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(35), "SENSEX")
    assert result["momentum"]["value"] == 50


def test_unconfigured_instrument_excluded():
    assert derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(35), "BANKNIFTY") is None


def test_trail_and_sl_are_copied_from_original_leg():
    trail = {"type": "Points", "x": 10, "y": 5}
    result = derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(40, trail=trail), "NIFTY")
    assert result["stoploss_pct"] == {"kind": "percentage", "value": 40}
    assert result["trail"] == trail


def test_no_trail_on_original_means_no_trail_on_lazy_leg():
    result = derive_lazy_leg(_leg("CE", "ATM"), _leg_risk(35, trail=None), "NIFTY")
    assert result["trail"] is None
