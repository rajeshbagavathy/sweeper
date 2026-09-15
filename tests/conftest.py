from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_read_rows_cache():
    """src.web.app._read_rows is now backed by an lru_cache keyed on (path,
    mtime_ns) - safe for the real app (a sweep/Force-refresh writing a file
    always advances its mtime), but a test that writes, reads, then rewrites
    the SAME tmp_path within one run could in principle hit a coarse
    filesystem mtime clock and get back stale cached rows. Clearing the cache
    before every test removes that risk entirely, at effectively zero cost -
    this cache exists purely as a same-request-burst optimization, never
    meant to survive across tests anyway."""
    from src.web.app import _read_rows_cached

    _read_rows_cached.cache_clear()
    yield
    _read_rows_cached.cache_clear()
