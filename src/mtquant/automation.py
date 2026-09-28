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
- LockAndTrail maps to the Target Settings "Portfolio Profit Protection"
  pair "If Profit Reaches" / "Lock Minimum Profit At" (txtProfitReaches /
  txtLockProfit).
- OverallTrailSL maps to the other pair in that same section: "For Every
  Increase In Profit By" / "Trail Profit By" (txtIncreaseInProfit /
  txtTrailProfit). The Stoploss Settings tab has its own trail pair with
  similar names; this builder does not fill that one.
- Combined-loss "SL wait" is txtCombinedSLWait, filled with 10 seconds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys
from pywinauto.mouse import click as mouse_click

from src.mtquant.field_mapping import MTQuantPortfolioPlan, grid_rows

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

# cboCombinedTargetType's option list, read off the opened dropdown on
# 2026-09-27. Same shape as the stoploss list, with CombinedProfit in the
# slot CombinedLoss occupies on the other tab.
TARGET_TYPE_OPTIONS = [
    "None",
    "CombinedProfit",
    "CombinedPremium",
    "AbsoluteCombinedPremium",
    "CombinedMultiTgt",
    "UnderlyingMovement",
    "Delta",
    "Theta",
]

# The DTE multi-select that replaces cboRunOnDays once the "DTE" radio is
# on. Measured against a fresh Add Portfolio V2 dialog (2026-09-27): every
# box starts checked, SelectAll is the first row, then DTE_0, DTE_1, ...
# DTE_0 through DTE_8 are on screen without scrolling. Offsets are from the
# combo's own rectangle, same idea as DROPDOWN_ROW_HEIGHT.
DTE_FIRST_ITEM_OFFSET = 15
DTE_ROW_HEIGHT = 27
DTE_CHECKBOX_X = 20
DTE_OK_X = 71
DTE_OK_Y = 320
DTE_VISIBLE_MAX = 8


def dte_option_name(dte: int) -> str:
    """Checkbox label for one DTE. Must be matched in full: DTE_1 is a prefix of DTE_10."""
    return f"DTE_{dte}"

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

# cboPredifinedStrategy is a tree, not a flat list. "Popular" is the first
# row and is expanded by default, so its children are already on screen
# (read off the open dropdown, 2026-09-27). Row pitch is tighter than the
# flat Syncfusion dropdowns above. Names are the control's own text
# (ShortStraddle, not "Short Straddle").
PREDEFINED_TREE_OPTIONS = [
    "Popular",
    "Custom",
    "ShortStraddle",
    "ShortStrangle",
    "IronCondor",
    "LongButterfly",
    "IronButterfly",
    "CallRatioSpread",
    "PutRatioSpread",
]
PREDEFINED_TREE_ROW_HEIGHT = 23

# "On Stoploss Action" opens frmLegAction. The + button adds a row whose first
# dropdown chooses the scope, and choosing "Leg" reveals cboLeg. Confirmed
# live 2026-09-27. AlgoTest AtCost re-entry is cboLeg's "ReEntry".
ON_SL_SCOPE_OPTIONS = ["None", "Leg", "Current_Portfolio", "Other_Portfolio"]
ON_SL_LEG_ACTIONS = [
    "SqOff",
    "Execute",
    "ReExecute",
    "ReEntry",
    "Keep_Running",
    "Sqoff_Linked",
    "Pyramiding",
    "ReExecute_At_Opposite_LTP",
    "Duplicate_And_Execute",
    "ReExecute_AfterFill_SqOff",
]
REENTRY_LEG_ACTION = {"AtCost": "ReEntry"}
# Add Portfolio (not V2) leg-grid "On Stoploss" dropdown, cboSLAction.
# Read live 2026-09-28 after a stop-loss value was entered on the original legs.
# Execute_Leg3 is index 12 and Execute_Leg4 is index 13.
CLASSIC_ON_SL_ACTIONS = [
    "None",
    "SqOff_Current_Portfolio",
    "SqOff_Leg1",
    "SqOff_Leg2",
    "SqOff_Leg3",
    "SqOff_Leg4",
    "SqOff_Leg5",
    "SqOff_Leg6",
    "SqOff_Leg7",
    "SqOff_Leg8",
    "Execute_Leg1",
    "Execute_Leg2",
    "Execute_Leg3",
    "Execute_Leg4",
    "Execute_Leg5",
    "Execute_Leg6",
    "Execute_Leg7",
    "Execute_Leg8",
    "SqOff_Other_Portfolio",
    "Execute_Other_Portfolio",
    "ReExecute_Current_Portfolio",
    "ReExecuteLeg",
    "ReEntry",
    "Keep_Leg_Running",
    "Sqoff_Linked_Legs",
    "ReExecute_Portfolio_OnComplete",
    "ReExecute_Portfolio_AtEntryPrice",
]
# Shown once cboLeg is ReEntry: Count and Delay (Sec). Same values on every leg.
REENTRY_COUNT = 1
REENTRY_DELAY_SECONDS = 5


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


