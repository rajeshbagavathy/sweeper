from __future__ import annotations

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.config import Selectors
from src.locators import resolve


class LoginNotConfigured(RuntimeError):
    """Raised when the session isn't logged in and selectors.yaml has no login selectors to act on."""


def is_logged_in(page: Page, selectors: Selectors, timeout_ms: int = 15000) -> bool:
    marker = selectors.login.logged_in_marker
    if marker is None:
        raise LoginNotConfigured(
            "selectors.yaml has no login.logged_in_marker configured, so session state "
            "can't be checked. Fill in the login section (see the NEEDS-CHECK notes in "
            "config/selectors.yaml) or log in manually before running a sweep."
        )
    # An immediate is_visible() check races the SPA's initial render (confirmed live:
    # the marker isn't painted yet right after page.goto(), even though the session is
    # genuinely logged in) - wait for it instead of sampling a single instant.
    try:
        resolve(page, marker).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def ensure_logged_in(page: Page, selectors: Selectors, email: str | None, password: str | None) -> None:
    if is_logged_in(page, selectors):
        return

    login = selectors.login
    if not (login.login_url and login.email_input and login.password_input and login.submit_button):
        raise LoginNotConfigured(
            "Not logged in, and login.login_url/email_input/password_input/submit_button "
            "aren't configured in config/selectors.yaml yet. Log in manually in a headed "
            "run, or fill in those selectors."
        )
    if not (email and password):
        raise LoginNotConfigured(
            "Not logged in, and no ALGOTEST_EMAIL/ALGOTEST_PASSWORD found in .env to "
            "re-authenticate with."
        )

    # Confirmed live: a logged-out session doesn't land on a login form by itself -
    # the builder URL just redirects to the marketing homepage (with a
    # ?redirect_to=... query param it never acts on automatically). Navigate to the
    # actual login page directly rather than assuming the form is already present.
    page.goto(login.login_url)
    resolve(page, login.email_input).fill(email)
    resolve(page, login.password_input).fill(password)
    resolve(page, login.submit_button).click()
    page.wait_for_load_state("networkidle")

    # Login doesn't necessarily land back on the builder page (confirmed live: it can
    # redirect elsewhere), and logged_in_marker is only confirmed to render on the
    # builder page - go there explicitly before the final check.
    page.goto(selectors.builder.url)
    if not is_logged_in(page, selectors):
        raise RuntimeError("Re-login attempt did not result in logged_in_marker becoming visible.")
