"""
Structured logging config (Pino.js equivalent for Python).

Usage:
    from app.logging_config import logger
    logger.info("event", key=value)
    logger.exception("error", key=value)  # auto-includes traceback
"""

import logging
import os
import sys
from contextvars import ContextVar

import structlog

# --- Context vars for request-scoped data ---
_request_ctx: ContextVar[dict | None] = ContextVar("request_ctx", default=None)


def add_correlation_id(logger, method_name, event_dict):
    """Attach req_id to every log (from contextvars)."""
    ctx = _request_ctx.get()
    req_id = ctx.get("req_id") if ctx else None
    if req_id:
        event_dict["req_id"] = req_id
    return event_dict


def add_tenant_id(logger, method_name, event_dict):
    """No tenant logic in stack boilerplate."""
    return event_dict


# --- Config ---
DEV = os.getenv("ENV", "dev").lower() == "dev"

# Silence DEBUG noise from HTTP transport libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Our middleware logs request lifecycle; disable uvicorn's duplicate access log
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Root handler: only add if no handler exists (preserves uvicorn's setup)
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
else:
    logging.getLogger().setLevel(logging.INFO)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_correlation_id,
        add_tenant_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.dev.ConsoleRenderer() if DEV else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


def bind_request_context(req_id: str, tenant_id: str | None = None):
    """Bind request-scoped context vars (call in middleware)."""
    _request_ctx.set({"req_id": req_id})


def clear_request_context():
    """Clear request-scoped context vars (call after request)."""
    _request_ctx.set({})
