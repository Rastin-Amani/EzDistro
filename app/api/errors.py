"""Centralized HTMX error handling.

Every mutation route wraps its body with an English message.
Unexpected exceptions are logged once (structlog, with the route name) and
the user receives a toast. Nothing leaks a traceback
into the UI, and the old ``print("… error:", e)`` noise is gone.

Usage::

    @router.post("/x")
    @hx_error("Save failed")
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
    with route context and answers with a toast instead of a crash.
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


def page_guard(fail: str) -> Callable[[F], F]:
    """Wrap a full-page GET route with graceful error handling.

    On any exception the error is logged WITH the full traceback (so the cause
    can be found in `make web`/`make worker` output) and an error
    page is rendered instead of a white screen. Handles sync and async routes.
    """

    def _error_page(request: Request, project_id: str, message: str) -> Any:
        from app.repositories.projects import ProjectRepo
        from app.templates import templates

        project = None
        pb = getattr(request.state, "pb", None)
        if pb is not None and project_id:
            try:
                project = ProjectRepo(pb).get(project_id)
            except Exception:
                project = None
        return templates.TemplateResponse(
            request,
            "pages/articles/render_error.html",
            {"project": project or {"id": project_id or ""}, "message": (message)},
        )

    def deco(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(request, *args, **kwargs)
                except Exception:
                    logger.exception(
                        "route.page_error", route=fn.__name__, path=str(request.url.path)
                    )
                    return _error_page(request, str(kwargs.get("project_id") or ""), fail)

            return cast(F, async_wrapper)

        @functools.wraps(fn)
        def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn(request, *args, **kwargs)
            except Exception:
                logger.exception("route.page_error", route=fn.__name__, path=str(request.url.path))
                return _error_page(request, str(kwargs.get("project_id") or ""), fail)

        return cast(F, wrapper)

    return deco
