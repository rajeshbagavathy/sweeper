from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.config import Selectors
from src.locators import ensure_toggle_on, resolve, scroll_to_center

# AlgoTest's <select> option text is capitalized differently than the short codes we
# use in sweep.yaml, so map between them here rather than forcing sweep.yaml authors
# to guess the UI's exact casing.
_ACTION_LABELS = {"BUY": "Buy", "SELL": "Sell"}
_OPTION_TYPE_LABELS = {"CE": "Call", "PE": "Put"}
_MOMENTUM_LABELS = {"UP": "Percent (%) ↑", "DOWN": "Percent (%) ↓"}
_REENTRY_LABELS = {"RE_ASAP": "RE ASAP", "RE_COST": "RE COST", "LAZY_LEG": "Lazy Leg"}
# A Lazy Leg's own "Simple Momentum" always reads the UNDERLYING's move (points),
# never the leg's own premium - see _apply_lazy_leg's docstring for why.
_LAZY_LEG_MOMENTUM_LABELS = {"UNDERLYING_UP": "Underlying Pts ↑", "UNDERLYING_DOWN": "Underlying Pts ↓"}


def apply_combination(page: Page, selectors: Selectors, combo: dict[str, Any]) -> None:
    b = selectors.builder

    # Fresh navigation beats incremental clearing (spec's own guidance) - but
    # goto() to a URL the page is already on can be a no-op for a client-side
    # router without a real reload, which is exactly what let a toggle (and
    # whatever value it gated) survive from the previous combo in the same tab.
    # A real reload is what actually clears the builder back to blank, confirmed
    # against how the site behaves manually - do it every time, not just for the
    # first navigation into a blank tab.
    page.goto(b.url)
    page.reload()

    _set_instrument(page, b, combo["instrument"])
    resolve(page, b.start_date_input).fill(combo["start_date"])
    resolve(page, b.end_date_input).fill(combo["end_date"])
    resolve(page, b.entry_time_input).fill(combo["entry_time"])
    resolve(page, b.exit_time_input).fill(combo["exit_time"])

    for i, leg in enumerate(combo["legs"], start=1):
        # Confirmed live: adding a 2nd+ leg can push this button's default
        # scroll-into-view position right underneath the sticky page header
        # (#strategy-header) - Playwright's own actionability check then refuses to
        # click it (correctly detects the overlap) and times out. Whether this
        # happens depends on exact page height/scroll position, which varies by
        # instrument tab and leg count. force=True is NOT a safe fix here - it
        # skips Playwright's check but the browser's own hit-testing still resolves
        # the click to whatever's actually on top (confirmed live: it silently
        # clicked the sticky header instead, so the 2nd leg was never added at
        # all). Scrolling the button to the vertical center of the viewport first
        # (rather than Playwright's default "just barely visible" edge placement)
        # keeps it clear of the header, so the click lands correctly.
        add_leg = resolve(page, b.add_leg_button)
        scroll_to_center(add_leg)
        add_leg.click()
        leg_locator = page.locator(b.leg_row.format(n=i))
        resolve(leg_locator, b.leg_action).select_option(label=_ACTION_LABELS[leg["action"]])
        resolve(leg_locator, b.leg_option_type).select_option(label=_OPTION_TYPE_LABELS[leg["option_type"]])
        _apply_leg_strike(leg_locator, b, leg["strike"])
        resolve(leg_locator, b.leg_lots).first.fill(str(leg["lots"]))
        _apply_leg_risk(leg_locator, b, combo.get("leg_risk"))
        # Per-LEG (own option_type/strike), unlike leg_risk above which is the
        # same dict applied to every leg - see src/web/expand.py's nest_combo,
        # which only attaches this key to a leg that's actually eligible.
        lazy_leg = leg.get("lazy_leg")
        if lazy_leg is not None:
            _select_lazy_leg_reentry(leg_locator, b, lazy_leg)

    _apply_stoploss(page, b, combo.get("stoploss"))
    _apply_target(page, b, combo.get("target"))
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
        ensure_toggle_on(resolve(leg_locator, b.leg_target_toggle), force=True)
        resolve(leg_locator, b.leg_target_type).first.select_option(label="Percent (%)")
        resolve(leg_locator, b.leg_target_value).fill(str(leg_risk["target_pct"]))

    stoploss = leg_risk.get("stoploss_pct")
    if stoploss is not None:
        ensure_toggle_on(resolve(leg_locator, b.leg_stoploss_toggle), force=True)
        # Confirmed live: AlgoTest's leg Stop Loss type dropdown offers "Underlying %"
        # (SL off a % move in the underlying) alongside "Percent (%)" (SL off the
        # leg's own premium) - both reuse the same single value input, just a
        # different dropdown label.
        label = "Underlying %" if stoploss["kind"] == "underlying_percentage" else "Percent (%)"
        resolve(leg_locator, b.leg_stoploss_type).first.select_option(label=label)
        resolve(leg_locator, b.leg_stoploss_value).fill(str(stoploss["value"]))
    # (leg-level target is percent-only per the original request - unlike stoploss
    # above, which now supports both a premium-% and an underlying-% basis)

    trail = leg_risk.get("trail")
    if trail is not None:
        ensure_toggle_on(resolve(leg_locator, b.leg_trail_toggle), force=True)
        resolve(leg_locator, b.leg_trail_type).select_option(label=trail["type"])
        resolve(leg_locator, b.leg_trail_x).fill(str(trail["x"]))
        resolve(leg_locator, b.leg_trail_y).fill(str(trail["y"]))

    momentum = leg_risk.get("momentum")
    if momentum is not None:
        ensure_toggle_on(resolve(leg_locator, b.leg_momentum_toggle), force=True)
        resolve(leg_locator, b.leg_momentum_type).select_option(label=_MOMENTUM_LABELS[momentum["direction"]])
        resolve(leg_locator, b.leg_momentum_value).fill(str(momentum["value"]))

    reentry_sl = leg_risk.get("reentry_sl")
    if reentry_sl is not None:
        if reentry_sl["type"] == "LAZY_LEG":
            # Deliberately a no-op here - unlike RE ASAP/RE COST below, a lazy
            # leg's actual values (strike/SL%/trail/momentum) are per-LEG, not
            # shared across every leg the way the rest of leg_risk is (see
            # src/web/expand.py's nest_combo, which attaches them to each
            # eligible leg's own "lazy_leg" key, never to leg_risk itself).
            # apply_combination's per-leg loop calls _select_lazy_leg_reentry
            # directly with that leg-specific data instead - this function
            # only ever sees the shared leg_risk dict, which has nothing to
            # fill the popup with.
            return
        ensure_toggle_on(resolve(leg_locator, b.leg_reentry_sl_toggle), force=True)
        resolve(leg_locator, b.leg_reentry_sl_type).select_option(label=_REENTRY_LABELS[reentry_sl["type"]])
        count_trigger = resolve(leg_locator, b.leg_reentry_sl_count_trigger)
        # Same sticky-header scroll issue as "Add Leg" (see apply_combination) -
        # confirmed live: this trigger can land under the sticky top nav, and
        # Playwright correctly refuses to click through it and times out. Centering
        # it in the viewport first (instead of Playwright's default "just barely
        # visible" edge placement) keeps it clear.
        scroll_to_center(count_trigger)
        count_trigger.click()
        # Confirmed live: this listbox's option buttons all carry role="none" (not
        # "option"), so get_by_role("option") never matches here - each option button
        # does carry a plain HTML value="N" attribute though, which is what this
        # matches on instead (visible text is "N" + a hover-only "Select" hint, so
        # exact text matching would be unreliable anyway). leg_risk is applied
        # identically to every leg, so a 2nd+ leg's trigger opens a 2nd listbox
        # while the first one hasn't fully unmounted yet (confirmed live) - a
        # page-wide "[role=listbox] button[value=...]" search then matches more
        # than one element (Playwright strict-mode error). Scope to the most
        # recently opened one instead of searching the whole page.
        leg_locator.page.locator('[role="listbox"]').last.locator(f'button[value="{reentry_sl["count"]}"]').click()