def _richedit_text(hwnd: int) -> str:
    import ctypes
    import win32gui

    length = win32gui.SendMessage(hwnd, 0x000E, 0, 0)  # WM_GETTEXTLENGTH
    buf = ctypes.create_unicode_buffer(length + 1)
    win32gui.SendMessage(hwnd, 0x000D, length + 1, buf)  # WM_GETTEXT
    return buf.value


def _clock_edit_hwnd(pane_hwnd: int) -> int:
    """The RichEdit inside a Timings clock. Its text is the visible HH:MM:SS."""
    import win32gui

    found: list[int] = []

    def _visit(child, _):
        if "RichEdit" in win32gui.GetClassName(child):
            found.append(child)

    win32gui.EnumChildWindows(pane_hwnd, _visit, None)
    if not found:
        raise MTQuantAutomationError("Portfolio clock has no text field to read back.")
    return found[0]


def _type_clock_digits(pane_hwnd: int, digits: str) -> None:
    """Types two digits into the clock segment that is already highlighted.

    Posting the keys to the clock pane is what the control accepts. Ordinary
    keyboard input sent to the foreground window does not change it.
    """
    import win32gui

    for char in digits:
        vk = ord(char)
        win32gui.PostMessage(pane_hwnd, 0x100, vk, 0)  # WM_KEYDOWN
        win32gui.PostMessage(pane_hwnd, 0x102, vk, 0)  # WM_CHAR
        win32gui.PostMessage(pane_hwnd, 0x101, vk, 0)  # WM_KEYUP
        time.sleep(0.06)


