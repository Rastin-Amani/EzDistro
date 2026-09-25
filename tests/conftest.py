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


@pytest.fixture(autouse=True)
def _fake_middleware_pb(monkeypatch):
    """Hermetic by default: middleware must never reach a real PocketBase.

    Session validation (``get_pb``) and data access (``get_data_pb``) both get
    a fresh in-memory fake, so TestClient-based tests (test_app, test_i18n,
    test_routes_misc, …) stay offline even though routes now read through the
    superuser data client (18-A(b)). Test files needing auth-capable fakes
    re-patch the same names on top of this — their patch wins until teardown.
    """
    from tests.fakes import FakePocketBase, default_unique_fields

    fake = FakePocketBase(default_unique_fields())
    monkeypatch.setattr("app.middleware.get_pb", lambda: fake)
    monkeypatch.setattr("app.middleware.get_data_pb", lambda: fake)
