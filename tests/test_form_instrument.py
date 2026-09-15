from __future__ import annotations

from src.form import _INSTRUMENT_TABS


def test_default_tab_instruments_need_no_tab_switch():
    # Confirmed live: a fresh page load defaults to this tab already.
    assert _INSTRUMENT_TABS["NIFTY"] == "Weekly & Monthly Expiries"
    assert _INSTRUMENT_TABS["SENSEX"] == "Weekly & Monthly Expiries"


def test_monthly_only_instruments_map_to_their_tab():
    # Confirmed live: these four only appear in the Index dropdown once this tab is active.
    for instrument in ["BANKNIFTY", "MIDCPNIFTY", "FINNIFTY", "BANKEX"]:
        assert _INSTRUMENT_TABS[instrument] == "Monthly Only Expiry"


def test_instrument_tab_selector_formats_with_label():
    from src.config import load_selectors
    from pathlib import Path

    selectors = load_selectors(Path("config/selectors.yaml"))
    formatted = selectors.builder.instrument_tab.format(label="Monthly Only Expiry")
    assert formatted == 'button:has-text("Monthly Only Expiry")'
