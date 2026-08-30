import time
import uuid

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from structlog import get_logger

from app.i18n import LOCALE_COOKIE, set_request_locale
from app.logging_config import bind_request_context, clear_request_context
from app.pb import get_pb

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

logger = get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # ---- Locale resolution (cookie preference, allowlisted, default fa) ----
        set_request_locale(request.cookies.get(LOCALE_COOKIE))

        # ---- Request ID for correlation ----
        req_id = str(uuid.uuid4())[:8]
        request.state.req_id = req_id
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

        # ---- Gate: authenticated users only ----
        path = request.url.path
        is_public = path == "/" or any(path.startswith(p) for p in PUBLIC_PATHS)
        if not request.state.user and not is_public:
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
