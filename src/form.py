from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Page

from src.config import Selectors
from src.locators import resolve

# AlgoTest's <select> option text is capitalized differently than the short codes we
# use in sweep.yaml, so map between them here rather than forcing sweep.yaml authors
# to guess the UI's exact casing.
_ACTION_LABELS = {"BUY": "Buy", "SELL": "Sell"}
_OPTION_TYPE_LABELS = {"CE": "Call", "PE": "Put"}


def apply_combination(page: Page, selectors: Selectors, combo: dict[str, Any]) -> None:
    b = selectors.builder

    # Fresh navigation beats incremental clearing (spec's own guidance) - but this
    # assumes /backtest resets to a blank default builder rather than resuming your
    # last-viewed saved strategy. Confirm this on the first --limit 3 smoke test.
    page.goto(b.url)

    _set_instrument(page, b, combo["instrument"])
    resolve(page, b.start_date_input).fill(combo["start_date"])
    resolve(page, b.end_date_input).fill(combo["end_date"])
    resolve(page, b.entry_time_input).fill(combo["entry_time"])
    resolve(page, b.exit_time_input).fill(combo["exit_time"])

    for i, leg in enumerate(combo["legs"], start=1):
        resolve(page, b.add_leg_button).click()
        leg_locator = page.locator(b.leg_row.format(n=i))
        resolve(leg_locator, b.leg_action).select_option(label=_ACTION_LABELS[leg["action"]])
        resolve(leg_locator, b.leg_option_type).select_option(label=_OPTION_TYPE_LABELS[leg["option_type"]])
        _apply_leg_strike(leg_locator, b, leg["strike"])
        resolve(leg_locator, b.leg_lots).first.fill(str(leg["lots"]))
        _apply_leg_risk(leg_locator, b, combo.get("leg_risk"))

    _apply_stoploss(page, b, combo.get("stoploss_pct"))
    _apply_target(page, b, combo.get("target_pct"))
    _apply_trail_sl(page, b, combo.get("trail_sl"))

    resolve(page, b.run_backtest_button).click()


def _apply_leg_strike(leg_locator, b, strike: str | dict[str, Any]) -> None:
    """strike is either a plain offset string ("ATM", "OTM3", ...) or
    {"mode": "premium_closest", "value": ...} (discovered live while building the
    sweep-config web UI)."""
    if isinstance(strike, str):
        resolve(leg_locator, b.leg_strike_selector).select_option(label=strike)
        return

    mode = strike["mode"]
    if mode == "premium_closest":
        resolve(leg_locator, b.leg_strike_criteria).select_option(label="Closest Premium")
        resolve(leg_locator, b.leg_strike_premium_value).fill(str(strike["value"]))
    else:
        raise ValueError(f"Unknown leg strike mode: {mode!r}")


def _apply_leg_risk(leg_locator, b, leg_risk: dict[str, Any] | None) -> None:
    """Per-leg Target Profit / Stop Loss / Trail SL - same config applied to every leg."""
    if leg_risk is None:
        return

    if leg_risk.get("target_pct") is not None:
        resolve(leg_locator, b.leg_target_toggle).click(force=True)
        resolve(leg_locator, b.leg_target_type).first.select_option(label="Percent (%)")
        resolve(leg_locator, b.leg_target_value).fill(str(leg_risk["target_pct"]))

    if leg_risk.get("stoploss_pct") is not None:
        resolve(leg_locator, b.leg_stoploss_toggle).click(force=True)
        resolve(leg_locator, b.leg_stoploss_type).first.select_option(label="Percent (%)")
        resolve(leg_locator, b.leg_stoploss_value).fill(str(leg_risk["stoploss_pct"]))

    trail = leg_risk.get("trail")
    if trail is not None:
        resolve(leg_locator, b.leg_trail_toggle).click(force=True)
        resolve(leg_locator, b.leg_trail_type).select_option(label=trail["type"])
        resolve(leg_locator, b.leg_trail_x).fill(str(trail["x"]))
        resolve(leg_locator, b.leg_trail_y).fill(str(trail["y"]))


def _set_instrument(page: Page, b, instrument: str) -> None:
    resolve(page, b.instrument_select).click()
    # The popup's options are two-line labels (e.g. "NIFTY\nNifty 50"), so exact text
    # match never hits; scope to the single open listbox and match the instrument code
    # as a prefix instead (avoids "NIFTY" matching "BANKNIFTY" if this list ever grows).
    listbox = page.get_by_role("listbox")
    listbox.get_by_role("option", name=re.compile(rf"^{re.escape(instrument)}\b")).click()


def _apply_stoploss(page: Page, b, stoploss_pct) -> None:
    if stoploss_pct is None:
        return
    # force=True: a normal click's actionability check silently misses these custom
    # toggle-switch buttons under headless Chromium (confirmed during the smoke test -
    # the click "succeeds" but the switch never flips without force).
    resolve(page, b.stoploss_toggle).click(force=True)
    resolve(page, b.stoploss_type).select_option(label="Total Premium %")
    resolve(page, b.stoploss_value).fill(str(stoploss_pct))


def _apply_target(page: Page, b, target_pct) -> None:
    if target_pct is None:
        return
    resolve(page, b.target_toggle).click(force=True)
    resolve(page, b.target_type).select_option(label="Total Premium %")
    resolve(page, b.target_value).fill(str(target_pct))


def _apply_trail_sl(page: Page, b, trail_sl) -> None:
    if trail_sl is None:
        return
    resolve(page, b.trail_sl_toggle).click(force=True)
    # "Lock and Trail" is the only overall-trailing mode exposing all 4 of these
    # fields together (confirmed live) - "Lock" alone only has x/y.
    resolve(page, b.trail_sl_mode).select_option(label="Lock and Trail")
    resolve(page, b.trail_sl_x).fill(str(trail_sl["x"]))
    resolve(page, b.trail_sl_y).fill(str(trail_sl["y"]))
    resolve(page, b.trail_sl_step).fill(str(trail_sl["step"]))
    resolve(page, b.trail_sl_trail_by).fill(str(trail_sl["trail_by"]))
