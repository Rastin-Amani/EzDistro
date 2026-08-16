"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_caches():
    """Isolate process-global caches between tests."""
    from app.services.stats import clear_stats_cache

    clear_stats_cache()
    yield
    clear_stats_cache()
