"""Shared async HTTP client + retry helpers for provider adapters.

One AsyncClient per provider instance: connection reuse, keep-alive, sane
timeouts. Retries are classification-aware (only transient failures are
retried).

Connection pooling: provider adapters are built per job; `acquire_async_client`
reuses clients across jobs (keyed by base_url + key-hash + timeout) so jobs
don't re-open TCP/TLS connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import logging
import re
import threading
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
# keyed by (base_url, key-hash, timeout) and reused for the life of the worker.
# They are never closed on a timer: a provider caches its client for as long as
# it exists, so closing one behind its back turned the next call into "Cannot
# send a request, as the client has been closed" (seen live, hours later). The
# pool is bounded by the number of integrations, so it needs no sweeper.
# Test transports are never pooled.
# ---------------------------------------------------------------------------
_CLIENT_POOL: dict[tuple, httpx.AsyncClient] = {}
_POOL_LOCK = threading.Lock()


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
    with _POOL_LOCK:
        client = _CLIENT_POOL.get(key)
        if client is not None and not client.is_closed:
            return client
        client = build_async_client(
            base_url, api_key=api_key, auth_header=auth_header, timeout=timeout
        )
        client._ezdistro_pooled = True  # type: ignore[attr-defined]
        _CLIENT_POOL[key] = client
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
    else:
        # Some gateways put the wait in the body instead of a header
        # ("Not enough capacity … reset after 1m 4s"). Without this the job
        # retried inside the provider's own recovery window and burned all its
        # attempts on a transient 429.
        hinted = _parse_retry_hint(body)
        if hinted is not None:
            details["retry_after_seconds"] = hinted
    if _looks_like_html(body):
        # A web UI (dashboard, landing page) was returned instead of an API
        # response — almost always a base URL pointing at the site root
        # instead of the OpenAI-compatible endpoint (usually …/v1).
        body = (
            "an HTML web page, not an API response — check that the base URL "
            "points at the API endpoint (usually ends in /v1), not the site root"
        )
    elif 400 <= status < 500:
        body = _explain_client_error(body, status)
    if status in (429,) or 500 <= status <= 599:
        raise TransientError(f"{what} failed with HTTP {status}: {body}", details)
    raise PermanentError(f"{what} failed with HTTP {status}: {body}", details)


def _explain_client_error(body: str, status: int) -> str:
    """Translate cryptic gateway/provider 4xx bodies into an actionable hint.

    OpenAI-compatible gateways (9router, OpenRouter, LiteLLM…) report the
    *upstream provider* they could not reach, which looks unrelated to the
    configured URL. The usual causes are a bare (un-namespaced) model ID or a
    base URL pointing at the wrong host.
    """
    lowered = body.lower()
    if "no active credentials for provider" in lowered:
        return (
            f"HTTP {status}: the gateway has no credentials for the upstream provider it routed "
            "your model to. Usually the model ID is not namespaced for this gateway — prefix it "
            f"with the provider it exposes (e.g. 'mistral/mistral-embed', 'openrouter/text-embedding-3-small'). "
            f"Raw: {body}"
        )
    if "no credentials for provider" in lowered:
        return (
            f"HTTP {status}: the gateway could not resolve a provider for this model. Check that the "
            "model ID is one the gateway lists (and namespaced if required, e.g. 'mistral/mistral-embed'). "
            f"Raw: {body}"
        )
    if "model_not_found" in lowered or "model not found" in lowered:
        return (
            f"HTTP {status}: the provider does not know this model ID. Set the exact model name the "
            f"provider lists. Raw: {body}"
        )
    return body


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


def _parse_retry_hint(body: str) -> int | None:
    """Wait time embedded in a 429 body ("reset after 1m 4s", "retry after 30s")."""
    match = re.search(
        r"(?:reset|retry|try again)\s+(?:after|in)\s+(?:(\d+)\s*m(?:in)?s?\s*)?(\d+)\s*s",
        body or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2) or 0)
    total = minutes * 60 + seconds
    return total or None


def llm_budget_exhausted(data: Any) -> bool:
    """True when the model returned no answer because it hit its token budget.

    A reasoning model counts its reasoning against `max_tokens`, so an empty
    completion with `finish_reason == "length"` means "it needed more room",
    not "the request was wrong" — callers retry with a bigger budget instead of
    failing the job.
    """
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (IndexError, KeyError, TypeError):
        return False
    if isinstance(content, str) and content.strip():
        return False
    return str(choice.get("finish_reason") or "") == "length"


def validate_llm_json_response(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict) or "choices" not in data:
        raise PermanentError(f"{what}: unexpected chat.completions response shape")
    try:
        content = data["choices"][0]["message"]["content"]
    except (IndexError, KeyError, TypeError) as exc:
        raise PermanentError(f"{what}: malformed chat.completions response") from exc
    if not isinstance(content, str) or not content.strip():
        # A reasoning model can spend the whole completion budget on its
        # reasoning and return no answer at all — say so, otherwise the error
        # ("empty completion content") points nowhere.
        finish = ""
        try:
            finish = str(data["choices"][0].get("finish_reason") or "")
        except (IndexError, KeyError, TypeError):
            finish = ""
        if finish == "length":
            raise PermanentError(
                f"{what}: the model used its entire max_tokens budget before returning an "
                "answer (reasoning models count their reasoning against it) — raise this "
                "role's Max tokens or point the role at a non-reasoning model"
            )
        # A 200 with no content and a normal finish is a gateway hiccup — the
        # live reseller does it on long generations. Retrying is the fix, and
        # the job's bounded attempt budget is what stops a truly broken model.
        raise TransientError(
            f"{what}: empty completion content (the provider returned no text; retrying)"
        )
    return data


def openai_base_url(default: str = "https://api.openai.com/v1") -> str:
    return settings.__dict__.get("llm_default_base_url", default) or default
