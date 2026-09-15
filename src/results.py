from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, expect

from src.config import Selectors
from src.locators import ensure_toggle_on, resolve, scroll_to_center


@dataclass
class ResultOutcome:
    status: str  # "ok" | "error" | "timeout"
    error: str | None = None


def wait_for_result(page: Page, selectors: Selectors, timeout_s: int = 180) -> ResultOutcome:
    """Poll for ready_marker vs error_marker, racing the two, instead of a fixed sleep."""
    r = selectors.results
    ready = resolve(page, r.ready_marker)
    error = resolve(page, r.error_marker) if r.error_marker else None

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if error is not None and error.count() > 0 and error.first.is_visible():
            return ResultOutcome(status="error", error=error.first.inner_text().strip())
        if ready.count() > 0 and ready.first.is_visible():
            return ResultOutcome(status="ok")
        page.wait_for_timeout(500)
    return ResultOutcome(status="timeout", error=f"No result within {timeout_s}s")


def _read_text(page: Page, selector: str) -> str | None:
    loc = resolve(page, selector)
    return loc.first.inner_text().strip() if loc.count() > 0 else None


def _wait_for_recalc_settle(
    page: Page, selector: str, before: str | None, timeout_s: float = 8.0, poll_ms: int = 250
) -> None:
    """Re-calculate and the DTE filter both recompute client-side with no network
    request at all (confirmed live: clicking Re-calculate fires zero XHR/fetch calls,
    just a ~1s in-page recomputation) - so `wait_for_load_state("networkidle")` returns
    almost immediately and scrape_metrics() would read the pre-recalculation numbers.
    Poll the same metric text instead: wait for it to change from `before`, then hold
    for two consecutive identical reads before treating it as settled."""
    deadline = time.monotonic() + timeout_s
    changed = False
    prev = before
    stable_count = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(poll_ms)
        current = _read_text(page, selector)
        if not changed and current != before:
            changed = True
        if current == prev:
            stable_count += 1
            if changed and stable_count >= 2:
                return
        else:
            stable_count = 0
        prev = current


def ensure_brokerage_rate(page: Page, selectors: Selectors, rate: float) -> None:
    """Set the ₹-per-order brokerage rate once. Confirmed live: this value (and the
    Include Brokerage toggle's on/off state) persists across page.goto() within the
    same browser session - a whole sweep run is one long-lived session - so the
    runner calls this exactly once (on the first combo), never per-combo."""
    r = selectors.results
    # The rate-edit button is disabled (pointer-events-none) while the Include
    # Brokerage toggle itself is off - confirmed live via a Locator.click timeout with
    # the resolved button still showing class="... pointer-events-none opacity-50".
    # The toggle's own state takes ~1s to settle after clicking (confirmed live
    # elsewhere in this app too), so wait before touching anything that depends on it.
    ensure_toggle_on(resolve(page, r.include_brokerage_toggle), force=True)
    page.wait_for_timeout(1200)
    edit_button = resolve(page, r.brokerage_rate_edit_button)
    scroll_to_center(edit_button)
    edit_button.click()
    page.wait_for_timeout(300)
    resolve(page, r.brokerage_rate_type_select).select_option(label="per order")
    resolve(page, r.brokerage_rate_input).fill(str(rate))
    page.wait_for_timeout(200)
    done_button = resolve(page, r.brokerage_rate_done_button).last
    scroll_to_center(done_button)
    done_button.click()
    page.wait_for_timeout(300)


