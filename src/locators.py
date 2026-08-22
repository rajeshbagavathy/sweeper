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
