import time
import uuid

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from structlog import get_logger

from app.api.deps import is_disabled
from app.i18n import LOCALE_COOKIE, set_request_locale
from app.logging_config import bind_request_context, clear_request_context
from app.pb import LazyDataPb, get_data_pb, get_pb

# Routes anyone can access without a token.
PUBLIC_PATHS = [
    "/login",
    "/locale/",
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
        # ---- Locale resolution (cookie preference, allowlisted, English default) ----
        set_request_locale(request.cookies.get(LOCALE_COOKIE))

        # ---- Request ID for correlation ----
        req_id = str(uuid.uuid4())[:8]
        request.state.req_id = req_id
        bind_request_context(req_id=req_id, tenant_id=None)

        # ---- Auth detection ----
        # Session validation runs on a THROWAWAY client: a refreshed/foreign
        # token must never clobber the shared superuser client's auth store.
        session_pb = get_pb()
        request.state.user = None
        disabled = False

        token = request.cookies.get("pb_auth")
        if token:
            try:
                session_pb.auth_store.save(token, None)
                session_pb.collection("users").auth_refresh()
                user = session_pb.auth_store.model
                if is_disabled(user):
                    # 18-A(c): a disabled account is logged out immediately,
                    # even with a still-valid token (the next request is
                    # already blocked — accepted under section 20).
                    disabled = True
                    session_pb.auth_store.clear()
                    logger.warning(
                        "user_disabled",
                        user_id=(
                            user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
                        ),
                    )
                else:
                    request.state.user = user
            except Exception as e:
                logger.warning("auth_refresh_failed", error=str(e))
                session_pb.auth_store.clear()

        # ---- Data access: lazily-authenticated superuser client (18-A(b)) ----
        request.state.pb = LazyDataPb(get_data_pb)

        # ---- Gate: authenticated users only ----
        path = request.url.path
        is_public = path == "/" or any(path.startswith(p) for p in PUBLIC_PATHS)
        if not request.state.user and not is_public:
            if disabled:
                response = RedirectResponse(url="/login?disabled=1", status_code=303)
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
