from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.sync_api import Page

from src.config import Selectors
from src.locators import resolve


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


def apply_result_settings(page: Page, selectors: Selectors, slippage_pct: float, dte_values: list[int]) -> None:
    """Enable brokerage + taxes & charges, set slippage, hit Re-calculate, then apply
    the DTE filter - all *before* scraping metrics, since each of these changes the
    scraped numbers (confirmed live)."""
    r = selectors.results

    resolve(page, r.include_brokerage_toggle).click(force=True)
    resolve(page, r.include_taxes_toggle).click(force=True)
    resolve(page, r.slippage_input).fill(str(slippage_pct))
    resolve(page, r.recalculate_button).first.click()
    page.wait_for_load_state("networkidle")

    if not dte_values:
        return

    resolve(page, r.dte_filter_add_button).click()
    resolve(page, r.dte_filter_value_trigger).click()
    listbox = page.get_by_role("listbox")
    wanted = set(dte_values)
    for value in range(7):  # confirmed live: options are 0-6
        option = listbox.get_by_role("option", name=str(value), exact=True)
        if option.count() == 0:
            continue
        is_selected = option.first.get_attribute("aria-selected") == "true"
        if is_selected != (value in wanted):
            option.first.click()
    page.keyboard.press("Escape")


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