def _select_lazy_leg_reentry(leg_locator, b, lazy_leg: dict[str, Any]) -> None:
    """Toggles a leg's Re-entry on SL on, sets its type to "Lazy Leg", and fills
    the "Create New Lazy Leg" popup - the one sequence both _apply_leg_risk's
    manual reentry_sl["type"] == "LAZY_LEG" path and apply_combination's own
    pipeline-derived per-leg "lazy_leg" attachment (see src/web/expand.py's
    nest_combo) both need.

    Confirmed live: selecting "Lazy Leg" auto-opens that popup only the FIRST
    time on a page with no lazy leg yet - a 2nd+ leg (e.g. the PE leg after a CE
    leg already created "lazy1") instead silently defaults to REUSING the
    existing one. Reusing would be wrong here - a lazy leg's option_type and
    momentum direction are CE-only or PE-only (see src/lazy_leg.py's
    derive_lazy_leg), so each leg that needs one must get its own, never share
    another leg's. Detected by waiting briefly for the popup; if it never
    appears, explicitly ask for a new one via the picker's "Create New" option
    instead of leaving whatever got auto-selected."""
    ensure_toggle_on(resolve(leg_locator, b.leg_reentry_sl_toggle), force=True)
    resolve(leg_locator, b.leg_reentry_sl_type).select_option(label=_REENTRY_LABELS["LAZY_LEG"])
    page = leg_locator.page
    modal = resolve(page, b.lazy_leg_modal)
    try:
        modal.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        resolve(leg_locator, b.lazy_leg_picker_trigger).click()
        page.get_by_role("option", name="Create New").click()
        modal.wait_for(state="visible", timeout=5000)
    _apply_lazy_leg(leg_locator, b, lazy_leg)


