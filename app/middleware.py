import threading
import time
import uuid

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from structlog import get_logger

from app.api.deps import is_disabled
from app.logging_config import bind_request_context, clear_request_context
from app.pb import LazyDataPb, get_data_pb, get_pb
from app.repositories.base import record_to_dict

# Routes anyone can access without a token.
PUBLIC_PATHS = [
    "/login",
    "/static",
    "/manifest.json",
    "/sw.js",
    "/favicon.ico",
    "/health",
    "/openapi.json",
    "/docs",
    "/offline",
]

# Paths that render identically for anonymous and authenticated visitors AND
# are hit constantly (assets, PWA shell, probes). Session validation is skipped
# entirely for these — no PocketBase round trip on every CSS/JS request.
# NOTE: "/" and "/login" are intentionally NOT here: they branch on the user.
NO_AUTH_PATHS = (
    "/static",
    "/manifest.json",
    "/sw.js",
    "/favicon.ico",
    "/health",
    "/openapi.json",
    "/docs",
    "/offline",
)

# Session validation cache. A PocketBase `auth_refresh()` is a full network
# round trip; doing it on every page load and every 2-8s HTMX poll dominated
# request latency. A short TTL collapses repeat requests from the same token
# while keeping revocation/disablement latency bounded to _SESSION_TTL.
_SESSION_TTL = 30.0
_SESSION_CACHE_MAX = 1024
_session_cache: dict[str, tuple[float, dict]] = {}
_session_lock = threading.Lock()


def clear_session_cache() -> None:
    """Drop all cached sessions (tests, secret rotation)."""
    with _session_lock:
        _session_cache.clear()


def _session_cached(token: str) -> dict | None:
    with _session_lock:
        entry = _session_cache.get(token)
    if not entry:
        return None
    if time.monotonic() - entry[0] >= _SESSION_TTL:
        with _session_lock:
            _session_cache.pop(token, None)
        return None
    return entry[1]


def _session_store(token: str, user: dict) -> None:
    with _session_lock:
        if len(_session_cache) >= _SESSION_CACHE_MAX:
            # Bounded memory: drop the oldest entry (cheap, not LRU).
            oldest = min(_session_cache, key=lambda k: _session_cache[k][0])
            _session_cache.pop(oldest, None)
        _session_cache[token] = (time.monotonic(), user)


def _session_evict(token: str) -> None:
    with _session_lock:
        _session_cache.pop(token, None)


def _resolve_user(token: str) -> dict | None:
    """Validate a session token, using the short-lived cache when possible."""
    cached = _session_cached(token)
    if cached is not None:
        return cached
    # Throwaway client: a refreshed/foreign token must never clobber the shared
    # superuser client's auth store.
    session_pb = get_pb()
    session_pb.auth_store.save(token, None)
    session_pb.collection("users").auth_refresh()
    user = record_to_dict(session_pb.auth_store.model)
    if user:
        _session_store(token, user)
    return user


logger = get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # ---- Request ID for correlation ----
        req_id = str(uuid.uuid4())[:8]
        request.state.req_id = req_id
        bind_request_context(req_id=req_id, tenant_id=None)

        # ---- Auth detection ----
        # Session validation is skipped for pure-asset/probe paths (see
        # NO_AUTH_PATHS) and otherwise served from a short TTL cache, so a
        # PocketBase round trip only happens on a cache miss.
        request.state.user = None
        disabled = False

        path = request.url.path
        no_auth = any(path.startswith(p) for p in NO_AUTH_PATHS)
        token = request.cookies.get("pb_auth")
        if token and not no_auth:
            try:
                user = _resolve_user(token)
                if user is not None and is_disabled(user):
                    # 18-A(c): a disabled account is logged out immediately,
                    # even with a still-valid token (the next request is
                    # already blocked — accepted under section 20).
                    disabled = True
                    _session_evict(token)
                    logger.warning("user_disabled", user_id=user.get("id"))
                elif user is not None:
                    request.state.user = user
            except Exception as e:
                logger.warning("auth_refresh_failed", error=str(e))
                _session_evict(token)

        # ---- Data access: lazily-authenticated superuser client (18-A(b)) ----
        request.state.pb = LazyDataPb(get_data_pb)

        # ---- Gate: authenticated users only ----
        is_public = path == "/" or any(path.startswith(p) for p in PUBLIC_PATHS)
        if not request.state.user and not is_public:
            login_url = "/login?disabled=1" if disabled else "/login"
            if request.headers.get("HX-Request") == "true":
                # A partial swap must not inject the login page into the target:
                # tell HTMX to navigate the whole browser instead.
                response = Response(status_code=200, headers={"HX-Redirect": login_url})
                if disabled:
                    response.delete_cookie("pb_auth")
                return response
            if disabled:
                response = RedirectResponse(url=login_url, status_code=303)
                # Drop the dead cookie so the browser stops replaying it.
                response.delete_cookie("pb_auth")
                return response
            return RedirectResponse(url="/login", status_code=303)

        # ---- Request lifecycle log ----
        start = time.time()
        method = request.method
        logger.info("request.started", method=method, path=path)

        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.time() - start
            logger.exception(
                "request.error",
                method=method,
                path=path,
                duration_ms=round(elapsed * 1000),
            )
            raise

        elapsed = time.time() - start
        status = response.status_code
        logger.info(
            "request.completed",
            method=method,
            path=path,
            status=status,
            duration_ms=round(elapsed * 1000),
        )

        clear_request_context()
        return response
