"""Selector-discovery helper.

Launches a headed, persistent Chromium session, makes a best-effort attempt to log
into AlgoTest with your .env credentials, then hands control to the Playwright
Inspector so you can navigate to the strategy builder yourself, run one backtest,
and use the element picker to capture selectors into config/selectors.yaml.

Usage:
    uv run python tools/record.py
"""

from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import os

ALGOTEST_URL = "https://algotest.in"
PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser-profile"


def try_login(page, email: str | None, password: str) -> None:
    """Best-effort login using only generic, semantic locators.

    This intentionally does not know AlgoTest's actual form structure — that is
    exactly what this whole tool exists to discover. If the heuristics below don't
    match (different layout, already logged in, OTP step, etc.) we just give up
    quietly and let you log in by hand once the Inspector pauses.
    """
    if not email or not password:
        print("No credentials in .env — skipping auto-login, log in manually.")
        return

    try:
        password_field = page.locator("input[type='password']").first
        password_field.wait_for(state="visible", timeout=8000)
    except Exception:
        print("No password field found within 8s — skipping auto-login.")
        return

    try:
        email_field = page.locator(
            "input[type='email'], input[autocomplete='username'], input[name='email']"
        ).first
        email_field.fill(email)
        password_field.fill(password)

        submit = page.get_by_role("button", name="Log in").or_(
            page.get_by_role("button", name="Login")
        ).or_(
            page.get_by_role("button", name="Sign in")
        )
        submit.first.click(timeout=5000)
        page.wait_for_load_state("networkidle", timeout=15000)
        print("Auto-login attempted — check the browser to confirm it worked.")
    except Exception as exc:
        print(f"Auto-login attempt failed ({exc}) — log in manually.")


def main() -> None:
    load_dotenv()
    email = os.environ.get("ALGOTEST_EMAIL")
    password = os.environ.get("ALGOTEST_PASSWORD")

    PROFILE_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(ALGOTEST_URL)

        try_login(page, email, password)

        print(
            "\nOver to you:\n"
            "  1. Navigate to the backtest / strategy-builder page.\n"
            "  2. Build one representative strategy and run one backtest.\n"
            "  3. In the Inspector window, click 'Pick locator' and click each\n"
            "     field to copy its selector into config/selectors.yaml.\n"
            "  4. Close the Inspector when done — this script will exit.\n"
        )
        page.pause()

        context.close()


if __name__ == "__main__":
    main()
