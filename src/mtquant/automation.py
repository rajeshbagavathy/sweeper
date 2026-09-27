"""Drives mtQuant's live "Create Options Portfolio V2" dialog via pywinauto
to build one portfolio from an MTQuantPortfolioPlan. Windows-only - this is
the one module in src/mtquant/ that needs the `mtquant` optional dependency
group (pywinauto); algtst_parser/field_mapping/preview have none.

Interaction strategy, confirmed live against the running app (2026-09-27):
- Most of the dialog OUTSIDE the leg grid is genuinely UI-Automation
  addressable by auto_id (confirmed by walking the real control tree, not
  guessed) - Default Lots, Portfolio Name, Run On Days, Start/SqOff Time,
  every tab's fields.
- This app's own dropdowns (Syncfusion-styled) do NOT expose their option
  lists to UI Automation at all (confirmed: `descendants(control_type=
  "ListItem")` returns nothing while a dropdown most definitely IS open on
  screen). Two keyboard approaches were tried and BOTH confirmed unreliable
  live: type-ahead jumps per-keystroke rather than buffering a search
  string (typing "CombinedLoss" character-by-character landed on
  "LossAndUnderlyingRange", because the trailing "L" re-jumped to the only
  other L-word); Home+Down navigation doesn't reset position either (Home
  turned out to be a no-op, so a relative Down-arrow from an unknown
  current position landed on the wrong item too). The only method
  confirmed to work reliably is coordinate-clicking the option's measured
  position after opening the dropdown via the combo's own CHEVRON
  specifically (a plain click on the field's text area does NOT open the
  list - confirmed live) - calibrated against the combo's own live
  rectangle (never a hardcoded absolute position), using a measured
  ~27.5px row height. See `_select_dropdown_option`.
- CORRECTED mid-session: the leg grid is NOT UI-Automation-invisible after
  all - that was true of the Multi-Leg tab's own SUMMARY grid (a different
  control entirely, checked before any leg row existed), but the ACTIVE
  row of THIS grid (the one currently being edited, before Enter commits
  it) exposes real auto_id'd controls: btnBuySell, btnCEPE (toggle
  buttons - one click flips Buy<->Sell or CE<->PE, confirmed live),
  nmLots (Spinner), cboStrike, cboSLType, cboTargetType (ComboBoxes -
  same coordinate-click selection as other dropdowns), txtSL, txtTgt,
  txtWT, txtSLWait, txtSpread (Edit), chkIdle, chkHedgeReq (CheckBox).
  Only revealed once a leg actually exists (click btnAdd first) and, for
  Trail SL/TGT, only once a Stoploss/Target type is actually chosen (the
  cboSLTrailing/cboTargetTrailing combos and their "N~M" popups don't
  exist in the tree before that - same dynamic-field-reveal pattern seen
  elsewhere in this dialog). The Trail SL popup's own two number fields
  and confirm/cancel buttons are NOT UI-Automation-addressable (same as
  every other popup in this app) - coordinate-clicked, calibrated against
  the triggering combo's own rectangle.

AlgoTest-value -> mtQuant-dropdown-option translation table (confirmed
live, not assumed):
- OverallSL/OverallTrailSL type "MTM" -> mtQuant's "CombinedLoss" (there is
  no literal "MTM" option; the real choices are None/CombinedLoss/
  CombinedPremium/AbsoluteCombinedPremium/UnderlyingMovement/
  LossAndUnderlyingRange/Delta/Theta).
- OverallTrailSL maps to the STOPLOSS SETTINGS tab's OWN trailing pair
  (txtSLTrailEveryIncreaseInProfit / txtSLTrailTrailSLBy) - confirmed via
  a live tooltip ("Combined Portfolio Stop Loss Settings... similar to the
  Combined Target Type"), NOT the Target Settings tab's trailing pair
  (that one is LockAndTrail's parallel "For Every Increase In Profit By /
  Trail Profit By", a different, separate feature that happens to share
  the same field *names*).
- LockAndTrail maps to the Target Settings tab's "If Profit Reaches" /
  "Lock Minimum Profit At" pair (txtProfitReaches / txtLockProfit).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys
from pywinauto.mouse import click as mouse_click

from src.mtquant.field_mapping import MTQuantPortfolioPlan

MTQUANT_EXE_NAME = "mtQuant.exe"

# Confirmed live auto_ids - the main window and the child dialog it opens.
MAIN_WINDOW_AUTO_ID = "MainForm"
ADD_PORTFOLIO_BUTTON_AUTO_ID = "btnAddPortfolio"
PORTFOLIO_DIALOG_AUTO_ID = "frmOptionPortfolioExecution"

# cboCombinedSLType's full option list, confirmed live by screenshotting the
# opened dropdown - order matters, it's how _select_dropdown_option computes
# an option's row position.
COMBINED_SL_TYPE_OPTIONS = [
    "None",
    "CombinedLoss",
    "CombinedPremium",
    "AbsoluteCombinedPremium",
    "UnderlyingMovement",
    "LossAndUnderlyingRange",
    "Delta",
    "Theta",
]

# AlgoTest's OverallSL/OverallTrailSL "type" string -> mtQuant's own dropdown
# option text. Only MTM has been seen in real data / confirmed live; anything
# else raises rather than guessing at an untested mapping.
OVERALL_SL_TYPE_MAP = {"MTM": "CombinedLoss"}

# A leg row's cboSLType full option list, confirmed live the same way as
# COMBINED_SL_TYPE_OPTIONS above (this is a DIFFERENT, shorter list - the
# leg-level and portfolio-level Stoploss Type dropdowns are not the same
# control despite the similar name).
LEG_SL_TYPE_OPTIONS = ["None", "Premium", "Underlying", "Strike", "AbsolutePremium", "Delta", "Theta"]

# AlgoTest's per-leg LegStopLoss "type" string -> mtQuant's leg-level
# dropdown option. Only Percentage has been seen in real data / confirmed
# live (every leg in the user's actual file uses it).
LEG_SL_TYPE_MAP = {"Percentage": "Premium"}

# The Strike column's "Premium / Greek Legs" sub-form's Value Type options,
# confirmed live (see field_mapping.py's build_premium_selection - this list
# is the same one that discovery was based on).
STRIKE_VALUE_TYPE_OPTIONS = ["Premium", "NearestPremium", "Delta", "IV", "Theta", "NearestDelta", "NearestStraddlePremium"]

# Empirically measured from the live app (2026-09-27): a dropdown's first
# item's vertical center sits this many pixels below the combo's own
# rectangle.bottom, and each subsequent item is this many pixels further
# down. Same combo control style throughout the dialog, so treated as
# dialog-wide constants rather than re-measuring per dropdown - flagged
# here precisely so a future mismatch is easy to spot and fix.
DROPDOWN_FIRST_ITEM_OFFSET = 14
DROPDOWN_ROW_HEIGHT = 27.5


class MTQuantAutomationError(RuntimeError):
    """Raised when the live app isn't in the state this module expects -
    never silently pressed on with a guess."""


def find_mtquant_pid() -> int:
    """Finds the running mtQuant.exe process via pywinauto's own process
    lookup (Application.connect(path=...) - no extra dependency needed;
    an earlier version of this function assumed psutil was available as a
    pywinauto transitive dependency, which a live `uv sync --extra mtquant`
    check disproved - not left as a guess). Raises if it's not running -
    the caller is expected to have the app open already, same as every
    live check this session did by hand."""
    app = Application(backend="uia").connect(path=MTQUANT_EXE_NAME)
    return app.process


def _select_dropdown_option(combo, option_list: list[str], option_text: str) -> None:
    """Selects an option from one of this app's Syncfusion-styled dropdowns
    by coordinate-clicking its measured position - see module docstring for
    why this is the method used (both keyboard approaches tried were
    confirmed unreliable live). `option_list` must be that combo's own full,
    ordered option list (e.g. COMBINED_SL_TYPE_OPTIONS) - position within it
    is how the click's Y offset is computed.
    """
    if option_text not in option_list:
        raise MTQuantAutomationError(f"{option_text!r} isn't in the known option list {option_list!r} for this dropdown.")
    index = option_list.index(option_text)

    rect = combo.rectangle()
    # Click the chevron specifically (rect's right edge) - a plain click
    # elsewhere on the field was confirmed live NOT to open the list.
    mouse_click(button="left", coords=(rect.right - 15, (rect.top + rect.bottom) // 2))
    time.sleep(0.5)

    x = rect.left + 30
    y = int(rect.bottom + DROPDOWN_FIRST_ITEM_OFFSET + index * DROPDOWN_ROW_HEIGHT)
    mouse_click(button="left", coords=(x, y))
    time.sleep(0.3)


def _set_edit_text(pane_or_edit, value: str) -> None:
    """Sets a text/numeric field's value. Several of this dialog's "Edit"
    controls are wrapped in an outer Pane (auto_id on the Pane, a nested
    Edit as the actual typeable child) - this handles both shapes."""
    target = pane_or_edit
    children = pane_or_edit.children(control_type="Edit")
    if children:
        target = children[0]
    target.click_input()
    send_keys("^a")
    time.sleep(0.1)
    send_keys(str(value), with_spaces=True)
    time.sleep(0.1)
    send_keys("{TAB}")
    time.sleep(0.2)


@dataclass
class MTQuantSession:
    """A connected, live mtQuant application - the main window only. Opening
    a new portfolio dialog returns a PortfolioDialog (below)."""

    pid: int
    app: Application
    main: object  # pywinauto WindowSpecification for the main window

    @classmethod
    def connect(cls) -> "MTQuantSession":
        pid = find_mtquant_pid()
        app = Application(backend="uia").connect(process=pid)
        main = app.window(auto_id=MAIN_WINDOW_AUTO_ID, control_type="Window")
        main.set_focus()
        time.sleep(0.3)
        return cls(pid=pid, app=app, main=main)

    def _close_stale_portfolio_dialogs(self) -> None:
        """Closes any already-open "Create Options Portfolio" dialog before
        starting a new one - avoids the app's own "Discard the changes on
        Exiting Opened Portfolio?" confirmation appearing mid-flow (opening
        Add Portfolio while one is already open triggers it), which this
        module doesn't otherwise handle. Always discards - callers that
        care about an existing draft's contents must read them out first."""
        import win32con
        import win32gui

        for w in Desktop(backend="uia").windows():
            if _safe_pid(w) != self.pid or w.handle == self.main.handle:
                continue  # never touch the main window - it also happens to be titled "Form"
            try:
                if w.window_text() == "Form" and w.child_window(auto_id="btnSave", control_type="Button").exists():
                    win32gui.PostMessage(w.handle, win32con.WM_CLOSE, 0, 0)
                    time.sleep(0.3)
                    # A "Discard changes?" confirmation may follow if the draft has edits.
                    self._dismiss_discard_confirmation()
            except Exception:
                continue

    def _dismiss_discard_confirmation(self) -> None:
        """Clicks "Yes" on the app's own "Discard the changes on Exiting
        Opened Portfolio?" popup, if one is currently showing. Best-effort:
        this popup wasn't found to be UI-Automation-addressable either, so
        it's identified by window text via plain win32 enumeration."""
        import win32con
        import win32gui

        def _enum(hwnd, results):
            if win32gui.IsWindowVisible(hwnd) and "discard" in win32gui.GetWindowText(hwnd).lower():
                results.append(hwnd)

        matches: list[int] = []
        win32gui.EnumWindows(_enum, matches)
        for hwnd in matches:
            # The confirmation's own title is "Confirm"; scan its children for a "Yes" button.
            def _enum_child(child_hwnd, results):
                if win32gui.GetWindowText(child_hwnd).strip().lower() == "yes":
                    results.append(child_hwnd)

            yes_buttons: list[int] = []
            win32gui.EnumChildWindows(hwnd, _enum_child, yes_buttons)
            for btn_hwnd in yes_buttons:
                win32gui.PostMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
            time.sleep(0.5)

    def open_add_portfolio_v2(self) -> "PortfolioDialog":
        """Clicks Add Portfolio -> Add Portfolio V2. The dropdown itself is
        a UI-Automation-invisible popup (confirmed live), so the second
        item is clicked at a fixed offset below the button's OWN rectangle
        (read live, not hardcoded) - matches this session's working
        approach exactly. Closes any stale draft dialog first (see
        _close_stale_portfolio_dialogs) so the "Discard changes?" prompt
        never appears mid-flow, and polls for the new window instead of a
        single fixed sleep - a single 1.5s wait was seen live to sometimes
        miss the new window entirely."""
        self._close_stale_portfolio_dialogs()

        before = {w.handle for w in Desktop(backend="uia").windows() if _safe_pid(w) == self.pid}

        btn = self.main.child_window(auto_id=ADD_PORTFOLIO_BUTTON_AUTO_ID)
        rect = btn.rectangle()
        btn.click_input()
        time.sleep(0.8)

        mouse_click(button="left", coords=(rect.left + 40, rect.bottom + 45))

        new_handle = None
        for _ in range(20):  # up to ~4s, polling every 0.2s
            time.sleep(0.2)
            for w in Desktop(backend="uia").windows():
                if _safe_pid(w) == self.pid and w.handle not in before and w.window_text() == "Form":
                    new_handle = w.handle
                    break
            if new_handle is not None:
                break
        if new_handle is None:
            raise MTQuantAutomationError("Add Portfolio V2 dialog didn't open - the app may be in an unexpected state.")

        dlg = self.app.window(handle=new_handle)
        _force_foreground(new_handle)
        return PortfolioDialog(session=self, dlg=dlg)


