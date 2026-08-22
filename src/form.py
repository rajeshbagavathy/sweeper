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
        resolve(leg_locator, b.leg_strike_selector).select_option(label=str(leg["strike"]))
        resolve(leg_locator, b.leg_lots).first.fill(str(leg["lots"]))

    _apply_stoploss(page, b, combo.get("stoploss_pct"))
    _apply_target(page, b, combo.get("target_pct"))
    _apply_trail_sl(page, b, combo.get("trail_sl"))

    resolve(page, b.run_backtest_button).click()


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
    resolve(page, b.trail_sl_x).fill(str(trail_sl["x"]))
    resolve(page, b.trail_sl_y).fill(str(trail_sl["y"]))