def _apply_lazy_leg(leg_locator, b, lazy_leg: dict[str, Any]) -> None:
    """Fills AlgoTest's "Create New Lazy Leg" popup - auto-opened by selecting
    "Lazy Leg" as a leg's Re-entry on SL type (see _apply_leg_risk above), the
    first time that leg's page has no lazy leg attached yet.

    `lazy_leg` carries every value pre-resolved by the caller (this function
    does no strike-math/eligibility decisions of its own):
      - "option_type": "CE"/"PE" - MUST match the parent leg's own side (a lazy
        leg's Option Type defaults to "Call" regardless of its parent leg -
        confirmed live - so it's always set explicitly here, never left as
        whatever the popup happened to default to).
      - "strike": same shape _apply_leg_strike already accepts.
      - "stoploss_pct": same shape leg_risk's own "stoploss_pct" accepts, or None.
      - "trail": same shape leg_risk's own "trail" accepts, or None.
      - "momentum": {"direction": "UNDERLYING_UP"/"UNDERLYING_DOWN", "value": ...},
        or None - the underlying-points reversal-confirmation condition.

    Confirmed live: the popup's own fields reuse the EXACT SAME id prefixes as
    a normal leg row (#select_OptionType, #select_/#select_StrikeType,
    StopLoss_, TrailSL_, simple_momentum_), just scoped inside this modal
    instead of "#backtest-leg-{n}" - so every field function/selector already
    used for a normal leg row (_apply_leg_strike, leg_stoploss_*, leg_trail_*,
    leg_momentum_*) is reused as-is below, resolved against the modal locator
    instead of leg_row.

    Only handles the FIRST lazy leg on the page (the auto-opened popup) - a 2nd+
    leg wanting to reuse or create a distinct lazy leg goes through a separate,
    not-yet-automated picker ("+ Create New" / "Select from existing") - out of
    scope for now."""
    modal = resolve(leg_locator.page, b.lazy_leg_modal)

    resolve(modal, b.leg_option_type).select_option(label=_OPTION_TYPE_LABELS[lazy_leg["option_type"]])
    _apply_leg_strike(modal, b, lazy_leg["strike"])

    stoploss = lazy_leg.get("stoploss_pct")
    if stoploss is not None:
        ensure_toggle_on(resolve(modal, b.leg_stoploss_toggle), force=True)
        label = "Underlying %" if stoploss["kind"] == "underlying_percentage" else "Percent (%)"
        resolve(modal, b.leg_stoploss_type).first.select_option(label=label)
        resolve(modal, b.leg_stoploss_value).fill(str(stoploss["value"]))

    trail = lazy_leg.get("trail")
    if trail is not None:
        ensure_toggle_on(resolve(modal, b.leg_trail_toggle), force=True)
        resolve(modal, b.leg_trail_type).select_option(label=trail["type"])
        resolve(modal, b.leg_trail_x).fill(str(trail["x"]))
        resolve(modal, b.leg_trail_y).fill(str(trail["y"]))

    momentum = lazy_leg.get("momentum")
    if momentum is not None:
        ensure_toggle_on(resolve(modal, b.leg_momentum_toggle), force=True)
        resolve(modal, b.leg_momentum_type).select_option(label=_LAZY_LEG_MOMENTUM_LABELS[momentum["direction"]])
        resolve(modal, b.leg_momentum_value).fill(str(momentum["value"]))

    resolve(modal, b.lazy_leg_create_and_select_button).click()


