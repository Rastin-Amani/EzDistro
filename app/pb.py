"""PocketBase client factories.

`auto_snake_case=False` keeps field names camelCase end-to-end (matching the
schema in docs/SCHEMA.md) — the SDK's default snake-casing would silently
rename every field on read.
"""

from __future__ import annotations

from pocketbase import PocketBase

from app.config import settings


def get_pb() -> PocketBase:
    """Unauthenticated client — used per request, then the middleware authenticates it."""
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
