from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser-profile"
# A 2nd/3rd AlgoTest account's own logged-in profile - completely separate cookies/
# session from PROFILE_DIR, used to split sweep parallelism across multiple accounts
# (each account gets its own concurrency budget on AlgoTest's side - confirmed live
# one account's heavy use doesn't slow the others down).
PROFILE_DIR_2 = Path(__file__).resolve().parent.parent / ".browser-profile-2"
PROFILE_DIR_3 = Path(__file__).resolve().parent.parent / ".browser-profile-3"


@contextmanager
def persistent_context(headless: bool = False, profile_dir: Path | None = None) -> Iterator[BrowserContext]:
    """profile_dir defaults to the single shared .browser-profile - pass a distinct
    path per worker for parallel multiprocess runs, since Chromium locks a user-data
    dir to one running instance at a time (see runner.run_sweep_multiprocess)."""
    target_dir = profile_dir if profile_dir is not None else PROFILE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(target_dir),
            headless=headless,
            # Confirmed live: without this, a headed window renders at whatever size
            # the OS opens it at (not Playwright's headless-default 1280x720), which
            # is enough of a layout difference to put a sticky header over the "Add
            # Leg" button - a click that always succeeds in a normal headless sweep
            # run timed out here. Pinning the viewport keeps headed and headless
            # renders identical.
            viewport={"width": 1280, "height": 720},
        )
        try:
            yield context
        finally:
            context.close()
