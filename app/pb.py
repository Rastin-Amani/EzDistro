"""PocketBase client factories.

`auto_snake_case=False` keeps field names camelCase end-to-end (matching the
schema in docs/SCHEMA.md) — the SDK's default snake-casing would silently
rename every field on read.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
from pocketbase import PocketBase

from app.config import settings

# Fail fast when PocketBase is unreachable: the SDK forwards **kwargs to
# httpx.Client, so an explicit timeout bounds every auth/health/CRUD call.
# Superuser client cache for request data access (18-A(b)). API rules are
# superuser-only, so handlers read/write through an admin client while
# authorization stays in app/api/deps.py. Re-auth happens at most every
# _DATA_TTL_SECONDS; the lock prevents a re-auth stampede.
_PB_TIMEOUT_SECONDS = 8.0
_DATA_TTL_SECONDS = 300.0

# One process-wide connection pool shared by every PocketBase client.
#
# The SDK builds its own httpx.Client per PocketBase instance, so without a
# shared transport every client — and every re-auth / every session-cache miss —
# opened and abandoned its own pool. Two consequences: an idle-then-navigate
# request (30s session TTL, or the SDK's 5s keep-alive expiry) paid a fresh
# TCP+TLS handshake to PocketBase for each of the ~20 dashboard queries, and the
# dead sockets piled up in the process (leaked clients were never closed). Tests
# measured ~0.5-1.6s per navigation that should be ~150ms.
#
# A shared transport keeps ONE warm, bounded pool for the whole process, so the
# pool outlives any single client. `retries` replays a request whose pooled
# socket the server already closed instead of stalling on it, and a long
# keepalive_expiry spans the session-cache gap.
_TRANSPORT = httpx.HTTPTransport(
    retries=2,
    limits=httpx.Limits(
        max_connections=32,
        max_keepalive_connections=32,
        keepalive_expiry=120.0,
    ),
)

_data_lock = threading.Lock()
_data_pb: PocketBase | None = None
_data_issued_at = 0.0


def _client() -> PocketBase:
    """A PocketBase client bound to the shared pool (see _TRANSPORT)."""
    return PocketBase(
        settings.pb_url,
        auto_snake_case=False,
        timeout=_PB_TIMEOUT_SECONDS,
        transport=_TRANSPORT,
    )


def get_pb() -> PocketBase:
    """Unauthenticated client — used for session validation and login."""
    return _client()


def _authenticate_admin(pb: PocketBase) -> None:
    """Log an existing client in as superuser, in place."""
    if not settings.pb_admin_email or not settings.pb_admin_password:
        raise RuntimeError(
            "PB_ADMIN_EMAIL/PB_ADMIN_PASSWORD are required for admin operations "
            "(worker, bootstrap)."
        )
    # PocketBase >= 0.23 uses _superusers; older versions use _admins.
    try:
        pb.collection("_superusers").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )
    except Exception:
        pb.collection("_admins").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )


def get_admin_pb() -> PocketBase:
    """Admin-authenticated client for workers, bootstrap and maintenance scripts."""
    pb = _client()
    _authenticate_admin(pb)
    return pb


def reauth_admin(pb: PocketBase) -> None:
    """Refresh a long-lived admin client's token in place.

    The SDK reads `auth_store.token` on every request, so re-authenticating the
    same client fixes the whole pipeline. The worker ticks this (see
    `app/workers/worker.py`); the web is covered by `get_data_pb`'s 300 s cache.
    """
    _authenticate_admin(pb)


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


# ---------------------------------------------------------------------------
# Batch writes
# ---------------------------------------------------------------------------
# PocketBase's /api/batch is a MUTATION-only transaction (GET sub-requests are
# rejected with 400), so it cannot collapse read round trips — but it turns N
# bulk writes into ceil(N/50) requests, which is exactly what the worker's
# per-row create/delete loops need.
BATCH_MAX_REQUESTS = 50
# Keep a batch comfortably under the server's max body size even when rows are
# large (a row that alone exceeds this simply gets its own batch).
BATCH_MAX_BYTES = 1_000_000


def batch_send(pb: PocketBase, requests: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Send ONE PocketBase batch (<=50 sub-requests).

    Returns the per-sub-request bodies on success, or None when batching is
    unavailable (disabled server-side) or the transaction failed — the caller
    then falls back to one request per row, so behaviour never depends on the
    batch endpoint being enabled.
    """
    import json

    if not requests:
        return []
    if len(requests) > BATCH_MAX_REQUESTS:
        raise ValueError(f"batch exceeds {BATCH_MAX_REQUESTS} requests")
    url = str(pb.base_url).rstrip("/") + "/api/batch"
    try:
        response = pb.http_client.post(
            url,
            data={"@jsonPayload": json.dumps({"requests": requests})},
            headers={"Authorization": pb.auth_store.token or ""},
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        items = response.json()
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    return [item.get("body") or {} for item in items]


def batch_chunks(
    items: list[Any], *, max_requests: int = BATCH_MAX_REQUESTS, max_bytes: int = BATCH_MAX_BYTES
) -> list[list[Any]]:
    """Split items into batches bounded by count AND serialized size."""
    import json

    chunks: list[list[Any]] = []
    current: list[Any] = []
    size = 0
    for item in items:
        try:
            weight = len(json.dumps(item))
        except (TypeError, ValueError):
            weight = 0
        if current and (len(current) >= max_requests or size + weight > max_bytes):
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += weight
    if current:
        chunks.append(current)
    return chunks
