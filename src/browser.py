from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser-profile"


@contextmanager
def persistent_context(headless: bool = False) -> Iterator[BrowserContext]:
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=headless,
        )
        try:
            yield context
        finally:
            context.close()
