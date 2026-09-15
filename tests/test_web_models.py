from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.web.models import LegRiskConfig, NumericRange, SweepUIConfig, TimeRange


def test_capture_dte_individually_defaults_to_true():
    # Explicit user request: split-by-DTE must be the default everywhere a config
    # is constructed without saying otherwise (a brand-new sweep, an old saved
    # execution/config predating this default flip) - never silently combined
    # unless someone actually unchecks it. See src/web/models.py's own comment.
    assert SweepUIConfig().capture_dte_individually is True


def test_capture_dte_individually_can_still_be_turned_off_explicitly():
    assert SweepUIConfig(capture_dte_individually=False).capture_dte_individually is False


def test_lazy_leg_not_in_default_reentry_types():
    # Unlike capture_dte_individually above, this is a brand-new, not-yet-proven
    # feature - opt-in only (never checked by default), never silently on for
    # an existing saved config just because reentry_sl_enabled happens to be on.
    assert "LAZY_LEG" not in SweepUIConfig().leg_risk.reentry_sl_types


def test_underlying_stoploss_default_is_within_sane_range():
    # The model default itself must obey the cap - not just user-entered values.
    assert LegRiskConfig().stoploss_underlying_pct.max <= 1.0


def test_underlying_stoploss_construction_never_raises_even_with_a_stale_bad_range():
    # NOT a validator on the model itself - deliberately. The model is
    # reconstructed from PAST data all over this app (saved executions, the
    # persisted sweep_ui.yaml, "save combo to AlgoTest"), so rejecting
    # construction here would break loading anything saved before this cutoff
    # existed, not just block a new bad entry. Confirmed live: that's exactly
    # what happened - it broke "Save basket in AlgoTest" for a sweep run off a
    # stale on-disk config still carrying 15-20%. The actual exclusion of
    # values above MAX_SANE_UNDERLYING_SL_PCT happens at combo-generation time
    # instead - see test_web_expand.py's own coverage of _stoploss_choices.
    cfg = LegRiskConfig(stoploss_underlying_enabled=True, stoploss_underlying_pct=NumericRange(min=15, max=20, step=5))
    assert cfg.stoploss_underlying_pct.max == 20


def test_time_range_accepts_values_within_market_hours():
    TimeRange(start="09:15", end="15:30", interval_minutes=15)  # should not raise


def test_time_range_rejects_am_pm_mixup():
    # The actual bug this validator exists for: exit_time="23:20" (11:20pm) instead
    # of the intended "11:20" (11:20am) - burned the full 180s timeout on every
    # combo in a real sweep since AlgoTest has no result for a time ~8h after close.
    with pytest.raises(ValidationError, match="outside market hours"):
        TimeRange(start="23:20", end="23:20")


def test_time_range_rejects_before_market_open():
    with pytest.raises(ValidationError, match="outside market hours"):
        TimeRange(start="09:00", end="10:00")


def test_time_range_rejects_after_market_close():
    with pytest.raises(ValidationError, match="outside market hours"):
        TimeRange(start="15:00", end="15:41")  # just past the CAS-extended close


def test_time_range_rejects_unparseable_time():
    with pytest.raises(ValidationError, match="not a valid"):
        TimeRange(start="9:15am", end="10:00")


def test_time_range_boundary_values_are_inclusive():
    TimeRange(start="09:15", end="09:15")  # market open, exact boundary
    TimeRange(start="15:40", end="15:40")  # market close, exact boundary (CAS-extended)
