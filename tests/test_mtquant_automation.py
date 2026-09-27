"""Unit tests for the pure-logic parts of src/mtquant/automation.py - error
paths, constant tables, and coordinate math that don't need a live mtQuant
app. The actual UI-driving methods were verified live against the running
app this session (see the commit history) - that verification can't be
replicated in CI, so this file deliberately only covers what CAN be tested
without a live Windows desktop.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pywinauto", reason="src.mtquant.automation needs the Windows-only `mtquant` extra (pywinauto)")

from src.mtquant.automation import (
    COMBINED_SL_TYPE_OPTIONS,
    DROPDOWN_FIRST_ITEM_OFFSET,
    DROPDOWN_ROW_HEIGHT,
    OVERALL_SL_TYPE_MAP,
    MTQuantAutomationError,
    PortfolioDialog,
    _select_dropdown_option,
)


def test_overall_sl_type_map_only_has_confirmed_mtm():
    # Deliberately narrow - only MTM has been confirmed live. Adding another
    # entry here should mean it was just confirmed live too, not guessed.
    assert OVERALL_SL_TYPE_MAP == {"MTM": "CombinedLoss"}


def test_combined_sl_type_options_matches_live_screenshot_order():
    assert COMBINED_SL_TYPE_OPTIONS == [
        "None",
        "CombinedLoss",
        "CombinedPremium",
        "AbsoluteCombinedPremium",
        "UnderlyingMovement",
        "LossAndUnderlyingRange",
        "Delta",
        "Theta",
    ]


def test_set_overall_stoploss_rejects_unconfirmed_type_before_touching_the_ui():
    dialog = PortfolioDialog(session=MagicMock(), dlg=MagicMock())
    with pytest.raises(MTQuantAutomationError, match="CombinedLoss"):
        dialog.set_overall_stoploss("SomeOtherType", 500)
    # Never even tried to switch tabs or touch a field - the type check
    # short-circuits before any UI interaction.
    dialog.dlg.descendants.assert_not_called()


def test_set_run_on_days_raises_not_implemented_rather_than_guessing():
    dialog = PortfolioDialog(session=MagicMock(), dlg=MagicMock())
    with pytest.raises(NotImplementedError):
        dialog.set_run_on_days(["Monday"])


def test_select_dropdown_option_computes_click_position_from_live_rect():
    combo = MagicMock()
    rect = MagicMock(left=2351, right=2602, top=870, bottom=903)
    combo.rectangle.return_value = rect

    import src.mtquant.automation as automation_mod

    calls = []
    automation_mod.mouse_click = lambda button, coords: calls.append(coords)

    _select_dropdown_option(combo, COMBINED_SL_TYPE_OPTIONS, "CombinedLoss")

    assert len(calls) == 2
    chevron_click, option_click = calls
    assert chevron_click == (rect.right - 15, (rect.top + rect.bottom) // 2)
    expected_y = int(rect.bottom + DROPDOWN_FIRST_ITEM_OFFSET + 1 * DROPDOWN_ROW_HEIGHT)  # index 1
    assert option_click == (rect.left + 30, expected_y)


def test_select_dropdown_option_rejects_unknown_option():
    combo = MagicMock()
    with pytest.raises(MTQuantAutomationError, match="isn't in the known option list"):
        _select_dropdown_option(combo, COMBINED_SL_TYPE_OPTIONS, "NotARealOption")
    combo.rectangle.assert_not_called()  # rejected before ever touching the UI