def apply_result_settings(page: Page, selectors: Selectors, slippage_pct: float, dte_values: list[int]) -> None:
    """Enable brokerage + taxes & charges, set slippage, hit Re-calculate, then apply
    the DTE filter - all *before* scraping metrics, since each of these changes the
    scraped numbers (confirmed live)."""
    r = selectors.results
    pnl_selector = r.metrics["total_pnl"]

    ensure_toggle_on(resolve(page, r.include_brokerage_toggle), force=True)
    ensure_toggle_on(resolve(page, r.include_taxes_toggle), force=True)
    resolve(page, r.slippage_input).fill(str(slippage_pct))

    before = _read_text(page, pnl_selector)
    recalc_button = resolve(page, r.recalculate_button).first
    scroll_to_center(recalc_button)
    recalc_button.click()
    _wait_for_recalc_settle(page, pnl_selector, before)

    if not dte_values:
        return

    before = _read_text(page, pnl_selector)
    # The Add button only creates the filter chip the FIRST time - re-clicking it
    # on a page that already has one (src/runner.py's
    # _capture_individual_dte_reports calls this function once per DTE, on the
    # SAME already-backtested page) fires the click with no wait for whatever it
    # actually does afterward, which raced with dte_value_trigger's own click
    # right below it (confirmed live: skipping this element-existence check and
    # not waiting for the value-trigger to become clickable/re-render is what
    # actually broke the toggle loop below, not the toggle logic itself - see
    # dte_value_trigger's own wait for why). Checking whether the trigger already
    # exists before clicking Add keeps this idempotent regardless.
    if page.locator(r.dte_filter_value_trigger).count() == 0:
        dte_add_button = resolve(page, r.dte_filter_add_button)
        scroll_to_center(dte_add_button)
        dte_add_button.click()
    dte_value_trigger = resolve(page, r.dte_filter_value_trigger)
    scroll_to_center(dte_value_trigger)
    dte_value_trigger.click()
    # Confirmed live: without this, the very next line's listbox lookup can catch
    # the dropdown mid-open (or, on a page where this function has already run
    # once, mid-close-then-reopen) and its options' aria-selected reads come back
    # against a stale/incomplete render - the toggle loop below then silently
    # thinks the wrong DTE is already selected and skips clicking it entirely.
    listbox = page.get_by_role("listbox")
    listbox.get_by_role("option").first.wait_for(state="visible", timeout=5000)
    wanted = set(dte_values)
    for value in range(7):  # confirmed live: options are 0-6
        option = listbox.get_by_role("option", name=str(value), exact=True)
        if option.count() == 0:
            continue
        want_selected = value in wanted
        is_selected = option.first.get_attribute("aria-selected") == "true"
        if is_selected != want_selected:
            option.first.click()
            # Playwright's click() only waits for the click to be DISPATCHED, not
            # for AlgoTest's own multi-select to finish re-rendering aria-selected
            # in response - firing the next option's click (or closing the
            # dropdown) before this one has visibly taken effect leaves it either
            # unchanged or mid-transition. Confirmed live: calling this function
            # repeatedly on the same page (src/runner.py's
            # _capture_individual_dte_reports, one call per DTE) without this wait
            # left TWO DTE values checked simultaneously ("2 Selected") instead of
            # just the one requested - every "isolated" per-DTE report after the
            # first was silently a stale mix of whichever DTEs never got
            # unchecked, not the single DTE its own filename/combo_id claimed.
            expect(option.first).to_have_attribute(
                "aria-selected", "true" if want_selected else "false", timeout=5000
            )
    # Escape alone is not reliable here: confirmed live, on a page where this
    # function has already applied a DTE filter once before, Escape can leave the
    # dropdown visibly open - the selection itself (e.g. "1 DTE" in the trigger's
    # own label) is already correct at this point, but it doesn't close.
    # listbox.is_visible() is NOT a reliable signal for this - confirmed live, it
    # reads False even while the dropdown is functionally still open (the
    # trigger's own label still contains "Select All" and the per-value options,
    # not just "N DTE"). Retry clicking the trigger (a toggle) against THAT
    # signal instead, since a single Escape/click is sometimes simply not enough
    # for this dropdown - not more than a handful of retries, since if it's
    # still open at that point something is genuinely wrong rather than just
    # slow.
    page.keyboard.press("Escape")
    for _ in range(5):
        if "Select All" not in dte_value_trigger.inner_text():
            break
        dte_value_trigger.click()
        page.wait_for_timeout(300)
    else:
        raise RuntimeError("DTE filter dropdown would not close after repeated attempts.")

    # Closing the dropdown updates the trigger's own label (e.g. "1 DTE")
    # correctly, but does NOT by itself make the results table recompute -
    # confirmed live: total_pnl/total_trades kept showing the PREVIOUS DTE's
    # numbers no matter how long anything waited afterward, on a page where a
    # DTE filter had already been applied once before. An explicit second
    # Re-calculate click (same as the brokerage/taxes/slippage one above) is
    # required to make the table actually reflect the new filter - the very
    # first DTE application on a fresh page happens to recompute without this
    # (there's nothing for it to have been stale against yet), which is why the
    # bug only ever showed up on the 2nd+ DTE applied to the same page (e.g.
    # src/runner.py's _capture_individual_dte_reports, one call per DTE).
    recalc_button = resolve(page, r.recalculate_button).first
    scroll_to_center(recalc_button)
    recalc_button.click()
    _wait_for_recalc_settle(page, pnl_selector, before)


def download_current_report(page: Page, selectors: Selectors, target: Path, cid: str) -> None:
    """Downloads the per-trade report for whatever result is CURRENTLY on screen -
    the caller must have already run the backtest, waited for the result, and
    applied brokerage/slippage/DTE settings (same prerequisites as scrape_metrics).
    Shared by src/correlate.py (Correlate's own download step) and src/runner.py
    (downloading inline during a normal sweep for a combo whose scraped metrics
    already look good, avoiding a full second replay just to get its report later)."""
    r = selectors.results
    heading = resolve(page, r.full_report_heading)
    scroll_to_center(heading)
    download_btn = resolve(page, r.download_report_button)
    scroll_to_center(download_btn)
    download_btn.click()
    page.wait_for_timeout(500)

    resolve(page, r.download_filename_input).fill(cid)
    confirm = resolve(page, r.download_confirm_link)
    target.parent.mkdir(parents=True, exist_ok=True)
    # The confirm control is a client-side <a download="..."> blob link, not a
    # <button> (confirmed live) - Playwright's expect_download() still wraps a click
    # on it the same way it would a real download-triggering anchor.
    with page.expect_download(timeout=15000) as dl_info:
        confirm.click()
    dl_info.value.save_as(str(target))


def scrape_metrics(page: Page, selectors: Selectors) -> dict[str, str | None]:
    raw: dict[str, str | None] = {}
    for name, selector in selectors.results.metrics.items():
        loc = resolve(page, selector)
        raw[name] = loc.first.inner_text().strip() if loc.count() > 0 else None
    return raw


def parse_number(raw: str | None) -> float | None:
    """Parse Indian-formatted numbers robustly, returning None rather than raising.

    Handles: "₹1,23,456", "-45.6%", "(2,340)" (parens = negative), "1.2L", "2.3Cr",
    "—", "" and other empty/placeholder text.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s in {"", "—", "-", "–", "N/A", "NA"}:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    s = s.replace("₹", "").replace(",", "").strip()

    if s.startswith("-"):
        negative = True
        s = s[1:]

    if s.endswith("%"):
        s = s[:-1]

    multiplier = 1.0
    lowered = s.lower()
    if lowered.endswith("cr"):
        multiplier = 1e7
        s = s[:-2]
    elif lowered.endswith("l"):
        multiplier = 1e5
        s = s[:-1]

    s = s.strip()
    if not s:
        return None

    try:
        value = float(s) * multiplier
    except ValueError:
        return None

    return -value if negative else value
