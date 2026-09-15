from __future__ import annotations

from src.form import _LAZY_LEG_MOMENTUM_LABELS, _REENTRY_LABELS


def test_reentry_labels_include_lazy_leg():
    # Confirmed live: AlgoTest's own leg_reentry_sl_type option list has this
    # exact visible text.
    assert _REENTRY_LABELS["LAZY_LEG"] == "Lazy Leg"


def test_lazy_leg_momentum_labels_are_underlying_only():
    # Confirmed live: a lazy leg's own Simple Momentum dropdown offers the same
    # 8 options as a normal leg, but this feature only ever uses the two
    # underlying-points ones (the reversal-confirmation condition) - never the
    # leg's-own-premium points/percent variants.
    assert _LAZY_LEG_MOMENTUM_LABELS == {
        "UNDERLYING_UP": "Underlying Pts ↑",
        "UNDERLYING_DOWN": "Underlying Pts ↓",
    }


def test_lazy_leg_selectors_are_present_in_config():
    from pathlib import Path

    from src.config import load_selectors

    selectors = load_selectors(Path("config/selectors.yaml"))
    assert selectors.builder.lazy_leg_modal == 'div:has(> div > p:text-is("Create New Lazy Leg"))'
    assert selectors.builder.lazy_leg_name_input == 'input[placeholder="Leg Identifier"]'
    assert selectors.builder.lazy_leg_create_and_select_button == 'button:text-is("Create and Select")'
    assert selectors.builder.lazy_leg_picker_trigger == (
        'div:has(> div > label > [id^="handle_rentry_"][id$="_Re-entry on SL"]) button[aria-haspopup="listbox"]'
    )