# Index options are split across AlgoTest's own top-level tabs, and only the options
# belonging to the currently-active tab appear in the dropdown (confirmed live). A
# fresh page load defaults to "Weekly & Monthly Expiries", so NIFTY/SENSEX need no tab
# click; the other indices live under "Monthly Only Expiry" instead.
_INSTRUMENT_TABS = {
    "NIFTY": "Weekly & Monthly Expiries",
    "SENSEX": "Weekly & Monthly Expiries",
    "BANKNIFTY": "Monthly Only Expiry",
    "MIDCPNIFTY": "Monthly Only Expiry",
    "FINNIFTY": "Monthly Only Expiry",
    "BANKEX": "Monthly Only Expiry",
}


def _set_instrument(page: Page, b, instrument: str) -> None:
    tab_label = _INSTRUMENT_TABS.get(instrument)
    if tab_label is not None:
        page.locator(b.instrument_tab.format(label=tab_label)).click()

    resolve(page, b.instrument_select).click()
    # The popup's options are two-line labels (e.g. "NIFTY\nNifty 50"), so exact text
    # match never hits; scope to the single open listbox and match the instrument code
    # as a prefix instead (avoids "NIFTY" matching "BANKNIFTY" if this list ever grows).
    listbox = page.get_by_role("listbox")
    listbox.get_by_role("option", name=re.compile(rf"^{re.escape(instrument)}\b")).click()


def _apply_stoploss(page: Page, b, stoploss: dict[str, Any] | None) -> None:
    if stoploss is None:
        return
    # Confirmed live (headed AND headless): unlike the leg-level and results-panel
    # toggles, this overall-section toggle silently no-ops under click(force=True) -
    # a plain click is what actually flips it. Verified via the .algtst export: with
    # force=True the exported OverallSL stayed {"Type": "None", "Value": 0} even
    # though the type dropdown and value input both looked correctly filled - the
    # backtest silently ran with no overall stop loss at all.
    ensure_toggle_on(resolve(page, b.stoploss_toggle))
    label = "Total Premium %" if stoploss["kind"] == "percentage" else "Max Loss"
    resolve(page, b.stoploss_type).select_option(label=label)
    resolve(page, b.stoploss_value).fill(str(stoploss["value"]))


def _apply_target(page: Page, b, target: dict[str, Any] | None) -> None:
    if target is None:
        return
    ensure_toggle_on(resolve(page, b.target_toggle))  # see _apply_stoploss - force=True no-ops here
    label = "Total Premium %" if target["kind"] == "percentage" else "Max Profit"
    resolve(page, b.target_type).select_option(label=label)
    resolve(page, b.target_value).fill(str(target["value"]))


def _apply_trail_sl(page: Page, b, trail_sl) -> None:
    if trail_sl is None:
        return
    ensure_toggle_on(resolve(page, b.trail_sl_toggle))  # see _apply_stoploss - force=True no-ops here
    # "Lock and Trail" is the only overall-trailing mode exposing all 4 of these
    # fields together (confirmed live) - "Lock" alone only has x/y.
    resolve(page, b.trail_sl_mode).select_option(label="Lock and Trail")
    resolve(page, b.trail_sl_x).fill(str(trail_sl["x"]))
    resolve(page, b.trail_sl_y).fill(str(trail_sl["y"]))
    resolve(page, b.trail_sl_step).fill(str(trail_sl["step"]))
    resolve(page, b.trail_sl_trail_by).fill(str(trail_sl["trail_by"]))