def _safe_pid(window) -> int | None:
    try:
        return window.process_id()
    except Exception:
        return None


def _force_foreground(hwnd: int) -> None:
    """Explicitly brings a window to the real OS foreground/topmost state.
    Added after a live failure: a freshly-opened dialog reported
    is_visible()==True and a correct rectangle via UIA, yet was actually
    BEHIND the main window on screen - every click_input() on it during
    that window silently landed on the covered main window instead (no
    exception; UIA element-finding succeeds regardless of Z-order, only
    the resulting mouse click is affected), producing an empty leg grid
    with no error raised anywhere. is_visible()/rectangle() are therefore
    not sufficient proof a dialog is actually clickable - this must be
    called before trusting coordinate-based clicks against a newly opened
    window."""
    import win32con
    import win32gui

    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)


@dataclass
class PortfolioDialog:
    """One open "Create Options Portfolio V2" dialog."""

    session: MTQuantSession
    dlg: object  # pywinauto WindowSpecification

    def _field(self, auto_id: str, control_type: str | None = None):
        kwargs = {"auto_id": auto_id}
        if control_type:
            kwargs["control_type"] = control_type
        return self.dlg.child_window(**kwargs)

    def _goto_tab(self, tab_auto_id: str) -> None:
        """Tab items ARE UIA-findable (they're the tab strip itself, not
        tab-page content), but only via a full descendants() scan - a
        direct child_window(auto_id=...) lookup was seen to intermittently
        miss them this session. Each non-active tab's own field controls
        don't exist in the tree at all until the tab has actually been
        clicked (lazy per-tab instantiation), so this must run before
        touching any field that lives on that tab."""
        items = self.dlg.descendants(control_type="TabItem")
        item = next((i for i in items if i.window_text() == tab_auto_id), None)
        if item is None:
            raise MTQuantAutomationError(f"Tab {tab_auto_id!r} not found - dialog may not be fully loaded yet.")
        item.click_input()
        time.sleep(0.6)

    # ---- Portfolio-level fields (all confirmed UIA-addressable) ----

    def set_default_lots(self, lots: int) -> None:
        spinner = self._field("nmDefaultLots")
        _set_edit_text(spinner, lots)

    def set_portfolio_name(self, name: str) -> None:
        pane = self._field("txtStrategyName")  # the actual Portfolio Name field, despite its auto_id
        _set_edit_text(pane, name)

    def set_run_on_days(self, days: list[str]) -> None:
        """Not implemented. cboRunOnDays is a multi-select control (not a
        plain single-choice dropdown), so neither the coordinate-click
        approach used for single-select combos nor either of the two failed
        keyboard approaches directly apply - it needs its own live
        investigation (does it use checkboxes in a dropdown panel? a
        comma-separated type-in?) before this can be implemented for real,
        rather than guessed at. Raises rather than silently doing nothing
        or doing the wrong thing."""
        raise NotImplementedError(
            "set_run_on_days: cboRunOnDays' interaction model hasn't been confirmed live yet - see this method's docstring."
        )

    def set_start_time(self, hhmmss: str) -> None:
        pane = self._field("dtStartTime")
        _set_edit_text(pane, hhmmss)

    def set_sqoff_time(self, hhmmss: str) -> None:
        pane = self._field("dtSquareOffTime")
        _set_edit_text(pane, hhmmss)

    # ---- Target Settings tab: Lock-and-trail (AlgoTest's LockAndTrail) ----

    def set_lock_and_trail(self, instrument_move, stoploss_move) -> None:
        self._goto_tab("tbTargetSettings")
        _set_edit_text(self._field("txtProfitReaches"), instrument_move)
        _set_edit_text(self._field("txtLockProfit"), stoploss_move)

    # ---- Stoploss Settings tab: Overall SL + its own trail (AlgoTest's
    # OverallSL / OverallTrailSL - NOT the Target Settings tab's trail
    # pair, see module docstring) + Move SL to Cost ----

    def set_overall_stoploss(self, algtst_type: str, value) -> None:
        if algtst_type not in OVERALL_SL_TYPE_MAP:
            raise MTQuantAutomationError(
                f"OverallSL type {algtst_type!r} has no confirmed mtQuant mapping yet (only 'MTM' -> 'CombinedLoss' is confirmed)."
            )
        self._goto_tab("tbStoplossSettings")
        _select_dropdown_option(self._field("cboCombinedSLType"), COMBINED_SL_TYPE_OPTIONS, OVERALL_SL_TYPE_MAP[algtst_type])
        _set_edit_text(self._field("txtCombinedSLValue"), value)

    def set_overall_trail(self, instrument_move, stoploss_move) -> None:
        self._goto_tab("tbStoplossSettings")
        _set_edit_text(self._field("txtSLTrailEveryIncreaseInProfit"), instrument_move)
        _set_edit_text(self._field("txtSLTrailTrailSLBy"), stoploss_move)

    def set_move_sl_to_cost(self, enabled: bool) -> None:
        self._goto_tab("tbStoplossSettings")
        chk = self._field("chkSLToCost")
        is_checked = bool(chk.get_toggle_state()) if hasattr(chk, "get_toggle_state") else None
        if is_checked != enabled:
            chk.click_input()
            time.sleep(0.2)

    # ---- Leg grid ----
    #
    # CORRECTED per the user's own direct guidance on how they use the app
    # (2026-09-27), replacing an earlier wrong assumption in this module:
    # for a PREMIUM-based strategy, click "Add Leg" once per leg needed
    # UP FRONT (e.g. twice for a 2-leg strategy) BEFORE filling any
    # fields, then go back and fill each row - not fill-one-then-add-next.
    # Confirmed live: with both rows added first, EVERY row's fields are
    # genuinely UI-Automation-addressable simultaneously (checked
    # btnBuySell, cboClosestPremium, cboSLType, cboTargetType - all gave
    # exactly 2 matches, one per row, distinguishable by rect.top). The
    # earlier "second row is broken" conclusion was from testing the WRONG
    # order (fill row 1 fully, including pressing Enter, before adding row
    # 2), which is not how the app is meant to be used.
    #
    # For an ATM/OTM strategy, the user's guidance is to use the
    # "Predefined Strategies" dropdown instead (Short Straddle for ATM,
    # Short Strangle for OTM) rather than building legs one at a time -
    # NOT YET IMPLEMENTED/TESTED in this module, see select_predefined_
    # strategy's docstring.
    #
    # The Strike column's live control is named "cboClosestPremium" here
    # (NOT "cboStrike" - that name doesn't exist once chkPremiumGreekLeg
    # is ticked; corrected from an earlier wrong guess in this module).
    # Because multiple rows can share the same auto_id once 2+ legs exist,
    # every leg-row method below takes an explicit `row_index` (0-based,
    # rows ordered top-to-bottom) and uses `_leg_field` to disambiguate.

    def _leg_field(self, auto_id: str, row_index: int, control_type: str | None = None):
        """Finds the row_index-th (0-based, sorted top-to-bottom) live
        control with this auto_id - needed because once 2+ legs exist,
        every leg-row auto_id has one match per row, and plain child_window
        picks an unspecified one."""
        kwargs = {"control_type": control_type} if control_type else {}
        matches = [d for d in self.dlg.descendants(**kwargs) if d.automation_id() == auto_id]
        matches.sort(key=lambda d: d.rectangle().top)
        if row_index >= len(matches):
            raise MTQuantAutomationError(
                f"row_index={row_index} out of range for {auto_id!r} - only {len(matches)} row(s) currently have a live control with this id. "
                "Legs must be added (add_leg(), once per leg, before filling any of them) before their fields can be set."
            )
        return matches[row_index]

    def add_leg(self) -> None:
        """Clicks "Add Leg", creating a new row - Buy/CE/1 lot/Weekly by
        default (Strike is blank when chkPremiumGreekLeg is ticked, "ATM"
        otherwise). Call this once per leg needed, for ALL legs, before
        filling any of their fields - see the class-level note above."""
        self._field("btnAdd", "Button").click_input()
        time.sleep(0.5)

    def select_predefined_strategy(self, name: str) -> None:
        """Not yet implemented/tested. The user's guidance for an ATM/OTM
        strategy is to use the "Predefined Strategies" dropdown (e.g.
        "ShortStraddle" for ATM, "ShortStrangle" for OTM) instead of
        building legs one at a time - this auto-populates the matching
        legs. Raises rather than guessing at the dropdown's exact option
        text or interaction pattern, neither of which has been confirmed
        live yet."""
        raise NotImplementedError("select_predefined_strategy: guidance received but not yet tested live - see this method's docstring.")

    def set_leg_buy_sell(self, row_index: int, side: str) -> None:
        """`side`: "Buy" | "Sell". One click flips the toggle button.

        CORRECTED mid-session: this used to check the button's current text
        first to be idempotent, but window_text() was confirmed live to
        always return the literal string "Button" for this control, never
        the actual "Buy"/"Sell" label - so that check was silently
        comparing against the wrong thing and could click when it
        shouldn't have (confirmed: it caused a real, wrong CE->PE flip on
        set_leg_ce_pe below). This method now assumes the KNOWN default
        for a freshly-added leg ("Buy") rather than querying unreliable
        state - it must be called exactly once per leg, right after
        add_leg(), not repeatedly."""
        if side.lower() != "buy":
            self._leg_field("btnBuySell", row_index, "Button").click_input()
            time.sleep(0.3)

    def set_leg_ce_pe(self, row_index: int, kind: str) -> None:
        """`kind`: "CE" | "PE". Same fixed pattern as set_leg_buy_sell -
        assumes the known default ("CE") for a freshly-added leg rather
        than an unreliable window_text() readback. Call exactly once per
        leg, right after add_leg()."""
        if kind.upper() != "CE":
            self._leg_field("btnCEPE", row_index, "Button").click_input()
            time.sleep(0.3)

    def set_leg_lots(self, row_index: int, lots: int) -> None:
        _set_edit_text(self._leg_field("nmLots", row_index, "Spinner"), lots)

    def set_leg_strike_premium(self, row_index: int, target_premium) -> None:
        """Opens the Strike column's "Premium / Greek Legs" sub-form (via
        its chevron - a plain click on the text portion does not open it)
        and sets Value Type=NearestPremium, Value=target_premium, leaving
        Cond ("Any"), Max Depth (15) and Side (BOTH) at their own defaults
        - see field_mapping.py's build_premium_selection for why
        NearestPremium with no range is the right choice for AlgoTest's
        EntryByPremium. Confirmed live for both a first AND a second leg
        row, using each row's own combo rectangle as the offset origin -
        not just tested once and assumed to generalize.

        REQUIRES chkPremiumGreekLeg to already be ticked at the time this
        leg was added via add_leg() - confirmed live that the sub-form
        vs. plain ATM-offset-picker choice is locked in at leg-CREATION
        time and does NOT change retroactively if the checkbox is ticked
        after the fact.
        """
        combo = self._leg_field("cboClosestPremium", row_index, "ComboBox")
        rect = combo.rectangle()
        mouse_click(button="left", coords=(rect.right - 8, (rect.top + rect.bottom) // 2))
        time.sleep(0.6)

        # Sub-form opens directly below the combo, left-aligned with it -
        # offsets measured live against both a first-leg and a second-leg
        # combo (2026-09-27), consistent between the two.
        vt_chevron_x = rect.left + 350
        vt_chevron_y = rect.bottom + 15
        mouse_click(button="left", coords=(vt_chevron_x, vt_chevron_y))
        time.sleep(0.6)
        index = STRIKE_VALUE_TYPE_OPTIONS.index("NearestPremium")
        mouse_click(
            button="left",
            coords=(rect.left + 30, int(vt_chevron_y + DROPDOWN_FIRST_ITEM_OFFSET + index * DROPDOWN_ROW_HEIGHT)),
        )
        time.sleep(0.4)

        # Value field: same row as Value Type, one field-row below it.
        mouse_click(button="left", coords=(rect.left + 30, rect.bottom + 72))
        time.sleep(0.2)
        send_keys("^a")
        time.sleep(0.1)
        send_keys(str(target_premium), with_spaces=True)
        time.sleep(0.2)
        # Click elsewhere on the dialog to commit + close the popup - Enter
        # was confirmed live to REVERT the value instead of committing it,
        # so this deliberately does not press Enter here.
        dlg_rect = self.dlg.rectangle()
        mouse_click(button="left", coords=(dlg_rect.left + 50, dlg_rect.top + 50))
        time.sleep(0.3)

    def set_leg_stoploss_premium(self, row_index: int, value) -> None:
        """Sets the leg's Stoploss Type to "Premium" (AlgoTest's
        "Percentage" type - see LEG_SL_TYPE_MAP) and its value."""
        combo = self._leg_field("cboSLType", row_index, "ComboBox")
        _select_dropdown_option(combo, LEG_SL_TYPE_OPTIONS, LEG_SL_TYPE_MAP["Percentage"])
        _set_edit_text(self._leg_field("txtSL", row_index, "Edit"), value)

    def set_leg_wait_trade(self, row_index: int, value: str) -> None:
        _set_edit_text(self._leg_field("txtWT", row_index, "Edit"), value)

    def set_leg_idle(self, row_index: int, idle: bool) -> None:
        chk = self._leg_field("chkIdle", row_index, "CheckBox")
        is_checked = bool(chk.get_toggle_state()) if hasattr(chk, "get_toggle_state") else None
        if is_checked != idle:
            chk.click_input()
            time.sleep(0.2)

    def set_leg_trail_sl(self, row_index: int, instrument_move, stoploss_move) -> None:
        """Opens the Trail SL popup (cboSLTrailing's chevron - only exists
        once a Stoploss Type has actually been chosen, so call
        set_leg_stoploss_premium first), types both values, and clicks the
        popup's own green confirm checkmark. Field/button offsets measured
        live against this exact combo (2026-09-27) - NOT yet re-verified
        against a second leg row the way set_leg_strike_premium was, so
        treat this one row-generalization as slightly less certain."""
        combo = self._leg_field("cboSLTrailing", row_index, "ComboBox")
        rect = combo.rectangle()
        mouse_click(button="left", coords=(rect.right - 8, (rect.top + rect.bottom) // 2))
        time.sleep(0.6)

        first_field_x = rect.left + 36
        first_field_y = rect.bottom + 100
        second_field_x = rect.left + 240
        confirm_x = rect.left + 378
        confirm_y = rect.bottom + 157

        mouse_click(button="left", coords=(first_field_x, first_field_y))
        time.sleep(0.2)
        send_keys("^a")
        time.sleep(0.1)
        send_keys(str(instrument_move), with_spaces=True)
        time.sleep(0.2)

        mouse_click(button="left", coords=(second_field_x, first_field_y))
        time.sleep(0.2)
        send_keys("^a")
        time.sleep(0.1)
        send_keys(str(stoploss_move), with_spaces=True)
        time.sleep(0.2)

        mouse_click(button="left", coords=(confirm_x, confirm_y))
        time.sleep(0.3)

    def save(self) -> None:
        """Clicks Save Portfolio. NOT yet exercised live in this module -
        every test this session stopped short of this deliberately. Calling
        code should get explicit user confirmation before ever calling this
        for real."""
        self._field("btnSave").click_input()
        time.sleep(1.0)


def build_portfolio(session: MTQuantSession, plan: MTQuantPortfolioPlan, *, save: bool = False) -> PortfolioDialog:
    """Builds one portfolio from a plan. Currently fills only the
    portfolio-level fields confirmed in this module (Default Lots, Name,
    Start/SqOff Time, Lock-and-trail, Overall SL/Trail, Move SL to Cost) -
    the leg grid is NOT yet filled, so the resulting draft is incomplete.
    `save=False` (the default) leaves the draft open for manual review
    instead of ever clicking Save automatically - this default should stay
    False until a human has reviewed at least one real build end-to-end.
    """
    dialog = session.open_add_portfolio_v2()

    if plan.default_lots is not None:
        dialog.set_default_lots(plan.default_lots)
    dialog.set_portfolio_name(plan.portfolio_name)
    if plan.start_time:
        dialog.set_start_time(plan.start_time)
    if plan.sqoff_time:
        dialog.set_sqoff_time(plan.sqoff_time)

    if plan.lock_and_trail and plan.lock_and_trail["type"] == "Points":
        v = plan.lock_and_trail["value"]
        dialog.set_lock_and_trail(v["InstrumentMove"], v["StopLossMove"])

    if plan.overall_stoploss:
        dialog.set_overall_stoploss(plan.overall_stoploss["type"], plan.overall_stoploss["value"])

    if plan.overall_trail and plan.overall_trail["type"] == "Points":
        v = plan.overall_trail["value"]
        dialog.set_overall_trail(v["InstrumentMove"], v["StopLossMove"])

    dialog.set_move_sl_to_cost(plan.move_sl_to_cost)

    if save:
        dialog.save()

    return dialog
