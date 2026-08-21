"""Centralized HTMX error handling — simple, native-Persian, no raw prints.

Every mutation route wraps its body with ``@hx_error("پیام فارسی …")``.
Unexpected exceptions are logged once (structlog, with the route name) and
the user receives a clean Persian toast. Nothing leaks an English traceback
into the UI, and the old ``print("… error:", e)`` noise is gone.

Usage::

    @router.post("/x")
    @hx_error("ذخیره ناموفق بود")
    def save_x(request: Request):
        require_hx(request)
        ...
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar, cast

from fastapi import Request
from structlog import get_logger

from app.utils import error_response

logger = get_logger("app.api.errors")

F = TypeVar("F", bound=Callable[..., Any])


def hx_error(fail: str) -> Callable[[F], F]:
    """Wrap an HTMX route with uniform error handling.

    Handles both sync and async routes; on any exception it logs the failure
    with route context and answers with a Persian toast instead of a crash.
    """

    def deco(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(request, *args, **kwargs)
                except Exception as exc:
                    logger.warning("route.error", route=fn.__name__, error=str(exc))
                    return error_response(fail)

            return cast(F, async_wrapper)

        @functools.wraps(fn)
        def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn(request, *args, **kwargs)
            except Exception as exc:
                logger.warning("route.error", route=fn.__name__, error=str(exc))
                return error_response(fail)

        return cast(F, wrapper)

    return deco
