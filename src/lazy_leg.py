"""Derives a leg's "Lazy Leg" re-entry config from that SAME leg's own already-
chosen parameters - see config/selectors.yaml's "Lazy Leg" section and
src/form.py's _apply_lazy_leg for what AlgoTest itself does with the result.

Deliberately NOT a sweep dimension of its own (the user's own words: "fixed
dynamic updates based on original parameters") - every value here is computed
from a leg/leg_risk/instrument that already exist elsewhere in the combo, so
enabling this feature does not change a sweep's combination count at all.
"""

from __future__ import annotations

from typing import Any

# Confirmed live: AlgoTest's underlying-points momentum threshold is the same
# fixed value for every combo of one instrument - not swept, not user-tunable
# per the user's own request. Instruments with no entry here are simply not
# eligible for a lazy leg at all (see derive_lazy_leg) rather than guessing a
# value that was never confirmed.
_MOMENTUM_POINTS_BY_INSTRUMENT = {
    "NIFTY": 20,
    "SENSEX": 50,
}

# A leg-level Stop Loss outside this range never gets a lazy leg - the user's own
# eligibility rule, meant to keep this feature from firing on strategies where
# the underlying SL is either too tight (constant whipsaw re-entries) or too
# loose (barely ever reached) to make the lazy-leg logic meaningful.
_ELIGIBLE_SL_PCT_MIN = 25
_ELIGIBLE_SL_PCT_MAX = 60


def _strike_offset_to_index(offset: str) -> int:
    """ATM/ITM*/OTM* -> a signed index along one continuous axis (ITM20=-20 ...
    ATM=0 ... OTM20=+20) - lets "2 strikes further OTM" be a single `+2`
    regardless of which side of ATM the original leg started on."""
    if offset == "ATM":
        return 0
    if offset.startswith("OTM"):
        return int(offset[3:])
    if offset.startswith("ITM"):
        return -int(offset[3:])
    raise ValueError(f"Unrecognized strike offset: {offset!r}")


def _index_to_strike_offset(index: int) -> str:
    if index == 0:
        return "ATM"
    if index > 0:
        return f"OTM{index}"
    return f"ITM{-index}"


def _derive_lazy_strike(strike: str | dict[str, Any]) -> str | dict[str, Any]:
    """The user's own rule: 2 strikes further OTM for a strike-type-based leg
    (ATM -> OTM2, OTM2 -> OTM4, ITM1 -> OTM1, ...), or half the premium for a
    premium-based leg. Clipped to AlgoTest's own ITM20..OTM20 range so an
    already-deep OTM original leg can't derive an out-of-range lazy strike."""
    if isinstance(strike, dict):
        if strike["mode"] == "premium_closest":
            return {"mode": "premium_closest", "value": strike["value"] / 2}
        raise ValueError(f"Unsupported strike mode for lazy leg derivation: {strike['mode']!r}")

    index = _strike_offset_to_index(strike) + 2
    index = max(-20, min(20, index))
    return _index_to_strike_offset(index)


def derive_lazy_leg(leg: dict[str, Any], leg_risk: dict[str, Any], instrument: str) -> dict[str, Any] | None:
    """None whenever this leg/combo isn't eligible for a lazy leg at all - the
    caller (src/web/expand.py's nest_combo) should leave the leg with no
    "lazy_leg" key at all in that case, same as every other "not set" leg_risk
    field in this codebase."""
    stoploss = leg_risk.get("stoploss_pct")
    if stoploss is None:
        return None
    # Excludes underlying-based leg SL scenarios entirely, per the user's own
    # request - a lazy leg's own reversal-confirmation logic is itself an
    # underlying-move condition, and stacking that on top of an already
    # underlying-based original SL was explicitly called out as unwanted.
    if stoploss["kind"] != "percentage":
        return None
    if not (_ELIGIBLE_SL_PCT_MIN <= stoploss["value"] <= _ELIGIBLE_SL_PCT_MAX):
        return None

    momentum_points = _MOMENTUM_POINTS_BY_INSTRUMENT.get(instrument)
    if momentum_points is None:
        return None

    direction = "UNDERLYING_DOWN" if leg["option_type"] == "CE" else "UNDERLYING_UP"

    return {
        "option_type": leg["option_type"],
        "strike": _derive_lazy_strike(leg["strike"]),
        "stoploss_pct": {"kind": "percentage", "value": stoploss["value"]},
        "trail": leg_risk.get("trail"),
        "momentum": {"direction": direction, "value": momentum_points},
    }