def _set_clock(control, hhmmss: str) -> None:
    """Sets a portfolio Start or SqOff clock to HH:MM:SS.

    `hhmmss` is already one second earlier than the strategy time
    (09:37 is passed as 09:36:59). The hour, minute, and second are each
    clicked and then typed. The up/down arrows on the field are not used.
    """
    if len(hhmmss) != 8 or hhmmss[2] != ":" or hhmmss[5] != ":":
        raise MTQuantAutomationError(f"Clock value {hhmmss!r} is not HH:MM:SS.")
    wrapper = control.wrapper_object()
    edit = _clock_edit_hwnd(wrapper.handle)
    rect = wrapper.rectangle()
    mid_y = (rect.top + rect.bottom) // 2
    # Offsets measured live on the Timings Start Time field.
    segments = (
        (rect.left + 22, hhmmss[0:2]),
        (rect.left + 57, hhmmss[3:5]),
        (rect.left + 78, hhmmss[6:8]),
    )
    for x, digits in segments:
        mouse_click(button="left", coords=(x, mid_y))
        time.sleep(0.12)
        _type_clock_digits(wrapper.handle, digits)
        time.sleep(0.08)
    raw = _richedit_text(edit).strip().split(":")
    if len(raw) == 3 and all(part.isdigit() for part in raw):
        final = f"{int(raw[0]):02d}:{int(raw[1]):02d}:{int(raw[2]):02d}"
    else:
        final = _richedit_text(edit).strip()
    if final != hhmmss:
        raise MTQuantAutomationError(f"Portfolio clock is {final!r} after setting {hhmmss!r}.")


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
    # pywinauto treats a bare % as Alt, so a stoploss of "25%" would not type the percent sign.
    send_keys(str(value).replace("%", "{%}"), with_spaces=True)
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
            title = win32gui.GetWindowText(hwnd).lower()
            # The popup's title is "Confirm". The question text is not the title.
            if win32gui.IsWindowVisible(hwnd) and ("discard" in title or title == "confirm"):
                results.append(hwnd)

        matches: list[int] = []
        win32gui.EnumWindows(_enum, matches)
        for hwnd in matches:
            # The confirmation's own title is "Confirm"; scan its children for a "Yes" button.
            def _enum_child(child_hwnd, results):
                label = win32gui.GetWindowText(child_hwnd).replace("&", "").strip().lower()
                if label == "yes":
                    results.append(child_hwnd)

            yes_buttons: list[int] = []
            win32gui.EnumChildWindows(hwnd, _enum_child, yes_buttons)
            for btn_hwnd in yes_buttons:
                win32gui.PostMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
            time.sleep(0.5)

    def open_add_portfolio(self) -> "PortfolioDialog":
        """Opens the original Add Portfolio dialog (not V2).

        That dialog's leg grid has an On Stoploss dropdown (cboSLAction)
        whose options include Execute_LegN. V2 does not. Lazy legs are
        built here.
        """
        return self._open_portfolio_dialog("Add Portfolio")

    def open_add_portfolio_v2(self) -> "PortfolioDialog":
        """Clicks Add Portfolio -> Add Portfolio V2."""
        return self._open_portfolio_dialog("Add Portfolio V2")

    def _open_portfolio_dialog(self, menu_text: str) -> "PortfolioDialog":
        """Opens one item from the Add Portfolio menu. The menu items are
        real MenuItem controls (confirmed live 2026-09-28), matched by exact
        text so "Add Portfolio" never clicks "Add Portfolio V2". Closes any
        stale draft first so the discard prompt never appears mid-flow."""
        self._close_stale_portfolio_dialogs()
        before = {w.handle for w in Desktop(backend="uia").windows() if _safe_pid(w) == self.pid}

        btn = self.main.child_window(auto_id=ADD_PORTFOLIO_BUTTON_AUTO_ID)
        btn.click_input()
        clicked = False
        for _ in range(15):
            time.sleep(0.2)
            for w in Desktop(backend="uia").windows():
                if _safe_pid(w) != self.pid:
                    continue
                try:
                    items = w.descendants(control_type="MenuItem")
                except Exception:
                    continue
                match = next((item for item in items if item.window_text() == menu_text), None)
                if match is not None:
                    match.click_input()
                    clicked = True
                    break
            if clicked:
                break
        if not clicked:
            raise MTQuantAutomationError(f"Add Portfolio menu item {menu_text!r} didn't appear.")

        new_handle = None
        for _ in range(20):
            time.sleep(0.2)
            for w in Desktop(backend="uia").windows():
                if _safe_pid(w) == self.pid and w.handle not in before and w.window_text() == "Form":
                    new_handle = w.handle
                    break
            if new_handle is not None:
                break
        if new_handle is None:
            raise MTQuantAutomationError(f"{menu_text} dialog didn't open - the app may be in an unexpected state.")

        dlg = self.app.window(handle=new_handle)
        _force_foreground(new_handle)
        return PortfolioDialog(session=self, dlg=dlg)

    def select_main_tab(self, tab_name: str) -> None:
        """Selects a main-window tab (Multi-Leg, Strategies, ...) by the UIA
        selection pattern. A mouse click on these tabs was confirmed not to
        change the selected tab; SelectionItem.Select() does."""
        tabs = [t for t in self.main.descendants(control_type="TabItem") if t.window_text() == tab_name]
        if not tabs:
            raise MTQuantAutomationError(f"Main-window tab {tab_name!r} not found.")
        tabs[0].iface_selection_item.Select()
        time.sleep(0.6)

    def list_strategy_tags(self) -> list[str]:
        """Strategy Tag values on the Strategies grid, top to bottom, skipping blanks."""
        self.select_main_tab("Strategies")
        grid = self.main.child_window(auto_id="gridStrategyCodes", control_type="Table")
        cells = [d for d in grid.descendants(control_type="DataItem") if (d.window_text() or "").startswith("StrategyTagRow")]
        cells.sort(key=lambda d: d.rectangle().top)
        tags: list[str] = []
        for cell in cells:
            try:
                value = (cell.iface_value.CurrentValue or "").strip()
            except Exception:
                value = ""
            if value:
                tags.append(value)
        return tags

    def ensure_strategy_tag(self, tag: str) -> list[str]:
        """Creates `tag` on the Strategies tab if it isn't there already.

        Clicking the empty cell under the Strategy Tag column opens an editor
        (confirmed live). Enter commits it. Returns the tag list afterwards,
        which is also the order used to pick the tag in the portfolio dialog.
        """
        existing = self.list_strategy_tags()
        if tag in existing:
            return existing

        grid = self.main.child_window(auto_id="gridStrategyCodes", control_type="Table")
        header = next(h for h in grid.descendants(control_type="Header") if h.window_text() == "StrategyTagRow0")
        header_rect = header.rectangle()
        cells = [d for d in grid.descendants(control_type="DataItem") if (d.window_text() or "").startswith("StrategyTagRow")]
        cells.sort(key=lambda d: d.rectangle().top)

        def _cell_value(cell) -> str:
            try:
                return (cell.iface_value.CurrentValue or "").strip()
            except Exception:
                return ""

        blank = next((c for c in cells if not _cell_value(c)), None)
        if blank is not None:
            rect = blank.rectangle()
            x = (rect.left + rect.right) // 2
            y = (rect.top + rect.bottom) // 2
        elif cells:
            last = cells[-1]
            row_h = last.rectangle().bottom - last.rectangle().top
            x = (header_rect.left + header_rect.right) // 2
            y = last.rectangle().bottom + row_h // 2
        else:
            x = (header_rect.left + header_rect.right) // 2
            y = header_rect.bottom + 21
        mouse_click(button="left", coords=(x, y))
        time.sleep(0.4)
        send_keys(tag, with_spaces=True)
        time.sleep(0.1)
        send_keys("{ENTER}")
        time.sleep(0.5)

        updated = self.list_strategy_tags()
        if tag not in updated:
            raise MTQuantAutomationError(f"Strategy tag {tag!r} was typed but didn't appear in the Strategies grid.")
        return updated


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

    def _timings_field(self, auto_id: str):
        """A clock on the Execution Parameters "Timings" group.

        dtStartTime is also the auto_id of each leg row's own Start Time,
        so a dialog-wide lookup finds several. The portfolio Start Time is
        the one inside the Timings group.
        """
        group = self.dlg.child_window(title="Timings", auto_id="groupBox1", control_type="Group")
        return group.child_window(auto_id=auto_id)

    def set_start_time(self, hhmmss: str) -> None:
        _set_clock(self._timings_field("dtStartTime"), hhmmss)

    def set_sqoff_time(self, hhmmss: str) -> None:
        _set_clock(self._timings_field("dtSquareOffTime"), hhmmss)

    def set_symbol(self, symbol: str) -> None:
        _set_edit_text(self._field("txtExchangeSymbol"), symbol)

    def set_premium_greek_leg(self, enabled: bool) -> None:
        """Must be on BEFORE add_leg() for a premium strike. The leg's strike
        editor is chosen at the moment the row is created."""
        chk = self._field("chkPremiumGreekLeg", "CheckBox")
        is_checked = bool(chk.get_toggle_state())
        if is_checked != enabled:
            chk.click_input()
            time.sleep(0.3)

    def select_strategy_tag(self, tag: str, tag_options: list[str]) -> None:
        """Picks the tag on Execution Parameters. `tag_options` is the Strategies
        grid order (see ensure_strategy_tag) — this dropdown doesn't expose its
        items, so the row is clicked by that index."""
        self._goto_tab("tbExecutionParameters")
        if tag not in tag_options:
            raise MTQuantAutomationError(f"Strategy tag {tag!r} isn't in the known tag list {tag_options!r}.")
        _select_dropdown_option(self._field("cboStrategyTag"), tag_options, tag)

    def set_dte_values(self, dtes: list[int]) -> None:
        """Selects the DTE radio, then checks only the requested DTE_n boxes.

        A fresh list opens with Select All on. Checkbox state is not exposed
        to UI Automation, so each box is read from its pixel color. Select All
        is cleared only when every DTE is on; otherwise each DTE_n box is
        toggled to match the request. OK is the button under the list — a
        fixed click from the combo lands on the DTE_10 row and checks it.
        """
        if not dtes:
            raise MTQuantAutomationError("No DTE values to select.")
        if any(d < 0 or d > DTE_VISIBLE_MAX for d in dtes):
            raise MTQuantAutomationError(
                f"DTE values {dtes} include one above DTE_{DTE_VISIBLE_MAX}, which isn't visible without scrolling."
            )
        self._goto_tab("tbExecutionParameters")
        radio = self._field("rdDTE")
        rect = radio.rectangle()
        mouse_click(button="left", coords=(rect.left + 10, (rect.top + rect.bottom) // 2))
        time.sleep(0.4)

        combo = self._field("cboRunOnDays")
        combo_rect = combo.rectangle()
        mouse_click(button="left", coords=(combo_rect.right - 15, (combo_rect.top + combo_rect.bottom) // 2))
        time.sleep(0.5)

        def _popup():
            """The open DTE list. First row is Select All; its UIA name is blank.
            The rest are exactly DTE_0, DTE_1, ... Match those in full.
            """
            for window in Desktop(backend="uia").windows():
                if _safe_pid(window) != self.session.pid:
                    continue
                try:
                    items = list(window.descendants(control_type="ListItem"))
                except Exception:
                    continue
                if any(item.window_text() == "DTE_0" for item in items):
                    items.sort(key=lambda item: item.rectangle().top)
                    return window, items
            return None, []

        def _click_checkbox(item) -> None:
            rect = item.rectangle()
            mouse_click(button="left", coords=(rect.left + 6, (rect.top + rect.bottom) // 2))
            time.sleep(0.25)

        def _box_checked(item) -> bool:
            import win32gui

            box = item.rectangle()
            x = box.left + 8
            y = (box.top + box.bottom) // 2
            hdc = win32gui.GetDC(0)
            try:
                color = win32gui.GetPixel(hdc, x, y)
            finally:
                win32gui.ReleaseDC(0, hdc)
            if color < 0:
                raise MTQuantAutomationError("Could not read a DTE checkbox.")
            red = color & 0xFF
            blue = (color >> 16) & 0xFF
            return blue > 180 and blue > red + 40

        popup, rows = _popup()
        if not rows:
            raise MTQuantAutomationError("DTE dropdown didn't show its list.")
        wanted = {dte_option_name(dte) for dte in dtes}
        dte_rows = [item for item in rows if (item.window_text() or "").startswith("DTE_")]
        if dte_rows and all(_box_checked(item) for item in dte_rows):
            _click_checkbox(rows[0])  # Select All is on; this clears every DTE
            popup, rows = _popup()
            dte_rows = [item for item in rows if (item.window_text() or "").startswith("DTE_")]
        for name in sorted(wanted):
            if not any(item.window_text() == name for item in dte_rows):
                raise MTQuantAutomationError(f"DTE option {name!r} was not in the open list.")
        for item in dte_rows:
            name = item.window_text()
            if _box_checked(item) != (name in wanted):
                _click_checkbox(item)
        popup, rows = _popup()
        still = [
            item.window_text()
            for item in rows
            if (item.window_text() or "").startswith("DTE_") and _box_checked(item) != (item.window_text() in wanted)
        ]
        if still:
            raise MTQuantAutomationError(f"DTE selection still wrong for {still}.")
        if popup is None:
            raise MTQuantAutomationError("DTE list closed before OK could be clicked.")
        # The OK button sits under the list. A fixed offset from the combo
        # lands on the DTE_10 row, which checks DTE_10 on the way out.
        buttons = []
        for button in popup.descendants(control_type="Button"):
            label = button.window_text() or ""
            if label in {"Line up", "Line down", "Page up", "Page down", "Position"}:
                continue
            box = button.rectangle()
            if box.width() < 40 or box.height() < 10:
                continue
            buttons.append(button)
        if not buttons:
            raise MTQuantAutomationError("DTE list has no OK button.")
        buttons.sort(key=lambda button: button.rectangle().left)
        ok = buttons[0].rectangle()
        mouse_click(button="left", coords=((ok.left + ok.right) // 2, (ok.top + ok.bottom) // 2))
        time.sleep(0.4)

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

    def set_sl_wait_seconds(self, seconds: int) -> None:
        """The "SL wait" box that appears beside Combined Loss. Only exists
        after a stoploss type has been chosen, so call set_overall_stoploss first."""
        self._goto_tab("tbStoplossSettings")
        _set_edit_text(self._field("txtCombinedSLWait"), seconds)

    def set_overall_trail(self, instrument_move, stoploss_move) -> None:
        """Portfolio Profit Protection on the Target Settings tab.

        "For Every Increase In Profit By" / "Trail Profit By". An earlier
        mapping put this pair on the Stoploss Settings tab; the workflow for
        this builder uses the Target Settings pair instead. The stoploss
        tab's own trail pair is left alone.
        """
        self._goto_tab("tbTargetSettings")
        _set_edit_text(self._field("txtIncreaseInProfit"), instrument_move)
        _set_edit_text(self._field("txtTrailProfit"), stoploss_move)

    def set_combined_profit(self, value) -> None:
        """Target Type = CombinedProfit, then the value box that appears under it."""
        self._goto_tab("tbTargetSettings")
        _select_dropdown_option(self._field("cboCombinedTargetType"), TARGET_TYPE_OPTIONS, "CombinedProfit")
        time.sleep(0.4)
        if value is None:
            return
        pane = self._field("tbTargetSettings", "Pane")
        known = {"txtProfitReaches", "txtLockProfit", "txtIncreaseInProfit", "txtTrailProfit"}
        candidate = None
        for auto_id in ("txtCombinedTargetValue", "txtCombinedTgtValue", "txtCombinedProfit"):
            matches = [d for d in pane.descendants() if d.automation_id() == auto_id]
            if matches:
                candidate = matches[0]
                break
        if candidate is None:
            edits = [d for d in pane.descendants(control_type="Edit") if d.automation_id() not in known]
            # The four protection fields wrap their edits in panes, so a bare
            # new Edit directly under the type dropdown is the value box.
            bare = [d for d in edits if not d.automation_id()]
            if len(bare) == 1:
                candidate = bare[0]
        if candidate is None:
            raise MTQuantAutomationError(
                "CombinedProfit was selected but the combined-profit value field didn't appear "
                "(looked for txtCombinedTargetValue / txtCombinedTgtValue / txtCombinedProfit)."
            )
        _set_edit_text(candidate, value)

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
    # ATM/OTM strategies use Predefined Strategies. Popular is expanded by
    # default; ShortStraddle and ShortStrangle are the first real entries
    # under it (see select_predefined_strategy). That creates SELL CE then
    # SELL PE, so B/S is not toggled afterwards.
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
        """Opens Predefined Strategies and clicks a child of the expanded
        Popular group. `name` is the row text (ShortStraddle / ShortStrangle).
        Clicking Popular itself would collapse the group, so that row is not
        a valid selection."""
        if name not in PREDEFINED_TREE_OPTIONS or name == "Popular":
            raise MTQuantAutomationError(
                f"Predefined strategy {name!r} isn't in the visible Popular list {PREDEFINED_TREE_OPTIONS[1:]!r}."
            )
        index = PREDEFINED_TREE_OPTIONS.index(name)
        combo = self._field("cboPredifinedStrategy")
        rect = combo.rectangle()
        mouse_click(button="left", coords=(rect.right - 12, (rect.top + rect.bottom) // 2))
        time.sleep(0.5)
        # Indented child label, clear of the expander glyph on the group row.
        x = rect.left + 70
        y = int(rect.bottom + DROPDOWN_FIRST_ITEM_OFFSET + index * PREDEFINED_TREE_ROW_HEIGHT)
        mouse_click(button="left", coords=(x, y))
        time.sleep(0.6)

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

    def _process_control(self, auto_id: str):
        """Finds a control by auto_id anywhere in this mtQuant process.

        The premium popup is its own window, not a child of the portfolio
        dialog, so a dialog-only search misses Value Type and Value.
        """
        for window in Desktop(backend="uia").windows():
            if _safe_pid(window) != self.session.pid:
                continue
            try:
                nodes = window.descendants()
            except Exception:
                continue
            for node in nodes:
                if node.automation_id() == auto_id:
                    return node
        return None

    def _click_list_item(self, option_text: str) -> bool:
        for window in Desktop(backend="uia").windows():
            if _safe_pid(window) != self.session.pid:
                continue
            try:
                items = window.descendants(control_type="ListItem")
            except Exception:
                continue
            match = next((item for item in items if item.window_text() == option_text), None)
            if match is not None:
                match.click_input()
                return True
        return False

    def set_leg_strike_premium(self, row_index: int, target_premium) -> None:
        """Opens the Strike popup and sets Value Type, then Value.

        Value Type must be NearestPremium. The Value box (txtFrom, labeled
        "Value :") is not on the popup until that option is selected.
        Typing before the selection lands in the Value Type dropdown.

        REQUIRES chkPremiumGreekLeg to already be ticked when the leg is
        added. Cond, Max Depth, and Side stay at the popup defaults.
        """
        combo = self._leg_field("cboClosestPremium", row_index, "ComboBox")
        rect = combo.rectangle()
        mouse_click(button="left", coords=(rect.right - 8, (rect.top + rect.bottom) // 2))
        time.sleep(0.6)

        value_type = None
        for _ in range(10):
            value_type = self._process_control("cboValueType")
            if value_type is not None:
                break
            time.sleep(0.2)
        if value_type is None:
            raise MTQuantAutomationError("Premium popup didn't show the Value Type dropdown.")
        type_rect = value_type.rectangle()
        mouse_click(button="left", coords=(type_rect.right - 12, (type_rect.top + type_rect.bottom) // 2))
        time.sleep(0.5)
        if not self._click_list_item("NearestPremium"):
            raise MTQuantAutomationError("NearestPremium was not in the Value Type dropdown.")
        time.sleep(0.4)

        value_box = None
        for _ in range(10):
            value_box = self._process_control("txtFrom")
            if value_box is not None:
                break
            time.sleep(0.2)
        if value_box is None:
            raise MTQuantAutomationError("Value box didn't appear after NearestPremium was selected.")
        _set_edit_text(value_box, target_premium)
        # Enter on this popup reverts the value. A click on the dialog commits it.
        dlg_rect = self.dlg.rectangle()
        mouse_click(button="left", coords=(dlg_rect.left + 50, dlg_rect.top + 50))
        time.sleep(0.3)

    def set_leg_stoploss_premium(self, row_index: int, value) -> None:
        """Sets the leg's Stoploss Type to "Premium" (AlgoTest's
        "Percentage" type - see LEG_SL_TYPE_MAP) and its value. Percentage
        values are typed with a percent sign ("15%"), matching Wait & Trade."""
        combo = self._leg_field("cboSLType", row_index, "ComboBox")
        _select_dropdown_option(combo, LEG_SL_TYPE_OPTIONS, LEG_SL_TYPE_MAP["Percentage"])
        _set_edit_text(self._leg_field("txtSL", row_index, "Edit"), value)

    def set_leg_target(self, row_index: int, value) -> None:
        """Same shape as the stoploss column: type Premium, then the value."""
        combo = self._leg_field("cboTargetType", row_index, "ComboBox")
        _select_dropdown_option(combo, LEG_SL_TYPE_OPTIONS, LEG_SL_TYPE_MAP["Percentage"])
        _set_edit_text(self._leg_field("txtTgt", row_index, "Edit"), value)

    def fill_leg_risk(self, row_index: int, leg) -> None:
        """Wait & Trade, premium stoploss, trail SL, and target for one row.
        Strike / buy-sell are set by the caller — they differ between the
        Add Leg path and a predefined strategy."""
        if leg.wait_trade:
            self.set_leg_wait_trade(row_index, leg.wait_trade)
        if leg.stoploss_text:
            self.set_leg_stoploss_premium(row_index, leg.stoploss_text)
        if leg.trail_sl:
            self.set_leg_trail_sl(row_index, leg.trail_sl["instrument_move"], leg.trail_sl["stoploss_move"])
        if leg.target_value is not None:
            self.set_leg_target(row_index, leg.target_value)
        if leg.reentry_kind in REENTRY_LEG_ACTION:
            # Same On Stoploss column as Execute_LegN. ReEntry is in that list
            # only after the stop-loss value above has been entered.
            self.set_leg_on_sl_action(row_index, REENTRY_LEG_ACTION[leg.reentry_kind])
            self._fill_reentry_count_and_delay()

    def set_leg_on_sl_action(self, row_index: int, action: str) -> None:
        """Selects cboSLAction on the original Add Portfolio leg grid.

        Call this only after that row's stop-loss value is filled. Until a
        stop loss is entered, this dropdown does not list Execute_LegN.
        The open list exposes ListItem names, so the option is clicked by
        that name rather than by a pixel index.
        """
        if action not in CLASSIC_ON_SL_ACTIONS:
            raise MTQuantAutomationError(f"On Stoploss action {action!r} isn't in {CLASSIC_ON_SL_ACTIONS!r}.")
        combo = self._leg_field("cboSLAction", row_index, "ComboBox")
        rect = combo.rectangle()
        mouse_click(button="left", coords=(rect.right - 12, (rect.top + rect.bottom) // 2))
        time.sleep(0.6)
        for _ in range(10):
            for w in Desktop(backend="uia").windows():
                if _safe_pid(w) != self.session.pid:
                    continue
                try:
                    items = w.descendants(control_type="ListItem")
                except Exception:
                    continue
                match = next((item for item in items if item.window_text() == action), None)
                if match is not None:
                    match.click_input()
                    time.sleep(0.3)
                    return
            time.sleep(0.2)
        raise MTQuantAutomationError(f"On Stoploss option {action!r} was not in the open dropdown.")

    def _fill_reentry_count_and_delay(self) -> None:
        """Count 1 and Delay 5, when selecting ReEntry opens that popup.

        The popup is the same frmLegAction used on the older dialog. If it
        does not appear, the On Stoploss selection already made is left as-is.
        """
        form = self.dlg.child_window(auto_id="frmLegAction", control_type="Window")
        if not form.exists(timeout=1):
            return
        _set_edit_text(form.child_window(auto_id="txtLegNo"), REENTRY_COUNT)
        _set_edit_text(form.child_window(auto_id="txtLegDelaySeconds"), REENTRY_DELAY_SECONDS)
        form.child_window(auto_id="btnSave", control_type="Button").click_input()
        time.sleep(0.4)

    def set_leg_reentry(self, row_index: int, action: str) -> None:
        """On Stoploss Action → + → scope Leg → ReEntry, then Count and Delay.

        Choosing ReEntry reveals Count (txtLegNo) and Delay (Sec)
        (txtLegDelaySeconds) in that same popup. btnAdd/btnSave here are
        the popup's own buttons, not Add Leg / SAVE PORTFOLIO.
        """
        if action not in ON_SL_LEG_ACTIONS:
            raise MTQuantAutomationError(f"Leg SL action {action!r} isn't in {ON_SL_LEG_ACTIONS!r}.")
        cell = self._leg_field("lblSLAction", row_index, "Text")
        rect = cell.rectangle()
        mouse_click(button="left", coords=((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))
        form = self.dlg.child_window(auto_id="frmLegAction", control_type="Window")
        if not form.exists(timeout=3):
            raise MTQuantAutomationError("On Stoploss Action popup didn't open.")
        form.child_window(auto_id="btnAdd", control_type="Button").click_input()
        time.sleep(0.4)
        _select_dropdown_option(form.child_window(auto_id="cboAction"), ON_SL_SCOPE_OPTIONS, "Leg")
        time.sleep(0.3)
        _select_dropdown_option(form.child_window(auto_id="cboLeg"), ON_SL_LEG_ACTIONS, action)
        if action == "ReEntry":
            time.sleep(0.3)
            _set_edit_text(form.child_window(auto_id="txtLegNo"), REENTRY_COUNT)
            _set_edit_text(form.child_window(auto_id="txtLegDelaySeconds"), REENTRY_DELAY_SECONDS)
        form.child_window(auto_id="btnSave", control_type="Button").click_input()
        time.sleep(0.4)

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


def build_portfolio(
    session: MTQuantSession,
    plan: MTQuantPortfolioPlan,
    *,
    strategy_tag: str,
    tag_options: list[str],
    save: bool = False,
) -> PortfolioDialog:
    """Fills one Create Options Portfolio dialog from a plan and, when
    `save` is True, clicks SAVE PORTFOLIO.

    Every portfolio opens the original Add Portfolio dialog. Add Portfolio
    V2 is not used.

    Leg lots and expiry are left at the dialog defaults (1 and Weekly).
    The portfolio-level Default Lots field still receives the AlgoTest
    multiplier. `save` defaults False so a caller can review the draft.
    """
    dialog = session.open_add_portfolio()

    if plan.symbol:
        dialog.set_symbol(plan.symbol)
    if plan.default_lots is not None:
        dialog.set_default_lots(plan.default_lots)

    if plan.entry_path == "premium":
        dialog.set_premium_greek_leg(True)
        rows = grid_rows(plan)
        for _leg in rows:
            dialog.add_leg()
        for index, leg in enumerate(rows):
            dialog.set_leg_buy_sell(index, leg.buy_sell)
            dialog.set_leg_ce_pe(index, leg.ce_pe)
            if leg.idle:
                dialog.set_leg_idle(index, True)
            if leg.strike_mode == "PREMIUM":
                dialog.set_leg_strike_premium(index, leg.strike_value)
            dialog.fill_leg_risk(index, leg)
        # Stop-loss values have to be in place before Execute_LegN is offered.
        for index, leg in enumerate(rows):
            if leg.on_sl_action:
                dialog.set_leg_on_sl_action(index, leg.on_sl_action)
    else:
        dialog.select_predefined_strategy(plan.predefined_strategy or "")
        # ShortStraddle / ShortStrangle already create two SELL legs (CE then
        # PE, strikes at ATM for a straddle). Don't toggle B/S — that control
        # can't be read back, and a click would flip SELL to BUY.
        if len(plan.legs) != 2:
            raise MTQuantAutomationError(
                f"{plan.predefined_strategy} creates 2 legs, but this strategy has {len(plan.legs)}."
            )
        if any(leg.strike_label not in (None, "ATM") for leg in plan.legs):
            raise MTQuantAutomationError(
                "Short strangle is selected, but rewriting its strikes to ATM+50 / ATM-50 is not wired yet."
            )
        for index, leg in enumerate(plan.legs):
            dialog.fill_leg_risk(index, leg)

    dialog.select_strategy_tag(strategy_tag, tag_options)
    if plan.dte:
        dialog.set_dte_values(plan.dte)
    if plan.start_time:
        dialog.set_start_time(plan.start_time)
    if plan.sqoff_time:
        dialog.set_sqoff_time(plan.sqoff_time)

    if plan.overall_stoploss:
        dialog.set_overall_stoploss(plan.overall_stoploss["type"], plan.overall_stoploss["value"])
        dialog.set_sl_wait_seconds(plan.sl_wait_seconds)

    if plan.overall_target and plan.overall_target.get("value") not in (None, 0):
        dialog.set_combined_profit(plan.overall_target["value"])

    if plan.lock_and_trail and plan.lock_and_trail.get("type") == "Points":
        v = plan.lock_and_trail["value"]
        dialog.set_lock_and_trail(v["InstrumentMove"], v["StopLossMove"])

    if plan.overall_trail and plan.overall_trail.get("type") == "Points":
        v = plan.overall_trail["value"]
        dialog.set_overall_trail(v["InstrumentMove"], v["StopLossMove"])

    dialog.set_portfolio_name(plan.portfolio_name)
    if save:
        dialog.save()
    return dialog
