"""On-demand "open a browser for me to log into" helper for the UI's login banner.

Never touches credential fields itself (that's prohibited and pointless anyway -
config/selectors.yaml has no login form selectors configured) - it just opens a
headed browser on the primary profile and polls for the logged_in_marker, so the
user can log in manually and the UI can tell when it's done.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from src import browser
from src.config import load_selectors

SELECTORS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "selectors.yaml"

# Which primary profile each account number opens - lets one login helper serve all
# 3 accounts instead of being hardcoded to account 1's profile (confirmed live this
# was a real gap: accounts 2/3's sessions go stale exactly like account 1's, but
# there was previously no way to open a login window against their profiles at all).
_ACCOUNT_PROFILE_DIRS = {1: browser.PROFILE_DIR, 2: browser.PROFILE_DIR_2, 3: browser.PROFILE_DIR_3}


@dataclass
class LoginHelperState:
    status: str = "idle"  # idle | opening | logging_out | waiting | logged_in | timeout | error
    message: str | None = None
    account: int = 1

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "message": self.message, "account": self.account}

    def start(self, *, relogin: bool = False, account: int = 1) -> None:
        if account not in _ACCOUNT_PROFILE_DIRS:
            raise ValueError(f"Unknown account {account!r} - must be 1, 2, or 3.")
        with self._lock:
            if self.status in ("opening", "logging_out", "waiting"):
                raise RuntimeError("A login window is already open - finish or close that one first.")
            self.status = "opening"
            self.message = None
            self.account = account
        thread = threading.Thread(target=self._run, args=(relogin, account), daemon=True)
        self._thread = thread
        thread.start()

    def _run(self, relogin: bool = False, account: int = 1) -> None:
        try:
            selectors = load_selectors(SELECTORS_PATH)
            profile_dir = _ACCOUNT_PROFILE_DIRS[account]
            # Confirmed safe even during an active parallel sweep: every worker uses
            # its own copied profile (see runner._ensure_worker_profile), so the
            # primary profile is never locked by a running multiprocess sweep. A
            # running *sequential* sweep does hold it, but by the time this banner
            # would appear that sweep has already crashed and released the lock.
            with browser.persistent_context(headless=False, profile_dir=profile_dir) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(selectors.builder.url)

                if relogin:
                    with self._lock:
                        self.status = "logging_out"
                    self._logout_if_logged_in(page, selectors)

                with self._lock:
                    self.status = "waiting"
                marker = page.locator(selectors.login.logged_in_marker)
                for i in range(400):  # ~20 minutes at 3s each
                    with self._lock:
                        if self.status != "waiting":
                            return  # cancelled
                    try:
                        if marker.count() > 0 and marker.first.is_visible():
                            with self._lock:
                                self.status = "logged_in"
                            page.wait_for_timeout(2000)
                            _refresh_worker_profiles(profile_dir)
                            return
                    except Exception:
                        pass
                    # Confirmed live: AlgoTest's login flow doesn't reliably land back
                    # on the builder page, so a login completed elsewhere (e.g. the
                    # homepage) would otherwise never be detected here. Nudge back to
                    # the builder page periodically rather than requiring the user to
                    # navigate there themselves.
                    if i > 0 and i % 10 == 0:
                        try:
                            page.goto(selectors.builder.url)
                        except Exception:
                            pass
                    page.wait_for_timeout(3000)
                with self._lock:
                    self.status = "timeout"
                    self.message = "Timed out waiting for login after ~20 minutes. Click the button again to retry."
        except Exception as exc:  # noqa: BLE001 - surface any crash to the UI
            with self._lock:
                self.status = "error"
                self.message = str(exc)

    def _logout_if_logged_in(self, page, selectors) -> None:
        """Best-effort: if logout fails for any reason, fall through to the normal
        wait-for-login loop anyway - the user can still log in (or already is), so a
        broken logout selector shouldn't block the whole relogin flow."""
        from src.auth import is_logged_in

        try:
            if not is_logged_in(page, selectors, timeout_ms=8000):
                return
            page.locator(selectors.login.account_menu_trigger).click()
            page.locator(selectors.login.logout_button).click()
            page.wait_for_timeout(2000)
        except Exception:
            pass

    def dismiss(self) -> None:
        """Lets the UI clear a timeout/error/logged_in state back to idle without
        restarting the server - doesn't touch an in-progress "waiting" window."""
        with self._lock:
            if self.status in ("timeout", "error", "logged_in"):
                self.status = "idle"
                self.message = None


def _refresh_worker_profiles(primary_profile_dir: Path) -> None:
    """Parallel workers use copies of the primary profile (see runner.py) - those
    copies are stale after a re-login, so clear them and let the next run recreate
    them fresh from the now-logged-in primary profile. Scoped to just the account
    that was re-logged-in (worker profiles are namespaced per account under
    WORKER_PROFILES_DIR) - clearing every account's copies just because one was
    refreshed would force healthy accounts to needlessly re-copy too."""
    import shutil

    from src.runner import WORKER_PROFILES_DIR

    account_ns = primary_profile_dir.name.lstrip(".")
    target = WORKER_PROFILES_DIR / account_ns
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


login_helper_state = LoginHelperState()
