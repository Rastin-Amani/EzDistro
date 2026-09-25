"""Shared async HTTP client + retry helpers for provider adapters.

One AsyncClient per provider instance: connection reuse, keep-alive, sane
timeouts. Retries are classification-aware (only transient failures are
retried).

Connection pooling: provider adapters are built per job; `acquire_async_client`
reuses clients across jobs (keyed by base_url + key-hash + timeout) so jobs
don't re-open TCP/TLS connections. Idle pooled clients are swept after IDLE_TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import logging
import threading
import time as _time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from app.config import settings
from app.domain.validation import UnsafeUrlError, validate_url
from app.providers.base import PermanentError, TransientError

logger = logging.getLogger(__name__)

T = TypeVar("T")


def safe_base_url(url: str, *, default: str = "") -> str:
    """Validate a provider base URL (scheme + SSRF). Returns it or the default."""
    candidate = url or default
    if not candidate:
        raise PermanentError("provider base URL is not configured")
    try:
        return validate_url(candidate)
    except UnsafeUrlError as exc:
        raise PermanentError(f"unsafe provider URL: {exc}") from exc


def build_async_client(
    base_url: str,
    *,
    api_key: str = "",
    auth_header: str = "",
    timeout: float = 120.0,
    transport: Any | None = None,
) -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(timeout, connect=15.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Connection pooling across jobs (worker-level).
#
# Without pooling each job opens fresh TCP/TLS connections. Pooled clients are
# keyed by (base_url, key-hash, timeout) and reused; the idle sweeper closes
# them after IDLE_TTL seconds. Test transports are never pooled.
# ---------------------------------------------------------------------------
_CLIENT_POOL: dict[tuple, tuple[float, httpx.AsyncClient]] = {}
_POOL_LOCK = threading.Lock()
IDLE_TTL = 120.0


def acquire_async_client(
    base_url: str,
    *,
    api_key: str = "",
    auth_header: str = "",
    timeout: float = 120.0,
    transport: Any | None = None,
) -> httpx.AsyncClient:
    if transport is not None:
        return build_async_client(
            base_url, api_key=api_key, auth_header=auth_header, timeout=timeout, transport=transport
        )
    secret = auth_header or api_key
    key = (base_url.rstrip("/"), hashlib.sha256(secret.encode()).hexdigest()[:16], timeout)
    now = _time.monotonic()
    with _POOL_LOCK:
        entry = _CLIENT_POOL.get(key)
        if entry is not None:
            _CLIENT_POOL[key] = (now, entry[1])
            client = entry[1]
            client._ezdistro_pooled = True  # type: ignore[attr-defined]
            return client
        client = build_async_client(
            base_url, api_key=api_key, auth_header=auth_header, timeout=timeout
        )
        client._ezdistro_pooled = True  # type: ignore[attr-defined]
        _CLIENT_POOL[key] = (now, client)
        return client


def release_async_client(client: httpx.AsyncClient) -> None:
    """Close non-pooled clients; pooled ones stay for reuse."""
    if not getattr(client, "_ezdistro_pooled", False):
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            loop.create_task(_aclose(client))


async def _aclose(client: httpx.AsyncClient) -> None:
    with contextlib.suppress(Exception):
        await client.aclose()


def sweep_idle_clients(now: float | None = None) -> int:
    """Close pooled clients idle for more than IDLE_TTL. Returns closed count."""
    now = now if now is not None else _time.monotonic()
    idle: list[httpx.AsyncClient] = []
    with _POOL_LOCK:
        for key, (last_used, client) in list(_CLIENT_POOL.items()):
            if now - last_used > IDLE_TTL:
                idle.append(client)
                del _CLIENT_POOL[key]
    if idle:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for client in idle:
            if loop is not None:
                loop.create_task(_aclose(client))
    return len(idle)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------
async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    what: str = "request",
    logger_name: str = "provider",
    retry_counter: list[int] | None = None,
) -> T:
    """Run an async operation with exponential backoff.

    Only TransientError and network-level failures are retried; PermanentError
    propagates immediately. The caller's own exceptions bubble up untouched.
    `retry_counter` (a mutable list) receives the number of retries performed —
    used by the observability layer; local per call, safe under concurrency.
    """
    log = logger.getChild(logger_name)
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except (TimeoutError, httpx.HTTPError, TransientError) as exc:
            if isinstance(exc, TransientError):
                retryable, message, details = True, str(exc), exc.details
            else:
                retryable, message, details = True, str(exc), {"exc_type": type(exc).__name__}
            if attempt >= attempts or not retryable:
                raise TransientError(
                    f"{what} failed after {attempts} attempts: {message}", details
                ) from exc
            if retry_counter is not None:
                retry_counter.append(1)
            log.warning(
                "%s attempt %d/%d failed (transient): %s — retrying in %.1fs",
                what,
                attempt,
                attempts,
                message,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
    raise RuntimeError("unreachable")


def raise_for_provider(response: httpx.Response, *, what: str) -> None:
    """Classify an HTTP response into ProviderError subclasses.

    Transient: 429 + 5xx (retryable). Permanent: other 4xx. The provider's
    Retry-After header (seconds or HTTP-date) is captured into details so the
    job engine can honor it when scheduling the retry.
    """
    if response.is_success:
        return
    status = response.status_code
    body = response.text[:500]
    details: dict[str, Any] = {"status": status}
    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    if _looks_like_html(body):
        # A web UI (dashboard, landing page) was returned instead of an API
        # response — almost always a base URL pointing at the site root
        # instead of the OpenAI-compatible endpoint (usually …/v1).
        body = (
            "an HTML web page, not an API response — check that the base URL "
            "points at the API endpoint (usually ends in /v1), not the site root"
        )
    if status in (429,) or 500 <= status <= 599:
        raise TransientError(f"{what} failed with HTTP {status}: {body}", details)
    raise PermanentError(f"{what} failed with HTTP {status}: {body}", details)


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip().lower()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")


def _parse_retry_after(value: str | None) -> int | None:
    """Retry-After: integer seconds or an HTTP-date. Returns seconds or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0, int(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when is not None:
            return max(0, int((when - datetime.datetime.now(datetime.UTC)).total_seconds()))
    except (TypeError, ValueError):
        pass
    return None


def validate_llm_json_response(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict) or "choices" not in data:
        raise PermanentError(f"{what}: unexpected chat.completions response shape")
    try:
        content = data["choices"][0]["message"]["content"]
    except (IndexError, KeyError, TypeError) as exc:
        raise PermanentError(f"{what}: malformed chat.completions response") from exc
    if not isinstance(content, str) or not content.strip():
        raise PermanentError(f"{what}: empty completion content")
    return data


def openai_base_url(default: str = "https://api.openai.com/v1") -> str:
    return settings.__dict__.get("llm_default_base_url", default) or default
