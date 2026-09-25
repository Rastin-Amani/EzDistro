"""PocketBase client factories.

`auto_snake_case=False` keeps field names camelCase end-to-end (matching the
schema in docs/SCHEMA.md) — the SDK's default snake-casing would silently
rename every field on read.
"""

from __future__ import annotations

import threading
import time

from pocketbase import PocketBase

from app.config import settings

# Superuser client cache for request data access (18-A(b)). API rules are
# superuser-only, so handlers read/write through an admin client while
# authorization stays in app/api/deps.py. Re-auth happens at most every
# _DATA_TTL_SECONDS (PocketBase superuser tokens live far longer); the lock
# prevents a re-auth stampede across concurrent requests.
_DATA_TTL_SECONDS = 300.0
_data_lock = threading.Lock()
_data_pb: PocketBase | None = None
_data_issued_at = 0.0


def get_pb() -> PocketBase:
    """Unauthenticated client — used for session validation and login."""
    return PocketBase(settings.pb_url, auto_snake_case=False)


def get_admin_pb() -> PocketBase:
    """Admin-authenticated client for workers, bootstrap and maintenance scripts."""
    if not settings.pb_admin_email or not settings.pb_admin_password:
        raise RuntimeError(
            "PB_ADMIN_EMAIL/PB_ADMIN_PASSWORD are required for admin operations "
            "(worker, bootstrap)."
        )
    pb = PocketBase(settings.pb_url, auto_snake_case=False)
    # PocketBase >= 0.23 uses _superusers; older versions use _admins.
    try:
        pb.collection("_superusers").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )
    except Exception:
        pb.collection("_admins").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )
    return pb


def get_data_pb() -> PocketBase:
    """Process-cached superuser client handed to request handlers.

    AuthMiddleware exposes this via LazyDataPb as ``request.state.pb``.
    Sharing one instance is safe because repos only perform stateless CRUD;
    if re-auth fails (PocketBase down / bad credentials) the exception
    propagates to the route, where the existing error handling surfaces it.
    """
    global _data_pb, _data_issued_at
    with _data_lock:
        if _data_pb is None or time.monotonic() - _data_issued_at >= _DATA_TTL_SECONDS:
            _data_pb = get_admin_pb()
            _data_issued_at = time.monotonic()
        return _data_pb


class LazyDataPb:
    """Proxy that resolves ``get_data_pb()`` on first attribute access.

    ``request.state.pb`` points at this wrapper so requests that never touch
    data (static files, the login page, health checks) do not force superuser
    authentication — and an outage only surfaces on routes that actually
    read/write.
    """

    __slots__ = ("_factory",)

    def __init__(self, factory) -> None:
        self._factory = factory

    def __getattr__(self, name: str):
        return getattr(self._factory(), name)
