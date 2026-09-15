from __future__ import annotations

from typing import Union

from playwright.sync_api import Locator, Page

from src.config import SelectorValue

PageOrLocator = Union[Page, Locator]


def resolve(scope: PageOrLocator, value: SelectorValue) -> Locator:
    """Turn a selectors.yaml value into a Playwright Locator, scoped to `scope`.

    `scope` is a Page for page-level selectors, or a Locator (e.g. one leg row) for
    selectors meant to be resolved relative to it.
    """
    if value is None:
        raise ValueError("Cannot resolve a locator from an unset (None) selector value")
    if isinstance(value, dict):
        return scope.get_by_role(value["role"], name=value["name"])
    return scope.locator(value)


def scroll_to_center(locator: Locator) -> None:
    """Scroll an element to the vertical center of the viewport before clicking it.

    Confirmed live (repeatedly, across several different buttons/toggles): this
    app's sticky top nav and sticky section headers can end up covering an
    element's *default* scroll-into-view position, which Playwright places at
    the edge of the viewport ("just barely visible") - Playwright then correctly
    refuses to click through the overlap and times out. Whether this happens
    depends on exact page height/scroll position at the time, which varies by
    instrument, leg count, and which sections are open - so it's intermittent
    rather than tied to one specific element. Centering first keeps it clear.
    """
    locator.first.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")


def ensure_toggle_on(toggle: Locator, *, force: bool = False) -> None:
    """Click a role="switch" toggle only if it isn't already checked.

    Confirmed live: several of these toggles (results-panel brokerage/taxes,
    overall stop loss/target/trailing) persist their on/off state across
    page.goto() within the same browser session/tab - which is exactly how one
    sweep run moves between combos (one long-lived page, not a fresh browser
    per combo). An unconditional click() therefore *flips* an already-on toggle
    back off on the next combo instead of leaving it on - use this instead of
    blind-clicking anywhere the desired state is "on".
    """
    if toggle.first.get_attribute("aria-checked") == "true":
        return
    scroll_to_center(toggle)
    toggle.click(force=force)
