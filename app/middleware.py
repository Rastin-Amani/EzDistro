import time
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request
from fastapi.responses import RedirectResponse
from structlog import get_logger

from app.pb import get_pb
from app.logging_config import bind_request_context, clear_request_context

# 🟢 1. Define routes that anyone can access without a token.
# Notice "/" is REMOVED from this list so .startswith() doesn't match everything.
PUBLIC_PATHS = [
    "/static",  # Required so your CSS/JS loads
    "/manifest.json",  # Required for PWA
    "/sw.js",  # Required for offline caching
    "/favicon.ico",
]

logger = get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # ---- Request ID for correlation ----
        req_id = str(uuid.uuid4())[:8]
        request.state.req_id = req_id

        # Bind request context for all logs in this request
        bind_request_context(req_id=req_id, tenant_id=None)

        # ---- Auth detection ----
        pb = get_pb()
        request.state.pb = pb
        request.state.user = None

        token = request.cookies.get("pb_auth")
        if token:
            try:
                pb.auth_store.save(token, None)
                pb.collection("users").auth_refresh()
                request.state.user = pb.auth_store.model
            except Exception as e:
                logger.warning("auth_refresh_failed", error=str(e))
                pb.auth_store.clear()

        # ---- Request lifecycle log ----
        start = time.time()
        method = request.method
        url = str(request.url.path)

        logger.info("request.started", method=method, path=url)

        try:
            response = await call_next(request)
        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "request.error",
                method=method,
                path=url,
                duration_ms=round(elapsed * 1000),
            )
            raise

        elapsed = time.time() - start
        status = response.status_code
        logger.info(
            "request.completed",
            method=method,
            path=url,
            status=status,
            duration_ms=round(elapsed * 1000),
        )

        clear_request_context()
        return response
